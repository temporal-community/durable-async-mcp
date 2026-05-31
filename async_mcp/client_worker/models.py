# ABOUTME: Shared data models for the client-side Temporal worker.
# Used by workflows, activities, and the UI for type-safe communication.

from dataclasses import dataclass
from typing import Optional


@dataclass
class ElicitationDetails:
    """Elicitation prompt and schema received from the MCP server."""

    message: str
    schema: dict


@dataclass
class TaskTrackerInput:
    """Input for TaskTrackerWorkflow.

    Pass invoice_json to start a new MCP task, or supply task_id to
    resume polling an already-started task.
    """

    invoice_json: dict
    task_id: Optional[str] = None
