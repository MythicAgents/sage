"""Tests for supervised-mode channel separation.

The Supervisor must not see REMAINING / next-steps / engagement-level context
from specialist summaries in supervised mode.  This filter prevents the
Supervisor from re-delegating with objectives the operator never asked for.
"""

from ai.langgraph.model import _strip_supervised_engagement_context


def test_remaining_section_stripped():
    text = (
        "**DONE:** getprivs returned medium integrity.\n\n"
        "**REMAINING:** Escalate to SYSTEM via GPO abuse, then DCSync.\n\n"
        "**Status:** Handing back."
    )
    result = _strip_supervised_engagement_context(text)
    assert "REMAINING" not in result
    assert "Escalate" not in result
    assert "DONE" in result
    assert "getprivs" in result


def test_next_steps_section_stripped():
    text = (
        "Callback 1 is active as samwell.tarly with medium integrity.\n\n"
        "**Next Steps:**\n"
        "1. Enumerate domain controllers\n"
        "2. Find path to DA\n"
        "3. Escalate privileges"
    )
    result = _strip_supervised_engagement_context(text)
    assert "Next Steps" not in result
    assert "Escalate" not in result
    assert "samwell.tarly" in result


def test_prioritized_next_actions_stripped():
    text = (
        "Ticket cache shows 3 TGTs.\n\n"
        "**Prioritized Next Actions:**\n"
        "- Check for Kerberoastable SPNs\n"
        "- Attempt GPO abuse on STARKWALLPAPER"
    )
    result = _strip_supervised_engagement_context(text)
    assert "Prioritized" not in result
    assert "Kerberoastable" not in result
    assert "3 TGTs" in result


def test_follow_up_stripped():
    text = (
        "Domain controllers: DC01 (10.4.10.10), DC02 (10.4.10.11).\n\n"
        "**Follow-up Actions:**\n"
        "- Run SharpHound\n"
        "- Check ADCS"
    )
    result = _strip_supervised_engagement_context(text)
    assert "Follow-up" not in result
    assert "SharpHound" not in result
    assert "DC01" in result


def test_suggested_next_steps_stripped():
    text = (
        "Integrity: Medium. SID: S-1-5-21-...\n\n"
        "**Suggested Next Steps:**\n"
        "- Escalate to high integrity\n"
        "- Dump credentials"
    )
    result = _strip_supervised_engagement_context(text)
    assert "Suggested" not in result
    assert "Dump credentials" not in result
    assert "Medium" in result


def test_remaining_tasks_bold_stripped():
    text = (
        "**DONE:** Listed callbacks.\n\n"
        "**Remaining Tasks:** Enumerate users, find DA path.\n\n"
        "**BLOCKER:** None."
    )
    result = _strip_supervised_engagement_context(text)
    assert "Remaining Tasks" not in result
    assert "Enumerate users" not in result
    assert "Listed callbacks" in result
    assert "BLOCKER" in result


def test_clean_text_unchanged():
    text = (
        "Callback 1 is active on CASTELBLACK as NORTH\\samwell.tarly.\n"
        "Integrity level: Medium. Process: apollo.exe (PID 4812).\n"
        "Kerberos tickets: 3 TGTs cached."
    )
    result = _strip_supervised_engagement_context(text)
    assert result == text


def test_empty_string():
    assert _strip_supervised_engagement_context("") == ""


def test_done_and_failed_preserved():
    text = (
        "**DONE:** getprivs task 59 returned medium integrity.\n\n"
        "**FAILED:** ticket_cache_list parsing error on callback 2.\n\n"
        "**REMAINING:** Try a different callback.\n\n"
        "**BLOCKER:** Callback 2 is dead."
    )
    result = _strip_supervised_engagement_context(text)
    assert "DONE" in result
    assert "FAILED" in result
    assert "BLOCKER" in result
    assert "REMAINING" not in result
    assert "different callback" not in result
