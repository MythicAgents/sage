"""
Unified Tool Manager for combining Mythic and MCP tools into a single interface.

This module provides the UnifiedToolManager class that coordinates tool access
across both Mythic API tools and MCP server tools, presenting them through
a consistent LangChain BaseTool interface.
"""

import logging
from typing import List, Optional, Dict, Any
from langchain_core.tools import BaseTool

from .mythic import MythicAPIClient
from .mcp import MCPServerManager
from mythic_container.logging import logger


class UnifiedToolManager:
    """
    Manages and coordinates tools from both Mythic API and MCP servers.
    
    This class provides a unified interface for accessing tools from multiple sources:
    - Mythic API tools (wrapped as LangChain BaseTool instances)
    - MCP server tools (already LangChain BaseTool instances)
    
    All tools are presented through a consistent LangChain BaseTool interface.
    """
    
    def __init__(self, mcp_manager: MCPServerManager):
        """
        Initialize the UnifiedToolManager.
        
        Args:
            mcp_manager: Instance of MCPServerManager for MCP tool access
        """
        self.mcp_manager = mcp_manager
        self.mythic_client: Optional[MythicAPIClient] = None
        self._logger = logging.getLogger(__name__)
    
    async def initialize_mythic_tools(self, task_id: str) -> bool:
        """
        Initialize Mythic tools for a specific task.
        
        Args:
            task_id: The Mythic task ID for API token creation
            
        Returns:
            bool: True if initialization successful, False otherwise
        """
        try:
            self.mythic_client = await MythicAPIClient.create(task_id)
            logger.info(f"Initialized Mythic tools for task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Mythic tools for task {task_id}: {e}")
            return False
    
    def get_all_tools(self) -> List[BaseTool]:
        """
        Get all tools from both Mythic API and MCP servers.
        
        Returns:
            List of all LangChain BaseTool instances from all sources
        """
        tools = []
        tool_names = set()
        
        # Add Mythic tools first (they take priority)
        if self.mythic_client:
            try:
                mythic_tools = self.mythic_client.get_langchain_tools()
                for tool in mythic_tools:
                    tools.append(tool)
                    tool_names.add(tool.name)
                logger.debug(f"Added {len(mythic_tools)} Mythic tools")
            except Exception as e:
                logger.error(f"Error getting Mythic tools: {e}")
        
        # Add MCP tools, checking for conflicts with Mythic tools
        try:
            mcp_tools = self.mcp_manager.get_all_tools()
            mcp_conflicts = 0
            mcp_added = 0
            
            for tool in mcp_tools:
                if tool.name in tool_names:
                    logger.error(f"Tool name conflict: MCP tool '{tool.name}' conflicts with existing Mythic tool. Excluding MCP tool.")
                    mcp_conflicts += 1
                else:
                    tools.append(tool)
                    tool_names.add(tool.name)
                    mcp_added += 1
            
            if mcp_conflicts > 0:
                logger.warning(f"Added {mcp_added} MCP tools ({mcp_conflicts} MCP tools excluded due to conflicts with Mythic tools)")
            else:
                logger.debug(f"Added {mcp_added} MCP tools")
                
        except Exception as e:
            logger.error(f"Error getting MCP tools: {e}")
        
        logger.info(f"Total tools available: {len(tools)} (Mythic has priority over MCP for naming conflicts)")
        return tools
    
    def get_mythic_tools(self) -> List[BaseTool]:
        """
        Get tools specifically from Mythic API.
        
        Returns:
            List of LangChain BaseTool instances from Mythic API
        """
        if not self.mythic_client:
            logger.warning("Mythic client not initialized")
            return []
        
        try:
            return self.mythic_client.get_langchain_tools()
        except Exception as e:
            logger.error(f"Error getting Mythic tools: {e}")
            return []
    
    def get_mcp_tools(self) -> List[BaseTool]:
        """
        Get tools from all connected MCP servers.
        
        Returns:
            List of LangChain BaseTool instances from MCP servers
        """
        try:
            return self.mcp_manager.get_all_tools()
        except Exception as e:
            logger.error(f"Error getting MCP tools: {e}")
            return []
    
    def get_tools_by_source(self) -> Dict[str, List[BaseTool]]:
        """
        Get tools organized by their source.
        
        Returns:
            Dictionary with source names as keys and tool lists as values
        """
        tools_by_source = {}
        
        # Mythic tools
        mythic_tools = self.get_mythic_tools()
        if mythic_tools:
            tools_by_source["mythic"] = mythic_tools
        
        # MCP tools by server
        for server_name in self.mcp_manager.get_connected_servers():
            server_tools = self.mcp_manager.get_tools_by_server(server_name)
            if server_tools:
                tools_by_source[f"mcp:{server_name}"] = server_tools
        
        return tools_by_source
    
    async def refresh_tools(self, task_id: Optional[str] = None) -> bool:
        """
        Refresh tools from all sources.
        
        Args:
            task_id: Optional task ID for reinitializing Mythic tools
            
        Returns:
            bool: True if refresh was successful, False otherwise
        """
        success = True
        
        # Refresh Mythic tools if task_id provided
        if task_id:
            if not await self.initialize_mythic_tools(task_id):
                success = False
        
        # Refresh MCP tools
        try:
            await self.mcp_manager.refresh_tools()
            logger.info("Refreshed MCP tools")
        except Exception as e:
            logger.error(f"Error refreshing MCP tools: {e}")
            success = False
        
        return success
    
    def get_tool_by_name(self, tool_name: str) -> Optional[BaseTool]:
        """
        Find a specific tool by name across all sources.
        
        Args:
            tool_name: Name of the tool to find
            
        Returns:
            The BaseTool instance if found, None otherwise
        """
        # Search Mythic tools first
        if self.mythic_client:
            mythic_tools = self.get_mythic_tools()
            for tool in mythic_tools:
                if tool.name == tool_name:
                    return tool
        
        # Search MCP tools
        mcp_tools = self.get_mcp_tools()
        for tool in mcp_tools:
            if tool.name == tool_name:
                return tool
        
        return None
    
    def get_tools_summary(self) -> Dict[str, Any]:
        """
        Get a comprehensive summary of all available tools.
        
        Returns:
            Dictionary containing tool summary information
        """
        tools_by_source = self.get_tools_by_source()
        all_tools = self.get_all_tools()
        
        # Check for potential conflicts by comparing individual source counts vs total
        individual_count = sum(len(tools) for tools in tools_by_source.values())
        actual_count = len(all_tools)
        conflicts_detected = individual_count > actual_count
        
        summary = {
            "total_tools": actual_count,
            "sources": len(tools_by_source),
            "mythic_initialized": self.mythic_client is not None,
            "mcp_servers_connected": len(self.mcp_manager.get_connected_servers()),
            "conflicts_detected": conflicts_detected,
            "tools_by_source": {}
        }
        
        if conflicts_detected:
            summary["conflicts_note"] = f"Some tools were excluded due to name conflicts. Individual sources have {individual_count} tools, but only {actual_count} unique tools are available."
        
        # Add details for each source
        for source_name, tools in tools_by_source.items():
            summary["tools_by_source"][source_name] = {
                "count": len(tools),
                "tool_names": [tool.name for tool in tools]
            }
        
        return summary
    
    def is_ready(self) -> bool:
        """
        Check if the tool manager has any tools available.
        
        Returns:
            bool: True if any tools are available, False otherwise
        """
        return len(self.get_all_tools()) > 0
    
    def cleanup(self):
        """Clean up resources and connections."""
        if self.mythic_client:
            # Mythic client cleanup would go here if needed
            pass
        
        # MCP cleanup is handled by the MCPServerManager
        logger.info("UnifiedToolManager cleanup completed")