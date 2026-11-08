"""Normalized, secret-free observations of Mythic records, and the joins that wait for a later record.

Reconciliation fetches Mythic's own records (tasks, their callbacks, credential-store entries). This module keeps
the parts a verifier needs as a normalized observation: the source record, the task and callback identity (the
callback's agent ID beside its display ID, so a reused display ID cannot join two callback lifetimes), a stable
subject, the observation time, the exact structured values, references to supporting records, and commitments over
them. Values are copied from an allow-list of fields; a secret (credential text, task parameters, response text)
never enters an observation.

A *join* is an observation whose proof is expected from a later record: for example a terminal credential task whose
Mythic credential-store entry is not there yet. The store keeps it beside the ledger, so a later pass resolves it
after the task cursor has moved on and across process restarts. Observations are evidence indexing, not progress:
nothing here admits a proof or writes a ledger row.

Pure: no transport, no model call, no task issue. No evaluation, range or operator package is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Mapping

try:
    from . import credential_artifacts, intent_classifier, task_evidence
except ImportError:  # pragma: no cover - flat import for tests that put ai/langgraph on sys.path
    import credential_artifacts  # type: ignore
    import intent_classifier  # type: ignore
    import task_evidence  # type: ignore


VERIFIER_VERSION = "v1"
NO_JOIN = "none"
JOIN_AWAITING = "awaiting"
JOIN_RESOLVED = "resolved"
JOIN_EXPIRED = "expired"
JOIN_STATES = (NO_JOIN, JOIN_AWAITING, JOIN_RESOLVED, JOIN_EXPIRED)

# A terminal credential task whose credential-store entry has not appeared yet.
CREDENTIAL_ENTRY_JOIN = "credential_entry"
CREDENTIAL_ENTRY_VERIFIER = "effect_observations:credential_entry"
# How long a pass keeps looking for that entry. Expiry stops the polling, not the proof: citing the entry with
# `/state reconcile cred:<id>` still records it.
CREDENTIAL_JOIN_WINDOW_SECONDS = 3600
# Awaiting joins one pass reads, oldest first.
JOIN_LIMIT = 50
MAX_REASON_CHARS = 200

# Task fields an observation may carry. Parameters and output are excluded: they can hold secrets.
_TASK_FIELDS = ("operation_id", "command_name", "status_timestamp_processed")
# Credential-store fields an observation may carry. ``credential_text`` and ``comment`` are excluded.
_CREDENTIAL_FIELDS = ("id", "operation_id", "account", "realm", "type")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _bounded(value: Any) -> str:
    return _text(value)[:MAX_REASON_CHARS]


@dataclass(frozen=True)
class Observation:
    """One normalized observation. ``observation_id`` is stable per (kind, source, subject), so recording the same
    observation again is a no-op in the store; ``values_sha256`` commits to what the verifier will read."""

    kind: str
    source_kind: str
    source_id: str
    subject: str
    observed_at: str
    values: Mapping[str, Any] = field(default_factory=dict)
    task_id: str = ""
    callback_id: str = ""
    agent_callback_id: str = ""
    supporting: tuple[str, ...] = ()
    verifier_id: str = ""
    verifier_version: str = VERIFIER_VERSION
    reason: str = ""
    join_state: str = NO_JOIN

    @property
    def observation_id(self) -> str:
        return hashlib.sha256(
            f"observation|{self.kind}|{self.source_kind}|{self.source_id}|{self.subject}".encode()).hexdigest()

    @property
    def values_sha256(self) -> str:
        return _sha256(dict(self.values))

    def payload(self) -> dict[str, Any]:
        return {
            "values": dict(self.values),
            "supporting": list(self.supporting),
            "verifier_id": self.verifier_id,
            "verifier_version": self.verifier_version,
            "values_sha256": self.values_sha256,
        }

    def record(self) -> dict[str, Any]:
        """Keyword arguments for the store's observation record."""
        return {
            "observation_id": self.observation_id,
            "kind": self.kind,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "subject": self.subject,
            "task_id": self.task_id,
            "callback_id": self.callback_id,
            "agent_callback_id": self.agent_callback_id,
            "observed_at": self.observed_at,
            "payload": self.payload(),
            "join_state": self.join_state,
            "join_reason": _bounded(self.reason),
        }


def normalize_task(task: Mapping[str, Any] | None) -> dict[str, Any]:
    """The task's identity and shared classification from its Mythic record; {} without a display ID."""
    if not isinstance(task, Mapping):
        return {}
    record = task_evidence.task_record_from_row(task)
    if record is None:
        return {}
    callback = task.get("callback") if isinstance(task.get("callback"), Mapping) else {}
    values: dict[str, Any] = {key: task.get(key) for key in _TASK_FIELDS if task.get(key) not in (None, "")}
    values.update({
        "task_id": record.task_id,
        "callback_id": record.callback_id,
        "agent_callback_id": _text(callback.get("agent_callback_id")),
        "task_classification": task_evidence.classify_task(record),
    })
    return values


def normalize_credential(credential: Mapping[str, Any] | None) -> dict[str, Any]:
    """A credential-store entry's identity, account, realm, type and producing task; never its secret."""
    if not isinstance(credential, Mapping) or credential.get("id") in (None, ""):
        return {}
    values = {key: credential.get(key) for key in _CREDENTIAL_FIELDS if credential.get(key) not in (None, "")}
    for key in ("account", "realm"):
        if key in values:
            values[key] = _text(values[key]).casefold()
    task = credential.get("task") if isinstance(credential.get("task"), Mapping) else {}
    if task.get("display_id") is not None:
        values["task_id"] = _text(task.get("display_id"))
        values["task_operation_id"] = task.get("operation_id")
    return values


def credential_join(task: Mapping[str, Any] | None, technique: str, target: str, observed_at: str) -> Observation | None:
    """An awaiting join for a terminal credential task with no credential-store entry yet, else None.

    Only a task the shared classifier calls terminal success qualifies: a failed task's entry will not come, and a
    task Mythic is still finishing is held by the cursor instead. ``technique``/``target`` are the task's modeled
    intent, which names the subject the entry is expected to prove.
    """
    if _text(technique) not in credential_artifacts.CREDENTIAL_TECHNIQUES:
        return None
    values = normalize_task(task)
    if not values or values.get("task_classification") != task_evidence.TERMINAL_SUCCESS:
        return None
    subject = f"{_text(technique).casefold()}:{_text(target).casefold()}"
    values.update({"technique": _text(technique).casefold(), "target": _text(target).casefold()})
    task_id = str(values["task_id"])
    return Observation(
        kind=CREDENTIAL_ENTRY_JOIN, source_kind="task", source_id=task_id, subject=subject,
        observed_at=_text(observed_at), values=values, task_id=task_id, callback_id=str(values["callback_id"]),
        agent_callback_id=str(values["agent_callback_id"]), supporting=(f"task:{task_id}",),
        verifier_id=CREDENTIAL_ENTRY_VERIFIER, reason="awaiting the task's Mythic credential-store entry",
        join_state=JOIN_AWAITING,
    )


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def join_expired(observed_at: Any, now: Any, window_seconds: int | None = None) -> bool:
    """Whether an awaiting join observed at ``observed_at`` is past its window at ``now``. An unreadable time never
    expires a join by itself (it stays bounded by the per-pass join limit)."""
    start, end = _parse_time(observed_at), _parse_time(now)
    if start is None or end is None:
        return False
    window = CREDENTIAL_JOIN_WINDOW_SECONDS if window_seconds is None else window_seconds
    return (end - start).total_seconds() > window


# --- Directory ACL reads ---------------------------------------------------------------------------------------
#
# A directory change (a granted right) is proven by a later read of the exact object, never by the write's own
# success message. Two read formats are supported: StandIn ``--object <filter> --access`` text, which names the
# object by its full distinguished name on its ``Path : LDAP://`` line and each trustee by ``NETBIOS\name``; and a
# structured JSON read carrying the object's DN/SID/GUID and raw ACE records. A JSON Boolean claiming the effect is
# not a read. Anything else is "not proven" with a reason, never a substring match.

ACL_VERIFIER = "effect_observations:acl"

# Extended-right GUIDs by schema display name (StandIn prints the name; a JSON read carries the GUID).
DS_REPLICATION_GET_CHANGES = "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2"
DS_REPLICATION_GET_CHANGES_ALL = "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2"
DS_REPLICATION_GET_CHANGES_FILTERED = "89e95b76-444d-4c62-991a-0facbeda640c"
EXTENDED_RIGHTS = {
    "ds-replication-get-changes": DS_REPLICATION_GET_CHANGES,
    "ds-replication-get-changes-all": DS_REPLICATION_GET_CHANGES_ALL,
    "ds-replication-get-changes-in-filtered-set": DS_REPLICATION_GET_CHANGES_FILTERED,
}
# What ``ds-replication-rights:{domain}`` needs: both rights, as the existing verifier already required.
REPLICATION_RIGHTS = (DS_REPLICATION_GET_CHANGES, DS_REPLICATION_GET_CHANGES_ALL)

# Trustees every principal belongs to: their deny ACEs defeat any principal's right.
_BROAD_TRUSTEES = frozenset({
    "s-1-1-0", "s-1-5-11", "everyone", "authenticated users", "nt authority\\authenticated users",
})
_ADS_CONTROL_ACCESS = 0x100
_GENERIC_ALL = 0x10000000
_FULL_CONTROL = 0xF01FF
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SID_RE = re.compile(r"^s-1-\d+(?:-\d+)+$")
_STANDIN_PATH_RE = re.compile(r"(?im)^\s*Path\s*:\s*LDAP://(?:[^/\r\n]+/)?(?P<dn>[^\r\n]+?)\s*$")
_STANDIN_IDENTITY_RE = re.compile(r"(?im)^\s*\[\+\]\s*Identity\s*-->\s*(?P<trustee>[^\r\n]+?)\s*$")
_STANDIN_FIELD_RE = re.compile(r"(?im)^\s*\|_\s*(?P<key>Type|Permission|Object)\s*:\s*(?P<value>[^\r\n]*?)\s*$")


def normalize_dn(value: Any) -> str:
    """A distinguished name compared case-insensitively, without whitespace around separators."""
    text = _text(value)
    if text.casefold().startswith("distinguishedname="):
        text = text[len("distinguishedname="):]
    parts = []
    for rdn in text.split(","):
        key, sep, val = rdn.partition("=")
        if not sep:
            return ""
        parts.append(f"{key.strip().casefold()}={val.strip().casefold()}")
    return ",".join(parts)


def _right_guid(value: Any) -> str:
    """'' for an ACE that applies to every extended right/property, the GUID for a named one, else None-like '?'."""
    text = _text(value).casefold().strip("{}")
    if text in ("", "all", "any", "00000000-0000-0000-0000-000000000000"):
        return ""
    if _GUID_RE.match(text):
        return text
    return EXTENDED_RIGHTS.get(text, "?" + text)


@dataclass(frozen=True)
class Ace:
    trustee: str = ""
    trustee_sid: str = ""
    allow: bool = True
    control_access: bool = False
    full_control: bool = False
    object_type: str = ""
    inherit_only: bool = False
    class_scoped: bool = False
    # A format that prints no inheritance flags (StandIn) cannot show that a broad ACE is inherit-only, so only an ACE
    # naming the exact right is credited from it.
    exact_right_only: bool = False

    def grants(self, right: str) -> bool:
        """Allow side, strict: an ACE that might apply only to descendants or to another class proves nothing."""
        if self.inherit_only or self.class_scoped:
            return False
        if self.exact_right_only:
            return self.control_access and self.object_type == right
        return (self.full_control or self.control_access) and self.object_type in ("", right)

    def may_deny(self, right: str) -> bool:
        """Deny side, conservative: a deny applies to the object unless it is explicitly inherit-only."""
        if self.inherit_only:
            return False
        return (self.full_control or self.control_access) and self.object_type in ("", right)


@dataclass(frozen=True)
class AclRead:
    """One directory read of one object: its DACL and, from a JSON read, its RBCD descriptor, members and gPLink."""

    source_format: str = ""
    object_dn: str = ""
    object_sid: str = ""
    object_guid: str = ""
    aces: tuple[Ace, ...] = ()
    complete: bool = False
    reason: str = ""
    has_dacl: bool = False
    rbcd: tuple[Ace, ...] = ()
    rbcd_complete: bool = False
    members: tuple[str, ...] = ()
    members_complete: bool = False
    gplink: str | None = None


@dataclass(frozen=True)
class AclVerdict:
    achieved: bool
    reason: str
    granted: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    values: Mapping[str, Any] = field(default_factory=dict)


def _permission_flags(value: Any) -> tuple[bool, bool]:
    """(control access, full control) for StandIn's comma-separated ``ActiveDirectoryRights`` or a JSON mask/list."""
    if isinstance(value, bool):
        return False, False
    if isinstance(value, int):
        return bool(value & _ADS_CONTROL_ACCESS), bool(value & _GENERIC_ALL) or (value & _FULL_CONTROL) == _FULL_CONTROL
    names = value if isinstance(value, (list, tuple)) else _text(value).split(",")
    rights = {_text(name).strip().casefold() for name in names}
    return "extendedright" in rights, "genericall" in rights


def _json_document(text: str) -> Any:
    stripped = text.strip()
    for candidate in (stripped, stripped[stripped.find("{"): stripped.rfind("}") + 1] if "{" in stripped else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _parse_json_acl(document: Mapping[str, Any]) -> AclRead:
    body = document.get("directory_read") if isinstance(document.get("directory_read"), Mapping) else document
    obj = body.get("object") if isinstance(body.get("object"), Mapping) else {}
    dacl = body.get("dacl") if isinstance(body.get("dacl"), Mapping) else None
    rbcd = body.get("rbcd") if isinstance(body.get("rbcd"), Mapping) else None
    member = body.get("member") if isinstance(body.get("member"), Mapping) else None
    gplink = body.get("gplink") if isinstance(body.get("gplink"), str) else None
    has_dacl = dacl is not None and isinstance(dacl.get("aces"), list)
    has_rbcd = rbcd is not None and isinstance(rbcd.get("aces"), list)
    has_member = member is not None and isinstance(member.get("sids"), list)
    if not (has_dacl or has_rbcd or has_member or gplink is not None):
        return AclRead("json", reason="the JSON read carries no DACL ACE records or other directory property")
    parsed: dict[str, tuple[Ace, ...]] = {}
    for label, container in (("dacl", dacl if has_dacl else None), ("rbcd", rbcd if has_rbcd else None)):
        if container is None:
            continue
        aces, reason = _json_aces(container["aces"])
        if reason:
            return AclRead("json", reason=reason)
        parsed[label] = aces
    return AclRead(
        "json", object_dn=normalize_dn(obj.get("dn")), object_sid=_text(obj.get("sid")).casefold(),
        object_guid=_text(obj.get("guid")).casefold().strip("{}"), aces=parsed.get("dacl", ()),
        complete=has_dacl and dacl.get("complete") is True, has_dacl=has_dacl,
        rbcd=parsed.get("rbcd", ()), rbcd_complete=has_rbcd and rbcd.get("complete") is True,
        members=tuple(_text(sid).casefold() for sid in member["sids"]) if has_member else (),
        members_complete=has_member and member.get("complete") is True, gplink=gplink,
    )


def _json_aces(records: list) -> tuple[tuple[Ace, ...], str]:
    aces = []
    for raw in records:
        if not isinstance(raw, Mapping):
            return (), "an ACE record is not structured"
        kind = _text(raw.get("type")).casefold()
        if kind not in ("allow", "deny"):
            return (), "an ACE record has no allow/deny type"
        if not isinstance(raw.get("inherit_only", False), bool):
            return (), "an ACE record's inherit_only is not a Boolean"
        control, full = _permission_flags(raw.get("rights"))
        aces.append(Ace(
            trustee=_text(raw.get("trustee")).casefold(),
            trustee_sid=_text(raw.get("trustee_sid")).casefold(),
            allow=kind == "allow", control_access=control, full_control=full,
            object_type=_right_guid(raw.get("object_type")),
            inherit_only=raw.get("inherit_only") is True,
            class_scoped=bool(_text(raw.get("inherited_object_type"))),
        ))
    return tuple(aces), ""


def _parse_standin_acl(text: str) -> AclRead:
    if re.search(r"(?im)^\s*\[\+\]\s*Set object access rules", text):
        return AclRead("standin", reason="a StandIn write, not a read of the object")
    failure = re.search(r"(?im)^\s*\[!\]\s*(?P<message>[^\r\n]*)", text)
    if failure:
        return AclRead("standin", reason=f"the read reported a failure: {_bounded(failure.group('message'))}")
    paths = list(_STANDIN_PATH_RE.finditer(text))
    if not paths:
        return AclRead("standin", reason="the read does not name its object (no Path : LDAP:// line)")
    if len(paths) > 1:
        return AclRead("standin", reason="the read names more than one object")
    path = paths[0]
    identities = list(_STANDIN_IDENTITY_RE.finditer(text))
    aces = []
    for index, match in enumerate(identities):
        end = identities[index + 1].start() if index + 1 < len(identities) else len(text)
        block = {m.group("key").casefold(): m.group("value") for m in _STANDIN_FIELD_RE.finditer(text, match.end(), end)}
        kind = _text(block.get("type")).casefold()
        if kind not in ("allow", "deny"):
            return AclRead("standin", reason="an access rule has no Allow/Deny type")
        control, full = _permission_flags(block.get("permission"))
        trustee = _text(match.group("trustee")).casefold()
        aces.append(Ace(
            trustee="" if _SID_RE.match(trustee) else trustee, trustee_sid=trustee if _SID_RE.match(trustee) else "",
            allow=kind == "allow", control_access=control, full_control=full,
            object_type=_right_guid(block.get("object")), exact_right_only=True,
        ))
    # StandIn lists every access rule of the object, or every rule of the --ntaccount trustee: complete for the
    # trustee it names. A deny reaching the trustee only through a group it belongs to is not resolvable here.
    return AclRead("standin", object_dn=normalize_dn(path.group("dn")), aces=tuple(aces), complete=True, has_dacl=True)


def parse_acl_read(output: Any) -> AclRead:
    """The object identity and ACE records of a directory ACL read, or an ``AclRead`` whose ``reason`` says why the
    output is not a usable read."""
    text = _text(output)
    if not text:
        return AclRead(reason="no read output")
    document = _json_document(text) if text.lstrip().startswith("{") or "\"dacl\"" in text else None
    if isinstance(document, Mapping):
        return _parse_json_acl(document)
    return _parse_standin_acl(text)


def verify_acl_rights(
    read: AclRead, *, object_dn: str, principal: str = "", principal_sid: str = "", rights: tuple[str, ...],
    object_sid: str = "", object_guid: str = "",
) -> AclVerdict:
    """Whether ``read`` proves the exact principal holds every one of ``rights`` on the exact object.

    The object binds by its full DN (whose DC components fix the domain) and, when both sides carry them, by SID and
    GUID. The principal binds by SID, or by the exact ``NETBIOS\\name`` string the caller already bound to the domain
    (the principal the same transaction granted). A right holds when an applicable allow ACE for that principal grants
    it and no applicable deny ACE for the principal or a group every principal belongs to takes it away. A partial
    DACL, a different trustee or object, or a read failure is not proof.
    """
    values: dict[str, Any] = {
        "source_format": read.source_format, "object_dn": read.object_dn, "complete": read.complete,
        "expected_object_dn": normalize_dn(object_dn), "trustee": _text(principal).casefold(),
        "trustee_sid": _text(principal_sid).casefold(), "rights": list(rights),
    }
    if read.reason:
        return AclVerdict(False, read.reason, values=values)
    expected_dn = normalize_dn(object_dn)
    if not expected_dn or read.object_dn != expected_dn:
        return AclVerdict(False, "the read is of a different object", values=values)
    for expected, observed, label in ((object_sid, read.object_sid, "SID"), (object_guid, read.object_guid, "GUID")):
        if _text(expected) and observed and _text(expected).casefold().strip("{}") != observed:
            return AclVerdict(False, f"the read's object {label} differs", values=values)
    if not read.has_dacl:
        return AclVerdict(False, "the read carries no DACL ACE records", values=values)
    if not read.complete:
        return AclVerdict(False, "the read does not declare a complete DACL", values=values)
    want_name, want_sid = _text(principal).casefold(), _text(principal_sid).casefold()
    if not want_name and not want_sid:
        return AclVerdict(False, "no bound principal to verify", values=values)

    def is_principal(ace: Ace) -> bool:
        return bool((want_sid and ace.trustee_sid == want_sid) or (want_name and ace.trustee == want_name))

    def is_broad(ace: Ace) -> bool:
        return ace.trustee in _BROAD_TRUSTEES or ace.trustee_sid in _BROAD_TRUSTEES

    granted = tuple(r for r in rights if any(a.allow and is_principal(a) and a.grants(r) for a in read.aces))
    denied = tuple(r for r in rights if any(
        not a.allow and (is_principal(a) or is_broad(a)) and a.may_deny(r) for a in read.aces))
    values.update({"granted": list(granted), "denied": list(denied)})
    if denied:
        return AclVerdict(False, "a deny ACE takes the right away", granted, denied, values)
    missing = [r for r in rights if r not in granted]
    if missing:
        return AclVerdict(False, "no allow ACE for the principal grants " + ", ".join(missing), granted, denied, values)
    return AclVerdict(True, "", granted, denied, values)


# --- Enrolled certificates ---------------------------------------------------------------------------------------
#
# An enrollment task's output may carry the CA-issued certificate itself (a PFX). It proves certificate material
# only when code opens it with the task's own certificate password (held in memory, never stored) and binds the
# public fingerprint, the requested UPN in the subject alternative name, the issuing CA and the template, and the
# private key matches the certificate's public key. The result is material, not a working credential. Only public
# values leave this function.

CERTIFICATE_VERIFIER = "effect_observations:certificate"
_UPN_OID = "1.3.6.1.4.1.311.20.2.3"
_TEMPLATE_NAME_OID = "1.3.6.1.4.1.311.20.2"      # v1 template: the template's name
_TEMPLATE_INFO_OID = "1.3.6.1.4.1.311.21.7"      # v2 template: the template's OID and version


@dataclass(frozen=True)
class CertificateBinding:
    bound: bool
    reason: str
    values: Mapping[str, Any] = field(default_factory=dict)


def _der_value(data: bytes, expected_tags: tuple[int, ...]) -> tuple[int, bytes] | None:
    """(tag, content) of the single DER TLV at the start of ``data`` when its tag is expected, else None."""
    if len(data) < 2 or data[0] not in expected_tags:
        return None
    length, offset = data[1], 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or count > 4 or len(data) < 2 + count:
            return None
        length, offset = int.from_bytes(data[2:2 + count], "big"), 2 + count
    if len(data) < offset + length:
        return None
    return data[0], data[offset:offset + length]


def _der_string(data: bytes) -> str:
    if data[:1] == b"\xa0":  # an [0] EXPLICIT wrapper around the string
        inner = _der_value(data, (0xA0,))
        data = inner[1] if inner else b""
    parsed = _der_value(data, (0x0C, 0x13, 0x16, 0x1E))
    if parsed is None:
        return ""
    tag, content = parsed
    try:
        return content.decode("utf-16-be") if tag == 0x1E else content.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _der_oid(content: bytes) -> str:
    if not content:
        return ""
    arcs = [content[0] // 40, content[0] % 40]
    value = 0
    for byte in content[1:]:
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(arc) for arc in arcs)


def _template_identity(certificate: Any) -> tuple[str, str]:
    """(template name, template OID) from the certificate's template extensions; '' where absent."""
    name, oid = "", ""
    for extension in certificate.extensions:
        dotted = extension.oid.dotted_string
        raw = getattr(extension.value, "value", b"")
        if dotted == _TEMPLATE_NAME_OID and isinstance(raw, bytes):
            name = _der_string(raw)
        elif dotted == _TEMPLATE_INFO_OID and getattr(extension.value, "template_id", None) is not None:
            oid = extension.value.template_id.dotted_string  # parsed by cryptography (MSCertificateTemplate)
        elif dotted == _TEMPLATE_INFO_OID and isinstance(raw, bytes):
            sequence = _der_value(raw, (0x30,))
            identifier = _der_value(sequence[1], (0x06,)) if sequence else None
            oid = _der_oid(identifier[1]) if identifier else ""
    return name, oid


def verify_enrolled_certificate(
    pfx: bytes, password: str, *, account: str, domain: str, ca_name: str, template: str, observed_at: Any = "",
) -> CertificateBinding:
    """Whether ``pfx`` is CA-issued certificate material for ``account@domain`` from ``ca_name`` under ``template``.

    ``ca_name`` may be the enrollment config ``host\\CA-Name``; the issuer's common name must equal the CA name. A v1
    template binds by name; a v2 certificate carries only its template's OID, which is recorded as the template
    binding (mapping it to the requested name needs a directory read). A PFX that does not open with the task's
    password, carries no key, has a key that does not match, names another UPN or CA, or is outside its validity at
    ``observed_at`` is not proof.
    """
    values: dict[str, Any] = {"pfx_sha256": hashlib.sha256(pfx or b"").hexdigest() if pfx else ""}
    if not pfx:
        return CertificateBinding(False, "no certificate artifact", values)
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import ExtensionOID, NameOID
    except ImportError:  # pragma: no cover - the dependency is declared
        return CertificateBinding(False, "the certificate parser is unavailable", values)
    try:
        key, certificate, _chain = pkcs12.load_key_and_certificates(pfx, _text(password).encode() or None)
    except Exception:  # noqa: BLE001 - any parse failure is "not proven", with no detail that could echo input
        return CertificateBinding(False, "the PFX does not open with the task's certificate password", values)
    if key is None:
        return CertificateBinding(False, "the PFX carries no private key", values)
    if certificate is None:
        return CertificateBinding(False, "the PFX carries no certificate", values)
    try:
        return _bind_certificate(key, certificate, values, x509, hashes, serialization, ExtensionOID, NameOID,
                                 account=account, domain=domain, ca_name=ca_name, template=template,
                                 observed_at=observed_at)
    except Exception:  # noqa: BLE001 - a malformed extension is "not proven", with no detail that could echo input
        return CertificateBinding(False, "the certificate's extensions could not be parsed", values)


def _bind_certificate(key, certificate, values, x509, hashes, serialization, ExtensionOID, NameOID, *,
                      account, domain, ca_name, template, observed_at) -> CertificateBinding:
    spki = serialization.PublicFormat.SubjectPublicKeyInfo
    public = certificate.public_key().public_bytes(serialization.Encoding.DER, spki)
    upns: list[str] = []
    try:
        san = certificate.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
        for other in san.get_values_for_type(x509.OtherName):
            if other.type_id.dotted_string == _UPN_OID:
                upns.append(_der_string(other.value).casefold())
    except x509.ExtensionNotFound:
        pass
    issuer_cn = [attr.value for attr in certificate.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)]
    template_name, template_oid = _template_identity(certificate)
    values.update({
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(),
        "subject": certificate.subject.rfc4514_string()[:MAX_REASON_CHARS],
        "upns": upns,
        "issuer_cn": _text(issuer_cn[0]) if issuer_cn else "",
        "template_name": template_name,
        "template_oid": template_oid,
        "requested_template": _text(template),
        "not_before": certificate.not_valid_before_utc.isoformat(),
        "not_after": certificate.not_valid_after_utc.isoformat(),
        "key_matches": key.public_key().public_bytes(serialization.Encoding.DER, spki) == public,
    })
    if not values["key_matches"]:
        return CertificateBinding(False, "the PFX private key does not match the certificate", values)
    expected_upn = f"{_text(account).casefold()}@{_text(domain).casefold()}"
    if not _text(account) or not _text(domain) or expected_upn not in upns:
        return CertificateBinding(False, "the certificate names a different UPN", values)
    expected_ca = _text(ca_name).rsplit("\\", 1)[-1].casefold()
    if not expected_ca or values["issuer_cn"].casefold() != expected_ca:
        return CertificateBinding(False, "the certificate was issued by a different CA", values)
    if template_name:
        if template_name.casefold() != _text(template).casefold():
            return CertificateBinding(False, "the certificate was issued under a different template", values)
    elif not template_oid:
        return CertificateBinding(False, "the certificate carries no template identity", values)
    moment = _parse_time(observed_at)
    if moment is not None and not (certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc):
        return CertificateBinding(False, "the certificate is outside its validity period", values)
    return CertificateBinding(True, "", values)


# --- RBCD, group membership and GPO links (structured JSON reads) ------------------------------------------------

_GPLINK_RE = re.compile(r"\[LDAP://cn=\{?(?P<guid>[0-9a-f-]{36})\}?,[^;\]]*;(?P<options>\d+)\]", re.IGNORECASE)


def _first_rdn_value(dn: str) -> str:
    first = normalize_dn(dn).split(",", 1)[0]
    return first.partition("=")[2]


def verify_rbcd(read: AclRead, *, computer: str, trustee_sid: str) -> AclVerdict:
    """Whether ``read`` shows ``trustee_sid`` allowed in ``computer``'s msDS-AllowedToActOnBehalfOfOtherIdentity.

    The computer binds by its object's first RDN (``CN=<host>``); the trustee only by SID. Computer creation alone,
    another computer or trustee, a deny, or an incomplete descriptor is not proof. This is configuration, not a usable
    service ticket."""
    host = _text(computer).casefold().rstrip("$").split(".", 1)[0]
    sid = _text(trustee_sid).casefold()
    values = {"object_dn": read.object_dn, "computer": host, "trustee_sid": sid, "source_format": read.source_format}
    if read.reason:
        return AclVerdict(False, read.reason, values=values)
    if not host or _first_rdn_value(read.object_dn) != host:
        return AclVerdict(False, "the read is of a different object", values=values)
    if not read.rbcd_complete:
        return AclVerdict(False, "the read does not declare a complete RBCD descriptor", values=values)
    if not sid:
        return AclVerdict(False, "no trustee SID to verify", values=values)
    if any(not a.allow and a.trustee_sid == sid and not a.inherit_only for a in read.rbcd):
        return AclVerdict(False, "a deny ACE names the trustee", values=values)
    if any(a.allow and a.trustee_sid == sid and (a.full_control or a.control_access) for a in read.rbcd):
        return AclVerdict(True, "", ("rbcd",), (), values)
    return AclVerdict(False, "the RBCD descriptor does not allow the trustee", values=values)


def verify_group_member(read: AclRead, *, group_dn: str, principal_sid: str, group_sid: str = "") -> AclVerdict:
    """Whether ``read`` shows ``principal_sid`` as a direct member of the exact group (complete member list)."""
    sid = _text(principal_sid).casefold()
    values = {"object_dn": read.object_dn, "principal_sid": sid, "source_format": read.source_format}
    if read.reason:
        return AclVerdict(False, read.reason, values=values)
    if not normalize_dn(group_dn) or read.object_dn != normalize_dn(group_dn):
        return AclVerdict(False, "the read is of a different object", values=values)
    if _text(group_sid) and read.object_sid and _text(group_sid).casefold() != read.object_sid:
        return AclVerdict(False, "the read's object SID differs", values=values)
    if not read.members_complete:
        return AclVerdict(False, "the read does not declare a complete member list", values=values)
    if not sid or sid not in read.members:
        return AclVerdict(False, "the principal is not a direct member", values=values)
    return AclVerdict(True, "", ("member",), (), values)


def verify_gpo_link(read: AclRead, *, container_dn: str, gpo_guid: str) -> AclVerdict:
    """Whether ``read`` shows the exact GPO linked and enabled on the exact container. A disabled link (option bit 1),
    another container or GPO, or a read without the gPLink value is not proof; a link is not SYSTEM execution."""
    guid = _text(gpo_guid).casefold().strip("{}")
    values = {"object_dn": read.object_dn, "gpo_guid": guid, "source_format": read.source_format}
    if read.reason:
        return AclVerdict(False, read.reason, values=values)
    if not normalize_dn(container_dn) or read.object_dn != normalize_dn(container_dn):
        return AclVerdict(False, "the read is of a different object", values=values)
    if read.gplink is None:
        return AclVerdict(False, "the read carries no gPLink value", values=values)
    links = {m.group("guid").casefold(): int(m.group("options")) for m in _GPLINK_RE.finditer(read.gplink)}
    if guid not in links:
        return AclVerdict(False, "the GPO is not linked to the container", values=values)
    if links[guid] & 1:
        return AclVerdict(False, "the GPO link is disabled", values=values)
    return AclVerdict(True, "", ("gplink",), (), values)


# --- Directory changes: a write is a candidate; a later read of the exact object proves it ---------------------
#
# A successful mutation task records an awaiting ``directory_change`` join naming the object, the principal and the
# property it changed (from its own parameters or returned identity, never from proximity). Each terminal read task
# indexes a ``directory_read`` observation of the object it read. A later pass resolves the join with the first read
# that comes after the mutation and verifies. The proving task is the reading task.

DIRECTORY_CHANGE_JOIN = "directory_change"
DIRECTORY_READ = "directory_read"
DIRECTORY_JOIN_WINDOW_SECONDS = 86400
PROPERTY_REPLICATION = "replication-rights"
PROPERTY_RBCD = "rbcd"
PROPERTY_MEMBER = "member"
PROPERTY_GPLINK = "gplink"
DIRECTORY_PROPERTIES = (PROPERTY_REPLICATION, PROPERTY_RBCD, PROPERTY_MEMBER, PROPERTY_GPLINK)


def _ace_record(ace: Ace) -> dict[str, Any]:
    return {key: getattr(ace, key) for key in Ace.__dataclass_fields__}


def read_values(read: AclRead) -> dict[str, Any]:
    """The read's normalized, secret-free values for its observation."""
    return {
        "source_format": read.source_format, "object_dn": read.object_dn, "object_sid": read.object_sid,
        "object_guid": read.object_guid, "complete": read.complete, "has_dacl": read.has_dacl,
        "aces": [_ace_record(ace) for ace in read.aces], "rbcd": [_ace_record(ace) for ace in read.rbcd],
        "rbcd_complete": read.rbcd_complete, "members": list(read.members), "members_complete": read.members_complete,
        "gplink": read.gplink,
    }


def read_from_values(values: Mapping[str, Any]) -> AclRead:
    def aces(key: str) -> tuple[Ace, ...]:
        out = []
        for raw in values.get(key) or []:
            if isinstance(raw, Mapping):
                out.append(Ace(**{k: raw[k] for k in Ace.__dataclass_fields__ if k in raw}))
        return tuple(out)

    return AclRead(
        source_format=_text(values.get("source_format")), object_dn=_text(values.get("object_dn")),
        object_sid=_text(values.get("object_sid")), object_guid=_text(values.get("object_guid")), aces=aces("aces"),
        complete=values.get("complete") is True, has_dacl=values.get("has_dacl") is True, rbcd=aces("rbcd"),
        rbcd_complete=values.get("rbcd_complete") is True, members=tuple(values.get("members") or ()),
        members_complete=values.get("members_complete") is True,
        gplink=values.get("gplink") if isinstance(values.get("gplink"), str) else None,
    )


def directory_read_observation(task: Mapping[str, Any] | None, output: Any, terminal_status: str,
                               observed_at: str) -> Observation | None:
    """A ``directory_read`` observation for a terminal-success task whose output is a supported read, else None."""
    values = normalize_task(task)
    if not values or values.get("task_classification") != task_evidence.TERMINAL_SUCCESS:
        return None
    read = parse_acl_read(output)
    if read.reason or not read.object_dn:
        return None
    values.update(read_values(read))
    values["terminal_status"] = _text(terminal_status)
    task_id = str(values["task_id"])
    return Observation(
        kind=DIRECTORY_READ, source_kind="task", source_id=task_id, subject=read.object_dn, observed_at=_text(observed_at),
        values=values, task_id=task_id, callback_id=str(values["callback_id"]),
        agent_callback_id=str(values["agent_callback_id"]), supporting=(f"task:{task_id}",), verifier_id=ACL_VERIFIER,
    )


def directory_change_join(task: Mapping[str, Any] | None, *, prop: str, technique: str, target: str, obj: str,
                          principal: str = "", principal_sid: str = "", observed_at: str = "") -> Observation | None:
    """An awaiting join for a terminal-success mutation task naming ``obj`` and its principal, else None."""
    values = normalize_task(task)
    if prop not in DIRECTORY_PROPERTIES or not values:
        return None
    if values.get("task_classification") != task_evidence.TERMINAL_SUCCESS:
        return None
    obj_key = normalize_dn(obj) if "=" in _text(obj) else _text(obj).casefold()
    if not obj_key or not (_text(principal) or _text(principal_sid)):
        return None
    values.update({
        "property": prop, "technique": _text(technique).casefold(), "target": _text(target).casefold(),
        "object": obj_key, "principal": _text(principal).casefold(), "principal_sid": _text(principal_sid).casefold(),
    })
    task_id = str(values["task_id"])
    subject = f"{prop}:{obj_key}|{values['principal'] or values['principal_sid']}"
    return Observation(
        kind=DIRECTORY_CHANGE_JOIN, source_kind="task", source_id=task_id, subject=subject,
        observed_at=_text(observed_at), values=values, task_id=task_id, callback_id=str(values["callback_id"]),
        agent_callback_id=str(values["agent_callback_id"]), supporting=(f"task:{task_id}",), verifier_id=ACL_VERIFIER,
        reason="awaiting a later read of the changed object", join_state=JOIN_AWAITING,
    )


def bound_principal(principal: str, netbios_map: Mapping[str, str] | None) -> str:
    """``principal`` when it is a SID or a ``NETBIOS\\name`` whose prefix the engagement NetBIOS map names; else ''."""
    text = _text(principal).casefold()
    if _SID_RE.match(text):
        return text
    prefix, sep, name = text.partition("\\")
    mapped = {_text(key).casefold() for key in (netbios_map or {})}
    return text if sep and name and prefix in mapped else ""


def _task_number(value: Any) -> int:
    try:
        return int(_text(value))
    except ValueError:
        return -1


def resolve_directory_change(join: Mapping[str, Any], reads: list[Mapping[str, Any]],
                             netbios_map: Mapping[str, str] | None) -> tuple[Mapping[str, Any] | None, str]:
    """(the first later read that proves the join's change, '') or (None, why not yet).

    ``join``/``reads`` are stored observation rows (``payload.values``). Only a read of a later task counts. A
    replication-rights join needs its principal bound by SID or by a mapped NetBIOS prefix."""
    values = (join.get("payload") or {}).get("values") or {}
    prop, mutation_task = values.get("property"), _task_number(values.get("task_id"))
    reason = "no later read of the changed object yet"
    principal = ""
    if prop == PROPERTY_REPLICATION:
        principal = bound_principal(values.get("principal"), netbios_map)
        if not principal:
            return None, "the granted principal is not bound (no SID, and its NetBIOS prefix is not mapped)"
    for read in sorted(reads, key=lambda row: _task_number(((row.get("payload") or {}).get("values") or {}).get("task_id"))):
        read_values_ = (read.get("payload") or {}).get("values") or {}
        if _task_number(read_values_.get("task_id")) <= mutation_task:
            continue
        observed = read_from_values(read_values_)
        if prop == PROPERTY_REPLICATION:
            sid = principal if _SID_RE.match(principal) else ""
            verdict = verify_acl_rights(observed, object_dn=values.get("object"), principal="" if sid else principal,
                                        principal_sid=sid, rights=REPLICATION_RIGHTS)
        elif prop == PROPERTY_RBCD:
            verdict = verify_rbcd(observed, computer=values.get("object"), trustee_sid=values.get("principal_sid"))
        elif prop == PROPERTY_MEMBER:
            verdict = verify_group_member(observed, group_dn=values.get("object"), principal_sid=values.get("principal_sid"))
        elif prop == PROPERTY_GPLINK:
            verdict = verify_gpo_link(observed, container_dn=values.get("object"), gpo_guid=values.get("principal"))
        else:
            return None, "unsupported directory property"
        if verdict.achieved:
            return read, ""
        reason = verdict.reason
    return None, reason


def contradicts_directory_change(join: Mapping[str, Any], reads: list[Mapping[str, Any]],
                                 netbios_map: Mapping[str, str] | None) -> tuple[Mapping[str, Any] | None, str]:
    """(the first later COMPLETE read of the exact object showing the right absent, '') or (None, why not).

    The inverse of :func:`resolve_directory_change`: a conclusive contradiction, used to retire a directory gain. A
    read only contradicts when it is of the same object, by a later task, and COMPLETE for that property — an
    incomplete read, a partial enumeration or a read of another object is not conclusive loss."""
    values = (join.get("payload") or {}).get("values") or {}
    prop, mutation_task = values.get("property"), _task_number(values.get("task_id"))
    obj = _text(values.get("object"))
    principal = ""
    if prop == PROPERTY_REPLICATION:
        principal = bound_principal(values.get("principal"), netbios_map)
        if not principal:
            return None, "the granted principal is not bound"
    for read in sorted(reads, key=lambda row: _task_number(((row.get("payload") or {}).get("values") or {}).get("task_id"))):
        rv = (read.get("payload") or {}).get("values") or {}
        if _task_number(rv.get("task_id")) <= mutation_task:
            continue
        observed = read_from_values(rv)
        same_object = (normalize_dn(observed.object_dn) == normalize_dn(obj) if "=" in obj
                       else _first_rdn_value(observed.object_dn).casefold() == obj.casefold())
        if not same_object:
            continue
        if prop == PROPERTY_REPLICATION:
            if not observed.complete:
                continue
            sid = principal if _SID_RE.match(principal) else ""
            verdict = verify_acl_rights(observed, object_dn=obj, principal="" if sid else principal,
                                        principal_sid=sid, rights=REPLICATION_RIGHTS)
        elif prop == PROPERTY_RBCD:
            if not observed.rbcd_complete:
                continue
            verdict = verify_rbcd(observed, computer=obj, trustee_sid=values.get("principal_sid"))
        elif prop == PROPERTY_MEMBER:
            if not observed.members_complete:
                continue
            verdict = verify_group_member(observed, group_dn=obj, principal_sid=values.get("principal_sid"))
        elif prop == PROPERTY_GPLINK:
            if observed.gplink is None:
                continue
            verdict = verify_gpo_link(observed, container_dn=obj, gpo_guid=values.get("principal"))
        else:
            continue
        if not verdict.achieved:
            return read, ""
    return None, "no complete later read of the object contradicts the change"


def dn_domain(dn: str) -> str:
    """The DNS domain a distinguished name's DC components name ('' without any)."""
    parts = [rdn.partition("=")[2] for rdn in normalize_dn(dn).split(",") if rdn.startswith("dc=")]
    return ".".join(parts)


def domain_head_dn(domain: str) -> str:
    """The distinguished name of a domain's naming-context head (``DC=...``), '' for an empty domain."""
    return ",".join(f"dc={part}" for part in _text(domain).casefold().split(".") if part)


def is_domain_head_dn(dn: str) -> bool:
    """Whether a DN is a domain naming-context head: every RDN is ``DC=`` (no OU or CN prefix). Replication rights
    live on this object; the same grant against an OU or leaf does not confer domain DCSync."""
    normalized = normalize_dn(dn)
    return bool(normalized) and all(rdn.startswith("dc=") for rdn in normalized.split(","))


# --- Cross-task session proof -----------------------------------------------------------------------------------
#
# A context-changing task (make_token, ticket import, revert) records a non-secret ``session_context`` observation:
# the identity now active on that callback lifetime. It is NOT evidence that authentication worked — it is the
# sequence anchor a later proof joins to. A ``session_use`` observation is a later terminal-success network action
# whose own response established access to a named resource. A working session is admitted only when a use joins the
# setup that controls it: the same callback lifetime, the latest context before the use, matching identity and logon
# id, with no intervening or ambiguous context change. The write's success message, a bare ticket/bind marker, a
# local whoami or an anonymous read never prove use.

SESSION_CONTEXT = "session_context"
SESSION_USE = "session_use"
SESSION_VERIFIER = "effect_observations:session"
# How long a pass keeps joining a setup to a later use before it stops polling. Citing the proving task still records
# it; expiry only ends the automatic search, as for the directory joins.
SESSION_JOIN_WINDOW_SECONDS = 86400
# The modeled rows a proven session records. Kerberos reuses the account-context vocabulary; a non-Kerberos resource
# access is scoped to the resource and never labelled Kerberos or inferred as domain administration.
SESSION_KERBEROS_TECHNIQUE = "kerberos-account-context"
SESSION_RESOURCE_TECHNIQUE = "authenticated-session"


def _session_identity_from_params(params: Any) -> str:
    """``account@domain`` a make_token's parameters name (``--username``/``--domain``, ``DOMAIN\\user`` or
    ``user@domain``), casefolded, else ''."""
    user = intent_classifier.flag_value(params, "username") or intent_classifier.flag_value(params, "user")
    domain = intent_classifier.flag_value(params, "domain") or intent_classifier.flag_value(params, "realm")
    user = _text(user)
    domain = _text(domain)
    if user and not domain and "\\" in user:
        domain, _, user = user.partition("\\")
    if user and not domain and "@" in user:
        user, _, domain = user.partition("@")
    return f"{user}@{domain}".casefold() if user and domain else ""


def _session_lifetime(values: Mapping[str, Any]) -> str:
    return _text(values.get("agent_callback_id")) or f"callback:{_text(values.get('callback_id'))}"


def session_context_observation(task: Mapping[str, Any] | None, output: Any, terminal_status: str, observed_at: str,
                                *, params: Any = "") -> Observation | None:
    """A ``session_context`` observation for a terminal-success context-changing task, else None.

    ``set`` (make_token / ticket import) carries the new identity and opens an awaiting join; ``cleared`` (revert /
    purge) only records the sequence break. A set whose account cannot be bound (no identity in output or parameters)
    is ambiguous and anchors nothing.
    """
    values = normalize_task(task)
    if not values or values.get("task_classification") != task_evidence.TERMINAL_SUCCESS:
        return None
    change = credential_artifacts.session_setup_change(values.get("command_name"), output)
    if change is None:
        return None
    identity = _session_identity_from_params(params) or _text(change.get("identity"))
    if change["change_kind"] == "set" and not identity:
        return None
    lifetime = _session_lifetime(values)
    values.update({
        "change_kind": change["change_kind"], "identity": identity, "is_kerberos": bool(change["is_kerberos"]),
        "logon_id": _text(change.get("logon_id")), "callback_lifetime": lifetime,
        "terminal_status": _text(terminal_status),
    })
    task_id = str(values["task_id"])
    subject = f"{lifetime}|{task_id}|{identity or change['change_kind']}"
    join_state = JOIN_AWAITING if change["change_kind"] == "set" else NO_JOIN
    reason = ("awaiting a later network action that proves this context reaches a resource"
              if join_state == JOIN_AWAITING else "")
    return Observation(
        kind=SESSION_CONTEXT, source_kind="task", source_id=task_id, subject=subject, observed_at=_text(observed_at),
        values=values, task_id=task_id, callback_id=str(values["callback_id"]),
        agent_callback_id=str(values["agent_callback_id"]), supporting=(f"task:{task_id}",),
        verifier_id=SESSION_VERIFIER, reason=reason, join_state=join_state,
    )


def session_use_observation(task: Mapping[str, Any] | None, output: Any, terminal_status: str,
                            observed_at: str) -> Observation | None:
    """A ``session_use`` observation for a terminal-success task whose response established authenticated access to a
    named resource, else None. A structured directory read of an exact object also counts, unless anonymous."""
    values = normalize_task(task)
    if not values or values.get("task_classification") != task_evidence.TERMINAL_SUCCESS:
        return None
    probe = dict(credential_artifacts.extract_session_use_probe(output))
    read = parse_acl_read(output)
    if not read.reason and read.object_dn and read.complete and not probe["anonymous"]:
        probe["ldap_access_proven"] = True
        if not probe["resource"]:
            probe["resource"] = read.object_dn
    if not (probe["service_access_proven"] or probe["ticket_valid"] or probe["ldap_access_proven"]):
        return None
    lifetime = _session_lifetime(values)
    values.update({"callback_lifetime": lifetime, "terminal_status": _text(terminal_status),
                   "logon_id": credential_artifacts.session_logon_id(output), **probe})
    task_id = str(values["task_id"])
    subject = f"{lifetime}|{task_id}|{probe.get('resource') or 'resource'}"
    return Observation(
        kind=SESSION_USE, source_kind="task", source_id=task_id, subject=subject, observed_at=_text(observed_at),
        values=values, task_id=task_id, callback_id=str(values["callback_id"]),
        agent_callback_id=str(values["agent_callback_id"]), supporting=(f"task:{task_id}",),
        verifier_id=SESSION_VERIFIER,
    )


def _values(row: Mapping[str, Any]) -> dict[str, Any]:
    return (row.get("payload") or {}).get("values") or {}


def resolve_session_use(join: Mapping[str, Any], contexts: list[Mapping[str, Any]],
                        uses: list[Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, str]:
    """(the first later use that proves this setup's context, '') or (None, why not yet).

    A use proves a setup only when the setup is the controlling context for that use: the latest ``set`` on the same
    callback lifetime before the use, with no intervening context change. Identity and logon id, where the use names
    them, must match; an anonymous read is never use."""
    jv = _values(join)
    lifetime = jv.get("callback_lifetime")
    setup_task = _task_number(jv.get("task_id"))
    identity = _text(jv.get("identity"))
    logon = _text(jv.get("logon_id"))
    sequence = sorted(
        ((_task_number(_values(ctx).get("task_id")), _values(ctx)) for ctx in contexts
         if _values(ctx).get("callback_lifetime") == lifetime),
        key=lambda item: item[0])
    reason = "no later network action proved this context reaches a resource"
    for use in sorted(uses, key=lambda row: _task_number(_values(row).get("task_id"))):
        uv = _values(use)
        if uv.get("callback_lifetime") != lifetime:
            continue
        use_task = _task_number(uv.get("task_id"))
        if use_task <= setup_task:
            continue
        controlling = None
        for number, cv in sequence:
            if number < use_task:
                controlling = (number, cv)
            else:
                break
        if controlling is None or controlling[0] != setup_task or controlling[1].get("change_kind") != "set":
            # a later or ambiguous context change controls this use, not this setup
            continue
        use_identity = _text(uv.get("client_identity"))
        if use_identity and identity and use_identity != identity:
            reason = "the later action proved a different account's access, not this session's"
            continue
        use_logon = _text(uv.get("logon_id"))
        if logon and use_logon and use_logon != logon:
            reason = "the later action ran under a different logon id (a different session)"
            continue
        if not (uv.get("service_access_proven") or uv.get("ticket_valid") or uv.get("ldap_access_proven")):
            continue
        return use, ""
    return None, reason


def session_superseded(join: Mapping[str, Any], contexts: list[Mapping[str, Any]]) -> bool:
    """Whether a later context change on the same callback lifetime has replaced this setup (a new ``set`` with a
    different identity, or a ``cleared``), so its session scope no longer holds even before any use proved it."""
    jv = _values(join)
    lifetime, setup_task = jv.get("callback_lifetime"), _task_number(jv.get("task_id"))
    identity = _text(jv.get("identity"))
    for ctx in contexts:
        cv = _values(ctx)
        if cv.get("callback_lifetime") != lifetime:
            continue
        number = _task_number(cv.get("task_id"))
        if number <= setup_task:
            continue
        if cv.get("change_kind") == "cleared" or _text(cv.get("identity")) != identity:
            return True
    return False


def session_row(use: Mapping[str, Any], setup: Mapping[str, Any]) -> tuple[str, str, str]:
    """(technique, target, resource) for the working-session row a resolved use records. Kerberos reuses the
    account-context vocabulary; a non-Kerberos access is scoped to its resource."""
    sv, uv = setup, use
    identity = _text(sv.get("identity"))
    resource = _text(uv.get("resource"))
    if sv.get("is_kerberos"):
        return SESSION_KERBEROS_TECHNIQUE, identity, resource
    target = f"{identity}@{resource}" if identity and resource else (identity or resource)
    return SESSION_RESOURCE_TECHNIQUE, target.casefold(), resource
