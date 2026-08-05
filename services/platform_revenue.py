"""
services/platform_revenue.py — FINAL, fixed to match the no-bank-details decision
=====================================================================================
PlatformPayout (with real bank details) was deleted. Replaced with
PlatformWithdrawalLog — just an amount and a note, filled in manually
by the admin after they withdraw via the Paystack Dashboard directly.
No bank account number, code, or name is ever stored by Kolliq's app.
"""

import logging
from decimal import Decimal
from django.db.models import Sum

logger = logging.getLogger(__name__)


def get_total_platform_fees_collected() -> Decimal:
    """Lifetime sum of every fee Kolliq has earned."""
    from apps.payments.models import Transaction

    total = Transaction.objects.filter(
        transaction_type=Transaction.Type.PLATFORM_FEE,
        status=Transaction.Status.SUCCESS,
    ).aggregate(total=Sum('amount'))['total']

    return total or Decimal('0.00')


def get_total_logged_withdrawals() -> Decimal:
    """
    Sum of withdrawals the admin has manually logged after pulling money
    out via the Paystack Dashboard. Self-reported, not verified against
    Paystack — exists purely so the dashboard number stays meaningful
    instead of just growing forever.
    """
    from apps.payments.models import PlatformWithdrawalLog

    total = PlatformWithdrawalLog.objects.aggregate(total=Sum('amount'))['total']
    return total or Decimal('0.00')


def get_available_platform_revenue() -> Decimal:
    """Amount Kolliq has earned and not yet logged as withdrawn."""
    collected = get_total_platform_fees_collected()
    logged_out = get_total_logged_withdrawals()
    available = collected - logged_out

    logger.info(
        f'Platform revenue: collected=₦{collected} '
        f'logged_withdrawn=₦{logged_out} available=₦{available}'
    )
    return available


def get_revenue_breakdown() -> dict:
    from apps.payments.models import Transaction

    by_source = (
        Transaction.objects
        .filter(transaction_type=Transaction.Type.PLATFORM_FEE, status=Transaction.Status.SUCCESS)
        .values('metadata__fee_type')
        .annotate(total=Sum('amount'))
    )

    return {
        'total_collected':   str(get_total_platform_fees_collected()),
        'total_logged_out':  str(get_total_logged_withdrawals()),
        'available':         str(get_available_platform_revenue()),
        'by_source': {
            (row['metadata__fee_type'] or 'unknown'): str(row['total'])
            for row in by_source
        },
    }