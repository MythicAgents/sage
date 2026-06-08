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

import re

# Techniques whose `achieved` record is gated on a real credential artifact in the output. Other
# techniques (gpo-abuse, golden-ticket, sid-history-escalation, rbcd-standin, dcsync-rights-grant)
# keep the legacy "record achieved unless it's a known failure signature" behavior — their artifacts
# are not reliably text-detectable yet and they are not the false-achieved bug. Opt-in per technique.
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
    cleaned = _strip_placeholder_lines(text)
    for rx in _LABEL_PATTERNS:
        for m in rx.finditer(cleaned):
            if _is_usable_hash(m.group(1)):
                return True
    for m in _SECRETSDUMP_LINE.finditer(cleaned):   # group(1) is the NT field
        if _is_usable_hash(m.group(1)):
            return True
    return False


def extract_credential_probe(output) -> dict:
    """Map free-form credential-dump output to the structured probe dict ``verify_effect`` consumes.

    Returns a dict with the verify-schema keys used by the credential techniques
    (``dcsync`` / ``dcsync-user`` / ``lsass-dump``). The authoritative key is
    ``credentials_dumped`` (and ``krbtgt_hash_present`` for the krbtgt ``dcsync``): True ONLY when a
    real, non-placeholder secret value is present. Partial keys reflect that a dump tool ran but no
    usable key was recovered — never enough for an ``achieved`` verdict.
    """
    text = output if isinstance(output, str) else ("" if output is None else str(output))
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
