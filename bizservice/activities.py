# ABOUTME: Temporal activity definitions for invoice processing.
# Contains validate_against_erp and payment_gateway with configurable failure rates.

import os
import random
from temporalio import activity
from temporalio.exceptions import ApplicationError


@activity.defn
async def validate_against_erp(invoice: dict) -> bool:
    activity.logger.info("Validating invoice %s", invoice.get("invoice_id"))
    fail_validate = os.getenv("FAIL_VALIDATE", "false").lower() == "true"
    no_fail_validate = os.getenv("NO_FAIL_VALIDATE", "false").lower() == "true"
    if fail_validate:
        raise ApplicationError("MISMATCH")
    if not no_fail_validate and random.random() < 0.3:
        raise ApplicationError("MISMATCH")
    return True

@activity.defn
async def payment_gateway(line: dict) -> bool:
    activity.logger.info("Paying %s", line.get("description"))
    fail_payment = os.getenv("FAIL_PAYMENT", "false").lower() == "true"
    no_fail_payment = os.getenv("NO_FAIL_PAYMENT", "false").lower() == "true"
    if fail_payment:
        raise ApplicationError(
            "INSUFFICIENT_FUNDS",
            type="INSUFFICIENT_FUNDS",
            non_retryable=True,
        )
    if not no_fail_payment and random.random() < 0.1:
        raise ApplicationError(
            "INSUFFICIENT_FUNDS",
            type="INSUFFICIENT_FUNDS",
            non_retryable=True,
        )
    # Simulate a retryable failure sometimes for payment processing
    if random.random() < 0.3:
        raise ApplicationError("PAYMENT_GATEWAY_ERROR", type="PAYMENT_GATEWAY_ERROR")
    activity.logger.info("Payment succeeded")
    return True
