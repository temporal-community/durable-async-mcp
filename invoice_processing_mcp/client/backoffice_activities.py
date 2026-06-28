# ABOUTME: Back-office activities the PurchaseOrderWorkflow runs alongside the MCP payment task.
# mcp-free (stdlib + temporalio.activity only) so the sandbox re-import of the parent stays clean.

from __future__ import annotations

import asyncio
import os

from temporalio import activity


def _delay() -> float:
    """Per-step delay so the back-office work visibly overlaps the pending payment task."""
    return float(os.getenv("BACKOFFICE_DELAY_SECONDS", "2"))


@activity.defn
async def record_goods_receipt(order: dict) -> dict:
    activity.logger.info("Recording goods receipt for PO %s", order.get("po_id"))
    await asyncio.sleep(_delay())
    return {"step": "goods_receipt", "po_id": order.get("po_id")}


@activity.defn
async def update_inventory(order: dict) -> dict:
    activity.logger.info("Updating inventory for PO %s", order.get("po_id"))
    await asyncio.sleep(_delay())
    return {"step": "inventory", "po_id": order.get("po_id")}


@activity.defn
async def notify_requester(order: dict) -> dict:
    activity.logger.info(
        "Notifying requester %s for PO %s", order.get("requester"), order.get("po_id")
    )
    await asyncio.sleep(_delay())
    return {"step": "requester_notified", "requester": order.get("requester")}


@activity.defn
async def close_po(order: dict) -> dict:
    activity.logger.info("Closing PO %s", order.get("po_id"))
    await asyncio.sleep(_delay())
    return {"step": "po_closed", "po_id": order.get("po_id")}
