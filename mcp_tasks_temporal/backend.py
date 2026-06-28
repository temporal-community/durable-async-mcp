# ABOUTME: The TaskBackend contract the server's tasks/* handlers call into, plus TaskState.
# A backend maps its own workflow/job state to MCP TaskState (e.g. Temporal-backed invoices).

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# MCP task status string ("working" | "input_required" | "completed" | "failed" | "cancelled").
TaskStatus = str

# An InputResponse is the client's answer to one outstanding inputRequest:
# {"action": "accept" | "decline" | "cancel", "content": {...} | None}.
InputResponse = dict[str, Any]


@dataclass
class InputRequest:
    """One human-input request the server surfaces while a task is `input_required`.

    `method` is the MCP method to satisfy it (`elicitation/create`); `params` carries the
    elicitation `message` and `requestedSchema`. The server pushes this to the client via
    `elicitation/create` during tasks/result.
    """

    method: str
    params: dict[str, Any]


@dataclass
class TaskState:
    """The MCP-facing state of a task — what the server renders onto the wire.

    Distinct from your underlying *workflow/job state*: the backend's job is to map
    workflow state -> TaskState (status plus the protocol payload). Populate
    `input_requests` while `input_required`, `result` when `completed`, `error` when `failed`.
    """

    task_id: str
    status: TaskStatus
    created_at: str
    last_updated_at: str
    status_message: str | None = None
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    input_requests: dict[str, InputRequest] | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


@runtime_checkable
class TaskBackend(Protocol):
    """What an application plugs in to back the tasks extension.

    The server owns the MCP protocol; the backend owns the durable job (e.g. a Temporal
    workflow whose ID is the task ID) and maps its workflow state to MCP TaskState.
    """

    async def start(self, tool_name: str, arguments: dict[str, Any]) -> TaskState:
        """Durably start the job for a task-augmented tool call; return its initial TaskState."""
        ...

    async def get_state(self, task_id: str) -> TaskState:
        """Map the job's current workflow state to MCP TaskState (drives tasks/get)."""
        ...

    async def submit_input(
        self, task_id: str, input_responses: dict[str, InputResponse]
    ) -> None:
        """Apply the client's answers to outstanding inputRequests (drives the elicitation)."""
        ...

    async def wait_result(self, task_id: str) -> dict[str, Any]:
        """Block until the job is terminal and return its CallToolResult payload.

        Called by tasks/result after an elicitation is satisfied. Per the MCP Tasks spec
        tasks/result blocks until terminal; in practice the client cancels the request right
        after the elicitation resolves and resumes polling, so this often does not return.
        """
        ...

    async def cancel(self, task_id: str) -> None:
        """Cooperatively cancel the task (drives tasks/cancel)."""
        ...
