# MCP Task Notifications: Polling vs. Push, and the Transport Question

*Research note — 2026-06-27. Status: exploratory; no implementation committed. Captures a design
discussion before scoping the notifications feature.*

## Why this note

We were about to plan implementing MCP Tasks **notifications** (`notifications/tasks` +
`subscriptions/listen`) so durable clients don't have to poll `tasks/get`. The discussion surfaced
deeper questions about transport mechanics, scaling to ~1M open tasks, and whether Temporal should be
the delivery mechanism — or even a custom MCP **transport**. This note captures the findings and the
open questions so we can decide scope deliberately.

## 1. What the ext-tasks spec says about notifications

From the ext-tasks spec (`specification/draft/tasks.md`) and the modelcontextprotocol.io overview:

- **`notifications/tasks`** — a server→client JSON-RPC notification (no id, one-way). `params` is the
  **full `DetailedTask`** — identical to what `tasks/get` would return at that moment (status, plus
  `result` / `error` / `inputRequests`).
- **`subscriptions/listen`** — a client→server **request** with `params.notifications.taskIds:
  string[]`. Clients can subscribe to many task IDs at once, before or after task creation. The client
  MUST have declared the `io.modelcontextprotocol/tasks` capability or the server MUST return a
  JSON-RPC error.
- **`notifications/subscriptions/acknowledged`** — server→client notification echoing the taskIds it
  agreed to push for.
- No explicit unsubscribe documented.
- **Delivery semantics are contested** — see Open Question #1. One fetched summary characterized
  notifications as best-effort ("polling authoritative; no ordering/delivery guarantees"), while the
  overview says *"If a server supports notifications, clients can rely on them instead of polling."*
  These are in tension and must be reconciled against the literal spec text before we design.

## 2. SDK reality (`mcp==2.0.0a2`) — the seams we'd need

Mirrors our existing `_sdk_compat` result-passthrough pattern:

- **Client receive:** `ClientSession(message_handler=…)` is the callback for server notifications. But
  the dispatcher's `_on_notify` runs `parse_server_notification(method, …)` first and **silently drops
  methods it can't type-parse**. `notifications/tasks` isn't in the SDK's typed union (it only has an
  older `notifications/tasks/status` stub, excluded from the union). → we need a passthrough so our two
  notification methods reach `message_handler`. We send `subscriptions/listen` via the existing
  `send_raw_request` path.
- **Server send:** `ServerSession.send_notification` only accepts *typed* notifications; for our custom
  method we use the raw `session._dispatcher.notify(method, params)` (fire-and-forget) — same
  "reach into the dispatcher" pattern we already use on the client. We also register a
  `subscriptions/listen` request handler.
- **Method-name mismatch:** spec `notifications/tasks` vs SDK `notifications/tasks/status`. We control
  both ends, so we'd use the spec's `notifications/tasks` — but worth flagging for upstream alignment.

## 3. Transport mechanics: where a notification actually goes

MCP separates the **protocol** (JSON-RPC messages) from the **transport** (how they're carried).
Notifications are connection/session-scoped — addressed to "the client on this connection," never a
durable address or broadcast.

- **stdio (our demo):** a **persistent, full-duplex pipe**, not per-request. Our durable client (the
  Temporal worker) opens **one** stdio session at startup (`MCPTasksClientPlugin.run_context`) and
  holds it for the worker's lifetime — that's *why* `poll_task` reuses the same session. The server
  subprocess stays alive that whole time and can write a `notifications/tasks` line to stdout anytime;
  the worker's receive loop reads it (today it drops unsolicited notifications — the seam above).
- **Streamable HTTP (the scale model):** plain POST request/response has **no standing channel**. The
  server can push only if the client holds open a long-lived **GET SSE stream** (or the server keeps
  the `subscriptions/listen` POST's SSE stream open). Session identified by `Mcp-Session-Id`.
  Resumability via SSE event IDs + `Last-Event-ID` lets a reconnecting client replay missed messages —
  but requires the server to keep a **per-stream replay buffer**.
- **Consequence:** for the server to push at the instant a task changes, **some** SSE stream to that
  session must be open at that instant. If none is open: drop (client finds out on next poll) or
  buffer-for-replay (server carries per-client state).

## 4. Scaling: polling vs. notifications at ~1M open tasks

The key insight: **polling cost scales with how many tasks are open; event cost scales with how often
they actually change.** For long-lived, mostly-idle tasks (invoice approvals waiting days), that's the
whole ballgame.

| Approach | Cost model | 1M tasks, mostly idle (5-day approval waits) |
|---|---|---|
| **Client polls `tasks/get`** (today) | `O(open_tasks × poll_freq)` | 1M / 2s ≈ **500K req/s**, each → a Temporal query; >99% return "no change". Idle tasks cost the same as active ones. |
| **Notifications, server *watches* Temporal** (naive) | still `O(open_tasks × poll_freq)`, moved server-side | Eliminates MCP wire traffic + client work, but the server now issues ~500K Temporal queries/s. **Relocates polling; doesn't fix the floor.** |
| **Notifications, *event-driven*** (workflow emits on transition) | `O(state-transition rate)` | ~6 transitions/task over a ~1-day life → **~70 events/s**, ~7000× less. **Idle tasks cost zero.** |

Other dimensions:
- **Latency:** notifications fire on transition; polling is up to one interval late.
- **Connection cost (the non-obvious trade):** **polling is connectionless** (connect, ask, drop —
  server holds nothing between polls). **Notifications require a standing connection per *connected
  client*** — not per task (a client multiplexes all its task subscriptions over one connection via
  `subscriptions/listen`), plus optionally a replay buffer. So: *polling scales connection cost with
  request rate; notifications scale it with concurrent-client count.* 1M tasks across a few thousand
  clients = a few thousand held streams (fine); 1M clients × 1 task = 1M held streams (its own problem).
- **Durable-client reconnect:** because `TaskTrackerWorkflow` is itself durable, on a dropped
  connection it can do a **single catch-up `tasks/get` on (re)subscribe** and otherwise wait on signals
  — event efficiency with polling only at `O(reconnects)`, not `O(time)`.

## 5. Using Temporal as the delivery mechanism

Temporal is a natural fit, and MCP won't mechanically obstruct it — **but where you point the signal
decides whether you're still doing MCP.**

- **Option 1 — server-workflow signals the client's `TaskTrackerWorkflow` directly.** Simplest;
  works. But it **bypasses MCP** — the "notification" is a private Temporal side-channel, not an MCP
  `notifications/tasks`. A generic MCP client gets nothing. Loses the interop the project demonstrates.
- **Option 2 — server-workflow signals the *MCP server process*, which emits a real
  `notifications/tasks` on the wire.** Temporal is the **internal event bus behind the server**; the
  MCP standard is preserved; a vanilla client still works. **Constraint:** a bare Temporal *client*
  can't await a signal — only workflows receive signals and only workers get work pushed. So the MCP
  server process must **run a Temporal worker** (host an activity/workflow the `InvoiceWorkflow`
  invokes on transition, routed to that session, e.g. a per-session task queue). Then *"hold a
  connection open"* becomes *"the MCP-server worker long-polls its Temporal task queue"* — Temporal's
  native, efficient model; no per-client replay buffer; idle tasks cost nothing.
- **Client-side bridge (either option):** the `message_handler` runs in the worker and can't resume a
  workflow directly — its job is to translate an incoming `notifications/tasks` into a **Temporal
  signal** to the right `TaskTrackerWorkflow`, which then awaits signals instead of polling. Needs:
  (a) a Temporal client alongside the MCP session, and (b) a **taskId → tracker-workflow-id lookup**
  (in-memory map filled when `start_task` returns — simple, single-worker, non-durable; or a Temporal
  **search attribute** on the tracker — robust, multi-worker, survives restart).

## 6. The deeper idea: MCP over a durable Temporal **transport**

MCP explicitly permits custom transports. From the spec's *Custom Transports* section (verbatim):

> Clients and servers **MAY** implement additional custom transport mechanisms to suit their specific
> needs. The protocol is transport-agnostic and can be implemented over any communication channel that
> supports bidirectional message exchange.
> Implementers who choose to support custom transports **MUST** ensure they preserve the JSON-RPC
> message format and lifecycle requirements defined by MCP. Custom transports **SHOULD** document their
> specific connection establishment and message exchange patterns to aid interoperability.

Three requirements: (1) bidirectional exchange, (2) preserve JSON-RPC format **and** MCP lifecycle (the
`initialize` handshake, version negotiation, request/response correlation), (3) *should* document the
pattern.

**A Temporal transport carries real MCP JSON-RPC messages** (`initialize`, `tools/call`, `tasks/get`,
`notifications/tasks`) over durable Temporal primitives — categorically different from Option 1, which
*bypassed* MCP. Both endpoints stay fully MCP-conformant at the protocol level; only the bottom layer
changes. Why it's compelling:

- **It dissolves much of *why Tasks exists*.** Tasks largely compensates for fragile transports
  (durable handle, resume-after-disconnect, best-effort notifications). A durable transport makes
  messages persisted, ordered, retried, and crash-surviving — so `notifications/tasks` becomes
  **guaranteed durable delivery**, not best-effort-over-a-maybe-open-stream. The held-connection /
  resumability / poll-fallback machinery becomes the transport's job.
- **"Listening" = a worker long-polling a Temporal queue** (Temporal's native model), not 1M held SSE
  streams. Idle tasks cost nothing.
- **Clean SDK fit.** The SDK separates transport from protocol: `stdio_client()` yields a
  `(read_stream, write_stream)` pair and `ClientSession(read, write)` / `Server.run(read, write, …)`
  run on top. A `temporal_session(...)` yielding Temporal-backed read/write streams would let the
  entire existing session machinery — including our `_sdk_compat` seams — run unchanged.

**Tradeoffs:** it's a **private transport** (only endpoints that both implement it interoperate — can't
reach Claude Desktop, which speaks stdio/HTTP); the **message protocol stays 100% MCP** (full
interop at that layer) but transport interop is ecosystem-scoped. Needs an addressing/bootstrap
convention (namespace + task queue + session handshake) and documentation. Per-message persistence
overhead suits task-grained, long-running interactions (Tasks' sweet spot), not chatty low-latency
calls. Larger build than the notifications feature.

**Three-way framing for the comparison:**
1. **Client polling** (today) — connectionless, simple, `O(open_tasks × freq)`.
2. **Push on a fragile transport** (the spec's notifications) — needs held connections + best-effort +
   poll fallback; reduces wire chatter and latency.
3. **MCP over a durable transport** (Temporal) — guaranteed delivery, no held connections, dissolves
   Tasks' resilience scaffolding; closed-ecosystem transport.

## 7. Implementation seams identified (for whichever scope we pick)

- Two `_sdk_compat`-style seams: client notification-passthrough (so `notifications/tasks` reaches
  `message_handler`); server raw `dispatcher.notify(method, params)`.
- Server must **retain the session** and push from a **background task** (the push happens long after
  `subscriptions/listen` returns).
- Client `message_handler` → Temporal **signal** bridge; `TaskTrackerWorkflow` awaits signal instead of
  sleeping; single catch-up `tasks/get` on (re)subscribe.
- taskId → tracker mapping (in-memory vs search attribute).
- For event-driven server push: `InvoiceWorkflow` emits a status-change event on transition (activity →
  the MCP-server worker / a broker).

## 8. Open questions

**From the user (to investigate later — not answered here):**
1. **Re-examine the "notifications may be dropped" assertion.** It may not be true. There is a place in
   the spec stating that *if notifications are supported, the client may depend on them* — which does
   not sound like dropping is allowed. Reconcile against the literal spec text. (Evidence to weigh: the
   overview says *"If a server supports notifications, clients can rely on them instead of polling,"*
   while a fetched summary of `tasks.md` characterized them as best-effort with no delivery guarantees.
   These conflict; determine which the normative spec actually says.)
2. **Understand what is really *in* vs *out* of the protocol specification.** Are transports part of
   the protocol spec, or a separate concern?
3. **What is the governance around transport protocols?** (Who defines/sanctions them; what's the
   process for a new transport; is there a registry or conformance notion?)

**From the design discussion (to resolve when scoping):**
4. **Delivery semantics → design impact.** If notifications are genuinely dependable when supported
   (per #1), does the client still need a polling fallback at all? Over a durable transport, is
   `subscriptions/listen` even necessary, or can the server just send `notifications/tasks`?
5. **Which build:** Option 2 (Temporal as event bus behind an MCP-conformant server) vs. Option 1
   (Temporal-direct, non-MCP) vs. the custom **durable Temporal transport** (#6). These make different
   points and have very different effort.
6. **Server push without polling requires the MCP server to run a Temporal worker** — routing model
   (per-session task queue? shared queue + routing key?) and how `InvoiceWorkflow` emits transitions.
7. **taskId → tracker-workflow-id mapping:** in-memory map vs Temporal search attribute (durability,
   multi-worker, restart).
8. **MCP lifecycle a custom transport must preserve** — read the `initialize`/version-negotiation spec
   before committing to a Temporal transport.
9. **Streamable-HTTP specifics:** which SSE stream carries `notifications/tasks` (the `subscriptions/
   listen` response stream vs. a standalone GET stream); cost of the resumability/`Last-Event-ID`
   replay buffer at scale.
10. **SDK alignment:** spec `notifications/tasks` vs SDK `notifications/tasks/status` — reconcile, and
    note as a candidate upstream fix alongside the existing `_sdk_compat` gap.
