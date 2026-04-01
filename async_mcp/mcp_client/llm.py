# ABOUTME: OpenAI Responses API integration for the MCP CLI client.
# Handles tool schema conversion (MCP→OpenAI), conversation state, and LLM calls.

import json
from dataclasses import dataclass, field

import mcp.types
import openai


def mcp_tool_to_openai_function(tool: mcp.types.Tool) -> dict:
    """Convert an MCP tool definition to OpenAI function-calling format."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description or "",
        "parameters": tool.inputSchema,
    }


def is_task_required(tool: mcp.types.Tool) -> bool:
    """Check if an MCP tool requires the task protocol."""
    return (
        tool.execution is not None and tool.execution.taskSupport == "required"
    )


@dataclass
class FunctionCall:
    """A parsed function call from an LLM response."""

    name: str
    arguments: dict
    call_id: str


class Conversation:
    """Manages the input list for OpenAI's Responses API.

    The Responses API accepts its own output items as input for the next turn,
    so we accumulate user messages, response outputs, and function results.
    """

    def __init__(self, system_prompt: str | None = None):
        self._input: list = []
        if system_prompt:
            self._input.append(
                {"role": "developer", "content": system_prompt}
            )

    def add_user_message(self, text: str) -> None:
        self._input.append({"role": "user", "content": text})

    def add_response_output(self, output_items: list) -> None:
        self._input.extend(output_items)

    def add_function_output(self, call_id: str, output: str) -> None:
        self._input.append(
            {"type": "function_call_output", "call_id": call_id, "output": output}
        )

    def get_input(self) -> list:
        return self._input


async def call_llm(
    client: openai.AsyncOpenAI,
    conversation: Conversation,
    tools: list[dict],
    model: str = "gpt-4o",
) -> object:
    """Call the OpenAI Responses API with the current conversation and tools."""
    return await client.responses.create(
        model=model,
        input=conversation.get_input(),
        tools=tools,
    )


def parse_response(response) -> tuple[str | None, list[FunctionCall]]:
    """Extract text content and function calls from an OpenAI response.

    Returns (text, function_calls) where text is the concatenated message
    content (or None) and function_calls is a list of parsed FunctionCall objects.
    """
    text_parts = []
    function_calls = []

    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if hasattr(content, "text"):
                    text_parts.append(content.text)
        elif item.type == "function_call":
            function_calls.append(
                FunctionCall(
                    name=item.name,
                    arguments=json.loads(item.arguments),
                    call_id=item.call_id,
                )
            )

    text = "\n".join(text_parts) if text_parts else None
    return text, function_calls
