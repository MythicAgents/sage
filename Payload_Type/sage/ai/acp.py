"""Channel-scoped connections to an operator-provisioned ACP coding agent (configuration only).

The deployer runs the coding agent (Codex first) in its own container. Its bridge exposes the agent's ACP
stdio on a private Unix socket; ``SAGE_ACP_SOCKET`` names that socket for the ``codex`` profile. This module
negotiates ACP v1 over it, creates a read-only session, reports the observed authentication state and lets
the operator select one of the models the agent advertises. It never sends ``session/prompt``, advertises no
filesystem or terminal capability, passes no MCP servers and refuses every permission request: no model
inference or coding happens on this path.

Code owns the lifecycle; no Sage model sees or drives it. State is per ``(operation_id, channel_id)`` and lives
only in this process. The selection that survives restarts (profile and model) is stored by the caller.

The official ACP SDK supplies JSON-RPC routing (:class:`acp.connection.Connection`) and the schema models used
to build requests and validate responses. Raw responses are kept beside the validated models because the
typed models drop agent extensions Sage reads (Codex's ``agentCapabilities.auth.status``). The SDK's own
newline transport joins lines past its reader limit, so Sage supplies a transport that enforces 1 MiB.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

ACP_SOCKET_ENV = "SAGE_ACP_SOCKET"
PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 1024 * 1024
CONNECT_DEADLINE_SECONDS = 30.0
REQUEST_DEADLINE_SECONDS = 15.0
CLOSE_DEADLINE_SECONDS = 2.0
MAX_CONNECTIONS = 4
# The ACP session's working directory is a path inside the worker: its empty, read-only workspace.
SESSION_CWD = "/workspace"
READ_ONLY_MODE = "read-only"
API_KEY_METHOD = "api-key"
AUTH_STATES = frozenset({"authenticated", "unauthenticated", "unknown", "not_required"})
_ERROR_TEXT_LIMIT = 200
# sockaddr_un.sun_path holds 108 bytes on Linux, including the terminating NUL.
_UNIX_PATH_LIMIT = 107

TRANSPORT_UNCONFIGURED = "unconfigured"
TRANSPORT_DISCONNECTED = "disconnected"
TRANSPORT_CONNECTING = "connecting"
TRANSPORT_CONNECTED = "connected"
TRANSPORT_FAILED = "failed"


@dataclass(frozen=True)
class Profile:
    """A deployment-provisioned coding agent. Chat selects it by name; it supplies nothing else."""

    name: str
    title: str
    socket_env: str


PROFILES: dict[str, Profile] = {"codex": Profile("codex", "Codex", ACP_SOCKET_ENV)}


class AcpError(Exception):
    """An operator-facing failure. The message names no secret and no raw protocol payload."""


class AcpUnconfigured(AcpError):
    """The profile's socket setting is missing or unusable; nothing was opened."""


class MessageTooLarge(ConnectionError):
    """A single ACP message exceeded :data:`MAX_MESSAGE_BYTES`."""


def _bounded(text: Any) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= _ERROR_TEXT_LIMIT else value[: _ERROR_TEXT_LIMIT - 1] + "…"


def _request_error_text(exc: BaseException) -> str:
    data = getattr(exc, "data", None)
    detail = ""
    if isinstance(data, dict):
        detail = str(data.get("message") or data.get("details") or "")
    elif isinstance(data, str):
        detail = data
    message = str(exc)
    return _bounded(f"{message}: {detail}" if detail and detail not in message else message)


class BoundedNdjsonTransport:
    """Newline-delimited JSON-RPC over an asyncio byte stream, at most one MiB per message.

    Implements the SDK's message-level ``Transport`` protocol. An oversized line in either direction fails
    the connection instead of being reassembled. ``lost`` records why the stream ended.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 *, limit: int = MAX_MESSAGE_BYTES) -> None:
        self._reader = reader
        self._writer = writer
        self._limit = limit
        self.lost = ""

    async def send(self, message: dict[str, Any]) -> None:
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(data) > self._limit:
            raise MessageTooLarge(f"outgoing ACP message exceeds {self._limit} bytes")
        self._writer.write(data)
        await self._writer.drain()

    async def receive(self) -> dict[str, Any] | None:
        while True:
            try:
                line = await self._reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                self.lost = self.lost or "the coding agent closed the connection"
                return None
            except asyncio.LimitOverrunError:
                self.lost = f"the coding agent sent a message over {self._limit} bytes"
                raise MessageTooLarge(self.lost) from None
            except (ConnectionError, OSError) as exc:
                self.lost = self.lost or f"connection error: {type(exc).__name__}"
                return None
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.lost = "the coding agent sent invalid JSON"
                raise ConnectionError(self.lost) from None
            if isinstance(message, dict):
                return message

    async def close(self) -> None:
        self.lost = self.lost or "closed by Sage"
        with contextlib.suppress(Exception):
            self._writer.close()
            await asyncio.wait_for(self._writer.wait_closed(), CLOSE_DEADLINE_SECONDS)


def _flatten_select(option: dict[str, Any]) -> list[dict[str, str]]:
    """Choices of a select config option, flat or grouped, with their exact values."""
    choices: list[dict[str, str]] = []
    for item in option.get("options") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("options"), list):
            group = str(item.get("name") or item.get("group") or "")
            for sub in item["options"]:
                if isinstance(sub, dict) and isinstance(sub.get("value"), str):
                    choices.append({"value": sub["value"], "name": str(sub.get("name") or ""), "group": group})
        elif isinstance(item.get("value"), str):
            choices.append({"value": item["value"], "name": str(item.get("name") or ""), "group": ""})
    return choices


def model_option(config_options: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The session's model selector: the select option in the ``model`` category."""
    for option in config_options or []:
        if isinstance(option, dict) and option.get("category") == "model" and option.get("type") == "select":
            return option
    return None


@dataclass
class _Live:
    profile: str
    conn: Any
    transport: BoundedNdjsonTransport
    protocol_version: int = 0
    agent_name: str = ""
    agent_version: str = ""
    auth_state: str = "unknown"
    auth_note: str = ""
    session_id: str = ""
    session_note: str = ""
    close_supported: bool = False
    config_options: list[dict[str, Any]] = field(default_factory=list)
    denied_requests: int = 0

    @property
    def lost(self) -> str:
        return self.transport.lost


@dataclass
class _Channel:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    live: _Live | None = None
    opening: BoundedNdjsonTransport | None = None
    state: str = TRANSPORT_DISCONNECTED
    last_error: str = ""


@dataclass(frozen=True)
class ChannelView:
    """What status reports for one channel. Never includes a secret or a session identifier."""

    transport: str
    profile: str = ""
    error: str = ""
    protocol_version: int = 0
    agent: str = ""
    auth_state: str = ""
    auth_note: str = ""
    session_note: str = ""
    effective_model: str = ""
    model_selectable: bool = False
    choices: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class ConnectResult:
    view: ChannelView
    notes: tuple[str, ...] = ()
    already_connected: bool = False


def _socket_path(profile: Profile) -> str:
    configured = os.environ.get(profile.socket_env, "").strip()
    if not configured:
        raise AcpUnconfigured(
            f"The `{profile.name}` coding agent is not configured: set {profile.socket_env} to the worker's "
            "socket path in Sage's environment and restart Sage."
        )
    if not os.path.isabs(configured):
        raise AcpUnconfigured(f"{profile.socket_env} must be an absolute path.")
    if len(os.fsencode(configured)) > _UNIX_PATH_LIMIT:
        raise AcpUnconfigured(
            f"{profile.socket_env} is longer than the {_UNIX_PATH_LIMIT}-byte Unix socket path limit; "
            "mount the socket directory at a shorter path."
        )
    return configured


def _sage_version() -> str:
    try:
        from _version import __version__
    except Exception:  # pragma: no cover - version banner is optional in alternate import roots
        return "unknown"
    return str(__version__ or "unknown")


class AcpConnectionManager:
    """Owns every channel's live ACP connection in this process."""

    def __init__(self) -> None:
        self._channels: dict[tuple[int, int], _Channel] = {}

    def _channel(self, key: tuple[int, int]) -> _Channel:
        return self._channels.setdefault(key, _Channel())

    def _live_count(self) -> int:
        return sum(1 for ch in self._channels.values() if ch.live is not None and not ch.live.lost)

    def view(self, key: tuple[int, int], profile: str = "codex") -> ChannelView:
        ch = self._channels.get(key)
        live = ch.live if ch is not None else None
        if live is not None and live.lost:
            return ChannelView(transport=TRANSPORT_DISCONNECTED, profile=live.profile,
                               error=f"Connection lost: {live.lost}.")
        if live is not None:
            option = model_option(live.config_options)
            return ChannelView(
                transport=TRANSPORT_CONNECTED,
                profile=live.profile,
                protocol_version=live.protocol_version,
                agent=f"{live.agent_name} {live.agent_version}".strip(),
                auth_state=live.auth_state,
                auth_note=live.auth_note,
                session_note=live.session_note,
                effective_model=str(option.get("currentValue") or "") if option else "",
                model_selectable=option is not None,
                choices=tuple(_flatten_select(option)) if option else (),
            )
        spec = PROFILES.get(profile)
        if spec is not None and not os.environ.get(spec.socket_env, "").strip():
            return ChannelView(transport=TRANSPORT_UNCONFIGURED, profile=profile,
                               error=f"Set {spec.socket_env} to the worker's socket path.")
        if ch is not None and ch.state in {TRANSPORT_CONNECTING, TRANSPORT_FAILED}:
            return ChannelView(transport=ch.state, profile=profile, error=ch.last_error)
        return ChannelView(transport=TRANSPORT_DISCONNECTED, profile=profile)

    async def connect(self, key: tuple[int, int], profile: str, saved_model: str = "") -> ConnectResult:
        spec = PROFILES.get(profile)
        if spec is None:
            raise AcpError(f"Unknown coding-agent profile `{profile}`.")
        socket_path = _socket_path(spec)
        ch = self._channel(key)
        async with ch.lock:
            if ch.live is not None and not ch.live.lost and ch.live.profile == profile:
                return ConnectResult(self.view(key, profile), already_connected=True)
            if ch.live is not None:
                await self._close_live(ch.live, graceful=False)
                ch.live = None
            if self._live_count() >= MAX_CONNECTIONS:
                raise AcpError(f"Sage already holds {MAX_CONNECTIONS} coding-agent connections; disconnect one first.")
            ch.state, ch.last_error = TRANSPORT_CONNECTING, ""
            try:
                live, notes = await asyncio.wait_for(
                    self._open(ch, spec, socket_path, saved_model), CONNECT_DEADLINE_SECONDS
                )
            except TimeoutError:
                ch.state, ch.last_error = TRANSPORT_FAILED, (
                    f"Connecting took longer than {CONNECT_DEADLINE_SECONDS:g} seconds."
                )
                raise AcpError(ch.last_error) from None
            except AcpError as exc:
                ch.state, ch.last_error = TRANSPORT_FAILED, str(exc)
                raise
            except (OSError, ConnectionError) as exc:
                reason = type(exc).__name__ + (f": {exc.strerror}" if getattr(exc, "strerror", None) else "")
                ch.state, ch.last_error = TRANSPORT_FAILED, (
                    f"The coding-agent worker is unavailable ({_bounded(reason)}). "
                    "Check that its container is running and its socket is mounted."
                )
                raise AcpError(ch.last_error) from None
            finally:
                ch.opening = None
            ch.live, ch.state = live, TRANSPORT_CONNECTED
            return ConnectResult(self.view(key, profile), tuple(notes))

    async def _open(self, ch: _Channel, spec: Profile, socket_path: str,
                    saved_model: str) -> tuple[_Live, list[str]]:
        from acp.connection import Connection
        from acp.exceptions import RequestError
        from acp.schema import (
            AuthenticateRequest,
            ClientCapabilities,
            FileSystemCapabilities,
            Implementation,
            InitializeRequest,
            InitializeResponse,
            NewSessionRequest,
            NewSessionResponse,
            SetSessionModeRequest,
        )

        reader, writer = await asyncio.open_unix_connection(socket_path, limit=MAX_MESSAGE_BYTES)
        transport = BoundedNdjsonTransport(reader, writer)
        ch.opening = transport
        live = _Live(profile=spec.name, conn=None, transport=transport)
        live.conn = Connection(lambda method, params, is_notification: self._inbound(
            live, method, params, is_notification), transport)
        notes: list[str] = []
        try:
            raw_init = await self._request(live, "initialize", InitializeRequest(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=False, write_text_file=False), terminal=False,
                ),
                client_info=Implementation(name="sage", version=_sage_version()),
            ), during_connect=True)
            version = raw_init.get("protocolVersion") if isinstance(raw_init, dict) else None
            if not isinstance(version, int) or isinstance(version, bool) or version != PROTOCOL_VERSION:
                raise AcpError(f"The coding agent offered ACP protocol {version!r}; Sage supports only 1.")
            init = InitializeResponse.model_validate(raw_init)
            live.protocol_version = version
            live.agent_name = init.agent_info.name if init.agent_info else ""
            live.agent_version = init.agent_info.version if init.agent_info else ""
            capabilities = raw_init.get("agentCapabilities") or {}
            auth_caps = capabilities.get("auth") if isinstance(capabilities, dict) else None
            session_caps = capabilities.get("sessionCapabilities") if isinstance(capabilities, dict) else None
            live.close_supported = isinstance(session_caps, dict) and "close" in session_caps

            if any(method.id == API_KEY_METHOD for method in init.auth_methods or []):
                try:
                    await self._request(live, "authenticate", AuthenticateRequest(method_id=API_KEY_METHOD),
                                        during_connect=True)
                except RequestError as exc:
                    live.auth_note = (
                        "No usable API-key configuration in the worker "
                        f"(the agent answered: {_request_error_text(exc)})."
                    )
            if isinstance(auth_caps, dict) and "status" in auth_caps:
                try:
                    status = await self._request(live, "auth/status", {}, during_connect=True)
                except RequestError as exc:
                    live.auth_state = "unknown"
                    live.auth_note = live.auth_note or f"Status unavailable: {_request_error_text(exc)}."
                else:
                    state = status.get("state") if isinstance(status, dict) else None
                    live.auth_state = state if state in AUTH_STATES else "unknown"
            else:
                live.auth_state = "unknown"

            try:
                raw_session = await self._request(live, "session/new", NewSessionRequest(
                    cwd=SESSION_CWD, mcp_servers=[],
                ), during_connect=True)
            except RequestError as exc:
                live.session_note = f"No session: {_request_error_text(exc)}."
                return live, notes
            session = NewSessionResponse.model_validate(raw_session)
            live.session_id = session.session_id
            live.config_options = [o for o in raw_session.get("configOptions") or [] if isinstance(o, dict)]
            modes = session.modes
            if modes is not None and modes.current_mode_id != READ_ONLY_MODE:
                if not any(mode.id == READ_ONLY_MODE for mode in modes.available_modes or []):
                    raise AcpError("The coding agent offers no read-only session mode.")
                await self._request(live, "session/set_mode", SetSessionModeRequest(
                    session_id=live.session_id, mode_id=READ_ONLY_MODE,
                ), during_connect=True)
            if saved_model:
                option = model_option(live.config_options)
                if option is None:
                    notes.append(f"Saved model `{saved_model}` was not applied: this agent has no model selector.")
                elif option.get("currentValue") == saved_model:
                    notes.append(f"Saved model `{saved_model}` is in effect.")
                elif saved_model not in {c["value"] for c in _flatten_select(option)}:
                    notes.append(
                        f"Saved model `{saved_model}` is not advertised by this agent; the agent's default "
                        "is in effect. Choose one with `/acp model <id>`."
                    )
                else:
                    await self._set_model_locked(live, saved_model)
                    notes.append(f"Reapplied saved model `{saved_model}`.")
            return live, notes
        except RequestError as exc:
            await self._close_live(live, graceful=False)
            raise AcpError(f"The coding agent refused the connection: {_request_error_text(exc)}.") from None
        except BaseException:
            await self._close_live(live, graceful=False)
            raise

    async def _request(self, live: _Live, method: str, params: Any, *, during_connect: bool = False) -> Any:
        """Send one request. Connect steps share the connect deadline; management requests get their own."""
        if hasattr(params, "model_dump"):
            params = params.model_dump(mode="json", by_alias=True, exclude_none=True)
        deadline = None if during_connect else REQUEST_DEADLINE_SECONDS
        try:
            return await asyncio.wait_for(live.conn.send_request(method, params), deadline)
        except TimeoutError:
            raise AcpError(f"The coding agent did not answer `{method}` within "
                           f"{REQUEST_DEADLINE_SECONDS:g} seconds.") from None
        except ConnectionError:
            raise AcpError(f"The coding-agent connection closed during `{method}`"
                           + (f": {live.lost}." if live.lost else ".")) from None

    async def _inbound(self, live: _Live, method: str, params: Any, is_notification: bool) -> Any:
        from acp.exceptions import RequestError

        if is_notification:
            if not isinstance(params, dict):
                return None
            if method == "session/update":
                if params.get("sessionId") != live.session_id:
                    return None
                update = params.get("update")
                if isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
                    options = update.get("configOptions")
                    if isinstance(options, list):
                        live.config_options = [o for o in options if isinstance(o, dict)]
            elif method == "auth/status_update":
                state = params.get("state")
                live.auth_state = state if state in AUTH_STATES else "unknown"
            return None
        live.denied_requests += 1
        logger.warning("Refused ACP agent request %s on a configuration-only connection", _bounded(method))
        if method == "session/request_permission":
            return {"outcome": {"outcome": "cancelled"}}
        raise RequestError.method_not_found(method)

    async def set_model(self, key: tuple[int, int], model_id: str) -> str:
        """Select an advertised model for the channel's session; returns the effective model."""
        ch = self._channels.get(key)
        if ch is None or ch.live is None or ch.live.lost:
            raise AcpError("No coding-agent connection on this channel. Run `/acp connect codex` first.")
        async with ch.lock:
            live = ch.live
            if live is None or live.lost:
                raise AcpError("No coding-agent connection on this channel. Run `/acp connect codex` first.")
            return await self._set_model_locked(live, model_id)

    async def _set_model_locked(self, live: _Live, model_id: str) -> str:
        from acp.exceptions import RequestError
        from acp.schema import SetSessionConfigOptionResponse, SetSessionConfigOptionSelectRequest

        if not live.session_id:
            raise AcpError(f"This connection has no session. {live.session_note}".strip())
        option = model_option(live.config_options)
        if option is None:
            raise AcpError("This coding agent has no model selector; its model cannot be changed from Sage.")
        choices = _flatten_select(option)
        if model_id not in {c["value"] for c in choices}:
            labelled = [c["value"] for c in choices if c["name"] == model_id]
            hint = f" `{model_id}` is a display name; use `{labelled[0]}`." if labelled else ""
            raise AcpError(f"`{model_id}` is not a model this agent advertises.{hint} See `/acp models`.")
        try:
            raw = await self._request(live, "session/set_config_option", SetSessionConfigOptionSelectRequest(
                session_id=live.session_id, config_id=str(option.get("id") or ""), value=model_id,
            ))
        except RequestError as exc:
            raise AcpError(f"The coding agent refused the model change: {_request_error_text(exc)}.") from None
        SetSessionConfigOptionResponse.model_validate(raw)
        returned = [o for o in raw.get("configOptions") or [] if isinstance(o, dict)]
        live.config_options = returned
        current = model_option(returned)
        effective = str(current.get("currentValue") or "") if current else ""
        if effective != model_id:
            raise AcpError(f"The coding agent reports `{effective or 'no model'}` after the change; "
                           "the saved selection was not changed.")
        return effective

    async def disconnect(self, key: tuple[int, int]) -> bool:
        """Close the channel's connection gracefully. Returns whether one was open."""
        return await self._close_channel(key, graceful=True)

    async def close_channel(self, key: tuple[int, int]) -> bool:
        """Close the channel's connection now (``/stop``), without waiting on an operation in progress."""
        return await self._close_channel(key, graceful=False)

    async def _close_channel(self, key: tuple[int, int], *, graceful: bool) -> bool:
        ch = self._channels.get(key)
        if ch is None:
            return False
        live, opening = ch.live, ch.opening
        ch.live = None
        ch.state, ch.last_error = TRANSPORT_DISCONNECTED, ""
        had = (live is not None and not live.lost) or opening is not None
        if opening is not None and (live is None or opening is not live.transport):
            await opening.close()
        if live is not None:
            await self._close_live(live, graceful=graceful)
        return had

    async def _close_live(self, live: _Live, *, graceful: bool) -> None:
        if graceful and live.session_id and live.close_supported and not live.lost:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    live.conn.send_request("session/close", {"sessionId": live.session_id}),
                    CLOSE_DEADLINE_SECONDS,
                )
        if live.conn is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(live.conn.close(), CLOSE_DEADLINE_SECONDS)
        await live.transport.close()

    async def close_all(self) -> None:
        for key in list(self._channels):
            await self.close_channel(key)


ACP_MANAGER = AcpConnectionManager()


def _cell(value: str) -> str:
    return str(value).replace("|", "\\|")


def render_status(view: ChannelView, binding: dict[str, str] | None, *, error: str = "",
                  notes: tuple[str, ...] = ()) -> str:
    saved_profile = (binding or {}).get("profile") or ""
    saved_model = (binding or {}).get("model") or ""
    profile = view.profile or saved_profile or "codex"
    rows = [
        "**Coding agent connection**",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Profile | `{_cell(profile)}`{' (saved for this channel)' if saved_profile else ''} |",
        f"| Transport | `{view.transport}` |",
    ]
    if view.transport == TRANSPORT_CONNECTED:
        rows.append(f"| Protocol | ACP v{view.protocol_version} |")
        if view.agent:
            rows.append(f"| Agent | {_cell(view.agent)} |")
        rows.append(f"| Authentication (observed) | `{view.auth_state or 'unknown'}` |")
    rows.append("| Provider access | unverified: Sage sends the coding agent no model request |")
    if view.transport == TRANSPORT_CONNECTED:
        if view.effective_model:
            rows.append(f"| Effective model | `{_cell(view.effective_model)}` |")
        elif view.session_note:
            rows.append("| Effective model | none (no session) |")
        elif not view.model_selectable:
            rows.append("| Effective model | not reported (no model selector) |")
    rows.append(f"| Saved model | {f'`{_cell(saved_model)}`' if saved_model else 'agent default'} |")
    lines = ["\n".join(rows)]
    extra = [error] if error else []
    if view.error and view.error != error:
        extra.append(view.error)
    if view.auth_note:
        extra.append(view.auth_note)
    if view.session_note:
        extra.append(view.session_note)
    extra.extend(notes)
    if view.transport == TRANSPORT_CONNECTED and view.auth_state == "authenticated":
        extra.append("`authenticated` means the agent holds a credential; it does not prove the provider accepts it.")
    if extra:
        lines.append("\n".join(f"- {_bounded_note(item)}" for item in extra))
    return "\n\n".join(lines)


def _bounded_note(text: str) -> str:
    return " ".join(str(text).split())[:400]


def render_models(view: ChannelView) -> str:
    if view.transport != TRANSPORT_CONNECTED:
        return "No coding-agent connection on this channel. Run `/acp connect codex` first."
    if not view.model_selectable:
        note = f" {view.session_note}" if view.session_note else ""
        return f"This coding agent advertises no model selector.{note}".rstrip()
    rows = ["**Coding agent models**", "", "| Model ID | Name | Current |", "|---|---|---|"]
    for choice in view.choices:
        name = f"{choice['group']}: {choice['name']}" if choice.get("group") else choice["name"]
        current = "yes" if choice["value"] == view.effective_model else ""
        rows.append(f"| `{_cell(choice['value'])}` | {_cell(name)} | {current} |")
    rows.extend(["", "Select one with `/acp model <id>`, using the exact model ID."])
    return "\n".join(rows)


ACP_USAGE = (
    "Usage: `/acp` or `/acp status` · `/acp connect codex` · `/acp models` · `/acp model <id>` · "
    "`/acp disconnect`. The coding agent is configured by the deployer; chat accepts no secrets or commands."
)
