"""Pure classifier from Mythic tool calls to engagement techniques."""

import json
import re
import shlex
from typing import Any


def classify_tool_call(command: str, params, callback_host: str | None = None) -> tuple[str, str] | None:
    """Return ``(technique, target_key)`` for modeled tool calls, or ``None``.

    ``callback_host`` is the host of the callback the task targets. Host-scoped techniques
    whose tool args carry no host (e.g. an LSASS dump runs on *this* callback's host) fall back
    to it, so the gate doesn't false-DEFER on an empty host predicate.
    """

    try:
        parsed = _parse_params(params)
        command_text = _text(command)
        argument_text = parsed["argument_text"]
        combined = f"{command_text} {argument_text}".strip()
        combined_cf = combined.casefold()
        tokens = _tokens(combined)
        token_set = {token.casefold() for token in tokens}

        # Ordering is deliberate: specific setup/effect rules must win before
        # broader token matches such as bare "golden" or generic dump wording.
        if "sharpgpoabuse" in combined_cf:
            return ("gpo-abuse", _flag_value(parsed, "gponame").casefold())

        if "standin" in combined_cf:
            object_value = _flag_value(parsed, "object")
            if _is_domain_dn(object_value) and (_has_flag(parsed, "grant") or _has_flag(parsed, "guid")):
                return ("dcsync-rights-grant", _fqdn_from_dn(object_value).casefold())

            if _has_flag(parsed, "rbcd"):
                return ("rbcd-standin", _flag_value(parsed, "target").casefold())

        if _is_domain_admin_membership_check(command_text, parsed, combined_cf):
            return ("domain-admin-membership-check", "")

        if "lsadump::dcsync" in combined_cf or (_apollo_dcsync(command_text, tokens, token_set)):
            user_raw = _flag_value(parsed, "user")
            domain = (_domain_value(parsed) or (_fqdn_from_dn(user_raw) if _is_domain_dn(user_raw) else "")).casefold()
            user = user_raw.casefold()
            # DCSyncing a SPECIFIC non-krbtgt principal (to recover THAT user's key, e.g. a SMALL COUNCIL
            # member for cross-forest LAPS) is a DISTINCT op from the domain krbtgt DCSync. Without this they
            # both collapse to ("dcsync", domain) → krbtgt-hash:{domain}, so once the krbtgt is dumped the gate
            # wrongly SKIPs every user DCSync (the 2026-06-07 lord.varys block).
            if user and not _is_krbtgt_dcsync_target(user):
                return ("dcsync-user", f"{user}@{domain}")
            return ("dcsync", domain)

        if ("kerberos::golden" in combined_cf or "golden" in token_set) and (
            _has_flag(parsed, "sids") or _has_flag(parsed, "sidhistory")
        ):
            # ExtraSIDs / SID-history golden ticket = intra-forest child→parent climb. `/domain` is the
            # CHILD we forge FROM (we hold its krbtgt); `/sids` carries the parent Enterprise Admins SID.
            # Classified distinctly so the gate models effect da:{parent}, not a duplicate child da:{domain}
            # (which would already be achieved → wrongly SKIPped).
            return ("sid-history-escalation", _domain_value(parsed).casefold())

        if "kerberos::golden" in combined_cf or "golden" in token_set:
            return ("golden-ticket", _domain_value(parsed).casefold())

        if _is_lsass_dump(combined_cf, token_set):
            return ("lsass-dump", (_host_value(parsed) or _text(callback_host)).casefold())

        # SharpHound/AzureHound collection -> the modeled collect-graph action. Target (the access-context
        # key) is empty here; the gate rebinds it from the issuing callback's foothold (like lsass-dump).
        # Only commands that can actually launch the collector should set collect-graph. Staging/registering
        # SharpHound is not collection; marking it in-flight before execution strands the run polling for a
        # ZIP that can never be created.
        if _is_collection_execution_command(command_text) and (
            "sharphound" in combined_cf or "azurehound" in combined_cf
        ):
            return ("collect-graph", "")

        return None
    except Exception:
        return None


def _parse_params(params: Any) -> dict[str, Any]:
    raw_text = ""
    values: dict[str, Any] = {}

    if isinstance(params, dict):
        values = dict(params)
        raw_text = _dict_text(params)
    elif isinstance(params, str):
        raw_text = params
        stripped = params.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    values = dict(parsed)
                    raw_text = _dict_text(parsed)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_text = params
    elif params is None:
        raw_text = ""
    else:
        raw_text = _text(params)

    argument_values = []
    for key in ("arguments", "argument", "args", "params", "parameters", "command", "commandline"):
        value = _dict_get(values, key)
        if value:
            argument_values.append(value)
    if argument_values:
        raw_text = " ".join([raw_text, *argument_values]).strip()

    return {
        "values": values,
        "argument_text": raw_text,
        "tokens": _tokens(raw_text),
    }


def _dict_text(values: dict) -> str:
    parts: list[str] = []
    try:
        for key, value in values.items():
            parts.append(_text(key))
            if isinstance(value, (dict, list, tuple)):
                try:
                    parts.append(json.dumps(value, sort_keys=True))
                except (TypeError, ValueError):
                    parts.append(_text(value))
            else:
                parts.append(_text(value))
    except Exception:
        return ""
    return " ".join(part for part in parts if part)


def _tokens(text: str) -> list[str]:
    raw = _text(text)
    if not raw:
        return []
    try:
        tokens = shlex.split(raw, posix=False)
    except ValueError:
        tokens = raw.split()
    except Exception:
        return []
    expanded: list[str] = []
    for token in tokens:
        expanded.append(token)
        stripped = _strip_quotes(_text(token))
        if stripped != token and stripped:
            try:
                expanded.extend(shlex.split(stripped, posix=False))
            except Exception:
                expanded.extend(stripped.split())
    return expanded


def _flag_value(parsed: dict[str, Any], flag: str) -> str:
    flag_cf = flag.casefold().lstrip("-/")

    for key in (flag_cf, f"--{flag_cf}", f"-{flag_cf}", f"/{flag_cf}"):
        value = _dict_get(parsed.get("values", {}), key)
        if value:
            return value

    tokens = list(parsed.get("tokens", []))
    prefixes = (f"--{flag_cf}:", f"-{flag_cf}:", f"/{flag_cf}:")
    names = {f"--{flag_cf}", f"-{flag_cf}", f"/{flag_cf}"}
    for index, token in enumerate(tokens):
        token_text = _strip_quotes(_text(token))
        token_cf = token_text.casefold()
        for prefix in prefixes:
            if token_cf.startswith(prefix):
                # rstrip trailing JSON-structural chars: a raw mimikatz command inside a {"Commands":[...]}
                # list tokenizes the last flag as e.g. `/user:krbtgt"]` — a value never legitimately ends in
                # these, so stripping them is safe and avoids misreading krbtgt as a distinct user.
                return _strip_quotes(token_text[len(prefix):]).rstrip("\"]},'")
        if token_cf in names and index + 1 < len(tokens):
            return _strip_quotes(_text(tokens[index + 1])).rstrip("\"]},'")
    return ""


def _has_flag(parsed: dict[str, Any], flag: str) -> bool:
    flag_cf = flag.casefold().lstrip("-/")
    values = parsed.get("values", {})
    for key in (flag_cf, f"--{flag_cf}", f"-{flag_cf}", f"/{flag_cf}"):
        if _dict_has(values, key):
            return True
    for token in parsed.get("tokens", []):
        token_cf = _strip_quotes(_text(token)).casefold()
        if token_cf in {f"--{flag_cf}", f"-{flag_cf}", f"/{flag_cf}"}:
            return True
        if token_cf.startswith((f"--{flag_cf}:", f"-{flag_cf}:", f"/{flag_cf}:")):
            return True
    return False


def _domain_value(parsed: dict[str, Any]) -> str:
    return _flag_value(parsed, "domain")


def _is_collection_execution_command(command: str) -> bool:
    command_cf = _text(command).strip().casefold()
    if command_cf in {
        "execute_assembly",
        "execute-assembly",
        "execute_pe",
        "execute-pe",
        "execute_bof",
        "execute-bof",
        "run",
        "shell",
        "cmd",
        "powershell",
        "powerpick",
        "inline_assembly",
        "inline-assembly",
        "load-assembly",
        "invoke-assembly",
    }:
        return True
    return False


def _host_value(parsed: dict[str, Any]) -> str:
    for flag in ("host", "computer", "target", "callback-host", "callback_host"):
        value = _flag_value(parsed, flag)
        if value:
            return value
    return ""


def _is_domain_admin_membership_check(command: str, parsed: dict[str, Any], combined_cf: str) -> bool:
    try:
        command_cf = _text(command).casefold()
        if command_cf not in {"run", "shell", "cmd", "powershell", "powerpick"}:
            return False
        if _has_flag(parsed, "add") or _has_flag(parsed, "delete"):
            return False
        if not _has_flag(parsed, "domain"):
            return False
        if "domain admins" not in combined_cf:
            return False
        return "net group" in combined_cf or "net.exe group" in combined_cf
    except Exception:
        return False


def _is_krbtgt_dcsync_target(value: str) -> bool:
    try:
        text = _strip_quotes(_text(value)).strip().casefold()
        if not text:
            return False
        account = re.split(r"[\\/]", text)[-1].split("@", 1)[0]
        if account == "krbtgt":
            return True
        return re.search(r"(?:^|,)\s*cn\s*=\s*krbtgt\s*(?:,|$)", text, re.IGNORECASE) is not None
    except Exception:
        return False


def _is_domain_dn(value: str) -> bool:
    try:
        return re.search(r"(?:^|,)DC=[^,]+(?:,DC=[^,]+)+", _text(value), re.IGNORECASE) is not None
    except Exception:
        return False


def _fqdn_from_dn(value: str) -> str:
    try:
        text = _text(value)
        # Anchor on the FIRST DC= component, even when the DN is embedded behind a prefix such as
        # "distinguishedname=DC=north,DC=sevenkingdoms,DC=local". That leading DC= is preceded by '='
        # (not '^'/','), so the old (?:^|,) anchor silently DROPPED the first label (north) and the NORTH
        # grant was filed under sevenkingdoms.local (2026-06-09 BUG). Find the first DC= run, then parse
        # only its contiguous ,DC= components so an unrelated later DC= in some other attribute can't leak in.
        match = re.search(r"DC=[^,]+(?:\s*,\s*DC=[^,]+)*", text, re.IGNORECASE)
        if not match:
            return ""
        parts = re.findall(r"DC=([^,]+)", match.group(0), re.IGNORECASE)
        return ".".join(part.strip() for part in parts if part.strip())
    except Exception:
        return ""


def _apollo_dcsync(command: str, tokens: list[str], token_set: set[str]) -> bool:
    command_cf = _text(command).casefold()
    return command_cf == "dcsync" or ("apollo" in token_set and "dcsync" in token_set)


def _is_lsass_dump(combined_cf: str, token_set: set[str]) -> bool:
    if "nanodump" in combined_cf or "sekurlsa::" in combined_cf or "sekurlsa::logonpasswords" in combined_cf:
        return True
    if "lsass" in token_set and "dump" in token_set:
        return True
    if "lsass" in combined_cf and "dump" in combined_cf:
        return True
    if "comsvcs" in combined_cf and "minidump" in combined_cf and "lsass" in combined_cf:
        return True
    return False


def _dict_get(values: Any, key: str) -> str:
    if not isinstance(values, dict):
        return ""
    key_cf = key.casefold().lstrip("-/")
    for existing_key, value in values.items():
        existing_cf = _text(existing_key).casefold().lstrip("-/")
        if existing_cf == key_cf:
            return _strip_quotes(_text(value))
    return ""


def _dict_has(values: Any, key: str) -> bool:
    if not isinstance(values, dict):
        return False
    key_cf = key.casefold().lstrip("-/")
    for existing_key in values.keys():
        if _text(existing_key).casefold().lstrip("-/") == key_cf:
            return True
    return False


def _strip_quotes(value: str) -> str:
    text = _text(value)
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        return str(value).strip()
    except Exception:
        return ""
