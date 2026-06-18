"""Pure-helper tests for the live seams (the error-prone parsing the lab adapters depend on).

The lab-touching factories (make_model_fn/tool_executor/cypher_run) are validated on the range, not
here. These pin the parsing: LLM decision, BloodHound literals, and the domain-count read.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai" / "hillclimb"))

import live_seams as ls  # noqa: E402


class _Msg:
    def __init__(self, tool_calls=None, content=""):
        self.tool_calls = tool_calls or []
        self.content = content


def test_parse_model_decision_tool_call():
    d = ls.parse_model_decision(_Msg(tool_calls=[{"name": "shell", "args": {"command": "whoami"}}]))
    assert d == {"tool": "shell", "args": {"command": "whoami"}}


def test_parse_model_decision_final():
    assert ls.parse_model_decision(_Msg(content="objective complete")) == {"final": "objective complete"}


def test_extract_literals():
    resp = {"data": {"literals": [{"value": "sevenkingdoms.local"}, {"value": "essos.local"}, {"k": "x"}]}}
    assert ls.extract_literals(resp) == ["sevenkingdoms.local", "essos.local"]
    assert ls.extract_literals("garbage") == []
    assert ls.extract_literals({"data": {}}) == []


def test_parse_domain_count():
    assert ls.parse_domain_count("available-domains: 200 count=3 -> ['a','b','c']") == 3
    assert ls.parse_domain_count("available-domains: 200 count=0 -> []") == 0
    assert ls.parse_domain_count("connection refused") == 0


def test_load_sage_defaults(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# Local Sage defaults\nSAGE_PROVIDER=OpenAI\nSAGE_MODEL=gpt-5.5-cyber-preview\n"
                   "SAGE_API_ENDPOINT=http://127.0.0.1:8100/v1\nSAGE_API_KEY=sk-abc\n")
    d = ls.load_sage_defaults(str(env))
    assert d["provider"] == "openai"            # lowercased for init_chat_model
    assert d["model"] == "gpt-5.5-cyber-preview"
    assert d["base_url"] == "http://127.0.0.1:8100/v1"
    assert d["api_key"] == "sk-abc"


def test_apollo_tools_spec_shape():
    spec = ls.apollo_tools_spec([{"cmd": "shell", "description": "run a shell command"}, {"cmd": "whoami"}])
    assert spec[0]["type"] == "function"
    assert spec[0]["function"]["name"] == "shell"
    assert "command_line" in spec[0]["function"]["parameters"]["properties"]


def test_apollo_tools_spec_curated_fallback():
    names = {t["function"]["name"] for t in ls.apollo_tools_spec([])}  # empty -> curated set
    assert "shell" in names and "whoami" in names


_NET_GROUP_OUT = """Group name     Domain Admins
Comment        Designated administrators of the domain

Members

-------------------------------------------------------------------------------
Administrator            samwell.tarly            evil.admin
The command completed successfully.
"""


def test_parse_net_group_members():
    assert ls.parse_net_group_members(_NET_GROUP_OUT) == {"administrator", "samwell.tarly", "evil.admin"}
    assert ls.parse_net_group_members("connection refused") == set()


def test_ad_probe_detects_escalation_vs_baseline():
    grew = lambda call: "Members\n----\nAdministrator  evil.admin\nThe command completed successfully."
    same = lambda call: "Members\n----\nAdministrator\nThe command completed successfully."
    assert ls.ad_domain_admins_probe(grew, "north", baseline={"administrator"})() is True
    assert ls.ad_domain_admins_probe(same, "north", baseline={"administrator"})() is False


def test_ad_probe_win_principal():
    tasker = lambda call: "Members\n----\nAdministrator  pwned\nThe command completed successfully."
    assert ls.ad_domain_admins_probe(tasker, "north", win_principals={"pwned"})() is True
    assert ls.ad_domain_admins_probe(tasker, "north", win_principals={"nope"})() is False
