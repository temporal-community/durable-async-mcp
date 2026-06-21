# ABOUTME: Plain dataclasses and activity-name constants shared by the durable client workflow + activities.
# Sandbox-safe: imports no `mcp`, so the workflow can import it without poisoning determinism checks.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Default task queue for the client tasks worker.
TASK_QUEUE = "mcp-tasks-client"

# Activity names (referenced by string from the workflow so it need not import the
# mcp-dependent activities module).
START_TASK = "mcp_tasks.start_task"
POLL_TASK = "mcp_tasks.poll_task"
SUBMIT_TASK_INPUT = "mcp_tasks.submit_task_input"
CANCEL_TASK = "mcp_tasks.cancel_task"

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
DEFAULT_POLL_INTERVAL_MS = 1000


@dataclass
class TaskTrackerInput:
    """Input to TaskTrackerWorkflow: which task-augmented tool to call, with what arguments."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartTaskInput:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class SubmitInput:
    task_id: str
    # {inputRequest-key: {"action": "accept"|"decline"|"cancel", "content": {...}}}
    input_responses: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskPollResult:
    """A poll result the workflow reasons about — the client-side view of the server's TaskState.

    Plain (no wire/mcp types) so the workflow sandbox never imports `mcp`.
    """

    status: str
    poll_interval_ms: int | None = None
    # {key: {"method": "elicitation/create", "params": {...}}} when input_required
    input_requests: dict[str, Any] | None = None
    result: dict[str, Any] | None = None  # CallToolResult shape when completed
    error: dict[str, Any] | None = None  # JSON-RPC error when failed
