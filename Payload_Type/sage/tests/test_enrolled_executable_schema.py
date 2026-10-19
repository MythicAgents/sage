"""The enrolled verb changes only the Operator's model-facing issue schema."""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain.agents import create_agent
from langchain_aws.chat_models.bedrock_converse import _format_tools
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai.langgraph.model import _EnrolledExecutableSchemaMiddleware  # noqa: E402
from ai.langgraph.executable_contracts import ContractValidationError, load_executable_contract  # noqa: E402


ISSUE = "issue_task_and_waitfor_task_output"
SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "const": "whoami"},
        "callback_display_id": {"type": "integer", "const": 7},
        "parameters": {
            "type": "object",
            "properties": {"arguments": {"type": "string"}},
            "required": ["arguments"],
            "additionalProperties": False,
        },
    },
    "required": ["command", "callback_display_id", "parameters"],
    "additionalProperties": False,
}


async def issue_task_and_waitfor_task_output(
    command: str, parameters: str | dict, callback_display_id: int
) -> str:
    """Existing task handler."""
    return "unused"


async def list_callbacks() -> str:
    """Existing read tool."""
    return "unused"


@dataclass
class _Request:
    tools: list

    def override(self, *, tools):
        return _Request(tools)


def _fixture(provider="openai"):
    client = SimpleNamespace(get_enrolled_issue_schema=Mock(return_value=copy.deepcopy(SCHEMA)))
    model = SimpleNamespace(mythic_client=client, provider=provider)
    issue = StructuredTool.from_function(coroutine=issue_task_and_waitfor_task_output)
    other = StructuredTool.from_function(coroutine=list_callbacks)
    return _EnrolledExecutableSchemaMiddleware(model), client, issue, other


def test_enrolled_schema_reaches_real_model_binding_without_mutating_registered_tools():
    middleware, client, issue, other = _fixture()
    initial_schema = issue.tool_call_schema.model_json_schema()
    request = _Request([issue, other])
    bound_request = middleware._with_enrolled_schema(request)

    assert bound_request is not request
    assert request.tools == [issue, other]
    assert bound_request.tools[1] is other
    assert bound_request.tools[0]["function"]["name"] == ISSUE
    assert bound_request.tools[0]["function"]["parameters"] == SCHEMA
    assert bound_request.tools[0]["function"]["strict"] is True
    assert issue.tool_call_schema.model_json_schema() == initial_schema

    # This is the installed LangChain OpenAI adapter's actual binding, without a provider call.
    llm = ChatOpenAI(model="gpt-4o-mini", api_key="sk-fake", base_url="http://127.0.0.1:9/v1")
    wired = llm.bind_tools(bound_request.tools)
    function = next(tool["function"] for tool in wired.kwargs["tools"] if tool["function"]["name"] == ISSUE)
    assert function["parameters"]["properties"]["parameters"]["properties"]["arguments"] == {"type": "string"}
    assert function["strict"] is True

    client.get_enrolled_issue_schema.return_value = None
    assert middleware._with_enrolled_schema(request) is request


def test_schema_switches_per_model_call_and_other_provider_omits_strict():
    middleware, client, issue, other = _fixture()
    request = _Request([issue, other])
    first = middleware._with_enrolled_schema(request)
    assert first.tools[0]["function"]["strict"] is True

    changed = copy.deepcopy(SCHEMA)
    changed["properties"]["command"]["const"] = "pwd"
    client.get_enrolled_issue_schema.return_value = changed
    second = middleware._with_enrolled_schema(request)
    assert second.tools[0]["function"]["parameters"]["properties"]["command"]["const"] == "pwd"
    assert first.tools[0]["function"]["parameters"]["properties"]["command"]["const"] == "whoami"
    assert issue.tool_call_schema.model_json_schema()["properties"]["command"]["type"] == "string"

    bedrock, _, _, _ = _fixture(provider="bedrock")
    assert "strict" not in bedrock._with_enrolled_schema(request).tools[0]["function"]
    other_provider, _, _, _ = _fixture(provider="anthropic")
    assert "strict" not in other_provider._with_enrolled_schema(request).tools[0]["function"]


def test_async_model_call_passes_only_replaced_request_to_handler():
    middleware, _, issue, other = _fixture()
    request = _Request([issue, other])
    seen = []

    async def handler(bound):
        seen.append(bound)
        return "response"

    assert asyncio.run(middleware.awrap_model_call(request, handler)) == "response"
    assert seen[0].tools[0]["function"]["parameters"] == SCHEMA
    assert seen[0].tools[1] is other


def test_invalid_enrollment_keeps_generic_handler_schema():
    middleware, client, issue, other = _fixture()
    client.get_enrolled_issue_schema.return_value = {"type": "object", "properties": {"parameters": {}}}
    request = _Request([issue, other])
    assert middleware._with_enrolled_schema(request) is request


def test_bedrock_nullable_type_array_becomes_optional_only_in_model_copy():
    middleware, client, issue, other = _fixture(provider="bedrock")
    schema = copy.deepcopy(SCHEMA)
    nested = schema["properties"]["parameters"]
    nested["properties"]["optional_flag"] = {"type": ["boolean", "null"]}
    nested["required"].append("optional_flag")
    client.get_enrolled_issue_schema.return_value = schema

    bound = middleware._with_enrolled_schema(_Request([issue, other]))
    bound_nested = bound.tools[0]["function"]["parameters"]["properties"]["parameters"]
    assert bound_nested["properties"]["optional_flag"] == {"type": "boolean"}
    assert bound_nested["required"] == ["arguments"]
    assert schema["properties"]["parameters"]["properties"]["optional_flag"] == {"type": ["boolean", "null"]}
    assert schema["properties"]["parameters"]["required"] == ["arguments", "optional_flag"]


def test_unsupported_keyword_projection_preserves_same_named_command_property():
    middleware, client, issue, other = _fixture()
    schema = copy.deepcopy(SCHEMA)
    nested = schema["properties"]["parameters"]
    nested["properties"]["minimum"] = {"type": "string", "minLength": 1}
    nested["required"].append("minimum")
    client.get_enrolled_issue_schema.return_value = schema

    bound = middleware._with_enrolled_schema(_Request([issue, other]))
    bound_nested = bound.tools[0]["function"]["parameters"]["properties"]["parameters"]
    assert "minimum" in bound_nested["properties"]
    assert "minimum" in bound_nested["required"]
    assert bound_nested["properties"]["minimum"]["description"] == "Minimum length: 1."
    assert "minLength" not in bound_nested["properties"]["minimum"]
    assert nested["properties"]["minimum"]["minLength"] == 1


class _RecordingModel(GenericFakeChatModel):
    bound_specs: list[list] = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        self.bound_specs.append(list(tools))
        return self


def test_real_agent_executes_typed_same_name_call_then_restores_generic_schema():
    contract = load_executable_contract(
        Path(__file__).resolve().parents[1] / "ttps" / "contracts" / "certify.json",
        "7f949a895c51e747fa334b380a9511c4442ad5d0",
    )
    invocation_schema = contract.strict_schema_for_verb("request")
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "enum": ["execute_assembly"]},
            "callback_display_id": {"type": "integer", "enum": [7]},
            "parameters": {
                "type": "object",
                "properties": {
                    "assembly_name": {"type": "string", "enum": ["Certify.exe"]},
                    "assembly_arguments": invocation_schema,
                },
                "required": ["assembly_name", "assembly_arguments"],
                "additionalProperties": False,
            },
        },
        "required": ["command", "callback_display_id", "parameters"],
        "additionalProperties": False,
    }
    options = {name: None for name in invocation_schema["properties"]["options"]["properties"]}
    options.update(ca=r"SERVER\CA", template="User")
    typed_args = {
        "command": "execute_assembly",
        "callback_display_id": 7,
        "parameters": {
            "assembly_name": "Certify.exe",
            "assembly_arguments": {"verb": "request", "options": options},
        },
    }
    received = []

    async def issue(command: str, parameters: str | dict, callback_display_id: int) -> str:
        received.append((command, parameters, callback_display_id))
        return "issued by existing handler"

    issue_tool = StructuredTool.from_function(coroutine=issue, name=ISSUE, description="Existing handler")
    client = SimpleNamespace(get_enrolled_issue_schema=Mock(return_value=schema))
    middleware = _EnrolledExecutableSchemaMiddleware(SimpleNamespace(mythic_client=client, provider="openai"))
    fake = _RecordingModel(messages=iter([
        AIMessage(content="", tool_calls=[{"name": ISSUE, "args": typed_args, "id": "typed-call"}]),
        AIMessage(content="typed done"),
        AIMessage(content="", tool_calls=[{
            "name": ISSUE,
            "args": {"command": "whoami", "callback_display_id": 7, "parameters": ""},
            "id": "generic-call",
        }]),
        AIMessage(content="generic done"),
    ]))
    agent = create_agent(model=fake, tools=[issue_tool], middleware=[middleware])

    first = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="typed")]}))
    assert any(isinstance(message, ToolMessage) and "issued by existing handler" in message.content
               for message in first["messages"])
    assert received[0] == ("execute_assembly", typed_args["parameters"], 7)
    openai_tool = fake.bound_specs[0][0]
    assert openai_tool["function"]["strict"] is True
    adapter = ChatOpenAI(model="gpt-4o-mini", api_key="sk-fake", base_url="http://127.0.0.1:9/v1")
    wire_tool = adapter.bind_tools([openai_tool]).kwargs["tools"][0]
    wire_schema = wire_tool["function"]["parameters"]
    assert wire_schema["required"] == ["command", "callback_display_id", "parameters"]
    assert wire_schema["additionalProperties"] is False
    assert wire_schema["properties"]["command"]["enum"] == ["execute_assembly"]
    assert wire_schema["properties"]["callback_display_id"]["enum"] == [7]
    wire_options = wire_schema["properties"]["parameters"]["properties"]["assembly_arguments"]["properties"]["options"]
    assert wire_options["additionalProperties"] is False
    assert set(wire_options["required"]) == set(invocation_schema["properties"]["options"]["required"])
    wire_integer = wire_options["properties"]["key-size"]["anyOf"][0]
    assert wire_integer["type"] == "integer"
    assert "minimum" not in wire_integer and "maximum" not in wire_integer
    assert "Minimum value: -2147483648." in wire_integer["description"]
    assert "Maximum value: 2147483647." in wire_integer["description"]
    assert wire_options["properties"]["ca"]["type"] == "string"
    assert "Minimum length: 1." in wire_options["properties"]["ca"]["description"]
    assert "Minimum length: 1." in wire_options["properties"]["upn"]["anyOf"][0]["items"]["description"]
    serialized = json.dumps(wire_schema)
    for unsupported in ('"minimum"', '"maximum"', '"multipleOf"', '"minLength"', '"maxLength"'):
        assert unsupported not in serialized
    assert invocation_schema["properties"]["options"]["properties"]["key-size"]["anyOf"][0]["maximum"] == 2147483647
    assert schema["properties"]["parameters"]["properties"]["assembly_arguments"] == invocation_schema
    with pytest.raises(ContractValidationError, match="2147483647"):
        contract.prepare("request", {"ca": r"SERVER\CA", "template": "User", "key-size": 2**31},
                         artifact_sha1=contract.artifact_sha1)
    with pytest.raises(ContractValidationError, match="at least 1 character"):
        contract.prepare("request", {"ca": "", "template": "User"},
                         artifact_sha1=contract.artifact_sha1)

    client.get_enrolled_issue_schema.return_value = None
    second = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="generic")]}))
    assert any(isinstance(message, ToolMessage) and "issued by existing handler" in message.content
               for message in second["messages"])
    assert received[1] == ("whoami", "", 7)
    assert isinstance(fake.bound_specs[2][0], StructuredTool)
    assert fake.bound_specs[2][0] is issue_tool

    # Bedrock receives a non-strict copy with optional properties omitted from required.
    bedrock = _EnrolledExecutableSchemaMiddleware(SimpleNamespace(
        mythic_client=client, provider="bedrock"))
    client.get_enrolled_issue_schema.return_value = schema
    bedrock_tool = bedrock._with_enrolled_schema(_Request([issue_tool])).tools[0]
    assert "strict" not in bedrock_tool["function"]
    bedrock_spec = _format_tools([bedrock_tool])[0]["toolSpec"]
    converted = bedrock_spec["inputSchema"]["json"]["properties"]["parameters"]["properties"]["assembly_arguments"]
    converted_options = converted["properties"]["options"]["properties"]
    required_options = set(converted["properties"]["options"]["required"])
    assert {"ca", "template"} <= required_options
    assert "subject" not in required_options
    assert "key-size" not in required_options
    assert invocation_schema["properties"]["options"]["properties"]["subject"]["anyOf"][-1] == {"type": "null"}
    assert converted_options["subject"]["type"] == "string"
    assert "anyOf" not in converted_options["subject"]
    prepared = contract.prepare("request", {"ca": r"SERVER\CA", "template": "User"},
                                artifact_sha1=contract.artifact_sha1)
    assert "--key-size 2048" in prepared.argument_string
