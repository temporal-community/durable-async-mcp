# Tool Architecture — Server Tools vs Client-Side Task Protocol Bridges

> **⚠️ Historical / superseded.** Describes the pre-v2 design with client-side tools wrapping
> `tasks/list` / `tasks/result` and the `mcp_client/` CLI — all removed in v2. Current direction: the
> Temporal-backed **Tasks extension** in [`mcp_tasks_temporal/`](../../mcp_tasks_temporal/README.md)
> (generic `TaskTrackerWorkflow`, no app-specific client tools) — see
> [ADR-002](../decisions/002-migrate-to-tasks-extension-v2.md). Kept for history; the v2 approach is the
> *current experiment*, not a permanent move off FastMCP.

## Overview

The LLM sees four tools, but they come from two different sources and are handled differently at execution time.

## Server-Side Tools

These are registered on the MCP server (`server.py`) and discovered by the client via `tools/list`:

| Tool | Task-Enabled | Purpose |
|------|-------------|---------|
| `process_invoice` | Yes (`task=required`) | Starts a Temporal workflow, returns task ID immediately |
| `invoice_status` | No | Queries Temporal workflow directly |

## Client-Side Tools

These are defined in `mcp_client/main.py` (lines 41-70) and appended to the LLM's tool list alongside the server tools. They never touch the MCP server's tool layer — instead they bridge to MCP task protocol operations:

| Client Tool | Bridges To | MCP Protocol Operation | Server Handler |
|-------------|-----------|----------------------|----------------|
| `list_tasks` | `client.list_tasks()` | `tasks/list` (ListTasksRequest) | `handle_tasks_list` |
| `resume_task` | `client.get_task_status()` + `client.get_task_result()` | `tasks/get` + `tasks/result` | `handle_tasks_get` + `handle_tasks_result` |

## Why Client-Side Tools Exist

The LLM only understands "tools." It has no concept of the MCP task protocol underneath. Without these bridge tools, the LLM could start a task via `process_invoice`, but would have no way to:

- Discover existing tasks (e.g., after a connection drop or in a new session)
- Reconnect to a task that's waiting for approval
- List what's currently in-flight

The client-side tools give the LLM a tool-shaped interface for these task protocol operations.

## Execution Flow

```
LLM selects tool
       │
       ├── process_invoice ──> client.call_tool(task=True) ──> MCP tools/call ──> server
       ├── invoice_status  ──> client.call_tool()           ──> MCP tools/call ──> server
       ├── list_tasks      ──> client.list_tasks()          ──> MCP tasks/list ──> server handler
       └── resume_task     ──> client.get_task_status()     ──> MCP tasks/get  ──> server handler
                               client.get_task_result()     ──> MCP tasks/result ──> server handler
```

## Note on Adding New Task-Enabled Tools

If new task-enabled tools are added to the server, the existing `list_tasks` and `resume_task` client-side tools should work for them without changes — the task protocol is tool-agnostic. However, `tasks/list` has no filtering by tool type (see `docs/research/tasks-protocol-gaps.md`), so the flat task list could become harder to navigate.
