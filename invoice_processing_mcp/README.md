# Invoice Processing MCP (Tasks extension, Temporal-backed)

The invoice-processing application: an **MCP server** that exposes `process_invoice` via the
**MCP Tasks extension** (`io.modelcontextprotocol/tasks`), and a **durable client** that drives it.
Both are thin consumers of the reusable [`mcp_tasks_temporal`](../mcp_tasks_temporal/README.md)
library; the Temporal workflow ID *is* the MCP task ID.

Two components:

- **`server/`** — the MCP server. `server.py` wires `process_invoice` to `InvoiceTaskBackend`
  (`invoice_backend.py`) via `register_tasks_extension`. It uses a Temporal *client* to start/query/
  signal `InvoiceWorkflow`; it is not a Temporal worker.
- **`client/`** — the client application: `worker.py` (the durable MCP client — a Temporal worker
  that adopts `MCPTasksClientPlugin`) and `ui.py` (an interactive CLI that talks to Temporal only).

The durable `TaskTrackerWorkflow` and the MCP wire activities live in `mcp_tasks_temporal`, not here.

## Processes

For a normal run you start **three** processes (plus the Temporal server). The MCP server is *not*
run separately — the client worker launches it over stdio (per `client_config.json`).

| Process | Command | What it does |
|---|---|---|
| Temporal server | `temporal server start-dev` | Orchestration backbone |
| bizservice worker | `python -m bizservice.worker` | Runs `InvoiceWorkflow` + `PayLineItem` on `invoice-task-queue` |
| client worker | `python -m invoice_processing_mcp.client.worker` | Temporal worker on `mcp-tasks-client`; adopts `MCPTasksClientPlugin`, which spawns the MCP server over stdio and owns the session |
| UI | `python -m invoice_processing_mcp.client.ui` | Interactive CLI; connects to Temporal only |

> Run the MCP server standalone only for debugging: `python -m invoice_processing_mcp.server`.

## Running the demo

```bash
temporal server start-dev                                   # terminal 1
python -m bizservice.worker                                 # terminal 2
python -m invoice_processing_mcp.client.worker             # terminal 3
python -m invoice_processing_mcp.client.ui                 # terminal 4  (or: python -m invoice_processing_mcp.client)
```

In the UI:

```
Invoice Processing Client
Commands: submit <file>, list, quit

> submit samples/invoice1.json
  Started: task-tracker-<uuid>

> list
  task-tracker-<uuid>  Running

Input needed for task task-tracker-<uuid>:
  Invoice INV-001 for Acme Corp ($...) requires approval. ...
  value [approve / reject]: approve
  Decision sent.
```

## What happens (v2 lifecycle — pure polling, elicitation as data)

1. **Start** — `submit` starts a `TaskTrackerWorkflow(tool_name="process_invoice", arguments={...})`.
   Its `start_task` activity calls `tools/call` (declaring the tasks extension in `_meta`); the
   server's `InvoiceTaskBackend.start` launches an `InvoiceWorkflow` and returns a `CreateTaskResult`
   (taskId = workflow ID).
2. **Validate** — the bizservice worker validates the invoice; `TaskTrackerWorkflow` polls `tasks/get`
   (`poll_task`), and `InvoiceTaskBackend.get_state` maps the workflow status → MCP task status.
3. **Approval surfaces as data** — at `PENDING-APPROVAL` the task is `input_required` and `tasks/get`
   carries an `inputRequests` map (the approve/reject elicitation). The workflow stores it.
4. **You decide** — `list` queries `get_pending_input`, the UI renders the prompt, and signals
   `user_decision` with the `inputResponses`.
5. **Submit** — the workflow's `submit_task_input` activity calls `tasks/update`, which
   `InvoiceTaskBackend.submit_input` turns into an `ApproveInvoice` / `RejectInvoice` signal.
6. **Pay & complete** — the bizservice worker pays line items in parallel; the workflow keeps polling
   until `tasks/get` returns `completed` with the result (`PAID`), then finishes.

There is **no server-initiated elicitation** and no `tasks/result` — both were removed in v2.

## Available tools

- **`process_invoice`** — task-augmented. With the tasks extension declared, `tools/call` returns a
  `CreateTaskResult` (a task handle) instead of the answer; the client polls `tasks/get`, answers any
  `inputRequests` via `tasks/update`, and reads the result from the terminal `tasks/get`.

## Architecture

### `server/server.py`
`build_server(client)` creates a `mcp.server.lowlevel.Server`, exposes `process_invoice` via
`on_list_tools`, and calls `register_tasks_extension(server, InvoiceTaskBackend(client),
task_tools={"process_invoice"})`. Served over stdio.

### `server/invoice_backend.py`
`InvoiceTaskBackend` implements the `TaskBackend` protocol — **the mapping layer between two state
models**: `InvoiceWorkflow`'s domain *workflow state* and the MCP *task state*.
- `start` → starts `InvoiceWorkflow` (queue `invoice-task-queue`), returns the initial `TaskState`
- `get_state` → maps `GetInvoiceStatus` via `TEMPORAL_TO_MCP_STATE`; on `PENDING-APPROVAL` builds the
  approval `inputRequests`; on `PAID`/`REJECTED` the terminal `result`; on `FAILED` an `error`
- `submit_input` → signals `ApproveInvoice` / `RejectInvoice`
- `cancel` → cancels the workflow

### `client/worker.py`
Reads `client_config.json` into `StdioServerParameters` and runs
`Worker(client, task_queue=models.TASK_QUEUE, plugins=[MCPTasksClientPlugin(server_params)])`. The
plugin registers `TaskTrackerWorkflow` + the wire activities and owns the MCP stdio session.

### `client/ui.py`
Temporal-only CLI: `submit <file>` starts a `TaskTrackerWorkflow`; `list` shows running tasks and
renders any pending `inputRequests` (querying `get_pending_input`), then signals `user_decision`.

## Task state mapping

| InvoiceWorkflow status | MCP task status |
|---|---|
| INITIALIZING | working |
| PENDING-VALIDATION | working |
| PENDING-APPROVAL | input_required |
| APPROVED | working |
| PAYING | working |
| PAID | completed |
| FAILED | failed |
| REJECTED | completed |

## Tests

```bash
uv run pytest invoice_processing_mcp/tests/   # InvoiceTaskBackend mapping + server wiring
uv run pytest mcp_tasks_temporal/tests/       # the reusable extension + client
```
