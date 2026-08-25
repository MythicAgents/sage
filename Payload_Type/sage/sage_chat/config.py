"""Config/secret resolution for the native chat container.

Maps a Mythic ``ChatRequest`` onto the exact keyword arguments the existing
``ai.langgraph.model.Model`` constructor already expects — so ``Model.__init__`` is
reused unchanged (PRD Section 9 / 11). The legacy PayloadType path built the same
``config["configurable"]`` dict in ``container/agent_functions/chat.py:262-279``; this
module reproduces that shape from chat primitives instead of task args.

Precedence (PRD Section 9): ``ChatRequest.Config`` (per-chat UI) → ``ChatSecretView``
(Mythic user secret) → container environment variable. The first non-empty wins.

Caveat (Cody, c): ``Config`` and ``APITokenID`` can change on *any* request, so callers
re-resolve these every turn and never cache them across turns.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from mythic_container.ChatBase import ChatConfigView, ChatRequest, ChatSecretView
from ai.langgraph.policy import POLICY_DEFAULT, resolve_policy_mode


def _resolve(
    config: ChatConfigView,
    secrets: ChatSecretView,
    key: str,
    *,
    env_key: str | None = None,
    default: str = "",
) -> str:
    """First non-empty of: per-chat Config → user Secret → env var → default."""
    for view in (config, secrets):
        if view.has(key):
            val = view.text(key, "")
            if val:
                return val
    if env_key:
        env_val = os.environ.get(env_key)
        if env_val:
            return env_val
    return default


_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _resolve_bool(
    config: ChatConfigView,
    secrets: ChatSecretView,
    key: str,
    *,
    env_key: str | None = None,
    default: bool = False,
) -> bool:
    """Boolean twin of ``_resolve`` — same Config → Secret → env → default order, parsed truthy.

    Mirrors ``ChatValueReader.boolean`` truthy/falsy tokens, but resolves across all three layers
    instead of a single view. Unrecognized tokens fall back to ``default`` rather than raising.
    """
    raw = _resolve(config, secrets, key, env_key=env_key, default="").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


try:
    from ai.bloodhound_config import (
        BLOODHOUND_CREDENTIAL_KEYS as BLOODHOUND_ENV_KEYS,
        BLOODHOUND_OPERATOR_CONFIG_KEYS,
        BLOODHOUND_URL_KEY,
        BloodHoundURLError,
        parse_bloodhound_url,
    )
except ImportError:  # pragma: no cover
    from ..ai.bloodhound_config import (  # type: ignore
        BLOODHOUND_CREDENTIAL_KEYS as BLOODHOUND_ENV_KEYS,
        BLOODHOUND_OPERATOR_CONFIG_KEYS,
        BLOODHOUND_URL_KEY,
        BloodHoundURLError,
        parse_bloodhound_url,
    )

logger = logging.getLogger(__name__)


SAGE_LLM_KEY_MAP = MappingProxyType(
    {
        "provider": "provider",
        "model": "model",
        "api_endpoint": "API_ENDPOINT",
        "api_key": "API_KEY",
        "aws_access_key_id": "AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY",
        "aws_session_token": "AWS_SESSION_TOKEN",
        "region": "AWS_DEFAULT_REGION",
    }
)
WATCHER_LLM_KEY_MAP = MappingProxyType(
    {
        "provider": "SAGE_WATCHER_PROVIDER",
        "model": "SAGE_WATCHER_MODEL",
        "api_endpoint": "SAGE_WATCHER_API_ENDPOINT",
        "api_key": "SAGE_WATCHER_API_KEY",
        "aws_access_key_id": "SAGE_WATCHER_AWS_ACCESS_KEY_ID",
        "aws_secret_access_key": "SAGE_WATCHER_AWS_SECRET_ACCESS_KEY",
        "aws_session_token": "SAGE_WATCHER_AWS_SESSION_TOKEN",
        "region": "SAGE_WATCHER_AWS_DEFAULT_REGION",
    }
)
WATCHER_OPTIONAL_USER_SECRETS = (
    WATCHER_LLM_KEY_MAP["api_endpoint"],
    WATCHER_LLM_KEY_MAP["api_key"],
    WATCHER_LLM_KEY_MAP["aws_access_key_id"],
    WATCHER_LLM_KEY_MAP["aws_secret_access_key"],
    WATCHER_LLM_KEY_MAP["aws_session_token"],
    WATCHER_LLM_KEY_MAP["region"],
)


@dataclass(frozen=True)
class ResolvedLLMProfile:
    """One active-process LLM configuration with redaction-safe source labels."""

    provider: str
    model: str
    api_endpoint: str = ""
    api_key: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""
    region: str = ""
    sources: tuple[tuple[str, str], ...] = ()

    def configurable(self) -> dict[str, str]:
        if self.provider == "bedrock":
            values = {
                "aws_access_key_id": self.aws_access_key_id,
                "aws_secret_access_key": self.aws_secret_access_key,
                "aws_session_token": self.aws_session_token,
                "region": self.region,
            }
        else:
            values = {
                "api_key": self.api_key,
                "base_url": self.api_endpoint,
            }
        return {key: value for key, value in values.items() if value}

    def init_chat_model_kwargs(self) -> dict[str, str]:
        return {
            "model_provider": self.provider,
            "model": self.model,
            **self.configurable(),
        }

    def source_for(self, logical_field: str) -> str:
        return dict(self.sources).get(logical_field, "default")


def _nonblank(value: Any) -> str:
    text = value if isinstance(value, str) else str(value or "")
    return text if text.strip() else ""


def _resolve_with_source(
    config: ChatConfigView,
    secrets: ChatSecretView,
    key: str,
    *,
    env_key: str,
    default: str = "",
    allow_secret: bool = True,
    include_secrets: bool = True,
) -> tuple[str, str]:
    if config.has(key):
        value = _nonblank(config.text(key, ""))
        if value:
            return value, "ui-config"
    if include_secrets and allow_secret and secrets.has(key):
        value = _nonblank(secrets.text(key, ""))
        if value:
            return value, "user-secret"
    value = _nonblank(os.environ.get(env_key, ""))
    if value:
        return value, "environment"
    return default, "default"


def resolve_llm_profile(
    request: ChatRequest,
    *,
    key_map: MappingProxyType[str, str] | dict[str, str] = SAGE_LLM_KEY_MAP,
    include_secrets: bool = True,
    provider_model_secrets: bool = True,
) -> ResolvedLLMProfile:
    """Resolve either role through one first-non-empty algorithm and field projection."""

    config = ChatConfigView.from_request(request)
    secrets = ChatSecretView.from_request(request)
    resolved: dict[str, str] = {}
    sources: list[tuple[str, str]] = []
    for logical_field, key in key_map.items():
        value, source = _resolve_with_source(
            config,
            secrets,
            key,
            env_key=key,
            default="openai" if logical_field == "provider" else "",
            allow_secret=(provider_model_secrets or logical_field not in {"provider", "model"}),
            include_secrets=include_secrets,
        )
        resolved[logical_field] = value
        sources.append((logical_field, source))
    resolved["provider"] = resolved["provider"].strip().casefold() or "openai"
    resolved["model"] = resolved["model"].strip()
    resolved["api_endpoint"] = resolved["api_endpoint"].strip()
    return ResolvedLLMProfile(**resolved, sources=tuple(sources))


def resolve_watcher_llm_profile(
    request: ChatRequest,
    *,
    include_secrets: bool = True,
) -> ResolvedLLMProfile:
    return resolve_llm_profile(
        request,
        key_map=WATCHER_LLM_KEY_MAP,
        include_secrets=include_secrets,
        provider_model_secrets=False,
    )


def init_chat_model_from_profile(profile: ResolvedLLMProfile) -> Any:
    """Construct either role's base model from the shared normalized field projection."""

    if not profile.model:
        raise ValueError("model is not configured")
    from langchain.chat_models import init_chat_model

    kwargs = profile.init_chat_model_kwargs()
    if profile.provider == "bedrock":
        missing = tuple(
            field
            for field in (
                "aws_access_key_id",
                "aws_secret_access_key",
                "aws_session_token",
            )
            if not kwargs.get(field)
        )
        if missing:
            raise ValueError(
                "Bedrock model configuration is missing: " + ", ".join(missing)
            )
        kwargs.setdefault("region", "us-east-1")
    return init_chat_model(**kwargs)


def build_bloodhound_env(request: ChatRequest) -> dict[str, str]:
    """Resolve BloodHound MCP credentials through the standard Config → Secret → env chain.

    These are forwarded into the MCP server subprocess rather than read by Sage itself. The
    forwarding is not optional plumbing: the MCP stdio client inherits only a safe subset of the
    parent environment (``HOME``/``LOGNAME``/``PATH``/``SHELL``/``TERM``/``USER`` on POSIX — see
    ``mcp.client.stdio.DEFAULT_INHERITED_ENV_VARS``), so a ``BLOODHOUND_*`` variable set on the Sage
    container never reaches the server on its own. The SDK merges what we pass over that safe set
    (``stdio/__init__.py``: ``{**get_default_environment(), **server.env}``), so supplying a partial
    dict adds to the defaults instead of replacing them.

    Only non-empty values are returned, so an unset key stays unset in the subprocess and the MCP
    server falls back to its own directory ``.env`` — which is what keeps the file-based workflow
    working unchanged for operators who prefer it.
    """
    config = ChatConfigView.from_request(request)
    secrets = ChatSecretView.from_request(request)
    resolved: dict[str, str] = {}

    # The operator configures ONE address, `BLOODHOUND_URL`, and it is expanded here — at the
    # subprocess-env boundary and nowhere else — into the three keys the MCP server actually reads.
    # Parsing at the edge means nothing downstream learns about the new shape: the allowlist, the
    # canonical-config guard and the server all keep seeing exactly what they saw before.
    url = _resolve(config, secrets, BLOODHOUND_URL_KEY, env_key=BLOODHOUND_URL_KEY, default="")
    if url:
        try:
            resolved.update(parse_bloodhound_url(url))
        except BloodHoundURLError as exc:
            # Deliberately not swallowed into an empty dict: an unparseable URL is a configuration
            # error the operator can fix in seconds, and dropping it here would surface later as
            # "credentials are unset", which sends them looking for the wrong thing entirely.
            logger.warning(f"{BLOODHOUND_URL_KEY} could not be used: {exc}")

    for key in (k for k in BLOODHOUND_OPERATOR_CONFIG_KEYS if k != BLOODHOUND_URL_KEY):
        value = _resolve(config, secrets, key, env_key=key, default="")
        if value:
            resolved[key] = value
    return resolved


def build_model_kwargs(request: ChatRequest) -> dict[str, Any]:
    """Produce the kwargs for ``Model(**kwargs)`` from a ``ChatRequest``.

    A chat request has no task, so ``task_id``/``agent_task_id`` are neutral placeholders
    (0 / "") — the streaming egress is redirected through ``response_emitter`` and the
    checkpointer thread key through ``_thread_id_override``, so neither placeholder is used
    for I/O. ``operation_id`` is threaded explicitly (Section 7).
    """
    config = ChatConfigView.from_request(request)
    secrets = ChatSecretView.from_request(request)
    llm_profile = resolve_llm_profile(request)

    # A local-test env may supply provider/model under those
    # exact lowercase keys plus API_ENDPOINT/API_KEY — resolve under the same names the legacy
    # get_secret path used so the local loopback endpoint keeps working with no ChatRequest.
    provider = llm_profile.provider
    model = llm_profile.model
    system_prompt = _resolve(config, secrets, "system_prompt", env_key="system_prompt", default="")

    mode = _resolve(config, secrets, "mode", env_key="mode", default="conversation").lower()
    if mode not in ("conversation", "supervised", "auto"):
        mode = "conversation"
    # Both legacy controls coexist (ChatArguments shipped `mode` AND `autonomous_solve`): Mode=Autonomous
    # OR the explicit autonomous_solve toggle enables it. The toggle resolves through the full chain.
    autonomous_solve = mode == "auto" or _resolve_bool(
        config, secrets, "autonomous_solve", env_key="autonomous_solve", default=False
    )
    if mode == "conversation" and autonomous_solve:
        mode = "auto"
    policy_mode_requested = _resolve(
        config,
        secrets,
        "policy_mode",
        env_key="SAGE_POLICY_MODE",
        default="",
    )
    policy_mode, policy_mode_resolution = resolve_policy_mode(
        policy_mode_requested,
        default=POLICY_DEFAULT,
    )
    eval_force_capability_prefix_json = _resolve(
        config,
        secrets,
        "SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON",
        env_key="SAGE_EVAL_FORCE_CAPABILITY_PREFIX_JSON",
        default="",
    ) or None

    max_steps_raw = _resolve(config, secrets, "max_steps", env_key="max_steps", default="")
    try:
        max_steps = int(max_steps_raw) if max_steps_raw else 200
    except (TypeError, ValueError):
        max_steps = 200

    configurable: dict[str, Any] = llm_profile.configurable()

    return {
        "provider": provider,
        "model": model,
        "system_prompt": system_prompt,
        "config": {"configurable": configurable},
        "task_id": 0,
        "agent_task_id": "",
        "mode": mode,
        "autonomous_solve": autonomous_solve,
        "policy_mode": policy_mode,
        "policy_mode_requested": policy_mode_requested,
        "policy_mode_resolution": policy_mode_resolution,
        "eval_force_capability_prefix_json": eval_force_capability_prefix_json,
        "max_steps": max_steps,
        "operation_id": request.OperationID,
        # Chat auth context (Section 8A P0): MythicTools mints a channel-scoped bot token from these.
        "channel_id": request.ChannelID,
        "apitoken_id": request.APITokenID,
    }
