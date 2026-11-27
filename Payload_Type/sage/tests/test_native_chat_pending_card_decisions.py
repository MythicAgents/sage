"""Native helper decisions on an approval card whose parent request is pending or streaming.

The helper is real end to end: its snapshot read, request-status gate, card selector, mutation and echo checks.
`FakeMythic` stands in for Mythic's GraphQL server and mirrors the card-resolution rules of Mythic d8a94943
(`chat_update_webhook.go`): the card must still be pending, the request must not be complete/error/cancelled, the
conditional update resolves a card once, and success echoes the card and original request ids.

Recorded input: `fixtures/native_chat/request_125_pending_card.json` (worker request 125, card 1032, Mythic rows
reduced to identity and card fields). Mythic leaves a Supervisor card's parent `pending` when the card is the
request's first row (`recv_chat_response.go:559-563`); no such recording exists here, so `pending` parents below are
a labelled synthetic mutation of the recorded row's status, and nothing else.
"""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "skills" / "sage-live-runner" / "scripts" / "native_chat.py"
SPEC = importlib.util.spec_from_file_location("native_chat_pending_card_decisions", SCRIPT)
native_chat = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(native_chat)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "native_chat" / "request_125_pending_card.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
RECORDED_CARD_ID = 1032
ACTIVE_STATUSES = ["streaming", "pending"]  # "streaming" is recorded; "pending" is the labelled mutation.
INACTIVE_STATUSES = ["complete", "completed", "error", "failed", "cancelled", "canceled", "", None,
                     "Pending", "STREAMING", " streaming", "submitted", "unknown"]
MYTHIC_TERMINAL = {"complete", "error", "cancelled"}
DECISIONS = {
    "accept": lambda client, rid: native_chat.approve_pending_input_card(client, rid),
    "reject": lambda client, rid: native_chat.reject_pending_input_card(client, rid),
    "respond": lambda client, rid: native_chat.reject_pending_input_card(client, rid, response="read it later"),
}
RESOLVED_STATUS = {"accept": "accepted", "reject": "rejected", "respond": "responded"}


def recorded(status="streaming"):
    """The recorded rows; any status other than the recorded `streaming` is a labelled synthetic mutation."""
    rows = copy.deepcopy(RECORDED)
    rows["request"]["status"] = status
    return rows


class FakeMythic:
    def __init__(self, rows, *, echo=None, interleave=0):
        self.request = rows["request"]
        self.messages = rows["messages"]
        self.mutations = []
        self.echo = echo
        self._interleave = interleave
        self._reads = 0

    def _card(self, message_id):
        return next((m for m in self.messages if m["id"] == message_id), None)

    async def execute(self, client, query, variables=None):
        await asyncio.sleep(0)
        if query == native_chat.REQUEST_QUERY:
            self._reads += 1
            assert variables == {"requestId": self.request["id"]}
            return {
                "chat_request": [copy.deepcopy(self.request)],
                "chat_message": [copy.deepcopy(m) for m in self.messages
                                 if m["chat_request_id"] == self.request["id"]],
            }
        if query == native_chat.REQUEST_MESSAGE_QUERY:
            return {"chat_message": [copy.deepcopy(m) for m in self.messages if m["id"] == variables["messageId"]]}
        if query == native_chat.CHAT_INPUT_RESPONSE_MUTATION:
            # Let racing callers finish their snapshot reads before either resolves the card.
            for _ in range(100):
                if self._reads >= self._interleave:
                    break
                await asyncio.sleep(0)
            self.mutations.append(dict(variables))
            return {"chatInputResponse": self._resolve(variables)}
        raise AssertionError(f"unexpected Mythic query: {query[:60]}")

    def _resolve(self, variables):
        card = self._card(variables["messageId"])
        snapshot = ((card or {}).get("metadata") or {}).get("input_requested")
        if card is None or card.get("deleted") or not isinstance(snapshot, dict):
            return {"status": "error", "error": "chat message is not an input request"}
        if snapshot.get("status") != "pending":
            return {"status": "error", "error": "input request is no longer pending"}
        if self.request["status"] in MYTHIC_TERMINAL:
            return {"status": "error", "error": "chat request is already complete"}
        action = variables["action"]
        if action not in RESOLVED_STATUS:
            return {"status": "error", "error": "invalid action"}
        snapshot["status"] = RESOLVED_STATUS[action]
        echoed = {"message_id": card["id"], "request_id": card["chat_request_id"]}
        echoed.update(self.echo or {})
        return {"status": "success", "error": "", **echoed}


@pytest.fixture
def mythic(monkeypatch):
    def install(rows, **kwargs):
        fake = FakeMythic(rows, **kwargs)
        monkeypatch.setattr(native_chat.mythic, "execute_custom_query", fake.execute)
        return fake
    return install


def card_status(fake, message_id=RECORDED_CARD_ID):
    return fake._card(message_id)["metadata"]["input_requested"]["status"]


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
@pytest.mark.parametrize("decision", sorted(DECISIONS))
def test_decision_reaches_the_one_pending_card_in_either_active_state(mythic, status, decision):
    fake = mythic(recorded(status))

    result = asyncio.run(DECISIONS[decision](object(), 125))

    expected_action = "respond" if decision == "respond" else decision
    assert fake.mutations == [{
        "messageId": RECORDED_CARD_ID,
        "action": expected_action,
        "response": "read it later" if decision == "respond" else None,
        "choiceId": None,
    }]
    assert result["chat_request_id"] == 125
    assert result["input_request_message_id"] == RECORDED_CARD_ID
    assert card_status(fake) == RESOLVED_STATUS[decision]


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
def test_snapshot_export_labels_a_pending_card_in_either_active_state(mythic, status):
    mythic(recorded(status))

    snapshot = asyncio.run(native_chat.fetch_request_snapshot(object(), 125))
    exported = native_chat.build_request_snapshot_export(snapshot)

    assert exported["capture_state"] == "pending_operator_input"
    assert exported["status"] == status


@pytest.mark.parametrize("status", INACTIVE_STATUSES)
@pytest.mark.parametrize("decision", sorted(DECISIONS))
def test_inactive_or_unknown_request_refuses_before_mutation(mythic, status, decision):
    fake = mythic(recorded(status))

    with pytest.raises(RuntimeError, match="not in an active status"):
        asyncio.run(DECISIONS[decision](object(), 125))
    assert fake.mutations == []
    assert card_status(fake) == "pending"


@pytest.mark.parametrize("status", INACTIVE_STATUSES)
def test_snapshot_export_does_not_label_an_inactive_request(mythic, status):
    mythic(recorded(status))

    snapshot = asyncio.run(native_chat.fetch_request_snapshot(object(), 125))

    assert native_chat.build_request_snapshot_export(snapshot)["capture_state"] == "request_snapshot"


def _second_card(rows, *, status="pending", deleted=False):
    card = copy.deepcopy(next(m for m in rows["messages"] if m["id"] == RECORDED_CARD_ID))
    card["id"] = RECORDED_CARD_ID + 1
    card["deleted"] = deleted
    card["metadata"]["input_requested"]["status"] = status
    rows["messages"].append(card)
    return rows


def _card_variants(status):
    zero = recorded(status)
    zero["messages"][-1]["metadata"]["input_requested"]["status"] = "accepted"
    deleted_only = recorded(status)
    deleted_only["messages"][-1]["deleted"] = True
    return {
        "zero": zero,
        "two": _second_card(recorded(status)),
        "deleted_only": deleted_only,
    }


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
@pytest.mark.parametrize("variant", ["zero", "two", "deleted_only"])
@pytest.mark.parametrize("decision", sorted(DECISIONS))
def test_ambiguous_or_missing_card_refuses_before_mutation(mythic, status, variant, decision):
    fake = mythic(_card_variants(status)[variant])

    with pytest.raises(RuntimeError, match="exactly one unresolved input_requested"):
        asyncio.run(DECISIONS[decision](object(), 125))
    assert fake.mutations == []


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
def test_deleted_duplicate_card_is_ignored(mythic, status):
    fake = mythic(_second_card(recorded(status), deleted=True))

    asyncio.run(native_chat.approve_pending_input_card(object(), 125))

    assert [m["messageId"] for m in fake.mutations] == [RECORDED_CARD_ID]


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
@pytest.mark.parametrize(("echo", "expected"), [
    ({"message_id": RECORDED_CARD_ID + 1}, "wrong message_id"),
    ({"request_id": 126}, "wrong request_id"),
    ({"message_id": str(RECORDED_CARD_ID)}, "message id must be an exact integer"),
    ({"request_id": True}, "request id must be an exact integer"),
])
def test_wrong_echo_fails_after_one_mutation(mythic, status, echo, expected):
    fake = mythic(recorded(status), echo=echo)

    with pytest.raises(RuntimeError, match=expected):
        asyncio.run(native_chat.approve_pending_input_card(object(), 125))
    assert len(fake.mutations) == 1


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
@pytest.mark.parametrize("first", sorted(DECISIONS))
@pytest.mark.parametrize("second", sorted(DECISIONS))
def test_second_decision_on_a_resolved_card_refuses_before_mutation(mythic, status, first, second):
    fake = mythic(recorded(status))
    asyncio.run(DECISIONS[first](object(), 125))

    with pytest.raises(RuntimeError, match="exactly one unresolved input_requested"):
        asyncio.run(DECISIONS[second](object(), 125))
    assert len(fake.mutations) == 1
    assert card_status(fake) == RESOLVED_STATUS[first]


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
@pytest.mark.parametrize(("first", "second"), [("accept", "accept"), ("accept", "reject"), ("reject", "accept")])
def test_concurrent_decisions_resolve_the_card_once(mythic, status, first, second):
    fake = mythic(recorded(status), interleave=2)

    async def race():
        return await asyncio.gather(
            DECISIONS[first](object(), 125), DECISIONS[second](object(), 125), return_exceptions=True,
        )

    outcomes = asyncio.run(race())

    assert len(fake.mutations) == 2
    winners = [o for o in outcomes if isinstance(o, dict)]
    losers = [o for o in outcomes if isinstance(o, Exception)]
    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], RuntimeError) and "no longer pending" in str(losers[0])
    assert card_status(fake) == RESOLVED_STATUS[winners[0]["action"]]
