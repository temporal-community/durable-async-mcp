# ABOUTME: Tests the UI's _prompt_for elicitation renderer — multi-field forms, optional skip, enums.
# Drives the prompt by faking builtins.input with a queued list of answers.

import asyncio
import builtins

from invoice_processing_mcp.client.ui import _prompt_for

COST_CENTER_PARAMS = {
    "message": "Assign a cost center",
    "requestedSchema": {
        "type": "object",
        "properties": {
            "cost_center": {"type": "string"},
            "memo": {"type": "string"},
        },
        "required": ["cost_center"],
    },
}

APPROVAL_PARAMS = {
    "message": "Approve?",
    "requestedSchema": {
        "type": "object",
        "properties": {"value": {"type": "string", "enum": ["approve", "reject"]}},
        "required": ["value"],
    },
}


def _run_with_inputs(params, answers, monkeypatch):
    queue = list(answers)
    monkeypatch.setattr(builtins, "input", lambda _prompt="": queue.pop(0))
    return asyncio.run(_prompt_for(params))


def test_multi_field_form_captures_all(monkeypatch):
    content = _run_with_inputs(COST_CENTER_PARAMS, ["CC-1000", "Q3 spend"], monkeypatch)
    assert content == {"cost_center": "CC-1000", "memo": "Q3 spend"}


def test_optional_field_skipped_when_blank(monkeypatch):
    content = _run_with_inputs(COST_CENTER_PARAMS, ["CC-2000", ""], monkeypatch)
    assert content == {"cost_center": "CC-2000"}


def test_enum_rejects_invalid_then_accepts(monkeypatch):
    content = _run_with_inputs(APPROVAL_PARAMS, ["maybe", "approve"], monkeypatch)
    assert content == {"value": "approve"}
