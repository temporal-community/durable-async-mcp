# ABOUTME: Tests for the MCP CLI client's LLM integration layer.
# Covers tool schema conversion, Conversation state management, and task-required detection.

import json
from unittest.mock import MagicMock

import pytest

from async_mcp.mcp_client.llm import (
    Conversation,
    FunctionCall,
    is_task_required,
    mcp_tool_to_openai_function,
    parse_response,
)


def _make_mcp_tool(
    name="test_tool",
    description="A test tool",
    input_schema=None,
    task_support=None,
):
    """Create a mock MCP Tool object."""
    tool = MagicMock()
    tool.name = name
    tool.description = description
    tool.inputSchema = input_schema or {
        "type": "object",
        "properties": {"arg1": {"type": "string"}},
        "required": ["arg1"],
    }
    if task_support is not None:
        tool.execution = MagicMock()
        tool.execution.taskSupport = task_support
    else:
        tool.execution = None
    return tool


# -- Tool schema conversion tests --


class TestMcpToolToOpenaiFunction:
    def test_basic_conversion(self):
        tool = _make_mcp_tool(
            name="process_invoice",
            description="Process an invoice",
            input_schema={
                "type": "object",
                "properties": {
                    "invoice": {"type": "object"},
                },
                "required": ["invoice"],
            },
        )

        result = mcp_tool_to_openai_function(tool)

        assert result["type"] == "function"
        assert result["name"] == "process_invoice"
        assert result["description"] == "Process an invoice"
        assert result["parameters"] == tool.inputSchema

    def test_empty_description_defaults_to_empty_string(self):
        tool = _make_mcp_tool(description=None)
        result = mcp_tool_to_openai_function(tool)
        assert result["description"] == ""

    def test_preserves_complex_schema(self):
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "tags": {"type": "array", "items": {"type": "string"}},
                "nested": {
                    "type": "object",
                    "properties": {"x": {"type": "integer"}},
                },
            },
            "required": ["name"],
        }
        tool = _make_mcp_tool(input_schema=schema)
        result = mcp_tool_to_openai_function(tool)
        assert result["parameters"] == schema


# -- is_task_required tests --


class TestIsTaskRequired:
    def test_required_returns_true(self):
        tool = _make_mcp_tool(task_support="required")
        assert is_task_required(tool) is True

    def test_optional_returns_false(self):
        tool = _make_mcp_tool(task_support="optional")
        assert is_task_required(tool) is False

    def test_forbidden_returns_false(self):
        tool = _make_mcp_tool(task_support="forbidden")
        assert is_task_required(tool) is False

    def test_no_execution_returns_false(self):
        tool = _make_mcp_tool(task_support=None)
        assert is_task_required(tool) is False

    def test_none_task_support_returns_false(self):
        tool = _make_mcp_tool()
        tool.execution = MagicMock()
        tool.execution.taskSupport = None
        assert is_task_required(tool) is False


# -- Conversation state tests --


class TestConversation:
    def test_empty_conversation(self):
        conv = Conversation()
        assert conv.get_input() == []

    def test_system_prompt(self):
        conv = Conversation(system_prompt="You are helpful.")
        inputs = conv.get_input()
        assert len(inputs) == 1
        assert inputs[0]["role"] == "developer"
        assert inputs[0]["content"] == "You are helpful."

    def test_add_user_message(self):
        conv = Conversation()
        conv.add_user_message("Hello")
        inputs = conv.get_input()
        assert len(inputs) == 1
        assert inputs[0] == {"role": "user", "content": "Hello"}

    def test_add_response_output(self):
        conv = Conversation()
        output_items = [{"type": "message", "content": "Hi"}]
        conv.add_response_output(output_items)
        inputs = conv.get_input()
        assert inputs == output_items

    def test_add_function_output(self):
        conv = Conversation()
        conv.add_function_output("call_123", '{"result": "ok"}')
        inputs = conv.get_input()
        assert len(inputs) == 1
        assert inputs[0] == {
            "type": "function_call_output",
            "call_id": "call_123",
            "output": '{"result": "ok"}',
        }

    def test_full_conversation_flow(self):
        """Simulate a multi-turn conversation."""
        conv = Conversation(system_prompt="Be helpful.")
        conv.add_user_message("Process invoice X")
        conv.add_response_output([{"type": "function_call", "id": "fc1"}])
        conv.add_function_output("fc1", "Invoice processed")
        conv.add_response_output([{"type": "message", "text": "Done!"}])

        inputs = conv.get_input()
        assert len(inputs) == 5
        assert inputs[0]["role"] == "developer"
        assert inputs[1]["role"] == "user"
        assert inputs[2]["type"] == "function_call"
        assert inputs[3]["type"] == "function_call_output"
        assert inputs[4]["type"] == "message"


# -- parse_response tests --


class TestParseResponse:
    def test_text_only_response(self):
        response = MagicMock()
        message_content = MagicMock()
        message_content.text = "Hello there!"
        message_item = MagicMock()
        message_item.type = "message"
        message_item.content = [message_content]
        response.output = [message_item]

        text, calls = parse_response(response)
        assert text == "Hello there!"
        assert calls == []

    def test_function_call_response(self):
        response = MagicMock()
        fc_item = MagicMock()
        fc_item.type = "function_call"
        fc_item.name = "process_invoice"
        fc_item.arguments = '{"invoice_id": "INV-001"}'
        fc_item.call_id = "call_abc"
        response.output = [fc_item]

        text, calls = parse_response(response)
        assert text is None
        assert len(calls) == 1
        assert calls[0].name == "process_invoice"
        assert calls[0].arguments == {"invoice_id": "INV-001"}
        assert calls[0].call_id == "call_abc"

    def test_mixed_text_and_function_calls(self):
        response = MagicMock()
        message_content = MagicMock()
        message_content.text = "Let me process that."
        message_item = MagicMock()
        message_item.type = "message"
        message_item.content = [message_content]

        fc_item = MagicMock()
        fc_item.type = "function_call"
        fc_item.name = "some_tool"
        fc_item.arguments = "{}"
        fc_item.call_id = "call_xyz"

        response.output = [message_item, fc_item]

        text, calls = parse_response(response)
        assert text == "Let me process that."
        assert len(calls) == 1
        assert calls[0].name == "some_tool"

    def test_empty_response(self):
        response = MagicMock()
        response.output = []
        text, calls = parse_response(response)
        assert text is None
        assert calls == []

    def test_multiple_function_calls(self):
        response = MagicMock()
        fc1 = MagicMock()
        fc1.type = "function_call"
        fc1.name = "tool_a"
        fc1.arguments = '{"x": 1}'
        fc1.call_id = "call_1"

        fc2 = MagicMock()
        fc2.type = "function_call"
        fc2.name = "tool_b"
        fc2.arguments = '{"y": 2}'
        fc2.call_id = "call_2"

        response.output = [fc1, fc2]

        text, calls = parse_response(response)
        assert text is None
        assert len(calls) == 2
        assert calls[0].name == "tool_a"
        assert calls[1].name == "tool_b"
