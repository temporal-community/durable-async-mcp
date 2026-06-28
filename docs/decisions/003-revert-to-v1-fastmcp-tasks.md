# ADR-003: Revert the MCP Tasks protocol to v1 (FastMCP + server-push elicitation)

Status: Accepted (2026-06-28)
Supersedes the protocol choice in [ADR-002](002-migrate-to-tasks-extension-v2.md).

## Context

ADR-002 migrated the demo to a hand-written implementation of the 2026-07-28 **MCP Tasks extension**
(`io.modelcontextprotocol/tasks`) on the `mcp==2.0.0a2` alpha SDK: a pull-based protocol where
`tasks/get` carries `inputRequests` as data and the client answers via `tasks/update`. That required a
hand-written `wire.py` (Pydantic wire types) and a `_sdk_compat.py` shim so a bare `CreateTaskResult`
could pass the alpha SDK's `tools/call` validation.

We decided to go back to the **v1** tasks mechanism (FastMCP's SEP-1686 task support, `fastmcp 2.14.3`),
where the server **pushes** an `elicitation/create` to the client during `tasks/result`. This keeps the
demo on a released, stable SDK and on the original "FastMCP + elicitation" design, while **retaining all
the higher-level work** built on top of v2: `PurchaseOrderWorkflow`, the NiceGUI board, the cost-center
second HITL gate, and the back-office activities.

## Decision

Swap only the protocol layer, freezing two contracts so the higher-level features barely changed:

- **Client `TaskTrackerWorkflow` external surface kept at the v2 shape** — `TaskTrackerInput(tool_name,
  arguments)`, dict return `{status, result, error}`, queries `get_status`/`get_pending_input`
  (keyed `{key: {method, params:{message, requestedSchema}}}`)/`get_task_id`, signal
  `user_decision(dict)`. Internals became v1: on `input_required` a `handle_elicitation` activity opens
  `tasks/result` (server push), its `_elicitation_handler` signals `elicitation_received` and reads the
  decision; the single-string decision was generalized to a keyed dict, and the server injects
  `x-task-id` + `x-request-key` into the `requestedSchema` so multi-field / multi-gate routing works.
- **Server `InvoiceTaskBackend` kept** (`get_state`/`submit_input`/state map). A generic FastMCP
  `tasks/result` handler reuses it: `get_state` → `elicit_form(message, schema)` → `submit_input` →
  `wait_result`. `mcp_tasks_temporal` stays the reusable package; `wire.py` and `_sdk_compat.py` were
  deleted.

`fastmcp[tasks]==2.14.3` is pinned: 2.14.7 dropped the `tasks` extra and `fastmcp 3.x` changed the task
wire protocol (server-generated ids). `mcp` resolves to 1.x via fastmcp.

## Forward compatibility (anticipatory of native v2)

The `TaskBackend` contract is intentionally shaped to the **v2 Tasks extension** model, not to v1's
flat elicitation. `TaskState.input_requests` (in `mcp_tasks_temporal/backend.py`) is a **keyed map**
`{key: InputRequest(method="elicitation/create", params={message, requestedSchema})}`, populated by
`get_state` exactly when status is `input_required` — which is the native v2 `inputRequests` structure.
`InvoiceTaskBackend` already produces it (`_approval_request`/`_cost_center_request`, distinct keys
`approval` / `cost-center-coding`), and the client UI consumes it as-is via `get_pending_input` /
`user_decision`.

The **only** v1-specific layer is the transport adaptation in `mcp_tasks_temporal/server.py`: the
generic `tasks/result` handler takes that v2-shaped `InputRequest`, injects `x-task-id` /
`x-request-key`, and **pushes** it via `elicit_form` (server-push) instead of *returning* it in
`tasks/get`. So a future move to native v2 is largely a **transport swap** — return
`state.input_requests` in `tasks/get` and accept `tasks/update` — leaving `InvoiceTaskBackend` and the
client surface (`get_pending_input` / `user_decision`) unchanged. The v2-shaped backend/UI contract is
deliberate, not incidental.

## Consequences

- **Reintroduces the reader-release concurrency caveat** (see
  [tasks-protocol-gaps.md](../research/tasks-protocol-gaps.md)): `_elicitation_handler` raises if no
  decision is ready so Temporal retries with backoff, briefly releasing the shared MCP stdio reader.
  SEP-2322 is the proposed spec fix; there is no merged SDK PR that removes the workaround.
- The legacy OpenAI-driven CLI client was **not** restored (no `openai` dependency).
- `durable_sync_mcp` (uses `mcp.server.fastmcp`) is importable again under mcp 1.x, but remains out of
  scope and untested.
