# Plan: Build a Simple MCP CLI Client

## Context

No existing MCP GUI/IDE client supports the Tasks protocol (SEP-1686). We need a client that can demo the full invoice processing flow: start a task-enabled tool, poll for status, handle elicitation (approval), and get the final result. The client uses an LLM (OpenAI Responses API) to drive tool selection and a FastMCP `Client` for MCP communication.

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `mcp_client/__init__.py` | Create | Package init |
| `mcp_client/__main__.py` | Create | Module entry point for `python -m mcp_client` |
| `mcp_client/main.py` | Create | Entry point: config loading, MCP connection, chat loop, elicitation handler, client-side tools |
| `mcp_client/llm.py` | Create | OpenAI Responses API: tool schema conversion, conversation state, API calls |
| `client_config.json` | Create | Sample config (Claude Desktop format) for the invoice processor server |
| `pyproject.toml` | Modify | Add `openai>=1.0.0` dependency |
| `tests/test_mcp_client.py` | Create | Tests for tool conversion, conversation management |

## Config Format (`client_config.json`)

Identical to Claude Desktop's format:

```json
{
  "mcpServers": {
    "invoice_processor": {
      "command": "python",
      "args": ["server.py"],
      "env": {
        "TEMPORAL_ADDRESS": "localhost:7233"
      }
    }
  }
}
```

Passed directly to `fastmcp.Client(config_dict)`, which creates the appropriate stdio transport(s). Single-server configs use direct transport (no tool name prefixing); multi-server configs prefix tool names with server name.

## Architecture

### `mcp_client/llm.py` — OpenAI Integration

**`mcp_tool_to_openai_function(tool: mcp.types.Tool) -> dict`**
Converts one MCP tool to OpenAI function format. MCP's `tool.inputSchema` (JSON Schema) maps directly to OpenAI's `parameters` field:
```python
{"type": "function", "name": tool.name, "description": tool.description, "parameters": tool.inputSchema}
```

**`is_task_required(tool: mcp.types.Tool) -> bool`**
Checks `tool.execution.taskSupport` for `"required"`. Returns False if execution/taskSupport is None.

**`class Conversation`**
Manages the input list for OpenAI's Responses API:
- `add_user_message(text)` — appends `{"role": "user", "content": text}`
- `add_response_output(output_items)` — appends raw output items from the response (the Responses API accepts its own output items as input for the next turn)
- `add_function_output(call_id, output)` — appends `{"type": "function_call_output", "call_id": ..., "output": ...}`
- `get_input() -> list` — returns the full history

**`async def call_llm(client, conversation, tools, model) -> response`**
Calls `await client.responses.create(model=model, input=conversation.get_input(), tools=tools)`.

**`parse_response(response) -> tuple[str | None, list[FunctionCall]]`**
Extracts text content and function call items from `response.output`. Each function call has `name`, `arguments` (parsed from JSON), and `call_id`.

### `mcp_client/main.py` — MCP + Chat Loop

**Config loading**: `load_config(path) -> dict` — reads JSON, returns the config dict. Default path: `./client_config.json`, overridden via `--config`.

**Elicitation handler**: `handle_elicitation(message, response_type, params, context) -> ElicitResult`
Called by FastMCP Client when the server sends an `ElicitRequest` during task execution.
1. Prints the message to stdout
2. Inspects `params.requestedSchema` for enum choices (e.g. `["approve", "reject"]`)
3. Prompts user via `asyncio.to_thread(input, ...)` (non-blocking to event loop)
4. Returns `ElicitResult(action="accept", content={field: choice})` or `ElicitResult(action="cancel")`

**Tool execution**: `execute_tool(client, tool_name, arguments, is_task) -> str`
- Client-side `list_tasks`: calls `client.list_tasks()` (`tasks/list` protocol), formats results
- Client-side `resume_task(task_id)`: calls `_poll_and_resolve_task()` on an existing task
- If `is_task`: `task = await client.call_tool(name, args, task=True)` → `ToolTask`
  - Print task ID
  - Call `_poll_and_resolve_task(client, task.task_id)` — see below
  - Return result text
- If not task: `result = await client.call_tool(name, args)` → `CallToolResult`
  - Return result text

**`_poll_and_resolve_task(client, task_id) -> str`** (shared by process_invoice and resume_task)
- Polls `client.get_task_status(task_id)` directly (NOT `task.status()` which caches stale results)
- Breaks when status is `input_required` or terminal (`completed`/`failed`/`cancelled`)
- Calls `client.get_task_result(task_id)` which sends `tasks/result` to the server — for `input_required` this triggers elicitation, for terminal states it returns the outcome
- Parses via `mcp.types.CallToolResult.model_validate()`

**Why not use ToolTask.result()?**
`ToolTask.result()` internally calls `wait()` with no state argument, which only unblocks on terminal states (`completed`/`failed`/`cancelled`). It never acts on `input_required`, so the task hangs at PENDING-APPROVAL forever. Additionally, `task.status()` caches the first response and never re-queries the server.

**Chat loop**: `chat_loop(client, openai_client, tools, conversation)`

```
while True:
    user_input = await asyncio.to_thread(input, "You: ")
    if user_input.strip().lower() in EXIT_COMMANDS:
        break
    conversation.add_user_message(user_input)

    # Inner loop: LLM may make multiple rounds of tool calls
    while True:
        response = await call_llm(openai_client, conversation, openai_tools)
        text, tool_calls = parse_response(response)
        conversation.add_response_output(response.output)

        if text:
            print(f"Assistant: {text}")
        if not tool_calls:
            break

        for tc in tool_calls:
            args_str = json.dumps(tc.arguments, separators=(",", ":")) if tc.arguments else ""
            print(f"  [Tool call: {tc.name}({args_str})]")
            result = await execute_tool(mcp_client, tc.name, tc.arguments, is_task_map.get(tc.name, False))
            print(f"  [Result: {result[:200]}]")
            conversation.add_function_output(tc.call_id, result)
        # Loop back to send tool results to LLM
```

**`async def main()`**: Entry point.
1. Parse `--config` and `--model` args
2. Load config
3. Create `fastmcp.Client(config, elicitation_handler=handle_elicitation)`
4. `async with client:` — connects to all MCP servers
5. List tools, build OpenAI tool list, append client-side tools (`LIST_TASKS_TOOL`, `RESUME_TASK_TOOL`), build task-required lookup map
6. Create `AsyncOpenAI()` client and `Conversation` with system prompt
7. Run `chat_loop(...)`

### Task Protocol Flow (end to end)

```
User types "Process invoice INV-001"
  → OpenAI returns function_call: process_invoice({...})
  → execute_tool detects task-required
  → client.call_tool("process_invoice", args, task=True) → ToolTask
  → _poll_and_resolve_task(client, task.task_id):
    → client.get_task_status() polls tasks/get → "working"
    → client.get_task_status() polls tasks/get → "working"
    → client.get_task_status() polls tasks/get → "input_required" ← breaks
    → client.get_task_result() sends tasks/result → server triggers elicitation
      → handle_elicitation prints "Invoice INV-001... approve/reject?"
      → User types "approve"
      → ElicitResult flows back to server
      → Server signals workflow, awaits completion
      → Server returns final CallToolResult
  → returns "Invoice processing result: PAID"
  → Result added to conversation as function_call_output
  → OpenAI generates text: "The invoice has been processed and paid."
```

### Resume Task Flow (list → approve existing)

```
User types "What are the open invoices?"
  → OpenAI returns function_call: list_tasks()
  → execute_tool calls client.list_tasks() → tasks/list protocol
  → Returns formatted list of active tasks with IDs and statuses
  → OpenAI presents the list to user

User types "Approve that invoice"
  → OpenAI returns function_call: resume_task(task_id="invoice-...")
  → execute_tool calls _poll_and_resolve_task(client, task_id)
  → Same poll → get_task_result → elicitation flow as above
```

## Error Handling

| Boundary | Approach |
|----------|----------|
| Config file missing/invalid | Let FileNotFoundError / json.JSONDecodeError propagate. Crash on startup. |
| MCP server won't connect | FastMCP Client raises on `__aenter__`. Print which server failed, crash. |
| OPENAI_API_KEY missing | AsyncOpenAI raises on first call. Let it propagate. |
| OpenAI API error mid-chat | Catch `openai.APIError` in inner loop, print error, continue to next user input. |
| Tool call fails (McpError) | Catch in execute_tool, return error text as tool result so LLM sees it. |
| Elicitation input EOF | Return `ElicitResult(action="cancel")`. |
| KeyboardInterrupt | Caught at top level, prints "Goodbye!" cleanly. |

## Dependencies

Add to `pyproject.toml`:
```toml
"openai>=1.0.0",
```

## Verification

1. **Unit tests**: Tool schema conversion (MCP → OpenAI), Conversation state management, is_task_required detection
2. **Manual integration test**:
   - Start Temporal dev server + worker (in separate terminals)
   - Run `python -m mcp_client --config client_config.json`
   - Type "Process this invoice: {invoice_id: INV-001, customer: Acme, lines: [{description: Widget, amount: 100, due_date: 2026-03-01}]}"
   - Verify: tool call is made, task starts, polling shows status, elicitation prompts for approval, final result is returned
   - Type "exit" to quit
