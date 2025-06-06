import asyncio
import logging
from datetime import timedelta
from typing import Dict, List, Any, Optional, Union
from dataclasses import dataclass
from enum import Enum

from mcp import ClientSession
from langchain_mcp_adapters.tools import load_mcp_tools, convert_mcp_tool_to_langchain_tool
from langchain_mcp_adapters.sessions import Connection, StdioConnection, SSEConnection, StreamableHttpConnection, create_session
from langchain_core.tools import BaseTool

from mythic_container.logging import logger


class ConnectionType(Enum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


@dataclass
class MCPConnectionConfig:
    """Configuration for MCP server connection"""
    name: str
    connection_type: ConnectionType
    # For STDIO connections
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    cwd: Optional[str] = None
    encoding: Optional[str] = None
    encoding_error_handler: Optional[str] = None
    # For SSE and Streamable HTTP connections
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    timeout: Optional[float] = None
    sse_read_timeout: Optional[float] = None
    # For Streamable HTTP specific
    terminate_on_close: Optional[bool] = None
    # Additional connection parameters
    session_kwargs: Optional[Dict[str, Any]] = None
    extra_params: Optional[Dict[str, Any]] = None


class MCPServerManager:
    """Manages multiple MCP server connections and their tools"""
    
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.connections: Dict[str, Connection] = {}
        self.tools: Dict[str, List[BaseTool]] = {}
        self.configs: Dict[str, MCPConnectionConfig] = {}
        self._session_contexts: Dict[str, Any] = {}
    
    async def connect_server(self, config: MCPConnectionConfig) -> bool:
        """
        Connect to an MCP server based on the provided configuration
        
        Args:
            config: MCPConnectionConfig object containing connection details
            
        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            if config.name in self.sessions:
                logger.warning(f"Server '{config.name}' already connected. Disconnecting first.")
                await self.disconnect_server(config.name)
            
            # Create connection config as dictionary (TypedDict)
            connection: Connection
            
            if config.connection_type == ConnectionType.STDIO:
                if not config.command:
                    raise ValueError("STDIO connection requires 'command' parameter")
                
                stdio_connection: StdioConnection = {
                    "transport": "stdio",
                    "command": config.command,
                    "args": config.args or [],
                    "env": config.env,
                    "cwd": config.cwd,
                    "encoding": config.encoding or "utf-8",
                    "encoding_error_handler": "strict",
                    "session_kwargs": config.session_kwargs
                }
                connection = stdio_connection
                
            elif config.connection_type == ConnectionType.SSE:
                if not config.url:
                    raise ValueError("SSE connection requires 'url' parameter")
                
                sse_connection: SSEConnection = {
                    "transport": "sse",
                    "url": config.url,
                    "headers": config.headers,
                    "timeout": config.timeout or 5.0,
                    "sse_read_timeout": config.sse_read_timeout or 300.0,
                    "session_kwargs": config.session_kwargs,
                    "httpx_client_factory": None
                }
                connection = sse_connection
                
            elif config.connection_type == ConnectionType.STREAMABLE_HTTP:
                if not config.url:
                    raise ValueError("Streamable HTTP connection requires 'url' parameter")
                
                streamable_connection: StreamableHttpConnection = {
                    "transport": "streamable_http",
                    "url": config.url,
                    "headers": config.headers,
                    "timeout": timedelta(seconds=config.timeout or 30),
                    "sse_read_timeout": timedelta(seconds=config.sse_read_timeout or 300),
                    "terminate_on_close": config.terminate_on_close if config.terminate_on_close is not None else True,
                    "session_kwargs": config.session_kwargs,
                    "httpx_client_factory": None
                }
                connection = streamable_connection
                
            else:
                raise ValueError(f"Unsupported connection type: {config.connection_type}")
            
            # Use the create_session context manager
            session_context = create_session(connection)
            session = await session_context.__aenter__()
            
            # Initialize the session
            await session.initialize()
            
            # Store the session and connection info
            self.sessions[config.name] = session
            self.connections[config.name] = connection
            self.configs[config.name] = config
            self._session_contexts[config.name] = session_context
            
            # Load and convert tools
            await self._load_tools_for_server(config.name)
            
            logger.info(f"Successfully connected to MCP server '{config.name}'")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to MCP server '{config.name}': {e}")
            return False
    
    async def disconnect_server(self, server_name: str) -> bool:
        """
        Disconnect from an MCP server
        
        Args:
            server_name: Name of the server to disconnect
            
        Returns:
            bool: True if disconnection successful, False otherwise
        """
        try:
            if server_name in self.sessions:
                # Exit the session context
                session_context = self._session_contexts[server_name]
                await session_context.__aexit__(None, None, None)
                
                # Clean up stored references
                del self.sessions[server_name]
                del self.connections[server_name]
                del self.configs[server_name]
                del self._session_contexts[server_name]
                
                # Remove tools for this server
                if server_name in self.tools:
                    del self.tools[server_name]
                
                logger.info(f"Disconnected from MCP server '{server_name}'")
                return True
            else:
                logger.warning(f"Server '{server_name}' not found in connections")
                return False
                
        except Exception as e:
            logger.error(f"Failed to disconnect from MCP server '{server_name}': {e}")
            return False
    
    async def _load_tools_for_server(self, server_name: str):
        """Load and convert MCP tools for a specific server"""
        try:
            session = self.sessions[server_name]
            connection = self.connections[server_name]
            
            # Use the langchain_mcp_adapters function to load and convert tools
            langchain_tools = await load_mcp_tools(session, connection=connection)
            
            self.tools[server_name] = langchain_tools
            logger.info(f"Loaded {len(langchain_tools)} tools from server '{server_name}'")
            
        except Exception as e:
            logger.error(f"Failed to load tools for server '{server_name}': {e}")
            self.tools[server_name] = []
    
    def get_all_tools(self) -> List[BaseTool]:
        """
        Get all tools from all connected MCP servers
        
        Returns:
            List of all LangChain BaseTool instances
        """
        all_tools = []
        for server_tools in self.tools.values():
            all_tools.extend(server_tools)
        return all_tools
    
    def get_tools_by_server(self, server_name: str) -> List[BaseTool]:
        """
        Get tools from a specific server
        
        Args:
            server_name: Name of the server
            
        Returns:
            List of LangChain BaseTool instances for the specified server
        """
        return self.tools.get(server_name, [])
    
    def get_connected_servers(self) -> List[str]:
        """
        Get list of all connected server names
        
        Returns:
            List of connected server names
        """
        return list(self.sessions.keys())
    
    def get_server_info(self, server_name: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific server
        
        Args:
            server_name: Name of the server
            
        Returns:
            Dictionary containing server information or None if not found
        """
        if server_name not in self.sessions:
            return None
        
        config = self.configs[server_name]
        tools_count = len(self.tools.get(server_name, []))
        
        return {
            "name": server_name,
            "connection_type": config.connection_type.value,
            "config": config,
            "tools_count": tools_count,
            "connected": True
        }
    
    async def refresh_tools(self, server_name: Optional[str] = None):
        """
        Refresh tools for a specific server or all servers
        
        Args:
            server_name: Name of server to refresh, or None for all servers
        """
        if server_name:
            if server_name in self.sessions:
                await self._load_tools_for_server(server_name)
            else:
                logger.warning(f"Server '{server_name}' not connected")
        else:
            for name in self.sessions.keys():
                await self._load_tools_for_server(name)
    
    async def get_tool_by_name(self, tool_name: str) -> Optional[BaseTool]:
        """
        Get a specific tool by name from any connected server
        
        Args:
            tool_name: Name of the tool to find
            
        Returns:
            The BaseTool instance if found, None otherwise
        """
        for tools in self.tools.values():
            for tool in tools:
                if tool.name == tool_name:
                    return tool
        return None
    
    def get_tools_summary(self) -> Dict[str, Any]:
        """
        Get a summary of all tools across all servers
        
        Returns:
            Dictionary with summary information
        """
        total_tools = 0
        server_summaries = {}
        
        for server_name, tools in self.tools.items():
            tool_names = [tool.name for tool in tools]
            server_summaries[server_name] = {
                "tool_count": len(tools),
                "tool_names": tool_names
            }
            total_tools += len(tools)
        
        return {
            "total_tools": total_tools,
            "connected_servers": len(self.sessions),
            "server_summaries": server_summaries
        }
    
    async def close_all_connections(self):
        """Close all MCP server connections"""
        server_names = list(self.sessions.keys())
        for server_name in server_names:
            await self.disconnect_server(server_name)


# Convenience functions to create connection configs
def create_stdio_config(name: str, command: str, args: List[str] | None, 
                       env: Dict[str, str] | None, cwd: str | None,
                       encoding: str | None, encoding_error_handler: str | None,
                       session_kwargs: Dict[str, Any] | None, **kwargs) -> MCPConnectionConfig:
    """Create a configuration for STDIO MCP connection"""
    return MCPConnectionConfig(
        name=name,
        connection_type=ConnectionType.STDIO,
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        encoding=encoding,
        encoding_error_handler=encoding_error_handler,
        session_kwargs=session_kwargs,
        extra_params=kwargs
    )


def create_sse_config(name: str, url: str, headers: Dict[str, str] | None = None,
                     timeout: float | None = None, sse_read_timeout: float | None = None,
                     session_kwargs: Dict[str, Any] | None = None, **kwargs) -> MCPConnectionConfig:
    """Create a configuration for SSE MCP connection"""
    return MCPConnectionConfig(
        name=name,
        connection_type=ConnectionType.SSE,
        url=url,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
        session_kwargs=session_kwargs,
        extra_params=kwargs
    )


def create_streamable_http_config(name: str, url: str, headers: Dict[str, str] | None = None,
                                 timeout: float | None = None, sse_read_timeout: float | None = None,
                                 terminate_on_close: bool | None = None,
                                 session_kwargs: Dict[str, Any] | None = None, **kwargs) -> MCPConnectionConfig:
    """Create a configuration for Streamable HTTP MCP connection"""
    return MCPConnectionConfig(
        name=name,
        connection_type=ConnectionType.STREAMABLE_HTTP,
        url=url,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
        terminate_on_close=terminate_on_close,
        session_kwargs=session_kwargs,
        extra_params=kwargs
    )
