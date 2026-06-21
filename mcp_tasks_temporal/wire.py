# ABOUTME: Pydantic wire types for the MCP Tasks extension (io.modelcontextprotocol/tasks), draft 2026-07-28.
# Hand-defined because mcp 2.0.0a2 deliberately omits the extension; serialized camelCase via alias.

from __future__ import annotations

from typing import Any, Literal

from mcp import types
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

# Extension identity and the per-request capability keys (declared in params._meta).
EXTENSION_ID = "io.modelcontextprotocol/tasks"
CLIENT_CAPABILITIES_KEY = "io.modelcontextprotocol/clientCapabilities"

TaskStatus = Literal["working", "input_required", "completed", "failed", "cancelled"]
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


class _Wire(BaseModel):
    """Base for extension wire models: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(
        populate_by_name=True, alias_generator=to_camel, extra="ignore"
    )

    def to_wire(self) -> dict[str, Any]:
        """Dump to the camelCase JSON shape, dropping absent optional fields."""
        return self.model_dump(by_alias=True, mode="json", exclude_none=True)


class CreateTaskResult(_Wire):
    """Returned (in lieu of CallToolResult) from a task-augmented tools/call."""

    result_type: Literal["task"] = "task"
    task_id: str
    status: TaskStatus
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None


class InputRequest(_Wire):
    """A single outstanding server→client request surfaced while input_required (e.g. an elicitation)."""

    method: str  # e.g. "elicitation/create"
    params: dict[str, Any]


class GetTaskResult(_Wire):
    """The tasks/get response. One model spanning the DetailedTask variants by status."""

    result_type: Literal["task"] = "task"
    task_id: str
    status: TaskStatus
    status_message: str | None = None
    created_at: str
    last_updated_at: str
    ttl_ms: int | None = None
    poll_interval_ms: int | None = None
    # input_required → outstanding requests keyed by a per-task-unique name.
    input_requests: dict[str, InputRequest] | None = None
    # completed → the CallToolResult the original request would have returned.
    result: dict[str, Any] | None = None
    # failed → the JSON-RPC error.
    error: dict[str, Any] | None = None


class InputResponse(_Wire):
    """A client's answer to one inputRequest, echoed back under the same key via tasks/update."""

    action: Literal["accept", "decline", "cancel"]
    content: dict[str, Any] | None = None


# --- Request param models for the server's lowlevel handlers. ---
# Subclass RequestParams so `_meta` parses uniformly; the runner validates by alias
# (by_name=False), so wire field names are given as explicit aliases.


class GetTaskRequestParams(types.RequestParams):
    task_id: str = Field(alias="taskId")


class UpdateTaskRequestParams(types.RequestParams):
    task_id: str = Field(alias="taskId")
    input_responses: dict[str, InputResponse] = Field(alias="inputResponses")


class CancelTaskRequestParams(types.RequestParams):
    task_id: str = Field(alias="taskId")


def client_capability_meta() -> dict[str, Any]:
    """The `_meta` payload a client adds per request to opt into the tasks extension."""
    return {CLIENT_CAPABILITIES_KEY: {"extensions": {EXTENSION_ID: {}}}}


def declares_tasks_extension(meta: Any) -> bool:
    """True if request `_meta` declares the tasks extension capability.

    `meta` is whatever RequestParams parsed from `_meta` (a mapping) or None.
    """
    if not meta:
        return False
    caps = meta.get(CLIENT_CAPABILITIES_KEY) if hasattr(meta, "get") else None
    if not isinstance(caps, dict):
        return False
    extensions = caps.get("extensions")
    return isinstance(extensions, dict) and EXTENSION_ID in extensions
