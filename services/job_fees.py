"""
services/job_fees.py — Tiered Job-Creation Platform Fee
==========================================================

NO PLATFORM WALLET INVOLVED. This is the whole point of this redesign.

OLD MODEL (removed):
  5% taken from escrow on job COMPLETION, credited to a fake "platform
  user" wallet (ARISE_WALLET_ID) that had to be created, tracked, and
  eventually withdrawn from like any other user.

NEW MODEL:
  A flat per-worker fee charged upfront at job CREATION, scaled by how
  much each worker is being paid. No wallet. No fake user. The fee is
  simply debited from the employer's wallet and logged as a
  PLATFORM_FEE Transaction. The money never "moves" anywhere — it just
  stays inside Kolliq's single Paystack balance, and the SUM of all
  PLATFORM_FEE transactions (minus payouts already made) tells you
  exactly how much of that shared balance actually belongs to Kolliq.

FEE TABLE (₦100 per ₦5,000 band of pay_per_worker):
  ₦0      – ₦5,000   → ₦100 per worker
  ₦5,001  – ₦10,000  → ₦200 per worker
  ₦10,001 – ₦15,000  → ₦300 per worker
  ₦15,001 – ₦20,000  → ₦400 per worker
  ... and so on, ₦100 higher per ₦5,000 band.

Formula: fee_per_head = 100 * ceil(pay_per_worker / 5000)
"""

import math
import logging
from decimal import Decimal, ROUND_UP

logger = logging.getLogger(__name__)

FEE_BAND_SIZE   = Decimal('5000')   # naira width of each pricing band
FEE_PER_BAND    = Decimal('100')    # fee increase per band


def calculate_fee_per_head(pay_per_worker: Decimal) -> Decimal:
    """
    Returns the platform fee charged per worker, based on how much
    that worker is being paid.

    Examples:
      ₦3,000  → ₦100
      ₦5,000  → ₦100
      ₦5,001  → ₦200
      ₦10,000 → ₦200
      ₦23,000 → ₦500
    """
    pay_per_worker = Decimal(str(pay_per_worker))
    if pay_per_worker <= 0:
        return Decimal('0')

    tier = int((pay_per_worker / FEE_BAND_SIZE).to_integral_value(rounding=ROUND_UP))
    if tier < 1:
        tier = 1

    return FEE_PER_BAND * tier


def calculate_total_job_creation_fee(
    pay_per_worker: Decimal,
    workers_needed: int,
) -> Decimal:
    """
    Total upfront fee an employer pays when creating a job.
    = fee_per_head × number of workers needed
    """
    fee_per_head = calculate_fee_per_head(pay_per_worker)
    total = (fee_per_head * workers_needed).quantize(Decimal('0.01'))
    return total


def charge_job_creation_fee(employer_wallet_id: str, job_id: str, fee_amount: Decimal) -> dict:
    """
    Debits the employer's wallet for the job-creation fee and records
    a PLATFORM_FEE Transaction. No wallet is credited — this IS the
    platform's revenue record. Use services.platform_revenue to
    compute total accumulated revenue later.

    Idempotent: keyed by job_id, safe to retry if called twice.
    Race-safe: uses SELECT FOR UPDATE on the employer wallet.

    Raises ValueError if the employer's wallet balance is insufficient.
    """
    from django.db import transaction as db_tx
    from apps.wallets.models import Wallet
    from apps.payments.models import Transaction

    if fee_amount <= 0:
        return {'charged': False, 'reason': 'zero_fee'}

    idempotency_key = f'job_creation_fee:{job_id}'

    if Transaction.objects.filter(
        idempotency_key=idempotency_key,
        transaction_type=Transaction.Type.PLATFORM_FEE,
    ).exists():
        logger.info(f'charge_job_creation_fee: already charged for job {job_id}')
        return {'charged': False, 'reason': 'duplicate'}

    with db_tx.atomic():
        wallet = Wallet.objects.select_for_update().get(id=employer_wallet_id)

        if wallet.balance < fee_amount:
            raise ValueError(
                f'Insufficient wallet balance for job creation fee. '
                f'Need ₦{fee_amount}, have ₦{wallet.balance}.'
            )

        wallet.balance -= fee_amount
        wallet.save(update_fields=['balance', 'updated_at'])

        Transaction.objects.create(
            user=wallet.user,
            transaction_type=Transaction.Type.PLATFORM_FEE,
            amount=fee_amount,
            status=Transaction.Status.SUCCESS,
            idempotency_key=idempotency_key,
            job_id=job_id,
            description=f'Job creation fee — job #{str(job_id)[:8]}',
            metadata={'job_id': job_id, 'fee_type': 'job_creation'},
        )

    logger.info(f'Job creation fee charged: ₦{fee_amount} for job={job_id} wallet={employer_wallet_id}')
    return {'charged': True, 'amount': fee_amount, 'new_balance': wallet.balance}