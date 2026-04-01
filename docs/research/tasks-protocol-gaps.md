# MCP Tasks Protocol Gaps (SEP-1686)

Research notes from investigating the MCP Tasks specification (experimental, as of 2026-03).

## Gap 1: `tasks/list` Has No Filtering or Tool Association

### Problem

The `Task` model in the spec has no field indicating which tool created it. The `ListTasksRequest` has no filter parameters — only a pagination cursor. If a server has multiple task-enabled tools, `tasks/list` returns a flat, untyped list with no way to scope by tool.

### Impact

- Clients must infer task origin from context (e.g., `statusMessage`, `taskId` naming convention, or by calling `tasks/get` and inspecting results).
- Becomes a real usability problem the moment a server has 2+ task-enabled tools.
- No public discussion found on this gap in the spec repo as of 2026-03-23.

### Task Model Fields (from `mcp/types.py`)

```
taskId, status, statusMessage, createdAt, lastUpdatedAt, ttl, pollInterval
```

No `toolName`, `type`, or similar field exists.

### Possible Mitigations

- **`_meta` on result envelopes (spec-endorsed):** `ListTasksResult` extends `Result`, which has a `_meta` field. The [spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/index#_meta) says `_meta` is intended for exactly this kind of protocol-level extensibility. A server could include a tool-to-task mapping in `_meta`:
  ```json
  {
    "_meta": {
      "taskTools": {
        "invoice-abc123": "process_invoice",
        "refund-xyz789": "process_refund"
      }
    },
    "tasks": [...]
  }
  ```
  Similarly, `GetTaskResult` extends both `Result` and `Task`, so individual `tasks/get` responses also carry `_meta`. The limitation is that `_meta` lives on the result envelope, not on individual `Task` objects within the `tasks` array — so the metadata is separated from the tasks it describes.
- **Convention-based:** encode tool name in `taskId` (e.g., `invoice-<uuid>`, `refund-<uuid>`)
- **`statusMessage`:** include tool/workflow context in the human-readable status message.
- **Protocol-level fix:** propose adding a `toolName` field to `Task` and filter params to `ListTasksRequest`.

## Gap 2: `tasks/result` Must Block Until Terminal State

### What the Spec Says

> "When a receiver receives a `tasks/result` request for a task in any other non-terminal status (`working` or `input_required`), it **MUST** block the response until the task reaches a terminal status."

While blocked, the server delivers queued messages (elicitation, sampling) as side-channel messages within the response stream. Multiple `working <-> input_required` cycles are allowed within a single blocked `tasks/result` call.

### Problem

For long-running workflows, "block until terminal" means potentially unbounded connection hold times. Our `InvoiceWorkflow` waits up to 5 days for approval. If a client calls `tasks/result` on a `working` task early, that connection must stay open for days.

The spec assumes clients will be smart about *when* they call `tasks/result` (only after seeing `input_required` or terminal via polling), but making it a MUST-block rather than allowing an error response means misbehaving or eager clients get silently stuck.

If the connection drops mid-block, the task keeps running (Temporal is durable), but the MCP protocol loses its handle. The client must rediscover the task via `tasks/list` and call `tasks/result` again.

### Our Divergence

Our `handle_tasks_result` in `temporal_task_handlers.py` (line 191-198) intentionally raises `McpError` for `working` states instead of blocking. This is technically non-compliant but more practical — it fails fast and lets the client decide what to do.

### Proposed Spec Fix

SEP-2322 ("Multi Round-Trip Requests") proposes a stateless `IncompleteResult` response pattern that would allow `tasks/result` to return without blocking, sending the client back to polling. This would address both the long-lived connection problem and improve resilience to connection failures.

## References

- [SEP-1686: Tasks Issue](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1686)
- [SEP-1686: Tasks PR](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1732)
- [2026 MCP Roadmap](https://modelcontextprotocol.io/development/roadmap)
