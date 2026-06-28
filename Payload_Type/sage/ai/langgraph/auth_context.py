"""Structured Apollo authentication-context observations."""

from dataclasses import dataclass
import ast
import json
import re
from typing import Any


@dataclass(frozen=True)
class KerberosTicket:
    client_name: str = ""
    client_realm: str = ""
    service_name: str = ""
    service_realm: str = ""
    luid: str = ""


@dataclass(frozen=True)
class AuthenticationContext:
    callback_id: str
    host: str
    primary_identity: str
    impersonation_identity: str
    current_luid: str
    current_luid_tickets: tuple[KerberosTicket, ...]
    known_domain_authorities: tuple[str, ...] = ()

    @property
    def active_identity(self) -> str:
        return self.impersonation_identity or self.primary_identity

    @property
    def has_domain_token(self) -> bool:
        return _is_domain_identity(
            self.active_identity,
            self.host,
            self.known_domain_authorities,
        )

    @property
    def has_domain_tgt(self) -> bool:
        return any(
            ticket.client_realm
            and _is_domain_authority(ticket.client_realm, self.host)
            and ticket.service_name.casefold().startswith("krbtgt/")
            for ticket in self.current_luid_tickets
        )

    @property
    def domain_capable(self) -> bool:
        # A non-host-local token can obtain tickets on demand; an injected current-LUID TGT can
        # authenticate even when the visible token remains local. Track and accept either mechanism.
        return self.has_domain_token or self.has_domain_tgt

    @property
    def evidence(self) -> str:
        signals = []
        if self.has_domain_token:
            signals.append(f"domain-token:{self.active_identity}")
        if self.has_domain_tgt:
            realms = sorted({
                ticket.client_realm.casefold()
                for ticket in self.current_luid_tickets
                if ticket.service_name.casefold().startswith("krbtgt/")
            })
            signals.append(f"current-luid-tgt:{','.join(realms)}")
        return ";".join(signals) or "local-token-without-current-luid-domain-tgt"


def build_authentication_context(
    callback_id: Any,
    host: str,
    identity_output: Any,
    ticket_output: Any,
    known_domain_authorities: tuple[str, ...] | set[str] = (),
    identity_parser: str = "apollo",
) -> AuthenticationContext:
    parser = _normalize_parser(identity_parser)
    identity_luid = ""
    if parser in {"merlin", "merlin-token", "merlin_token"}:
        primary, impersonation, identity_luid = parse_merlin_token_identity_output(identity_output)
    else:
        primary, impersonation = parse_apollo_identity_output(identity_output)
    current_luid, tickets = parse_apollo_ticket_cache_output(ticket_output)
    current_luid = current_luid or identity_luid
    current_tickets = tuple(
        ticket
        for ticket in tickets
        if current_luid and _normalize_luid(ticket.luid) == current_luid
    )
    authorities = _observed_domain_authorities(
        known_domain_authorities,
        impersonation or primary,
        current_tickets,
    )
    return AuthenticationContext(
        callback_id=str(callback_id),
        host=str(host or "").strip(),
        primary_identity=primary,
        impersonation_identity=impersonation,
        current_luid=current_luid,
        current_luid_tickets=current_tickets,
        known_domain_authorities=tuple(sorted(authorities)),
    )


def parse_apollo_identity_output(output: Any) -> tuple[str, str]:
    text = _text(output)
    primary = _labeled_value(text, "Local Identity")
    impersonation = _labeled_value(text, "Impersonation Identity")
    if primary or impersonation:
        return primary, impersonation
    identities = re.findall(
        r"(?im)(?:^|[\s\"'])([a-z0-9_.-]+\\[a-z0-9_.$@-]+)(?=$|[\s\"'])",
        text,
    )
    fallback = identities[-1] if identities else ""
    return fallback, fallback


def parse_merlin_token_identity_output(output: Any) -> tuple[str, str, str]:
    """Return process identity, effective thread identity, and effective LUID from `token whoami` output."""
    text = _text(output)
    process = _merlin_token_section(text, "Process")
    thread = _merlin_token_section(text, "Thread")
    primary = _merlin_token_value(process, "User")
    impersonation = _merlin_token_value(thread, "User") or primary
    luid = _normalize_luid(_merlin_token_value(thread, "Logon ID") or _merlin_token_value(process, "Logon ID"))
    if primary or impersonation:
        return primary, impersonation, luid
    fallback_primary, fallback_impersonation = parse_apollo_identity_output(text)
    return fallback_primary, fallback_impersonation, luid


def parse_apollo_ticket_cache_output(output: Any) -> tuple[str, tuple[KerberosTicket, ...]]:
    text = _text(output)
    rows = _json_array(text)
    tickets = []
    current_luid = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_current_luid = _normalize_luid(row.get("current_luid"))
        if row_current_luid:
            current_luid = row_current_luid
        tickets.append(KerberosTicket(
            client_name=_text(row.get("client_name")),
            client_realm=_text(row.get("client_realm")),
            service_name=_text(row.get("service_name")),
            service_realm=_text(row.get("service_realm")),
            luid=_normalize_luid(row.get("luid")),
        ))
    if not current_luid:
        match = re.search(r"(?im)^\s*(0x[0-9a-f]+|\d+)\s*$", text)
        if match:
            current_luid = _normalize_luid(match.group(1))
    return current_luid, tuple(tickets)


def _json_array(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (TypeError, ValueError):
            continue
        if isinstance(value, list):
            return value
    return []


def _labeled_value(text: str, label: str) -> str:
    match = re.search(rf"(?im)^\s*{re.escape(label)}\s*:\s*(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _is_domain_identity(
    identity: str,
    host: str,
    known_domain_authorities: tuple[str, ...] = (),
) -> bool:
    authority, separator, _ = str(identity or "").strip().partition("\\")
    return bool(
        separator
        and _is_domain_authority(authority, host, known_domain_authorities)
    )


def _is_domain_authority(
    authority: str,
    host: str,
    known_domain_authorities: tuple[str, ...] = (),
) -> bool:
    normalized = str(authority or "").strip().casefold()
    host_short = str(host or "").strip().split(".", 1)[0].casefold()
    return bool(
        normalized
        and normalized not in {".", "nt authority", "builtin"}
        and (not host_short or normalized != host_short)
        and (
            "." in normalized
            or normalized in {
                str(item or "").strip().casefold()
                for item in known_domain_authorities
            }
        )
    )


def _observed_domain_authorities(
    existing: tuple[str, ...] | set[str],
    active_identity: str,
    tickets: tuple[KerberosTicket, ...],
) -> set[str]:
    authorities: set[str] = set()
    for item in existing:
        normalized = str(item or "").strip().casefold()
        if not normalized:
            continue
        authorities.add(normalized)
        if "." in normalized:
            authorities.add(normalized.split(".", 1)[0])
    for ticket in tickets:
        for realm in (ticket.client_realm, ticket.service_realm):
            normalized = str(realm or "").strip().casefold()
            if not normalized:
                continue
            authorities.add(normalized)
            authorities.add(normalized.split(".", 1)[0])
    identity_authority, separator, _ = str(active_identity or "").strip().partition("\\")
    normalized_identity_authority = identity_authority.casefold()
    if separator and "." in normalized_identity_authority:
        authorities.add(normalized_identity_authority)
        authorities.add(normalized_identity_authority.split(".", 1)[0])
    return authorities


def _normalize_luid(value: Any) -> str:
    text = _text(value).casefold()
    if not text:
        return ""
    try:
        return f"0x{int(text, 0):x}"
    except ValueError:
        return text


def _normalize_parser(value: Any) -> str:
    return str(value or "").strip().casefold()


def _merlin_token_section(text: str, label: str) -> str:
    match = re.search(
        rf"(?ims)^\s*{re.escape(label)}\s+\([^)]*\)\s+Token:\s*(.*?)(?=^\s*(?:Process|Thread)\s+\([^)]*\)\s+Token:|\Z)",
        text,
    )
    return match.group(1) if match else ""


def _merlin_token_value(section: str, label: str) -> str:
    match = re.search(rf"(?i)\b{re.escape(label)}\s*:\s*([^,\r\n]+)", section)
    return match.group(1).strip() if match else ""


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    text = str(value or "").strip()
    if len(text) >= 3 and text[0] == "b" and text[1] in {"'", '"'}:
        try:
            decoded = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            decoded = None
        if isinstance(decoded, bytes):
            return decoded.decode("utf-8", errors="replace").strip()
    return text
