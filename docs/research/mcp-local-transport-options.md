# Local Invocation Approaches for MCP Servers

Learning notes (2026-06-03) exploring why MCP servers run locally over stdio, and
what other local transports could be considered. Grew out of the discussion in
[`tasks-protocol-gaps.md`](tasks-protocol-gaps.md) about the single-reader "funnel"
that stdio imposes — see its *Transport Dependency* section.

MCP servers intentionally run locally, and stdio supports that efficiently. The
alternatives below are really about relaxing one specific property of stdio.

## Why stdio was the right default

- **Locality is the model, not a deployment choice.** The server runs as a child of
  the client. No network, no ports, no service discovery.
- **Efficiency.** JSON-RPC framed over kernel pipes — no TCP/IP stack, no checksums,
  no Nagle, no TLS handshake. About as cheap as IPC gets.
- **Trust boundary = process spawn.** If you can launch the command, you're authorized.
  No auth layer needed at all. That's a huge simplification.
- **Zero config and universal.** A `command` + `args` line works on every OS. This is
  why Claude Desktop config is just that.
- **Lifecycle is free.** Server lives and dies with the client context. No orphan
  processes, no "is it still running?"

The cost is the one the funnel discussion exposed: stdio is **1:1, launch-based,
single-stream** — one parent, one child, one pipe pair, one reader. That only became a
liability under *concurrent in-flight Tasks*, which postdates the transport choice.
stdio wasn't wrong; the workload changed.

## The local-invocation design space

The key axis the funnel discussion exposed is **addressable vs. launch-based** — i.e.,
can multiple independent connections (independent readers) exist? Here's the space, all
staying local:

| Approach | Local? | Efficiency | Multi-client / independent readers? | Notes |
|---|---|---|---|---|
| **stdio (current)** | yes | very high | **no** — 1:1, launch-based | trust = spawn; zero config; the funnel lives here |
| **Unix domain socket** | yes | very high (≈ pipes, kernel-only) | **yes** — server `accept()`s many conns, each its own stream | addressable via socket path; `SO_PEERCRED` for auth; not a *standardized* MCP transport, but Streamable HTTP can bind to one |
| **Loopback TCP (127.0.0.1)** | yes | high (TCP/IP overhead) | **yes** | this *is* "Streamable HTTP on localhost" — a supported MCP transport; exposes a port → bind/auth care |
| **Named pipe (Win) / FIFO (POSIX)** | yes | high | Win named pipes: yes (multi-instance); POSIX FIFOs: awkward | OS-idiomatic but cross-platform pain |
| **In-process / direct link** | yes (same process) | maximal (no serialization) | n/a | fastmcp can connect a `Client` straight to a `FastMCP` object; great for embedding/tests, but no isolation |
| **Shared memory / mmap ring** | yes | maximal throughput | possible but bespoke | no standard framing; overkill for JSON-RPC control traffic; only pays off for large payload streaming |
| **gRPC / HTTP-2 local** | yes | high | yes — **native stream multiplexing** | structurally dodges the funnel via independent streams, but not MCP's JSON-RPC model |

## The insight that matters

**"Local" does not imply "1:1."** The funnel is a property of stdio's single
point-to-point stream, not of running locally. The moment you pick a transport that is a
*listener with independent connections* — a **Unix domain socket** being the cleanest, or
**loopback HTTP/2** — you keep locality and nearly all the efficiency, but each consumer
gets its own stream and its own reader. That's exactly the "per-request response streams"
escape the `tasks-protocol-gaps.md` Transport Dependency section pointed at: independent
readers can't starve each other, so the #2489 problem evaporates *at the transport layer*
even without the SDK fix.

A Unix domain socket is the sweet spot for "local but addressable": no network stack, no
port exposure, throughput comparable to pipes, multiple simultaneous clients, and it can
even pass peer credentials for auth. Its only real downsides versus stdio are that
*something* has to manage the server's lifecycle (it's no longer a child that dies with
you) and it's not a first-class MCP transport — though **Streamable HTTP bound to a Unix
socket** gives you a spec-supported way to get there (uvicorn/httpx both support UDS;
you'd want to verify the exact fastmcp wiring).

## And the convergence with the 2026 redesign

There's a nice double here. The stateless redesign already dissolved the funnel *at the
protocol level* (no more unsolicited elicitation push; poll `tasks/get` + `tasks/update`).
It *also* made standard HTTP load-balancing and per-request streams natural — which makes
the multiplexed local transports (UDS / loopback HTTP) the obvious fit. So the protocol's
direction and the transport design-space point the same way: **stdio for the simple
single-flight local case; local HTTP-over-socket when you have real concurrency.** Two
independent routes to the same place, which is part of why the funnel stopped being a
load-bearing problem.

## Related notes

- [`tasks-protocol-gaps.md`](tasks-protocol-gaps.md) — Gap 3 and the *Transport
  Dependency* analysis (the stdio single-reader bottleneck, SDK issue #2489 / PR #2490).
- [`mcp-2026-07-28-spec-impact.md`](mcp-2026-07-28-spec-impact.md) — the stateless
  redesign and why the funnel is no longer load-bearing.
- [`../design/durable-client-thesis-after-stateless-redesign.md`](../design/durable-client-thesis-after-stateless-redesign.md)
  — the "stateless wire, stateful edges" thesis.
