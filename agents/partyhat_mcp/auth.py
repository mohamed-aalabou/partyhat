"""
The hook point for x402 micropayment verification.
"""

import os
from typing import Optional


# Pricing per tool call in USDC (to set later via env or hardcoded defaults)
TOOL_PRICES: dict[str, float] = {
    "start_planning": float(os.getenv("PRICE_PLAN", "0.10")),
    "generate_contract": float(os.getenv("PRICE_GENERATE", "0.25")),
    "run_tests": float(os.getenv("PRICE_TEST", "0.15")),
    "deploy_contract": float(os.getenv("PRICE_DEPLOY", "0.50")),
    "audit_contract": float(os.getenv("PRICE_AUDIT", "0.20")),
}

PAYMENT_ADDRESS = os.getenv("PARTYHAT_PAYMENT_ADDRESS", "")


class PaymentRequired(Exception):
    """
    Raised when a tool call is made without valid payment proof.
    The MCP server catches this and returns the 402 response with pricing info.
    """

    def __init__(self, tool_name: str):
        self.tool_name = tool_name
        self.price_usdc = TOOL_PRICES.get(tool_name, 0.10)
        self.payment_address = PAYMENT_ADDRESS
        super().__init__(
            f"Payment required: {self.price_usdc} USDC to {self.payment_address}"
        )


def verify_payment(
    tool_name: str,
    payment_proof: Optional[str] = None,
) -> bool:
    """
    Verify that a valid x402 payment has been made for this tool call.
    """
    # Payments disabled forr now in dev mode
    if os.getenv("PARTYHAT_PAYMENTS_ENABLED", "false").lower() != "true":
        return True

    # Real x402 verification goes here
    # if not payment_proof:
    #     raise PaymentRequired(tool_name)
    # verified = x402_verify(payment_proof, TOOL_PRICES[tool_name], PAYMENT_ADDRESS)
    # if not verified:
    #     raise PaymentRequired(tool_name)
    # #################

    return True
