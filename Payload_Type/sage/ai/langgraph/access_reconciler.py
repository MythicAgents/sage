"""Project Mythic callback access into engagement-state footholds."""

import json
from datetime import datetime, timezone
from typing import Any

try:
    from . import engagement_state
except ImportError:  # allow running directly / under pytest sys.path injection
    import engagement_state

try:
    from .mythic_tools import assess_callback_liveness
except ImportError:
    from mythic_tools import assess_callback_liveness

Foothold = engagement_state.Foothold

DEFAULT_NETBIOS_TO_FQDN: dict[str, str] = {
    "NORTH": "north.sevenkingdoms.local",
    "ESSOS": "essos.local",
    "SEVENKINGDOMS": "sevenkingdoms.local",
}

_SOURCE = "mythic:get_all_active_callbacks"
_INTEGRITY_LEVELS = {
    1: "low",
    2: "medium",
    3: "high",
    4: "system",
}


def project_access(
    raw_callbacks: list[dict],
    now: str,
    liveness: dict[str, bool],
    *,
    netbios_to_fqdn: dict[str, str] | None = None,
) -> list[Foothold]:
    """Return footholds projected from raw Mythic callbacks."""

    footholds: list[Foothold] = []
    safe_liveness = liveness if isinstance(liveness, dict) else {}
    for raw_callback in raw_callbacks:
        callback = raw_callback if isinstance(raw_callback, dict) else {}
        display_id = _text(_callback_display_id(callback))
        identity = _text(callback.get("user"))
        domain = _domain_from_callback(callback)
        footholds.append(
            Foothold(
                callback_id=display_id,
                agent=_agent_from_callback(callback),
                host=_text(callback.get("host")),
                forest=normalize_forest(domain, netbios_to_fqdn=netbios_to_fqdn),
                identity=identity,
                integrity=_normalize_integrity(callback),
                alive=bool(safe_liveness.get(display_id, False)),
                source=_SOURCE,
                timestamp=now,
            )
        )
    return footholds


def normalize_forest(domain: str, *, netbios_to_fqdn: dict[str, str] | None = None) -> str:
    """Return a normalized forest name from a domain hint."""

    raw = _text(domain).strip()
    if not raw:
        return ""

    mappings = dict(DEFAULT_NETBIOS_TO_FQDN)
    if isinstance(netbios_to_fqdn, dict):
        mappings.update({_text(key).upper(): _text(value).casefold() for key, value in netbios_to_fqdn.items()})

    mapped = mappings.get(raw.upper())
    if mapped:
        return mapped.casefold()
    return raw.casefold()


def is_stale(foothold: Foothold, now: str, ttl_seconds: int) -> bool:
    """Return whether a foothold fact is older than its TTL."""

    observed_at = _parse_iso_datetime(_text(getattr(foothold, "timestamp", "")))
    current = _parse_iso_datetime(now)
    if observed_at is None or current is None:
        return True
    return (current - observed_at).total_seconds() > ttl_seconds


async def reconcile_access(mythic_tools: Any, now: str) -> list[Foothold]:
    """Fetch Mythic callbacks and return reconciled footholds."""

    raw_payload = await mythic_tools.get_all_active_callbacks()
    raw_callbacks = _callbacks_from_json(raw_payload)
    liveness: dict[str, bool] = {}
    for callback in raw_callbacks:
        display_id = _text(_callback_display_id(callback))
        liveness[display_id] = False
        parsed_display_id = _parse_int(display_id)
        if parsed_display_id is None:
            continue
        try:
            result = await assess_callback_liveness(mythic_tools.client, parsed_display_id)
            liveness[display_id] = bool(result.get("alive")) if isinstance(result, dict) else False
        except Exception:
            liveness[display_id] = False
    return project_access(raw_callbacks, now, liveness)


def _text(value: Any) -> str:
    """Return a string value without raising."""

    if value is None:
        return ""
    return str(value).strip()


def _callback_display_id(callback: dict) -> Any:
    """Return a callback display id from known Mythic shapes."""

    display_id = callback.get("display_id")
    if display_id is None:
        display_id = callback.get("id")
    return display_id


def _agent_from_callback(callback: dict) -> str:
    """Return the callback agent name from known Mythic fields."""

    agent = callback.get("agent")
    if agent is None:
        agent = callback.get("payloadtype")
    if agent is None:
        payload = callback.get("payload") if isinstance(callback.get("payload"), dict) else {}
        agent = payload.get("payloadtype") or payload.get("payload_type")
    if isinstance(agent, dict):
        return _text(agent.get("name"))
    return _text(agent)


def _domain_from_callback(callback: dict) -> str:
    """Return the best domain hint from callback domain or identity."""

    domain = _text(callback.get("domain"))
    if domain:
        return domain
    return _domain_from_identity(_text(callback.get("user")))


def _domain_from_identity(identity: str) -> str:
    """Return a domain hint parsed from a user identity."""

    raw = _text(identity)
    if "\\" in raw:
        return raw.split("\\", 1)[0]
    if "@" in raw:
        return raw.rsplit("@", 1)[1]
    return ""


def _normalize_integrity(callback: dict) -> str:
    """Return an engagement-state integrity value for a Mythic callback."""

    identity = _text(callback.get("user"))
    if _is_system_identity(identity):
        return "system"

    level = callback.get("integrity_level")
    if level is None:
        level = callback.get("integrity")

    parsed_level = _parse_int(level)
    if parsed_level in _INTEGRITY_LEVELS:
        return _INTEGRITY_LEVELS[parsed_level]

    normalized = _text(level).casefold()
    if normalized in ("system", "nt authority\\system"):
        return "system"
    return normalized


def _is_system_identity(identity: str) -> bool:
    """Return whether an identity is the local SYSTEM user."""

    normalized = _text(identity).casefold()
    return normalized in ("system", "nt authority\\system")


def _parse_int(value: Any) -> int | None:
    """Parse an integer value without raising."""

    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp without raising."""

    raw = _text(value)
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _callbacks_from_json(raw_payload: Any) -> list[dict]:
    """Return callback dictionaries from a Mythic JSON payload."""

    try:
        payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
    except (TypeError, json.JSONDecodeError):
        return []
    return _callbacks_from_payload(payload)


def _callbacks_from_payload(payload: Any) -> list[dict]:
    """Return callback dictionaries from known wrapped payload shapes."""

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("callbacks", "callback"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return _callbacks_from_payload(data)
    return []
