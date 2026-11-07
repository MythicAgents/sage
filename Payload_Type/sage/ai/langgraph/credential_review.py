"""Independent review of a credential candidate in a Mythic task's output, for a task only post-processing failed.

A task Mythic classified ``postprocessing_error_with_output`` (the agent answered, then Mythic's completion function
failed) is never achieved from its output alone. This module decides whether that output proves **credential
material** for the task's target account:

1. A *locator* finds candidates and the span each occupies in the output. Locators know tool output shapes (a
   mimikatz DCSync block, a secretsdump line); they never decide validity.
2. A *validator*, selected by credential type alone, checks the candidate's structure.
3. A *binding* check requires the account (and any realm) the output names around the candidate to be the task's
   target account and realm.
4. The cited span is re-read from the output and validated again, so a citation that does not point at a valid
   candidate never counts.

The result is a review record carrying only type, account, realm, a salted one-way value hash, the output location,
the verdict and its commitments: never a secret value or output text. The proof boundary admits a review-backed
proof as material only (``cred-material:<kind>:<account>@<realm>``); no review asserts a working credential.

Pure stdlib plus the proof boundary. No evaluation, range or operator package is imported here.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import re
from typing import Any, Mapping

try:
    from . import proof_boundary
except ImportError:  # pragma: no cover - flat import for tests that put ai/langgraph on sys.path
    import proof_boundary  # type: ignore


RULE_VERSION = "credential-review/1"
REVIEWER_DETERMINISTIC = "deterministic"
REVIEWER_ID = "sage.credential_review.deterministic/1"
VERIFIER_ID = f"credential_review:{RULE_VERSION}"

VALID = "valid"
REJECTED = "rejected"
NOT_APPLICABLE = "not_applicable"

# Credential type -> the credential-store material kind its effect uses (``cred-material:<kind>:...``).
MATERIAL_KIND = {"ntlm": "hash", "rc4": "hash", "aes128": "key", "aes256": "key"}
# Preference when one output holds several valid candidates for the account.
_TYPE_ORDER = ("ntlm", "aes256", "aes128", "rc4")
_MAX_REASON_CHARS = 160
# Names taken from the output are target-controlled; only well-formed, bounded ones are ever stored.
_ACCOUNT_NAME = re.compile(r"^[a-z0-9][a-z0-9._$-]{0,63}$")
_REALM_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}$")


# --- output text and digests ------------------------------------------------------------------------------------

def review_text(output: Any) -> str:
    """The exact text a review reads, and the text its locations index: decoded, with a bytes-repr unwrapped."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    text = str(output)
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


def output_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def value_sha256(value: str) -> str:
    """A one-way hash of a candidate value, salted by the review rule so it does not match a bare hash of the value."""
    return hashlib.sha256(f"sage:{RULE_VERSION}:".encode() + value.strip().casefold().encode()).hexdigest()


# --- validators, by credential type -----------------------------------------------------------------------------

_DEGENERATE = frozenset({
    "aad3b435b51404eeaad3b435b51404ee",  # empty LM
    "31d6cfe0d16ae931b73c59d7e0c089c0",  # NT of an empty password
})


def _hex_validator(width: int):
    def check(value: str) -> tuple[bool, str]:
        candidate = value.strip().casefold()
        if len(candidate) != width or not re.fullmatch(r"[0-9a-f]+", candidate):
            return False, f"not {width} hexadecimal characters"
        if len(set(candidate)) == 1:
            return False, "a constant value"
        if candidate in _DEGENERATE:
            return False, "an empty-password or empty-LM constant"
        if "deadbeef" in candidate:
            return False, "a placeholder value"
        return True, ""
    return check


VALIDATORS = {
    "ntlm": _hex_validator(32),
    "rc4": _hex_validator(32),
    "aes128": _hex_validator(32),
    "aes256": _hex_validator(64),
}


def validate_candidate(credential_type: str, value: str) -> tuple[bool, str]:
    """Structural validation selected by credential type, whatever tool produced the output."""
    validator = VALIDATORS.get(str(credential_type or "").casefold())
    if validator is None:
        return False, f"no structural validator for {credential_type or 'an unknown type'}"
    return validator(value)


# --- locators ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A located candidate. ``account``/``realm`` are what the output names around it ("" when it names none)."""

    credential_type: str
    start: int
    end: int
    account: str = ""
    realms: tuple[str, ...] = ()
    locator: str = ""


_LABELS = (
    ("aes256", re.compile(r"aes256[_-]?hmac[^\n:]*:[ \t]*", re.IGNORECASE)),
    ("aes128", re.compile(r"aes128[_-]?hmac[^\n:]*:[ \t]*", re.IGNORECASE)),
    ("rc4", re.compile(r"rc4[_-]?hmac[^\n:]*:[ \t]*", re.IGNORECASE)),
    ("ntlm", re.compile(r"(?:hash[ \t]+ntlm|^[ \t*]*ntlm)[ \t]*:[ \t]*", re.IGNORECASE | re.MULTILINE)),
)
_HEX_RUN = re.compile(r"[0-9a-fA-F]+")
_HEX_CONTINUATION = re.compile(r"[ \t]*\r?\n[ \t]*([0-9a-fA-F]+)[ \t]*(?=\r?\n|$)")
_BLOCK_START = re.compile(r"^[ \t]*Object RDN[ \t]*:", re.IGNORECASE | re.MULTILINE)
_SAM_USERNAME = re.compile(r"^[ \t]*SAM Username[ \t]*:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_UPN = re.compile(r"^[ \t]*User Principal Name[ \t]*:[ \t]*\S+@(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE)
_DC_DOMAIN = re.compile(r"\[DC\][ \t]*'([^'\n]+)'[ \t]*will be the domain", re.IGNORECASE)
_DC_USER = re.compile(r"\[DC\][ \t]*'([^'\n]+)'[ \t]*will be the user account", re.IGNORECASE)
_SECRETSDUMP = re.compile(
    r"^(?:([^\s:\\]+)\\)?([^\s:\\]+):\d+:[0-9a-fA-F]{32}:([0-9a-fA-F]+):::", re.MULTILINE)


def _value_span(text: str, start: int, width: int) -> tuple[int, int]:
    """The span of a hex value starting at ``start``; a short first run may continue on following lines (mimikatz
    wraps long AES keys), up to ``width`` hex characters."""
    match = _HEX_RUN.match(text, start)
    if match is None:
        return start, start
    end = match.end()
    total = end - start
    while total < width:
        nxt = _HEX_CONTINUATION.match(text, end)
        if nxt is None:
            break
        end = nxt.end(1)
        total += len(nxt.group(1))
    return start, end


def _split_principal(value: str) -> tuple[str, tuple[str, ...]]:
    """(account, realms named) from ``NETBIOS\\user``, ``user@realm`` or a bare account."""
    value = value.strip().casefold()
    if "\\" in value:
        prefix, _, account = value.rpartition("\\")
        return account, (prefix,)
    if "@" in value:
        account, _, realm = value.rpartition("@")
        return account, (realm,)
    return value, ()


def _block_names(text: str, position: int) -> tuple[str, tuple[str, ...]]:
    """The account and realms a mimikatz DCSync output names for the object block holding ``position``."""
    starts = [match.start() for match in _BLOCK_START.finditer(text)]
    block_start = max((s for s in starts if s <= position), default=0)
    block_end = min((s for s in starts if s > position), default=len(text))
    block = text[block_start:block_end]
    realms: list[str] = []
    account = ""
    # The nearest account line before the candidate: an output without object headers lists accounts in sequence.
    sams = list(_SAM_USERNAME.finditer(block, 0, position - block_start))
    if sams:
        account, named = _split_principal(sams[-1].group(1))
        realms.extend(named)
    for upn in _UPN.finditer(block):
        realms.append(upn.group(1).casefold())
    before = text[:position]
    domains = _DC_DOMAIN.findall(before)
    if domains:
        realms.append(domains[-1].strip().casefold())
    users = _DC_USER.findall(before)
    if users:
        user_account, named = _split_principal(users[-1])
        realms.extend(named)
        account = account or user_account
    return account, tuple(realms)


def locate_candidates(text: str) -> list[Candidate]:
    """Every credential candidate a known output shape exposes, with its span and the names around it."""
    out: list[Candidate] = []
    seen: set[tuple[int, int]] = set()
    for credential_type, label in _LABELS:
        width = 64 if credential_type == "aes256" else 32
        for match in label.finditer(text):
            start, end = _value_span(text, match.end(), width)
            if end <= start or (start, end) in seen:
                continue
            seen.add((start, end))
            account, realms = _block_names(text, start)
            out.append(Candidate(credential_type, start, end, account, realms, "mimikatz-label"))
    for match in _SECRETSDUMP.finditer(text):
        start, end = match.span(3)
        if (start, end) in seen:
            continue
        seen.add((start, end))
        realms = (match.group(1).casefold(),) if match.group(1) else ()
        out.append(Candidate("ntlm", start, end, match.group(2).casefold(), realms, "secretsdump-line"))
    return out


# --- citation and binding ---------------------------------------------------------------------------------------

def check_citation(text: str, location: Mapping[str, Any], credential_type: str) -> tuple[bool, str]:
    """Re-read a cited span from the output and validate it by type. The span must hold only the value."""
    try:
        start, end = int(location.get("start")), int(location.get("end"))
    except (TypeError, ValueError, AttributeError):
        return False, "the citation has no character span"
    if not 0 <= start < end <= len(text):
        return False, "the cited span is outside the task output"
    span = text[start:end]
    if not re.fullmatch(r"[0-9a-fA-F]+(?:[ \t]*\r?\n[ \t]*[0-9a-fA-F]+)*", span):
        return False, "the cited span is not a credential value"
    if start > 0 and re.match(r"[0-9a-fA-F]", text[start - 1]) or end < len(text) and re.match(
            r"[0-9a-fA-F]", text[end]):
        return False, "the cited span is part of a longer value"
    return validate_candidate(credential_type, re.sub(r"\s+", "", span))


def _realm_matches(named: str, realm: str) -> bool:
    named = named.strip().casefold().rstrip(".")
    if "." in named:
        return named == realm
    return bool(named) and named == realm.split(".", 1)[0]


def check_binding(candidate: Candidate, account: str, realm: str) -> tuple[bool, str]:
    """The candidate is the target account's: the output names that account for it, and no other realm."""
    if not candidate.account:
        return False, "the output names no account for the candidate"
    if candidate.account != account:
        return False, "the candidate belongs to another account"
    for named in candidate.realms:
        if not _realm_matches(named, realm):
            return False, "the output names another realm for the candidate"
    return True, ""


# --- the review -------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewOutcome:
    """One review decision. ``record`` is what Sage persists; it never holds a value or output text."""

    record: dict = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        return str(self.record.get("verdict") or NOT_APPLICABLE)

    @property
    def material_kind(self) -> str:
        candidate = self.record.get("candidate") if isinstance(self.record.get("candidate"), Mapping) else {}
        return MATERIAL_KIND.get(str(candidate.get("type") or ""), "unknown")

    @classmethod
    def not_applicable(cls, reason: str) -> "ReviewOutcome":
        return cls({"verdict": NOT_APPLICABLE, "reason": str(reason)[:_MAX_REASON_CHARS]})


def review_id(task_id: Any, canonical_effect: str, digest: str, reviewer_kind: str = REVIEWER_DETERMINISTIC) -> str:
    return proof_boundary.credential_review_id(task_id, canonical_effect, digest, RULE_VERSION, reviewer_kind)


def review_output(output: Any, *, task_id: Any, canonical_effect: str, account: str, realm: str) -> ReviewOutcome:
    """Deterministic review of one task's output for one account secret.

    ``valid`` when a located candidate passes its type validator, its binding and the re-read of its citation;
    ``rejected`` when candidates are located but none passes; ``not_applicable`` when no locator applies.
    """
    text = review_text(output)
    digest = output_sha256(text)
    account, realm = str(account or "").strip().casefold(), str(realm or "").strip().casefold()
    base = {
        "review_id": review_id(task_id, canonical_effect, digest),
        "rule_version": RULE_VERSION,
        "reviewer_kind": REVIEWER_DETERMINISTIC,
        "reviewer_id": REVIEWER_ID,
        "model": "",
        "prompt_version": "",
        "task_id": str(task_id),
        "canonical_effect": str(canonical_effect),
        "input_sha256": digest,
    }
    candidates = sorted(locate_candidates(text), key=lambda c: (_TYPE_ORDER.index(c.credential_type), c.start))
    if not candidates:
        return ReviewOutcome({**base, "verdict": NOT_APPLICABLE, "reason": "no deterministic locator applies"})
    first_failure: tuple[Candidate, str, str] | None = None
    for candidate in candidates:
        location = {"start": candidate.start, "end": candidate.end,
                    "line": text.count("\n", 0, candidate.start) + 1, "locator": candidate.locator}
        valid, why = check_citation(text, location, candidate.credential_type)
        if valid:
            valid, why = check_binding(candidate, account, realm)
        if valid:
            value = re.sub(r"\s+", "", text[candidate.start:candidate.end])
            return ReviewOutcome({
                **base, "verdict": VALID, "reason": "",
                "location": location,
                "candidate": {"type": candidate.credential_type, "account": account, "realm": realm,
                              "value_sha256": value_sha256(value)},
                "validator": {"type": candidate.credential_type, "outcome": VALID},
            })
        if first_failure is None:
            first_failure = (candidate, why, location)
    candidate, why, location = first_failure
    return ReviewOutcome({
        **base, "verdict": REJECTED, "reason": why[:_MAX_REASON_CHARS],
        "location": location,
        "candidate": {"type": candidate.credential_type,
                      "account": candidate.account if _ACCOUNT_NAME.fullmatch(candidate.account) else "",
                      "realm": next((r for r in candidate.realms if _REALM_NAME.fullmatch(r)), ""),
                      "value_sha256": ""},
        "validator": {"type": candidate.credential_type, "outcome": REJECTED},
    })


def review_for_task(output: Any, *, task_id: Any, canonical_effect: str, account: str, realm: str,
                    stored: Mapping[str, Any] | None = None) -> ReviewOutcome:
    """The review decision for this task, effect, output and rule: the stored record when one exists (a reload or a
    repeat writer never regrades), else a new deterministic review."""
    digest = output_sha256(review_text(output))
    existing = (stored or {}).get(review_id(task_id, canonical_effect, digest))
    if isinstance(existing, Mapping) and existing.get("verdict") in {VALID, REJECTED}:
        return ReviewOutcome(dict(existing))
    return review_output(output, task_id=task_id, canonical_effect=canonical_effect, account=account, realm=realm)


def review_observation(
    outcome: ReviewOutcome,
    *,
    subject: str,
    engagement_id: str,
    task_id: Any,
    callback_id: Any,
    command: str,
    transaction_id: str,
    captured_at: str,
) -> tuple[str, dict, dict] | None:
    """(effect, evidence, proof envelope) for a decisive review, or None when no review decided.

    The envelope is the boundary's review-backed material form: lineage to the Mythic task, the
    ``postprocessing_error_with_output`` class as terminal status, and commitments to the review input and record.
    A rejected review keeps its record and reason on the task's contribution and is never admitted.
    """
    record = dict(outcome.record)
    if outcome.verdict not in {VALID, REJECTED} or not record.get("review_id"):
        return None
    effect = f"cred-material:{outcome.material_kind}:{subject}"
    classification = proof_boundary.TASK_POSTPROCESSING_ERROR
    verifier_input = proof_boundary.credential_review_verifier_input(
        task_id, record["canonical_effect"], record["input_sha256"], record["rule_version"], record["reviewer_kind"])
    envelope = proof_boundary.make_runtime_task_envelope(
        engagement_id=engagement_id,
        callback_id=callback_id,
        task_id=task_id,
        terminal_status=classification,
        command=command,
        verifier_id=f"credential_review:{record['rule_version']}",
        captured_at=captured_at,
        transaction_id=transaction_id,
        verifier_input=verifier_input,
        verifier_result=record,
        metadata={
            proof_boundary.TASK_CLASSIFICATION_KEY: classification,
            proof_boundary.PROOF_FORM_KEY: proof_boundary.CREDENTIAL_REVIEW_MATERIAL,
            "review_id": record["review_id"],
            "review_rule_version": record["rule_version"],
            "reviewer_kind": record["reviewer_kind"],
            "review_verdict": record["verdict"],
            "review_output_sha256": record["input_sha256"],
            "claimed_effect": effect,
        },
    )
    evidence = {
        "source": "credential_review",
        "provenance": "review",
        "mythic_task_id": str(task_id),
        "callback_id": str(callback_id or ""),
        "transaction_id": transaction_id,
        "terminal_task_status": classification,
        "command": command,
        "verifier_id": envelope.verifier_id,
        "verifier_input_sha256": envelope.verifier_input_sha256,
        "verifier_result_sha256": envelope.verifier_result_sha256,
        "task_classification": classification,
        "review_id": record["review_id"],
        "review_verdict": record["verdict"],
        "review_reason": record.get("reason") or "",
        # Lifted onto the task's contribution as its persisted review record (see task_evidence.contribution_row).
        "credential_review": record,
    }
    return effect, evidence, envelope.to_dict()
