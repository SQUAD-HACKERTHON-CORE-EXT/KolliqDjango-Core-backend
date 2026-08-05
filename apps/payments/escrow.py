"""
services/escrow.py — FINAL, properly wired to services/ledger.py
====================================================================
Earlier versions of this file mutated wallet.escrow_balance directly
with no locking or idempotency. This version routes every balance
change through services/ledger.py, which provides SELECT FOR UPDATE
locking and duplicate-webhook protection.

Two DVAs feed into this:
  1. KOLLIQ ESCROW DVA — employer sends job-funding money here
  2. Each user's personal DVA — handled separately in webhook_views.py

Both land in the same Paystack balance — this file just updates the
DB-level accounting of who owns what slice of it.
"""

from decimal import Decimal
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

PLATFORM_FEE_PERCENT = Decimal(str(getattr(settings, 'PLATFORM_FEE_PERCENT', '0')))


def get_escrow_payment_instructions(job) -> dict:
    """Returns the bank account and amount the employer must transfer to fund the job."""
    short_ref = str(job.id).replace('-', '')[:12].upper()
    total_amount = job.pay_per_worker * job.workers_needed

    job.escrow_reference = short_ref
    job.save(update_fields=['escrow_reference', 'updated_at'])

    return {
        'account_number': settings.KOLLIQ_ESCROW_DVA_NUMBER,
        'bank_name': getattr(settings, 'KOLLIQ_ESCROW_DVA_BANK', 'Wema Bank'),
        'account_name': getattr(settings, 'KOLLIQ_ESCROW_DVA_NAME', 'Kolliq Escrow'),
        'amount': float(total_amount),
        'reference': short_ref,
        'instruction': (
            f'Transfer exactly ₦{total_amount:,.0f} to the account above. '
            f"Include reference '{short_ref}' in your payment narration. "
            f'Job will go live within 60 seconds of payment confirmation.'
        ),
    }


def match_escrow_payment_to_job(
    narration: str,
    amount: Decimal,
    paystack_reference: str,
) -> bool:
    """
    Matches an incoming escrow DVA payment to a pending job by scanning
    for the job's escrow_reference inside the payment narration.

    Routes the actual balance update through services.ledger.credit_escrow_inbound()
    — locked and idempotent, safe against duplicate webhook deliveries.
    """
    from apps.jobs.models import Job
    from services.ledger import credit_escrow_inbound

    narration_upper = (narration or '').upper()

    pending_jobs = Job.objects.filter(
        escrow_funded=False,
        escrow_reference__isnull=False,
    ).exclude(escrow_reference='')

    matched_job = None
    for job in pending_jobs:
        if job.escrow_reference in narration_upper:
            matched_job = job
            break

    if not matched_job:
        logger.warning(
            f'Escrow payment unmatched: narration="{narration}" '
            f'amount=₦{amount} ref={paystack_reference}'
        )
        return False

    result = credit_escrow_inbound(
        employer_wallet_id=str(matched_job.employer.wallet.id),
        job_id=str(matched_job.id),
        amount=amount,
        paystack_reference=paystack_reference,
        narration=narration,
    )

    if not result.get('credited'):
        # Already processed (duplicate webhook) — job may already be live
        logger.info(f'Escrow payment already processed for job {matched_job.id}')
        return True

    matched_job.escrow_funded = True
    matched_job.save(update_fields=['escrow_funded', 'updated_at'])

    logger.info(
        f'Escrow matched: job={matched_job.id} '
        f'ref={matched_job.escrow_reference} amount=₦{amount}'
    )

    from apps.jobs.tasks import trigger_job_matching_notifications
    trigger_job_matching_notifications.delay(str(matched_job.id))

    return True


def release_escrow(job_id: str, worker_id: str):
    """
    Called when employer marks a job as completed.
    Routes through services.ledger.release_escrow() — locked, idempotent,
    double-entry (debits employer escrow, credits worker, records platform fee).
    """
    from apps.jobs.models import Job
    from django.contrib.auth import get_user_model
    from services.ledger import release_escrow as ledger_release_escrow

    User = get_user_model()

    job = Job.objects.select_related('employer__wallet').get(id=job_id)
    worker = User.objects.select_related('wallet').get(id=worker_id)

    gross = job.pay_per_worker

    result = ledger_release_escrow(
        job_id=job_id,
        employer_wallet_id=str(job.employer.wallet.id),
        worker_wallet_id=str(worker.wallet.id),
        gross=gross,
        platform_fee_percent=PLATFORM_FEE_PERCENT,
    )

    if not result.get('released'):
        logger.info(f'Escrow release already processed: job={job_id} worker={worker_id}')
        return

    logger.info(
        f'Escrow released: job={job_id} worker={worker_id} '
        f'net=₦{result["net_to_worker"]} fee=₦{result["platform_fee"]}'
    )

    from apps.scoring.tasks import recalculate_score
    from services.notifications import notify_worker_payment

    recalculate_score.delay(str(worker_id))
    notify_worker_payment.delay(
        str(worker_id),
        str(result['net_to_worker']),
        str(result['worker_new_balance']),
    )