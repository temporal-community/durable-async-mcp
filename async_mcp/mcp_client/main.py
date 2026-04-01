# ABOUTME: Entry point for the MCP CLI client.
# Connects to MCP servers, runs a chat loop with LLM-driven tool calling, and handles elicitation.

import argparse
import asyncio
import json
import sys

import mcp.types as mcp_types
import openai
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

from async_mcp.mcp_client.llm import (
    Conversation,
    FunctionCall,
    call_llm,
    is_task_required,
    mcp_tool_to_openai_function,
    parse_response,
)

EXIT_COMMANDS = {"exit", "quit", "q", "bye"}


SYSTEM_PROMPT = (
    "You are a helpful assistant that can process invoices. "
    "You have access to tools provided via MCP (Model Context Protocol). "
    "When a user asks you to process an invoice, use the process_invoice tool. "
    "Invoice JSON should have: invoice_id, customer, and lines "
    "(each with description, amount, due_date in ISO 8601 format). "
    "When a user asks about open or active invoices, use the list_tasks tool. "
    "When a user wants to process, approve, or continue an existing invoice "
    "from the task list, use the resume_task tool with its task ID. "
    "When a user asks about a specific invoice status and you have a workflow ID, "
    "use the invoice_status tool."
)

# Client-side tool exposed to the LLM that maps to the MCP tasks/list protocol.
# The server's task handlers return active invoice workflows from Temporal.
LIST_TASKS_TOOL = {
    "type": "function",
    "name": "list_tasks",
    "description": (
        "List all active invoice processing tasks. Returns task IDs, "
        "statuses, and status messages for running invoice workflows."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

RESUME_TASK_TOOL = {
    "type": "function",
    "name": "resume_task",
    "description": (
        "Resume an existing invoice task by its task ID. Polls for status "
        "and handles approval when the task requires input. Use this when "
        "the user wants to process, approve, or continue an invoice that "
        "is already in the system (e.g. from list_tasks results)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task ID (workflow ID) of the invoice to resume.",
            },
        },
        "required": ["task_id"],
    },
}


def load_config(path: str) -> dict:
    """Load an MCP server config file (Claude Desktop format)."""
    with open(path) as f:
        return json.load(f)


async def handle_elicitation(message, response_type, params, context):
    """Handle elicitation requests from the MCP server.

    Prints the message and any enum choices, prompts the user for input,
    and returns an ElicitResult.
    """
    print(f"\n--- Server needs input ---\n{message}")

    # Extract field names and enum choices from the requested schema
    schema = getattr(params, "requestedSchema", None) or {}
    properties = schema.get("properties", {})

    field_responses = {}
    for field_name, field_schema in properties.items():
        enum_values = field_schema.get("enum")
        if enum_values:
            options_str = " / ".join(str(v) for v in enum_values)
            prompt = f"  {field_name} [{options_str}]: "
        else:
            prompt = f"  {field_name}: "

        try:
            value = await asyncio.to_thread(input, prompt)
            field_responses[field_name] = value.strip()
        except EOFError:
            print("\n  (Input closed, cancelling)")
            return ElicitResult(action="cancel")

    if not field_responses:
        # No schema fields — simple confirm/cancel
        try:
            value = await asyncio.to_thread(input, "  Your response: ")
            field_responses["response"] = value.strip()
        except EOFError:
            return ElicitResult(action="cancel")

    print("--- Input submitted ---\n")
    return ElicitResult(action="accept", content=field_responses)


def _result_to_text(result) -> str:
    """Extract text from a CallToolResult."""
    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts) if parts else str(result)


TERMINAL_TASK_STATES = {"completed", "failed", "cancelled"}
POLL_INTERVAL = 2  # seconds


async def _poll_and_resolve_task(client: Client, task_id: str) -> str:
    """Poll a task until it leaves 'working' state, then call tasks/result.

    For input_required tasks, tasks/result triggers elicitation on the server.
    For terminal tasks, tasks/result returns the final outcome.
    """
    print("  [Waiting for task to complete (will prompt if approval needed)...]")
    while True:
        status_result = await client.get_task_status(task_id)
        current = status_result.status
        print(f"  [Polling... status: {current}]")
        if current in TERMINAL_TASK_STATES or current == "input_required":
            break
        await asyncio.sleep(POLL_INTERVAL)

    raw_result = await client.get_task_result(task_id)
    mcp_result = mcp_types.CallToolResult.model_validate(raw_result)
    return _result_to_text(mcp_result)


async def execute_tool(client: Client, tool_name: str, arguments: dict, is_task: bool) -> str:
    """Execute an MCP tool, using the task protocol if required.

    Client-side tools (list_tasks, resume_task) map to MCP task protocol
    operations. Task-enabled tools poll for status and trigger elicitation
    when input_required.
    """
    try:
        if tool_name == "list_tasks":
            result = await client.list_tasks()
            tasks = result.get("tasks", [])
            if not tasks:
                return "No active invoice tasks."
            lines = []
            for t in tasks:
                tid = t.get("taskId", "?")
                status = t.get("status", "?")
                msg = t.get("statusMessage", "")
                lines.append(f"  {tid}: {status} — {msg}")
            return f"Active tasks ({len(tasks)}):\n" + "\n".join(lines)

        if tool_name == "resume_task":
            task_id = arguments.get("task_id", "")
            if not task_id:
                return "Error: task_id is required."
            print(f"  [Resuming task: {task_id}]")
            return await _poll_and_resolve_task(client, task_id)

        if is_task:
            task = await client.call_tool(tool_name, arguments, task=True)
            print(f"  [Task started: {task.task_id}]")

            if task.returned_immediately:
                return _result_to_text(await task.result())

            return await _poll_and_resolve_task(client, task.task_id)
        else:
            result = await client.call_tool(tool_name, arguments)
            return _result_to_text(result)
    except Exception as e:
        return f"Error calling tool {tool_name}: {e}"


async def chat_loop(
    mcp_client: Client,
    openai_client: openai.AsyncOpenAI,
    openai_tools: list[dict],
    is_task_map: dict[str, bool],
    conversation: Conversation,
    model: str,
) -> None:
    """Run the interactive chat loop."""
    print("\nMCP Client ready. Type a message or 'exit' to quit.\n")

    while True:
        try:
            user_input = await asyncio.to_thread(input, "You: ")
        except (EOFError, KeyboardInterrupt):
            break

        if user_input.strip().lower() in EXIT_COMMANDS:
            break

        conversation.add_user_message(user_input)

        # Inner loop: LLM may make multiple rounds of tool calls
        while True:
            try:
                response = await call_llm(openai_client, conversation, openai_tools, model)
            except openai.APIError as e:
                print(f"  [OpenAI API error: {e}]")
                break

            text, tool_calls = parse_response(response)
            conversation.add_response_output(response.output)

            if text:
                print(f"Assistant: {text}")

            if not tool_calls:
                break

            for tc in tool_calls:
                args_str = json.dumps(tc.arguments, separators=(",", ":")) if tc.arguments else ""
                print(f"  [Tool call: {tc.name}({args_str})]")
                result = await execute_tool(
                    mcp_client, tc.name, tc.arguments, is_task_map.get(tc.name, False)
                )
                print(f"  [Result: {result[:200]}]")
                conversation.add_function_output(tc.call_id, result)


async def main() -> None:
    """Entry point: parse args, connect to MCP servers, run chat loop."""
    parser = argparse.ArgumentParser(description="MCP CLI Client with LLM-driven tool calling")
    parser.add_argument(
        "--config",
        default="async_mcp/client_config.json",
        help="Path to MCP server config file (Claude Desktop format)",
    )
    parser.add_argument(
        "--model",
        default="gpt-4o",
        help="OpenAI model to use (default: gpt-4o)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    mcp_client = Client(config, elicitation_handler=handle_elicitation)

    async with mcp_client:
        tools = await mcp_client.list_tools()
        print(f"Connected. Available tools: {[t.name for t in tools]}")

        openai_tools = [mcp_tool_to_openai_function(t) for t in tools]
        openai_tools.append(LIST_TASKS_TOOL)
        openai_tools.append(RESUME_TASK_TOOL)
        is_task_map = {t.name: is_task_required(t) for t in tools}
        is_task_map["list_tasks"] = False
        is_task_map["resume_task"] = False

        task_tools = [name for name, is_task in is_task_map.items() if is_task]
        if task_tools:
            print(f"Task-enabled tools: {task_tools}")

        openai_client = openai.AsyncOpenAI()
        conversation = Conversation(system_prompt=SYSTEM_PROMPT)

        await chat_loop(
            mcp_client, openai_client, openai_tools, is_task_map, conversation, args.model
        )

    print("Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
        sys.exit(0)
