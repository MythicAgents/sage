import asyncio
import contextvars
import inspect
import json
import logging
import traceback
import warnings
from datetime import timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass
from enum import Enum

import anyio
import httpx
from mcp import ClientSession
from langchain_mcp_adapters.tools import load_mcp_tools, convert_mcp_tool_to_langchain_tool
from langchain_mcp_adapters.sessions import Connection, StdioConnection, SSEConnection, StreamableHttpConnection, create_session
from langchain_core.tools import BaseTool

from mythic_container.logging import logger


MCP_EXECUTION_CLASS_UNCLASSIFIED = "unclassified"
MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE = "non_target_control_plane"
# Compatibility symbol for callers that imported the older name before the boundary contract was finalized.
MCP_EXECUTION_CLASS_CONTROL_PLANE = MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE
MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE = "bloodhound_control_plane"
MCP_EXECUTION_CLASS_TARGET_FACING = "target_facing"
MCP_EXECUTION_CONTEXT_GENERAL = "general"
MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME = "offensive_runtime"
ALLOWED_MCP_EXECUTION_CLASSES = frozenset({
    MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
    MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE,
})
_LEGACY_MCP_EXECUTION_CLASS_ALIASES = {
    "control_plane": MCP_EXECUTION_CLASS_NON_TARGET_CONTROL_PLANE,
}


def normalize_execution_class(value: Any) -> str:
    normalized = str(value or "").strip().casefold() or MCP_EXECUTION_CLASS_UNCLASSIFIED
    return _LEGACY_MCP_EXECUTION_CLASS_ALIASES.get(normalized, normalized)


def execution_class_allowed(value: Any) -> bool:
    return normalize_execution_class(value) in ALLOWED_MCP_EXECUTION_CLASSES


_execution_observer: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "sage_execution_observer",
    default=None,
)
_execution_activity: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "sage_execution_activity",
    default=None,
)
_execution_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sage_mcp_execution_context",
    default=MCP_EXECUTION_CONTEXT_GENERAL,
)


def _execution_result_text(value: Any) -> str:
    """Serialize the complete MCP result for operator-visible execution evidence."""
    try:
        if isinstance(value, str):
            return value
        return json.dumps(value, default=str, ensure_ascii=False)
    except Exception:
        return str(value)


# Filter to suppress noisy SSE ping messages from MCP libraries
class SSEPingFilter(logging.Filter):
    def filter(self, record):
        # Suppress "Unknown SSE event: ping" messages
        if "Unknown SSE event: ping" in record.getMessage():
            # Uncomment below to log at DEBUG level instead of suppressing entirely
            # logger.debug(f"[MCP SSE] {record.getMessage()}")
            return False
        return True

# Apply filter to root logger and all MCP/SSE-related loggers
_sse_filter = SSEPingFilter()
for logger_name in [
    "",  # root logger
    "mcp",
    "mcp.client",
    "mcp.client.sse",
    "mcp.client.streamable_http",
    "langchain_mcp_adapters",
    "httpx",
    "httpx_sse",
    "httpcore",
    "anyio",
]:
    logging.getLogger(logger_name).addFilter(_sse_filter)

# Also suppress as a warning in case it's emitted that way
warnings.filterwarnings("ignore", message=".*Unknown SSE event.*")


def _create_insecure_httpx_client(**kwargs) -> httpx.AsyncClient:
    """Create an httpx client that skips SSL verification (for development/testing only)"""
    # Remove verify from kwargs if present, we're forcing it to False
    kwargs.pop('verify', None)
    return httpx.AsyncClient(verify=False, **kwargs)


def _transport_is_closed(exc: BaseException) -> bool:
    """Return True when an MCP failure proves the underlying transport is already dead."""
    if isinstance(exc, (anyio.ClosedResourceError, anyio.BrokenResourceError, anyio.EndOfStream)):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_transport_is_closed(nested) for nested in exc.exceptions)
    for nested in (getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(nested, BaseException) and _transport_is_closed(nested):
            return True
    return False


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
    # SSL verification (set to False to disable - NOT recommended for production)
    ssl_verify: Optional[bool] = True
    # Additional connection parameters
    session_kwargs: Optional[Dict[str, Any]] = None
    extra_params: Optional[Dict[str, Any]] = None
    # Sage boundary classification. Unclassified and target-facing servers are denied before connect.
    sage_execution_class: str = MCP_EXECUTION_CLASS_UNCLASSIFIED

    def __post_init__(self) -> None:
        self.sage_execution_class = normalize_execution_class(self.sage_execution_class)


class MCPServerManager:
    """Manages multiple MCP server connections and their tools"""
    
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self.connections: Dict[str, Connection] = {}
        self.tools: Dict[str, List[BaseTool]] = {}
        self.configs: Dict[str, MCPConnectionConfig] = {}
        self._session_contexts: Dict[str, Any] = {}
        self._execution_seq = 0

    def set_execution_observer(self, observer):
        """Bind a request-local execution observer and return its reset token."""
        return _execution_observer.set(observer if callable(observer) else None)

    def reset_execution_observer(self, token) -> None:
        _execution_observer.reset(token)

    def set_execution_activity(self, activity: dict[str, str] | None):
        """Bind a request-local grouping activity and return its reset token."""
        return _execution_activity.set(dict(activity) if isinstance(activity, dict) else None)

    def reset_execution_activity(self, token) -> None:
        _execution_activity.reset(token)

    def current_execution_activity(self) -> dict[str, str] | None:
        activity = _execution_activity.get()
        return dict(activity) if isinstance(activity, dict) else None

    def set_execution_context(self, execution_context: str):
        """Bind the request-local MCP authorization context and return its reset token."""
        if execution_context not in {
            MCP_EXECUTION_CONTEXT_GENERAL,
            MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME,
        }:
            raise ValueError(f"unsupported MCP execution context: {execution_context!r}")
        return _execution_context.set(execution_context)

    def reset_execution_context(self, token) -> None:
        _execution_context.reset(token)

    def current_execution_context(self) -> str:
        return _execution_context.get()

    def _forget_server(self, server_name: str) -> None:
        """Drop one server from the in-memory registry after a proven dead transport."""
        self.sessions.pop(server_name, None)
        self.connections.pop(server_name, None)
        self.configs.pop(server_name, None)
        self._session_contexts.pop(server_name, None)
        self.tools.pop(server_name, None)

    async def _notify_execution_observer(self, event: dict[str, Any]) -> None:
        observer = _execution_observer.get()
        if observer is None:
            return
        try:
            observed = observer(event)
            if inspect.isawaitable(observed):
                await observed
        except Exception as exc:
            logger.debug(f"MCP execution observer failed (non-fatal): {exc}")

    def _wrap_tool_for_visibility(self, server_name: str, tool: BaseTool) -> Optional[BaseTool]:
        """Clone one MCP tool with lifecycle observation around its outbound call.

        The wrapper is part of the execution boundary. If it cannot be installed,
        the tool is not registered.
        """
        original = getattr(tool, "coroutine", None)
        if not callable(original):
            logger.warning(f"MCP tool {server_name}.{tool.name} has no async coroutine; tool denied")
            return None

        self._execution_seq += 1
        wrapper_version = self._execution_seq
        manager = self

        async def _observed_coroutine(*args, **kwargs):
            if (
                manager.current_execution_context() == MCP_EXECUTION_CONTEXT_OFFENSIVE_RUNTIME
                and not manager.is_bloodhound_server(server_name)
            ):
                raise PermissionError(
                    f"MCP tool '{server_name}.{tool.name}' denied: offensive runtime permits only "
                    "the canonical BloodHound control-plane server"
                )
            manager._execution_seq += 1
            call_id = f"mcp:{server_name}:{tool.name}:{manager._execution_seq}"
            arguments = kwargs if kwargs else {"args": list(args)}
            base_event = {
                "event_id": call_id,
                "source": "mcp",
                "server": server_name,
                "tool_name": tool.name,
                "arguments": arguments,
                "activity": manager.current_execution_activity(),
            }
            await manager._notify_execution_observer({**base_event, "status": "started"})
            try:
                result = await original(*args, **kwargs)
            except BaseException as exc:
                if _transport_is_closed(exc):
                    manager._forget_server(server_name)
                    logger.warning(
                        f"MCP server '{server_name}' dropped from registry after closed transport: "
                        f"{type(exc).__name__}: {exc}"
                    )
                await manager._notify_execution_observer(
                    {
                        **base_event,
                        "status": "error",
                        "result_preview": f"{type(exc).__name__}: {exc}",
                        "output": f"{type(exc).__name__}: {exc}",
                    }
                )
                raise
            result_text = _execution_result_text(result)
            await manager._notify_execution_observer({
                **base_event,
                "status": "completed",
                "result_preview": result_text,
                "output": result_text,
            })
            return result

        try:
            wrapped = tool.model_copy(deep=False)
            object.__setattr__(wrapped, "coroutine", _observed_coroutine)
            metadata = dict(getattr(wrapped, "metadata", None) or {})
            metadata["sage_visibility_wrapper"] = wrapper_version
            object.__setattr__(wrapped, "metadata", metadata)
            return wrapped
        except Exception as exc:
            logger.warning(
                f"Could not wrap MCP tool {server_name}.{tool.name} for visibility; tool denied: {exc}"
            )
            return None

    @staticmethod
    def _is_canonical_bloodhound_config(config: MCPConnectionConfig | None) -> bool:
        """Recognize the one BloodHound launch shape admitted to offensive runtime."""
        args = list(config.args or []) if config is not None else []
        return bool(
            config is not None
            and config.name == "BloodHound"
            and normalize_execution_class(config.sage_execution_class)
            == MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE
            and config.connection_type == ConnectionType.STDIO
            and bool(config.cwd)
            and args == ["--directory", config.cwd, "run", "main.py"]
        )

    def is_bloodhound_server(self, server_name: str) -> bool:
        config = self.configs.get(server_name)
        return bool(server_name == "BloodHound" and self._is_canonical_bloodhound_config(config))
    
    async def connect_server(self, config: MCPConnectionConfig) -> tuple[bool, Optional[str]]:
        """
        Connect to an MCP server based on the provided configuration

        Args:
            config: MCPConnectionConfig object containing connection details

        Returns:
            tuple: (success: bool, error_message: Optional[str])
        """
        if not execution_class_allowed(getattr(config, "sage_execution_class", None)):
            execution_class = normalize_execution_class(getattr(config, "sage_execution_class", None))
            return (
                False,
                f"MCP server '{config.name}' denied before connect: execution class '{execution_class}' is not allowed",
            )
        if (
            normalize_execution_class(getattr(config, "sage_execution_class", None))
            == MCP_EXECUTION_CLASS_BLOODHOUND_CONTROL_PLANE
            and not self._is_canonical_bloodhound_config(config)
        ):
            return (
                False,
                f"MCP server '{config.name}' denied before connect: bloodhound_control_plane requires "
                "the canonical BloodHound stdio configuration",
            )

        # Set up a temporary exception handler to capture background task errors
        captured_exceptions: List[str] = []
        loop = asyncio.get_event_loop()
        original_handler = loop.get_exception_handler()

        def capture_exception_handler(loop, context):
            """Temporary exception handler to capture background task errors"""
            exc = context.get('exception')
            if exc:
                # Extract meaningful error from the background task exception
                exc_str = str(exc)
                exc_type = type(exc).__name__
                # Check if it's an SSL/connection error
                if any(kw in exc_str for kw in ['SSL', 'certificate', 'ConnectError', 'Connection']):
                    captured_exceptions.append(f"{exc_type}: {exc_str}")
                elif isinstance(exc, BaseExceptionGroup):
                    # Try to extract from exception group
                    for nested in exc.exceptions:
                        nested_str = str(nested)
                        if any(kw in nested_str for kw in ['SSL', 'certificate', 'ConnectError', 'Connection']):
                            captured_exceptions.append(f"{type(nested).__name__}: {nested_str}")
            # Call original handler if it exists
            if original_handler:
                original_handler(loop, context)
            else:
                # Default behavior: log the exception
                logger.debug(f"Background task exception captured: {context.get('message', 'Unknown')}")

        loop.set_exception_handler(capture_exception_handler)

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

                # Use insecure client if SSL verification is disabled
                client_factory = None
                if config.ssl_verify is False:
                    logger.warning(f"SSL verification disabled for MCP server '{config.name}' - NOT recommended for production")
                    client_factory = _create_insecure_httpx_client

                sse_connection: SSEConnection = {
                    "transport": "sse",
                    "url": config.url,
                    "headers": config.headers,
                    "timeout": config.timeout or 5.0,
                    "sse_read_timeout": config.sse_read_timeout or 300.0,
                    "session_kwargs": config.session_kwargs,
                    "httpx_client_factory": client_factory
                }
                connection = sse_connection

            elif config.connection_type == ConnectionType.STREAMABLE_HTTP:
                if not config.url:
                    raise ValueError("Streamable HTTP connection requires 'url' parameter")

                # Use insecure client if SSL verification is disabled
                client_factory = None
                if config.ssl_verify is False:
                    logger.warning(f"SSL verification disabled for MCP server '{config.name}' - NOT recommended for production")
                    client_factory = _create_insecure_httpx_client

                streamable_connection: StreamableHttpConnection = {
                    "transport": "streamable_http",
                    "url": config.url,
                    "headers": config.headers,
                    "timeout": timedelta(seconds=config.timeout or 30),
                    "sse_read_timeout": timedelta(seconds=config.sse_read_timeout or 300),
                    "terminate_on_close": config.terminate_on_close if config.terminate_on_close is not None else True,
                    "session_kwargs": config.session_kwargs,
                    "httpx_client_factory": client_factory
                }
                connection = streamable_connection
                
            else:
                raise ValueError(f"Unsupported connection type: {config.connection_type}")
            
            # Use the create_session context manager
            session_context = create_session(connection)
            try:
                session = await session_context.__aenter__()
            except BaseException as enter_exc:
                logger.debug(f"Exception during __aenter__: {type(enter_exc).__name__}: {enter_exc}")
                raise

            # Initialize the session
            try:
                await session.initialize()
            except BaseException as init_exc:
                logger.debug(f"Exception during initialize: {type(init_exc).__name__}: {init_exc}")
                # Try to clean up the session context
                try:
                    await session_context.__aexit__(type(init_exc), init_exc, init_exc.__traceback__)
                except Exception:
                    pass  # Ignore cleanup errors
                raise init_exc
            
            # Store the session and connection info
            self.sessions[config.name] = session
            self.connections[config.name] = connection
            self.configs[config.name] = config
            self._session_contexts[config.name] = session_context
            
            # Load and convert tools
            await self._load_tools_for_server(config.name)
            
            logger.info(f"Successfully connected to MCP server '{config.name}'")
            return (True, None)

        except BaseException as e:
            # Log full traceback for debugging
            logger.error(f"Failed to connect to MCP server '{config.name}':\n{traceback.format_exc()}")

            # Build error message - prioritize captured background exceptions (SSL errors etc)
            if captured_exceptions:
                error_msg = captured_exceptions[0]
            else:
                # Just use the exception directly
                error_msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__

            return (False, error_msg)

        finally:
            # Always restore the original exception handler
            loop.set_exception_handler(original_handler)

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
                # Try to gracefully exit the session context
                # Note: This may fail if called from a different task than where it was created
                # due to anyio's cancel scope restrictions
                session_context = self._session_contexts.get(server_name)
                if session_context:
                    try:
                        await session_context.__aexit__(None, None, None)
                    except RuntimeError as e:
                        if "cancel scope" in str(e).lower() or "different task" in str(e).lower():
                            # This is expected when disconnecting from a different task
                            # The session will be cleaned up when garbage collected
                            logger.warning(f"Could not gracefully close session for '{server_name}' (cross-task context manager). Resources will be cleaned up on garbage collection.")
                        else:
                            raise

                # Clean up stored references regardless of context exit success.
                self._forget_server(server_name)

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
            config = self.configs.get(server_name)
            if config is None or not execution_class_allowed(config.sage_execution_class):
                logger.warning(f"MCP server '{server_name}' tool load denied: missing allowed execution class")
                self.tools[server_name] = []
                return
            session = self.sessions[server_name]
            connection = self.connections[server_name]

            # Use the langchain_mcp_adapters function to load and convert tools
            langchain_tools = await load_mcp_tools(session, connection=connection)

            # Check for tool name conflicts with existing tools (warn but don't exclude)
            existing_tool_names = self._get_all_existing_tool_names()
            conflicts_found = 0

            for tool in langchain_tools:
                if tool.name in existing_tool_names:
                    logger.warning(f"Tool name conflict: '{tool.name}' from server '{server_name}' conflicts with existing tool. Use server_name parameter to disambiguate.")
                    conflicts_found += 1

            # Store ALL tools for this server (including ones with conflicting names).
            # Observation is installed here so every Sage caller shares the same outbound boundary.
            wrapped_tools = [
                self._wrap_tool_for_visibility(server_name, tool)
                for tool in langchain_tools
            ]
            self.tools[server_name] = [tool for tool in wrapped_tools if tool is not None]

            if conflicts_found > 0:
                logger.warning(f"Loaded {len(langchain_tools)} tools from server '{server_name}' ({conflicts_found} tools have name conflicts with other servers)")
            else:
                logger.info(f"Loaded {len(langchain_tools)} tools from server '{server_name}'")

        except Exception as e:
            logger.error(f"Failed to load tools for server '{server_name}': {e}")
            self.tools[server_name] = []

    def _get_all_existing_tool_names(self) -> set:
        """Get a set of all existing tool names from all connected servers"""
        existing_names = set()
        for server_tools in self.tools.values():
            for tool in server_tools:
                existing_names.add(tool.name)
        return existing_names

    def get_servers_with_tool(self, tool_name: str) -> List[str]:
        """Get list of server names that have a tool with the given name"""
        servers = []
        for server_name, tools in self.tools.items():
            for tool in tools:
                if tool.name == tool_name:
                    servers.append(server_name)
                    break
        return servers

    def has_tool_conflict(self, tool_name: str) -> bool:
        """Check if a tool name exists on multiple servers"""
        return len(self.get_servers_with_tool(tool_name)) > 1
    
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
    
    async def get_tool_by_name(self, tool_name: str, server_name: Optional[str] = None) -> Optional[BaseTool]:
        """
        Get a specific tool by name, optionally from a specific server

        Args:
            tool_name: Name of the tool to find
            server_name: Optional server name to limit search to

        Returns:
            The BaseTool instance if found, None otherwise
        """
        if server_name:
            # Search only the specified server
            server_tools = self.tools.get(server_name, [])
            for tool in server_tools:
                if tool.name == tool_name:
                    return tool
            return None
        else:
            # Search all servers, return first match
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

MCPManager = MCPServerManager()

# Convenience functions to create connection configs
def create_stdio_config(name: str, command: str, args: List[str] | None, 
                       env: Dict[str, str] | None, cwd: str | None,
                       encoding: str | None, encoding_error_handler: str | None,
                       session_kwargs: Dict[str, Any] | None, **kwargs) -> MCPConnectionConfig:
    """Create a configuration for STDIO MCP connection"""
    sage_execution_class = kwargs.pop("sage_execution_class", MCP_EXECUTION_CLASS_UNCLASSIFIED)
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
        extra_params=kwargs,
        sage_execution_class=sage_execution_class,
    )


def create_sse_config(name: str, url: str, headers: Dict[str, str] | None = None,
                     timeout: float | None = None, sse_read_timeout: float | None = None,
                     ssl_verify: bool | None = True,
                     session_kwargs: Dict[str, Any] | None = None, **kwargs) -> MCPConnectionConfig:
    """Create a configuration for SSE MCP connection"""
    sage_execution_class = kwargs.pop("sage_execution_class", MCP_EXECUTION_CLASS_UNCLASSIFIED)
    return MCPConnectionConfig(
        name=name,
        connection_type=ConnectionType.SSE,
        url=url,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
        ssl_verify=ssl_verify,
        session_kwargs=session_kwargs,
        extra_params=kwargs,
        sage_execution_class=sage_execution_class,
    )


def create_streamable_http_config(name: str, url: str, headers: Dict[str, str] | None = None,
                                 timeout: float | None = None, sse_read_timeout: float | None = None,
                                 terminate_on_close: bool | None = None,
                                 ssl_verify: bool | None = True,
                                 session_kwargs: Dict[str, Any] | None = None, **kwargs) -> MCPConnectionConfig:
    """Create a configuration for Streamable HTTP MCP connection"""
    sage_execution_class = kwargs.pop("sage_execution_class", MCP_EXECUTION_CLASS_UNCLASSIFIED)
    return MCPConnectionConfig(
        name=name,
        connection_type=ConnectionType.STREAMABLE_HTTP,
        url=url,
        headers=headers,
        timeout=timeout,
        sse_read_timeout=sse_read_timeout,
        terminate_on_close=terminate_on_close,
        ssl_verify=ssl_verify,
        session_kwargs=session_kwargs,
        extra_params=kwargs,
        sage_execution_class=sage_execution_class,
    )
