# ABOUTME: Plain dataclasses and activity-name constants shared by the durable client workflow + activities.
# Sandbox-safe: imports no `mcp`/`fastmcp`, so the workflow can import it without poisoning determinism.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Default task queue for the client tasks worker.
TASK_QUEUE = "mcp-tasks-client"

# Activity names (referenced by string from the workflow so it need not import the
# fastmcp-dependent activities module — keeping the workflow sandbox clean).
START_TASK = "mcp_tasks.start_task"
POLL_TASK = "mcp_tasks.poll_task"
HANDLE_ELICITATION = "mcp_tasks.handle_elicitation"
GET_TASK_RESULT = "mcp_tasks.get_task_result"

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
DEFAULT_POLL_INTERVAL_MS = 2000


@dataclass
class TaskTrackerInput:
    """Input to TaskTrackerWorkflow: which task-augmented tool to call, with what arguments."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class StartTaskInput:
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
