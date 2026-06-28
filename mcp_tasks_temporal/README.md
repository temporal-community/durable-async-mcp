# mcp_tasks_temporal

A reusable, **Temporal-backed implementation of the MCP task protocol on FastMCP**
(SEP-1686, `fastmcp==2.14.3`). It gives you:

- **Server side** — `register_tasks_extension(mcp, backend, task_tools=...)` overwrites FastMCP's task
  handlers (`tools/call` + `tasks/get` / `tasks/result` / `tasks/cancel`) with ones backed by any
  durable job system via a small `TaskBackend` protocol. HITL is **server-push elicitation**: while a
  task is `input_required`, `tasks/result` pushes an `elicitation/create`.
- **Client side** — a durable client packaged as a **Temporal Plugin**: one `TaskTrackerWorkflow` per
  in-flight task (the durable task handle), adopted with a single `plugins=[...]` entry.

> **Status (2026-06-28):** this package reverted from the v2 alpha (`mcp==2.0.0a2`) pull-based tasks
> *extension* to the FastMCP push-elicitation task protocol. The sections below that describe the v2
> wire types (`wire.py`, `_sdk_compat.py`), `tasks/update`, capability `_meta`, and "Path to native"
> are **historical** — the current API is `tasks/result` push elicitation with `x-task-id` /
> `x-request-key` routing, mirrored in `CLAUDE.md` and `docs/decisions/003-revert-to-v1-fastmcp-tasks.md`.

> **Why this package exists.** The `mcp==2.0.0a2` SDK ships the `extensions` capability container but
> **deliberately omits the tasks extension itself** — this package *is* that implementation
> (intended to become the canonical one). It hand-defines the wire types and registers the `tasks/*`
> handlers. The one wrinkle: `tools/call` returning a bare `CreateTaskResult` is rejected by the
> SDK's result validation (a closed union with no task variant), so a narrow seam (`_sdk_compat`) is
> installed automatically on both ends. That seam is the single core-SDK gap "native tasks" must
> close — see [**Path to native**](#path-to-native-upstreaming-the-seam) and
> [`../docs/research/v2-alpha-spike-findings.md`](../docs/research/v2-alpha-spike-findings.md).

Requires `mcp==2.0.0a2` and `temporalio>=1.19` (for the Plugins API).

The invoice app is the full worked example: server in
[`../invoice_processing_mcp/server/server.py`](../invoice_processing_mcp/server/server.py) + [`../invoice_processing_mcp/server/invoice_backend.py`](../invoice_processing_mcp/server/invoice_backend.py);
client in [`../invoice_processing_mcp/client/`](../invoice_processing_mcp/client/).

---

## Concepts

- **Task** — a long-running `tools/call`. The server returns a `CreateTaskResult` (a handle) instead
  of the answer; the client polls `tasks/get` until terminal.
- **Status** — `working` → `input_required` ↔ `working` → terminal (`completed` / `failed` / `cancelled`).
- **Task state vs. workflow state** — `TaskState` is the MCP-facing state (the five statuses above +
  the protocol payload). Your job/workflow has its *own* state model; the backend maps workflow
  state → `TaskState`. (Distinct from Temporal's durable execution state — `TaskState` is a protocol
  view, not a persistence checkpoint.)
- **Human-in-the-loop** — when a task needs input it goes `input_required` and surfaces an
  `inputRequests` map (e.g. an `elicitation/create`) in `tasks/get`; the client answers with
  `tasks/update` (`inputResponses`). **No server-initiated requests.**
- **Capability gating** — the client opts in per request via
  `_meta.io.modelcontextprotocol/clientCapabilities.extensions["io.modelcontextprotocol/tasks"]`;
  the server must not create a task for a client that didn't declare it.

---

## Server side

Implement `TaskBackend` and wire it onto a lowlevel server.

### 1. Implement `TaskBackend`

The backend's core job is to **map your own workflow/job state to MCP `TaskState`** (status +
protocol payload). Those are two distinct state models; the mapping is the part you write.

```python
from mcp_tasks_temporal.backend import TaskBackend, TaskState
from mcp_tasks_temporal.wire import InputRequest, InputResponse

class MyBackend:  # structural — satisfies the TaskBackend protocol
    async def start(self, tool_name: str, arguments: dict) -> TaskState:
        job_id = await my_system.start(arguments)            # durably create the job
        return TaskState(job_id, "working", created_at=..., last_updated_at=...,
                         ttl_ms=..., poll_interval_ms=2000)

    async def get_state(self, task_id: str) -> TaskState:
        workflow_state = await my_system.get(task_id)        # YOUR state model
        state = TaskState(task_id, to_task_status(workflow_state),  # map -> MCP task status
                          created_at=..., last_updated_at=...)
        if state.status == "input_required":
            state.input_requests = {"approval": InputRequest(
                method="elicitation/create",
                params={"mode": "form", "message": "Approve?",
                        "requestedSchema": {"type": "object",
                                            "properties": {"value": {"type": "string"}},
                                            "required": ["value"]}})}
        elif state.status == "completed":
            state.result = {"content": [{"type": "text", "text": "done"}], "resultType": "complete"}
        elif state.status == "failed":
            state.error = {"code": -32603, "message": "..."}
        return state

    async def submit_input(self, task_id: str, input_responses: dict[str, InputResponse]) -> None:
        resp = input_responses["approval"]                   # resp.action / resp.content
        await my_system.resolve(task_id, resp.content)

    async def cancel(self, task_id: str) -> None:
        await my_system.cancel(task_id)                      # cooperative
```

`TaskState` fields: `task_id`, `status`, `created_at`, `last_updated_at`, and optional
`status_message`, `ttl_ms`, `poll_interval_ms`, `input_requests`, `result`, `error`. Populate
`input_requests` while `input_required`, `result` when `completed`, `error` when `failed`. The
invoice app's worked mapping is `invoice_processing_mcp/server/invoice_backend.py` (`TEMPORAL_TO_MCP_STATE` + `get_state`).

> **Forward compatibility.** `input_requests` is intentionally the **native v2 Tasks `inputRequests`
> shape** — a keyed map of `InputRequest(method="elicitation/create", params={message,
> requestedSchema})`. Your backend speaks v2 regardless of transport: on this FastMCP layer the server
> *pushes* that request via `elicit_form` during `tasks/result` (v1), but re-targeting native v2 would
> just *return* `state.input_requests` in `tasks/get` — your `TaskBackend` and the client UI surface
> stay unchanged. See `docs/decisions/003-revert-to-v1-fastmcp-tasks.md` → "Forward compatibility".

> **Durability contract.** The spec requires the task be *durably created before the response is
> sent*. With Temporal, `await client.start_workflow(...)` satisfies this — it returns only after the
> workflow is persisted. Use the workflow ID as the task ID (1:1, no lookup table).

### 2. Wire it onto a lowlevel `Server`

```python
from mcp import types
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp_tasks_temporal.server import register_tasks_extension

async def on_list_tools(ctx, params):
    return types.ListToolsResult(tools=[types.Tool(name="my_tool", description="...",
                                                    inputSchema={...})])

server = Server("my-server", on_list_tools=on_list_tools)
register_tasks_extension(server, MyBackend(), task_tools={"my_tool"})

async with stdio_server() as (read, write):
    await server.run(read, write, server.create_initialization_options())
```

`register_tasks_extension(server, backend, *, task_tools=None, fallback_call_tool=None)`:
- installs the compat shim and advertises the `io.modelcontextprotocol/tasks` capability,
- wraps `tools/call`: for a task tool (any tool if `task_tools is None`) it requires the client to
  have declared the extension, then calls `backend.start(...)` and returns a `CreateTaskResult`;
  non-task tools fall through to `fallback_call_tool` (or the server's existing `tools/call` handler),
- registers `tasks/get` / `tasks/update` / `tasks/cancel` → `backend.get_state` / `submit_input` / `cancel`.

---

## Client side

The client is a Temporal Plugin that bundles `TaskTrackerWorkflow` + the wire activities + the MCP
session lifecycle. You run a worker with the plugin, then start one workflow per task.

### 1. Run a worker with the plugin

```python
from mcp.client.stdio import StdioServerParameters
from temporalio.client import Client
from temporalio.worker import Worker
from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.plugin import MCPTasksClientPlugin

server_params = StdioServerParameters(command="python", args=["-m", "my_server"])
client = await Client.connect("localhost:7233")

worker = Worker(client, task_queue=models.TASK_QUEUE,
                plugins=[MCPTasksClientPlugin(server_params)])  # registers workflow + activities
await worker.run()
```

The plugin's `run_context` opens the MCP stdio session for the worker's lifetime and binds it to the
activities. No explicit `workflows=`/`activities=` needed.

### 2. Start a task and drive HITL (e.g. from a UI process)

```python
from mcp_tasks_temporal.client import models
from mcp_tasks_temporal.client.workflows import TaskTrackerWorkflow

handle = await client.start_workflow(
    TaskTrackerWorkflow.run,
    models.TaskTrackerInput(tool_name="my_tool", arguments={...}),
    id=f"task-tracker-{uuid4()}", task_queue=models.TASK_QUEUE)

# When the task needs input, the workflow surfaces it:
pending = await handle.query(TaskTrackerWorkflow.get_pending_input)   # {key: {method, params}} | None
if pending:
    # build inputResponses keyed to match, then signal:
    await handle.signal(TaskTrackerWorkflow.user_decision,
                        {"approval": {"action": "accept", "content": {"value": "approve"}}})

result = await handle.result()   # {"status": ..., "result": ..., "error": ...}
```

**`TaskTrackerWorkflow` surface:**
- signal `user_decision(decision: dict)` — the `inputResponses` map answering the pending requests
- query `get_pending_input()` → outstanding `inputRequests` (or `None`)
- query `get_status()` → current status string
- query `get_task_id()` → the server-side MCP task ID

> **No `tasks/list`.** v2 removed it. The workflow *is* the durable handle, and Temporal
> `list_workflows` is your task registry — enumerate running `TaskTrackerWorkflow`s to recover
> in-flight tasks after a restart.

---

## Package layout

| Module | What |
|---|---|
| `wire.py` | Pydantic types (`CreateTaskResult`, `GetTaskResult`, `InputRequest`/`InputResponse`, request params) + `client_capability_meta()` / `declares_tasks_extension()` |
| `server.py` | `register_tasks_extension(server, backend, ...)` |
| `backend.py` | `TaskBackend` protocol + `TaskState` (the MCP-facing task state you map your workflow state onto) |
| `_sdk_compat.py` | `install_tasks_result_passthrough()` — the integration seam (auto-installed; guard-tested; becomes a sanctioned SDK call once the result-surface gap is closed upstream — see Path to native) |
| `client/workflows.py` | `TaskTrackerWorkflow` |
| `client/activities.py` | `MCPActivities` (`start_task` / `poll_task` / `submit_task_input` / `cancel_task`) |
| `client/session.py` | `connect_tasks_session()` + `task_request()` (adds capability `_meta`) |
| `client/models.py` | `TASK_QUEUE`, `TaskTrackerInput`, activity-name constants, plain dataclasses |
| `plugin.py` | `MCPTasksClientPlugin(server_params)` |

**Sandbox note.** `TaskTrackerWorkflow` imports only `client/models.py` (pure dataclasses) and calls
activities by string name, so `mcp` never loads in the workflow sandbox. Keep your own workflow code
on the same discipline (don't import `mcp`/wire types into a workflow module).

---

## Path to native (upstreaming the seam)

This package is intended to *be* the tasks implementation, not a stopgap waiting for someone else's.
Almost all of it is already spec-shaped and final: the wire types, the `tasks/*` handlers (the SDK
lets you register arbitrary extension methods), the backend protocol, the capability negotiation, and
the entire client. Adding new methods needs no patching.

The **one** core gap is that the SDK validates a `tools/call` *result* against a closed union
(`CallToolResult | InputRequiredResult`) with no task variant, so a bare `CreateTaskResult` is
rejected on both the server's outbound and the client's inbound path. `_sdk_compat` monkeypatches the
two validation functions to pass `resultType:"task"` through. That monkeypatch is the only piece that
shouldn't ship as-is in a canonical implementation.

"Native tasks" is reached by closing that gap **in the SDK**, either of:

1. **Widen the result surface** — add a task variant (e.g. `CreateTaskResult`) to
   `mcp.types.methods.SERVER_RESULTS[("tools/call", version)]` / `AnyCallToolResult`, gated on the
   client having declared the tasks extension. The runner then accepts it natively and the monkeypatch
   is removed outright. (The SDK runner already has a `# TODO` acknowledging `resultType` handling is
   unfinished here.)
2. **Add a result-type registration hook** — let an extension register an additional allowed result
   type for a method (instead of hardcoding the union). `register_tasks_extension` would then call
   that hook for `tools/call` instead of patching `serialize_server_result` / `validate_server_result`.

Either way the seam resolves into a sanctioned SDK call and the wire types / handlers / client stay
unchanged. Tracking item: file the SDK change (preferably option 2 — it generalizes to any extension
that augments a core method's result). Until it lands, the seam stays narrow and guard-tested
(`tests/test_sdk_compat.py` fails loudly if the targeted SDK version or patched functions drift).

## Testing

The package is exercised without a network or a Temporal server:
- **Server + extension** end-to-end over the in-memory MCP transport
  (`mcp.shared.memory.create_client_server_memory_streams`) with a fake backend — see
  `tests/test_server.py`.
- **Client workflow** under `temporalio.testing.WorkflowEnvironment.start_time_skipping()` with fake
  activities — see `tests/test_client_workflow.py`.
- **Compat shim** has a guard test (`tests/test_sdk_compat.py`) that fails loudly if the targeted SDK
  version or the patched functions drift.

Run: `uv run pytest mcp_tasks_temporal/tests/`.
