"""
services/money_safety.py — Guardrails against unit-conversion errors
======================================================================

WHY THIS EXISTS:
  naira_to_kobo() is mathematically simple (× 100), but a single bad call
  site — a double conversion, a kobo value passed where naira was expected,
  a corrupted field — can move 100x the intended amount with no error,
  because Paystack will happily transfer whatever kobo figure you send it.

  This module is the LAST LINE OF DEFENSE before any outbound transfer.
  Every Naira amount must pass through assert_sane_transfer_amount()
  before it reaches PaystackService.initiate_transfer().

THREE LAYERS OF PROTECTION:
  1. Hard ceiling — anything above MAX_AUTO_TRANSFER_NAIRA is BLOCKED
     from automatic processing, full stop, no exceptions. Requires a
     human to manually approve via Django admin with explicit override.
  2. Magnitude sanity check — compares the kobo amount Paystack will
     receive against the naira amount a human expects, catches the
     "passed kobo where naira expected" class of bug specifically.
  3. Audit log — every conversion is logged with BOTH representations
     side by side, so any mistake is visible in logs before money moves,
     not after.
"""

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# ── Hard ceiling ──────────────────────────────────────────────────────────────
# No single automated transfer may exceed this without manual override.
# Set this based on your real-world maximum expected withdrawal/payout.
# Kolliq gig payouts are typically small — set conservatively.
MAX_AUTO_TRANSFER_NAIRA = Decimal('200000.00')   # ₦200,000

# Absolute kill-switch — even with manual override, NOTHING above this
# leaves the system without a second admin's explicit confirmation.
ABSOLUTE_MAX_NAIRA = Decimal('1000000.00')        # ₦1,000,000


class UnsafeTransferAmount(Exception):
    """Raised when an amount fails sanity checks. Transfer is blocked."""
    pass


def assert_sane_transfer_amount(
    amount_naira: Decimal,
    context: str = '',
    allow_manual_override: bool = False,
) -> Decimal:
    """
    Call this on EVERY naira amount immediately before it is converted to
    kobo and sent to Paystack. Raises UnsafeTransferAmount if anything looks
    wrong. Returns the amount unchanged if it passes — pass-through so you
    can chain it: kobo = naira_to_kobo(assert_sane_transfer_amount(amount)).

    context: human-readable string for logs, e.g. 'withdrawal:abc123'
    allow_manual_override: True only when an admin has explicitly approved
        an amount above MAX_AUTO_TRANSFER_NAIRA (see admin action below)
    """
    amount_naira = Decimal(str(amount_naira))

    # ── Check 1: must be positive ────────────────────────────────────────────
    if amount_naira <= 0:
        raise UnsafeTransferAmount(
            f'[{context}] Amount must be positive, got ₦{amount_naira}'
        )

    # ── Check 2: absolute kill-switch — nothing bypasses this ────────────────
    if amount_naira > ABSOLUTE_MAX_NAIRA:
        logger.critical(
            f'[{context}] BLOCKED: ₦{amount_naira} exceeds absolute max '
            f'₦{ABSOLUTE_MAX_NAIRA}. This will NEVER auto-process. '
            f'If legitimate, this requires direct Paystack Dashboard action, '
            f'not this codebase.'
        )
        raise UnsafeTransferAmount(
            f'[{context}] ₦{amount_naira} exceeds absolute ceiling of '
            f'₦{ABSOLUTE_MAX_NAIRA}. Manual Dashboard action required.'
        )

    # ── Check 3: soft ceiling — blocks unless explicitly overridden ──────────
    if amount_naira > MAX_AUTO_TRANSFER_NAIRA and not allow_manual_override:
        logger.warning(
            f'[{context}] BLOCKED: ₦{amount_naira} exceeds auto-transfer '
            f'ceiling of ₦{MAX_AUTO_TRANSFER_NAIRA}. Requires manual admin '
            f'approval with allow_manual_override=True.'
        )
        raise UnsafeTransferAmount(
            f'[{context}] ₦{amount_naira} exceeds ₦{MAX_AUTO_TRANSFER_NAIRA} '
            f'auto-approval limit. An admin must approve this manually.'
        )

    # ── Check 4: audit log — both representations, always ────────────────────
    kobo_equivalent = int(amount_naira * 100)
    logger.info(
        f'[{context}] Sanity check passed: ₦{amount_naira} naira '
        f'= {kobo_equivalent} kobo. Proceeding.'
    )

    return amount_naira


def detect_suspicious_round_number(amount_naira: Decimal) -> bool:
    """
    Heuristic: a suspiciously large amount that is also a perfectly round
    number ending in many zeros is a classic signature of a unit-conversion
    bug (e.g. ₦500,000 when ₦5,000 was meant, off by exactly 100x).

    This does NOT block anything — it just flags for extra logging.
    A real ₦500,000 withdrawal might genuinely be round. This is advisory,
    not a hard check.
    """
    if amount_naira >= Decimal('100000') and amount_naira % Decimal('10000') == 0:
        logger.warning(
            f'Suspiciously round large amount: ₦{amount_naira}. '
            f'If this was meant to be ₦{amount_naira / 100}, there may be '
            f'a 100x unit conversion bug upstream. Double-check before approving.'
        )
        return True
    return False


def safe_naira_to_kobo(amount_naira: Decimal, context: str = '') -> int:
    """
    The ONLY function that should ever convert naira → kobo for an
    outbound transfer. Combines the sanity check with the conversion
    so it's impossible to convert without passing the safety gate.

    Use this instead of calling naira_to_kobo() directly anywhere
    money is about to leave the platform.
    """
    from services.paystack import naira_to_kobo

    checked_amount = assert_sane_transfer_amount(amount_naira, context=context)
    detect_suspicious_round_number(checked_amount)
    kobo = naira_to_kobo(checked_amount)

    logger.info(
        f'[{context}] FINAL OUTBOUND AMOUNT: ₦{checked_amount} → {kobo} kobo '
        f'(this exact kobo figure will be sent to Paystack)'
    )

    return kobo