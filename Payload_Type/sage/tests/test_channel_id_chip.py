"""ISC-68 — the chat header exposes the Mythic channel ID.

Mythic's UI surfaces no readable channel id, which made "stay in chat 57" an instruction Russel
could not follow, and makes log correlation guesswork (thread keys are `<channel>:generation:<uuid>`).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sage_chat.metadata import _channel_id


class _M:
    def __init__(self, thread_id):
        self._thread_id_override = thread_id


def test_extracts_channel_id_from_thread_key():
    assert _channel_id(_M("57:generation:1d963028def94e63a55e5607f36e9380")) == "57"


def test_degrades_to_empty_rather_than_raising():
    # A header must never break a turn — every surprise shape returns "".
    assert _channel_id(_M("")) == ""
    assert _channel_id(_M(None)) == ""
    assert _channel_id(_M("not-a-channel:generation:abc")) == ""
    assert _channel_id(object()) == ""


def test_chip_is_present_and_leftmost():
    from sage_chat import metadata

    items = metadata.build_channel_metadata(_M("57:generation:abc")).get("items", [])
    chip = next((i for i in items if i.get("key") == "channel_id"), None)
    assert chip is not None, "channel_id chip must be published"
    assert chip["value"] == "57"
    assert chip["order"] == 0, "must sort ahead of every other chip"
    assert chip["order"] < min(i["order"] for i in items if i.get("key") != "channel_id")
