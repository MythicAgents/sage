"""Deterministic extraction of credential-dump artifacts from C2 task output.

Pure, stdlib-only. Turns free-form DCSync / mimikatz / secretsdump / Rubeus output into the
structured boolean probe dict that ``engagement_state.verify_effect`` interprets against a
technique's ``verify`` schema. This is the **verify-on-record** seam: a credential hop is only
recorded ``achieved`` when the task output actually contains a *usable* secret (a real NTLM/AES/
RC4 key), not merely because the task did not emit a known failure signature.

Why this exists (2026-06-08): the record path (`mythic_tools._record_engagement_success`) recorded
a hop ``achieved`` whenever the output was not a *known failure signature*. A ``dcsync-user`` DCSync
of a SMALL COUNCIL member that failed with **8439 DS_DRA_BAD_DN** (the Mythic task "succeeds" but no
key is returned) is not a known failure signature, so ``creds:cersei.lannister@…`` was recorded
ACHIEVED — a false-achieved ledger. The agent then forged a ticket with a PLACEHOLDER key. This
module supplies the missing string→struct step so the existing ``verify_effect`` seam can gate the
record on a real artifact.

Design notes:
  * ``engagement_state.py`` stays pure (it interprets structured dicts, never parses error strings).
    All free-form parsing lives here.
  * Detection is LABEL-ANCHORED: a hash counts only when it follows the field name that introduces it
    (``Hash NTLM:``, ``aes256_hmac … :``) or appears in a secretsdump ``user:rid:lm:nt:::`` line. A bare
    hex blob with no label does NOT count — that avoids flagging GUIDs/SIDs/random hex as a credential.
  * Placeholder tokens (``REPLACE_ME``, ``PLACEHOLDER`` …) never count as a usable secret.
"""

from __future__ import annotations

import ast
import re
from typing import Any

# Techniques whose `achieved` record is gated on a real credential artifact in the output. Other
# techniques must opt in to their own verifier sets below; do not use "task completed" as an artifact.
CREDENTIAL_TECHNIQUES: set[str] = {"dcsync", "dcsync-user", "lsass-dump"}

# Field-label-anchored secret patterns. Each requires the introducing label AND a hex value of the
# right width, so a stray hex token never matches. Covers both mimikatz dcsync (`Hash NTLM: <hex>`,
# `aes256_hmac (4096) : <hex>`) and sekurlsa::logonpasswords (`* NTLM : <hex>`, `* SHA1 : <hex>`) —
# the `\s*` around the colon absorbs the column padding in both shapes. Hex value is captured so the
# caller can reject degenerate constants (empty-LM / blank-NT / all-zero).
#   NTLM / LM hash       -> 32 hex
#   Kerberos aes256_hmac -> 64 hex
#   Kerberos aes128_hmac -> 32 hex
#   Kerberos rc4_hmac    -> 32 hex (== the NT hash)
_NTLM_LABEL = re.compile(r"(?:hash\s+ntlm|\bntlm)\s*:\s*([0-9a-fA-F]{32})\b", re.IGNORECASE)
_AES256_LABEL = re.compile(r"aes256[_-]?hmac[^\n:]*:\s*([0-9a-fA-F]{64})\b", re.IGNORECASE)
_AES128_LABEL = re.compile(r"aes128[_-]?hmac[^\n:]*:\s*([0-9a-fA-F]{32})\b", re.IGNORECASE)
_RC4_LABEL = re.compile(r"rc4[_-]?hmac[^\n:]*:\s*([0-9a-fA-F]{32})\b", re.IGNORECASE)

_LABEL_PATTERNS = (_NTLM_LABEL, _AES256_LABEL, _AES128_LABEL, _RC4_LABEL)

_MATERIAL_LABELS = (
    (
        "aes256",
        "key",
        re.compile(r"aes256[_-]?hmac[^\n:]*:\s*([0-9a-fA-F]{1,64})", re.IGNORECASE),
        64,
    ),
    (
        "aes128",
        "key",
        re.compile(r"aes128[_-]?hmac[^\n:]*:\s*([0-9a-fA-F]{1,32})", re.IGNORECASE),
        32,
    ),
    (
        "rc4",
        "hash",
        re.compile(r"rc4[_-]?hmac[^\n:]*:\s*([0-9a-fA-F]{1,32})", re.IGNORECASE),
        32,
    ),
    (
        "ntlm",
        "hash",
        re.compile(r"(?:hash\s+ntlm|\bntlm(?:-\s*\d+)?)\s*:\s*([0-9a-fA-F]{1,32})", re.IGNORECASE),
        32,
    ),
)

_SECRET_LABEL_VALUE = re.compile(
    r"((?:hash\s+ntlm|\bntlm(?:-\s*\d+)?|aes256[_-]?hmac[^\n:]*|aes128[_-]?hmac[^\n:]*|"
    r"rc4[_-]?hmac[^\n:]*)\s*:\s*)([0-9a-fA-F]{8,64})(?:\s*\n\s*([0-9a-fA-F]{8,64}))?",
    re.IGNORECASE,
)

# secretsdump / impacket style:  DOMAIN\user:rid:lmhash:nthash:::   The NT hash (4th colon field) is the
# usable secret — the LM field is almost always the empty-LM constant in modern AD, so we validate the NT.
_SECRETSDUMP_LINE = re.compile(
    r"^[^\s:]+:\d+:[0-9a-fA-F]{32}:([0-9a-fA-F]{32}):::", re.MULTILINE
)

# Hashes that match the shape but are NOT a usable secret: the empty-LM constant, the blank-password NT
# hash, and any all-zero value. Forging with these is the same phantom-credential failure mode in softer
# clothing, so they never count as a dumped credential.
_DEGENERATE_HASHES = {
    "aad3b435b51404eeaad3b435b51404ee",  # empty LM
    "31d6cfe0d16ae931b73c59d7e0c089c0",  # NT of a blank password
}


def _is_usable_hash(value: str) -> bool:
    """True iff a captured hex hash is a real, usable secret (not a degenerate/empty constant)."""
    v = (value or "").lower()
    if not v or set(v) <= {"0"}:          # empty or all-zero
        return False
    return v not in _DEGENERATE_HASHES

# Tokens that mark a value as a non-secret placeholder the agent inserted (never a usable key).
_PLACEHOLDER_TOKENS = (
    "replace_me", "placeholder", "todo", "changeme", "<aes", "<rc4", "<ntlm",
    "$(", "xxxxxxxx", "deadbeef",
)

# Soft signal that a dump *tool* ran / connected (partial credit only — never sufficient on its own).
_DUMP_STARTED_TOKENS = (
    "dcsync", "lsadump", "sekurlsa", "secretsdump", "object rdn", "** sam account **",
    "primary:kerberos", "credentials:",
)


def _strip_placeholder_lines(text: str) -> str:
    """Drop lines whose only hex-looking content is an agent placeholder, so a real key elsewhere is
    still detected but a placeholder-only output yields nothing."""
    kept = []
    for line in text.splitlines():
        low = line.lower()
        if any(tok in low for tok in _PLACEHOLDER_TOKENS):
            continue
        kept.append(line)
    return "\n".join(kept)


def _has_real_key(text: str) -> bool:
    """True iff a real, label-anchored NTLM/AES/RC4 secret or a secretsdump credential line is present
    AND its value is a usable hash (not an empty-LM / blank-NT / all-zero degenerate constant)."""
    return bool(extract_credential_material(text))


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _probe_text(value: Any) -> str:
    text = _text(value)
    stripped = text.strip()
    if re.match(r"(?is)^[rubf]*['\"]", stripped):
        try:
            parsed = ast.literal_eval(stripped)
        except (SyntaxError, ValueError, TypeError):
            return text
        if isinstance(parsed, bytes):
            return parsed.decode(errors="replace")
        if isinstance(parsed, str):
            return parsed
    return text


def _following_hex(cleaned: str, start: int, want: int) -> str:
    """Return hex continuation text after a short labeled value.

    Mimikatz sometimes wraps AES keys after the label line. The verifier only needed the NTLM line in
    most runs, but the runtime credential importer should salvage the stronger AES key when it is present.
    """
    out = []
    pos = start
    while len("".join(out)) < want:
        line_end = cleaned.find("\n", pos)
        if line_end == -1:
            line_end = len(cleaned)
        line = cleaned[pos:line_end].strip()
        if line:
            if not re.fullmatch(r"[0-9a-fA-F]+", line):
                break
            out.append(line)
        if line_end >= len(cleaned):
            break
        pos = line_end + 1
    return "".join(out)


def extract_credential_material(output, account: str = "", realm: str = "") -> list[dict[str, str]]:
    """Extract concrete credential material from verified credential-dump output.

    Returned dictionaries intentionally carry the raw secret only for the caller that immediately writes
    it to the operation credential store. Do not serialize these dictionaries into the engagement ledger.
    """
    text = _text(output)
    cleaned = _strip_placeholder_lines(text)
    materials: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(secret_type: str, credential_type: str, value: str, source: str) -> None:
        value = (value or "").strip().lower()
        if not value or not _is_usable_hash(value):
            return
        key = (credential_type, value)
        if key in seen:
            return
        seen.add(key)
        materials.append({
            "account": (account or "").strip(),
            "realm": (realm or "").strip().casefold(),
            "secret_type": secret_type,
            "credential_type": credential_type,
            "credential": value,
            "source": source,
        })

    for secret_type, credential_type, rx, width in _MATERIAL_LABELS:
        for match in rx.finditer(cleaned):
            value = match.group(1)
            if len(value) < width:
                value = (value + _following_hex(cleaned, match.end(), width - len(value)))[:width]
            if len(value) == width:
                add(secret_type, credential_type, value, match.group(0).split(":", 1)[0].strip())

    for match in _SECRETSDUMP_LINE.finditer(cleaned):
        add("ntlm", "hash", match.group(1), "secretsdump")
    return materials


def redact_credential_material(output) -> str:
    """Redact label-anchored credential values from operator-visible previews."""
    text = _text(output)

    def repl(match: re.Match) -> str:
        return f"{match.group(1)}<redacted>"

    redacted = _SECRET_LABEL_VALUE.sub(repl, text)

    def secretsdump_repl(match: re.Match) -> str:
        # Redact by the captured span, NOT by value: in a `user:rid:LM:NT:::` line the LM field precedes
        # NT, so a value-based `.replace(NT, ..., 1)` would redact the FIRST equal hex (the LM) and leave
        # the real NT secret visible whenever LM == NT (or NT recurs left of its field).
        line = match.group(0)
        start, end = match.span(1)
        rel_start, rel_end = start - match.start(), end - match.start()
        return line[:rel_start] + "<redacted>" + line[rel_end:]

    return _SECRETSDUMP_LINE.sub(secretsdump_repl, redacted)


def extract_credential_probe(output) -> dict:
    """Map free-form credential-dump output to the structured probe dict ``verify_effect`` consumes.

    Returns a dict with the verify-schema keys used by the credential techniques
    (``dcsync`` / ``dcsync-user`` / ``lsass-dump``). The authoritative key is
    ``credentials_dumped`` (and ``krbtgt_hash_present`` for the krbtgt ``dcsync``): True ONLY when a
    real, non-placeholder secret value is present. Partial keys reflect that a dump tool ran but no
    usable key was recovered — never enough for an ``achieved`` verdict.
    """
    text = _probe_text(output)
    has_secret = _has_real_key(text)
    low = text.lower()
    started = any(tok in low for tok in _DUMP_STARTED_TOKENS)
    return {
        # achieved_all anchors for the credential techniques:
        "credentials_dumped": has_secret,   # dcsync-user, lsass-dump
        "krbtgt_hash_present": has_secret,   # dcsync (krbtgt) — the technique already targets krbtgt
        # partial_any signals (informational; insufficient for achieved on their own):
        "user_hash_present": has_secret,
        "domain_hashes_dumped": has_secret,
        "secretsdump_connected": started,
    }


# Techniques whose `achieved` record is gated on a grant-application artifact (the DS-Replication ACE
# was actually written), parallel to CREDENTIAL_TECHNIQUES. Kept SEPARATE from CREDENTIAL_TECHNIQUES so
# the credential-key schema invariant (test_credential_techniques_matches_verify_schema) stays exact.
GRANT_TECHNIQUES: set[str] = {"dcsync-rights-grant"}

# The three DS-Replication extended-rights, by GUID and by name. A StandIn `--grant` that lands echoes
# the right it added; an unauthorized attempt does not apply one. (filtered-set is optional for DCSync but
# StandIn grants it alongside the other two.)
_REPL_GUID_GET_CHANGES = "1131f6aa-9c07-11d1-f79f-00c04fc2dcd2"
_REPL_GUID_GET_CHANGES_ALL = "1131f6ad-9c07-11d1-f79f-00c04fc2dcd2"
_REPL_GUID_GET_CHANGES_FILTERED = "89e95b76-444d-4c62-991a-0facbeda640c"

# Explicit "the ACE was applied" markers StandIn / SharpAllowedToAct-style tools print on success.
_GRANT_SUCCESS_TOKENS = (
    "dacl modified", "successfully", "ace added", "added ace", "rights granted",
    "granted dcsync", "added the", "applied", "[+] done", "modified the dacl",
)
# Hard failure signatures: an attempted-but-rejected grant must NEVER record achieved. Empty output (the
# GPO/SYSTEM scheduled-task that vanished from SYSVOL before firing) also yields no success marker -> failed.
_GRANT_FAILURE_TOKENS = (
    "access is denied", "access denied", "0x5", "unauthorized", "exception",
    "insufficient", " error", "errorcode", "denied", "failed", "could not",
    "not have", "0x80070005",
)


def extract_grant_probe(output) -> dict:
    """Map free-form StandIn `--grant` output to the structured probe dict ``verify_effect`` consumes for
    ``dcsync-rights-grant``. ``ds_replication_rights`` (the achieved_all anchor) is True ONLY when the
    output shows an explicit ACE-applied success marker AND names a replication right AND carries NO failure
    signature. This kills the 2026-06-09 false-achieved-grant bug: a grant that returned ``Access is denied``
    (samwell, medium integrity) or produced NO output (the GPO SYSTEM task that never fired) was recorded
    ``achieved`` because the legacy path only checked for a known failure signature."""
    text = output if isinstance(output, str) else ("" if output is None else str(output))
    low = text.lower()
    denied = (not low.strip()) or any(tok in low for tok in _GRANT_FAILURE_TOKENS)

    get_changes = (_REPL_GUID_GET_CHANGES in low) or ("get-changes" in low and "get-changes-all" not in low) \
        or ("get changes" in low and "get changes all" not in low) or ("replicating directory changes" in low)
    get_changes_all = (_REPL_GUID_GET_CHANGES_ALL in low) or ("get-changes-all" in low) \
        or ("get changes all" in low) or ("replicating directory changes all" in low)
    get_changes_filtered = (_REPL_GUID_GET_CHANGES_FILTERED in low) or ("filtered set" in low) \
        or ("get-changes-in-filtered-set" in low)
    ace_named = get_changes or get_changes_all or get_changes_filtered
    success_marker = any(tok in low for tok in _GRANT_SUCCESS_TOKENS)

    applied = success_marker and ace_named and not denied
    return {
        # achieved_all anchor for dcsync-rights-grant:
        "ds_replication_rights": applied,
        # partial_any signals — a right was named but no confirmed application (insufficient for achieved):
        "ace_present": ace_named and not denied,
        "get_changes": get_changes and not denied,
        "get_changes_all": get_changes_all and not denied,
        "get_changes_in_filtered_set": get_changes_filtered and not denied,
    }


# Ticket techniques must never use the legacy "no known failure string means achieved" path. Forging or
# injecting a ticket is only setup. The durable effect (da:<domain>) is achieved only when the ticket is
# proven usable by a post-action access check or group-membership proof.
TICKET_TECHNIQUES: set[str] = {"golden-ticket", "sid-history-escalation"}

_TICKET_FAILURE_TOKENS = (
    "unhandled rubeus exception",
    "system.argumentexception",
    "parameter name:",
    "value was invalid",
    "mimikatz_dolocal",
    "command of \"standard\" module not found",
    "error kuhl",
    "error kull",
    "kdc_err",
    "krb_ap_err",
    "failed",
    "access is denied",
    "access denied",
    "0x80070005",
    "getncchanges",
    "rtlAdjustPrivilege".casefold(),
)

_TICKET_FORGE_TOKENS = (
    "kerberos::golden",
    "action: build tgt",
    "building pac",
    "golden ticket",
    "ticket successfully imported",
    "successfully submitted",
    "ptt",
)

_TGT_TOKENS = (
    "cached tickets",
    "ticket cache",
    "krbtgt/",
    "krbtgt\\",
    "server: krbtgt",
)

# A genuine remote/admin directory listing succeeded. NOTE: generic success strings (e.g.
# "the command completed successfully") are NOT proof of access — they appear on countless benign commands
# and were a false-achieved vector. Bare "\\c$"/"admin$" path fragments are also insufficient on their own;
# proof requires an actual directory-listing marker, optionally domain-correlated (see _service_access_proven).
_SERVICE_ACCESS_TOKENS = (
    "directory of \\\\",
    "volume in drive",
    "successfully listed",
)

# Group lines that must NEVER count as effective membership: a deny-only / disabled SID, or an explicit
# non-membership / empty-group statement.
_GROUP_DENY_MARKERS = ("deny only", "use_for_deny_only", "for deny only")
_GROUP_NEGATION_MARKERS = (
    "is not a member", "not a member of", "no members", "0 members", "membership: none", "not present",
)
# Well-known RIDs under a domain SID — the UNFORGEABLE correlation token for the privileged groups.
_WELL_KNOWN_GROUP_RID = {"domain admins": "-512", "enterprise admins": "-519"}
_ADMIN_SHARE_RE = re.compile(r"\\\\([^\\\s/]+)\\(?:[a-zA-Z]\$|[Aa][Dd][Mm][Ii][Nn]\$)")


def _derive_netbios(expected_domain, expected_netbios) -> str:
    if expected_netbios:
        return str(expected_netbios).strip().casefold()
    dom = str(expected_domain or "").strip().casefold()
    return dom.split(".", 1)[0] if dom else ""


def _looks_like_membership_entry(low_line: str) -> bool:
    """A line that plausibly asserts EFFECTIVE group membership (a whoami / net-group listing row), not prose."""
    if "s-1-5-" in low_line:
        return True
    if re.search(r"[a-z0-9_.\-]+\\(?:domain admins|enterprise admins)", low_line):
        return True
    return any(a in low_line for a in ("enabled group", "enabled by default", "mandatory group", "member of", "members", "group name"))


def _line_correlates_to_domain(low_line, expected_domain, expected_netbios, expected_domain_sid, group_name) -> bool:
    """True when a membership line is bound to the TARGET domain. With NO domain context supplied the gate is
    open (the deny-only / negation / entry-shape filters still apply); with context it requires a NetBIOS
    prefix, the FQDN, or the well-known RID SID under the target domain SID. This stops a child-domain group
    line (e.g. ``NORTH\\Domain Admins``) from proving a parent-domain effect (``da:sevenkingdoms.local``)."""
    if not (expected_domain or expected_netbios or expected_domain_sid):
        return True
    netbios = _derive_netbios(expected_domain, expected_netbios)
    dom = str(expected_domain or "").strip().casefold()
    # EXACT domain-qualifier match: extract the `QUALIFIER\<group>` prefix and require QUALIFIER to EQUAL the
    # target NetBIOS or FQDN — never a free substring. A naked `dom in low_line` substring let a child FQDN
    # (`north.sevenkingdoms.local\Domain Admins`) correlate to the parent (`sevenkingdoms.local`); an unbounded
    # `f"{netbios}\\"` substring let `evil-sevenkingdoms\Domain Admins` match. Both are now rejected.
    m = re.search(rf"([a-z0-9_.\-]+)\\{re.escape(group_name)}", low_line)
    if m:
        qualifier = m.group(1)
        if (netbios and qualifier == netbios) or (dom and qualifier == dom):
            return True
    # SID RID correlation — the unforgeable anchor when the target domain SID is known. The well-known group
    # SID is domain_sid + RID (Domain Admins -512, Enterprise Admins -519); only the real domain can present it.
    sid = str(expected_domain_sid or "").strip().casefold()
    if sid:
        rid = _WELL_KNOWN_GROUP_RID.get(group_name, "")
        if rid and f"{sid}{rid}" in low_line:
            return True
    return False


def _qualifying_group_memberships(text, expected_domain=None, expected_netbios=None, expected_domain_sid=None) -> list:
    """Privileged groups the principal is an EFFECTIVE, domain-correlated member of.

    A line qualifies ONLY when it (a) names Domain Admins / Enterprise Admins, (b) is NOT a deny-only /
    disabled / negated entry, (c) looks like a membership listing row (not prose), and (d) correlates to the
    target domain when domain context is supplied. Closes the cross-domain (child DA proving parent DA) and
    deny-only (disabled SID counted as member) false-achieved holes.
    """
    out: list = []
    for raw in str(text).splitlines():
        low = raw.casefold()
        for group_name in ("domain admins", "enterprise admins"):
            if group_name not in low:
                continue
            if any(m in low for m in _GROUP_DENY_MARKERS):
                continue
            if any(m in low for m in _GROUP_NEGATION_MARKERS):
                continue
            if not _looks_like_membership_entry(low):
                continue
            if not _line_correlates_to_domain(low, expected_domain, expected_netbios, expected_domain_sid, group_name):
                continue
            label = "Domain Admins" if group_name == "domain admins" else "Enterprise Admins"
            if label not in out:
                out.append(label)
    return out


def _service_access_proven(text, expected_domain=None, expected_netbios=None) -> bool:
    """A genuine remote directory listing succeeded. With domain context, the listed admin share must belong
    to the TARGET domain (the host's immediate parent domain == target) — a child-host listing does not prove
    a parent-domain effect."""
    low = str(text).casefold()
    if not any(tok in low for tok in _SERVICE_ACCESS_TOKENS):
        return False
    if not (expected_domain or expected_netbios):
        return True
    dom = str(expected_domain or "").strip().casefold()
    for m in _ADMIN_SHARE_RE.finditer(str(text)):
        host = m.group(1).casefold()
        if dom and (host == dom or ("." in host and host.split(".", 1)[1] == dom)):
            return True
    return False


def extract_ticket_probe(output, expected_domain=None, expected_netbios=None, expected_domain_sid=None) -> dict:
    """Map golden-ticket / SID-history output to the structured proof schema.

    Achieved proof is deliberately narrow AND domain-correlated:
      * domain_admin/member_of from an EFFECTIVE, deny-only-filtered, target-domain-correlated group line,
      * service_access_proven from a real directory listing of an admin share in the target domain, or
      * explicit ticket_valid phrasing with no failure markers.

    ``expected_domain`` is the domain of the EFFECT being proven (the PARENT for ``sid-history-escalation``,
    whose effect is ``da:{parent}``). When supplied, a group/access line belonging to a DIFFERENT domain does
    not count — this stops a child ``Domain Admins`` line from proving a parent-domain effect. A deny-only /
    disabled group SID is never membership, and generic success strings are not proof on their own.
    A forge-only task is partial. Any error marker records failed, not achieved.
    """
    text = _probe_text(output)
    low = text.casefold()
    failed = (not low.strip()) or any(token.casefold() in low for token in _TICKET_FAILURE_TOKENS)

    member_of = (
        _qualifying_group_memberships(text, expected_domain, expected_netbios, expected_domain_sid)
        if not failed else []
    )
    group_success = bool(member_of)
    service_access = (
        not failed
        and _service_access_proven(text, expected_domain, expected_netbios)
        and not any(token in low for token in ("access is denied", "access denied", "logon failure"))
    )
    explicit_valid = (
        not failed
        and any(marker in low for marker in (
            "ticket valid",
            "validated ticket",
            "kerberos ticket is valid",
            "ap-req succeeded",
            "authentication succeeded",
        ))
    )
    ticket_forged = not failed and any(token.casefold() in low for token in _TICKET_FORGE_TOKENS)
    tgt_present = not failed and any(token.casefold() in low for token in _TGT_TOKENS)

    return {
        "domain_admin": bool(group_success),
        "ticket_valid": bool(explicit_valid or service_access),
        "service_access_proven": bool(service_access),
        "member_of": list(member_of),
        "ticket_forged": bool(ticket_forged),
        "tgt_present": bool(tgt_present),
        "ptt_attempted": bool(ticket_forged and "/ptt" in low),
        "ticket_error": bool(failed),
    }
