"""
apps/wallets/payout_tasks.py
============================
Celery task called by admin when a WithdrawalRequest is approved.

Full withdrawal flow:
  User requests withdrawal (views.py)
    → wallet.balance debited immediately
    → WithdrawalRequest created with status=PENDING

  Admin approves in Django admin
    → process_withdrawal.delay(withdrawal_id) fired
    → Paystack Transfer API called
    → paystack_reference saved, status → PROCESSING

  Paystack fires webhook (webhook_views.py)
    → transfer.success → status → COMPLETED
    → transfer.failed  → status → FAILED + wallet refunded
    → transfer.reversed → status → FAILED + wallet refunded

Paystack transfer fees (deducted from Kolliq's Paystack balance):
  ≤ ₦5,000:  ₦10 flat
  > ₦5,000:  1% capped at ₦300

NOTE: If the Paystack account is on Starter Business tier, transfers are
rejected outright ("You cannot initiate third party payouts as a starter
business") — this requires a compliance upgrade to Registered Business in
the Paystack Dashboard, not a retry. See the dedicated branch below.
"""

from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_withdrawal(self, withdrawal_id: str):
    """
    Execute an approved withdrawal via Paystack Transfers API.

    Steps:
      1. Get or create transfer recipient (cached on wallet.paystack_recipient_code)
      2. Initiate transfer with deterministic reference (safe to retry)
      3. Save reference + set status to PROCESSING
      4. Paystack webhook handles final COMPLETED / FAILED state

    On timeout (408): verify transfer before retrying — NEVER retry with new reference.
    """
    from apps.wallets.models import WithdrawalRequest
    from services.paystack import PaystackService, PaystackAPIError

    try:
        withdrawal = WithdrawalRequest.objects.select_related(
            'wallet__user'
        ).get(id=withdrawal_id)
    except WithdrawalRequest.DoesNotExist:
        logger.error(f'process_withdrawal: WithdrawalRequest {withdrawal_id} not found')
        return

    if withdrawal.status != WithdrawalRequest.Status.APPROVED:
        logger.info(f'Withdrawal {withdrawal_id} is {withdrawal.status} — skipping')
        return

    paystack = PaystackService()

    # Deterministic reference: same withdrawal always gets same reference
    # Paystack deduplicates within 24h — safe to retry with same ref
    reference = f'KLQ-{str(withdrawal.id).replace("-", "")[:20]}'

    try:
        # ── Step 1: Get or create Paystack transfer recipient ─────────────────
        wallet = withdrawal.wallet
        recipient_code = wallet.paystack_recipient_code

        if not recipient_code:
            recipient_result = paystack.create_transfer_recipient(
                account_name=withdrawal.bank_account_name,
                account_number=withdrawal.bank_account_number,
                bank_code=withdrawal.bank_code,
            )
            recipient_code = recipient_result.get('recipient_code', '')

            # Cache it to avoid re-creating on future withdrawals to same account
            wallet.paystack_recipient_code = recipient_code
            wallet.save(update_fields=['paystack_recipient_code', 'updated_at'])
            logger.info(f'Recipient created: {recipient_code} for withdrawal {withdrawal_id}')

        # ── Step 2: Initiate transfer ─────────────────────────────────────────
        result = paystack.initiate_transfer(
            amount_naira=withdrawal.amount,
            recipient_code=recipient_code,
            reference=reference,
            reason=f'Kolliq withdrawal {str(withdrawal.id)[:8]}',
        )

        # ── Step 3: Save reference and update status ──────────────────────────
        withdrawal.paystack_reference = reference
        withdrawal.status             = WithdrawalRequest.Status.PROCESSING
        withdrawal.save(update_fields=['paystack_reference', 'status', 'updated_at'])

        logger.info(
            f'Withdrawal processing: id={withdrawal_id} '
            f'ref={reference} transfer_code={result.get("transfer_code")} '
            f'status={result.get("status")}'
        )

    except PaystackAPIError as exc:
        if exc.status_code == 408:
            # Timeout — verify what actually happened before doing anything
            logger.warning(
                f'Transfer timeout for withdrawal {withdrawal_id} ref={reference} '
                f'— verifying status'
            )
            try:
                verify = paystack.verify_transfer(reference)
                transfer_status = verify.get('status', '')

                if transfer_status in ('success', 'pending', 'otp'):
                    # Transfer is in-flight — just mark PROCESSING and let webhook finish
                    withdrawal.paystack_reference = reference
                    withdrawal.status             = WithdrawalRequest.Status.PROCESSING
                    withdrawal.save(update_fields=['paystack_reference', 'status', 'updated_at'])
                    logger.info(
                        f'Transfer already in-flight ({transfer_status}) — marked PROCESSING'
                    )
                    return

                # If failed/reversed/not found, fall through to retry
                logger.warning(
                    f'Transfer verify returned {transfer_status} — will retry'
                )
            except PaystackAPIError as verify_exc:
                logger.error(f'Transfer verify also failed: {verify_exc}')

        logger.error(
            f'Paystack transfer failed for withdrawal {withdrawal_id}: '
            f'{exc} | raw={exc.raw}'
        )

        # Starter Business tier cannot send third-party payouts at all — no
        # amount of retrying fixes this, it needs a Paystack Dashboard
        # compliance upgrade to Registered Business. Fail fast instead of
        # burning 3 retries x 60s on every withdrawal attempt.
        msg = str(exc).lower()
        if 'starter business' in msg or 'third party payouts' in msg:
            withdrawal.status = WithdrawalRequest.Status.FAILED
            withdrawal.rejection_reason = (
                'Platform payout account is on Starter tier and cannot send '
                'third-party transfers. Upgrade to Registered Business in the '
                'Paystack Dashboard to enable withdrawals.'
            )
            withdrawal.save(update_fields=['status', 'rejection_reason', 'updated_at'])

            from services.ledger import reverse_withdrawal
            reverse_withdrawal(
                wallet_id=str(withdrawal.wallet_id),
                amount=withdrawal.amount,
                withdrawal_request_id=str(withdrawal.id),
                reason=withdrawal.rejection_reason,
            )
            logger.error(
                f'Withdrawal {withdrawal_id} permanently failed and refunded — '
                f'Paystack account needs Registered Business upgrade. Not retrying.'
            )
            return

        raise self.retry(exc=exc)


@shared_task
def reconcile_pending_withdrawals():
    """
    Periodic task — run every 30 minutes via Celery Beat.
    Checks all PROCESSING withdrawals and verifies their Paystack status.
    Catches cases where the webhook was missed.

    Add to CELERY_BEAT_SCHEDULE:
        'reconcile-withdrawals': {
            'task': 'apps.wallets.payout_tasks.reconcile_pending_withdrawals',
            'schedule': crontab(minute='*/30'),
        }
    """
    from apps.wallets.models import WithdrawalRequest
    from services.paystack import PaystackService, PaystackAPIError
    from django.db import transaction as db_tx
    from django.utils import timezone
    from datetime import timedelta

    # Only check withdrawals that have been PROCESSING for more than 10 minutes
    cutoff = timezone.now() - timedelta(minutes=10)
    stuck = WithdrawalRequest.objects.filter(
        status=WithdrawalRequest.Status.PROCESSING,
        updated_at__lt=cutoff,
        paystack_reference__isnull=False,
    ).exclude(paystack_reference='')

    if not stuck.exists():
        return

    paystack = PaystackService()
    logger.info(f'Reconciling {stuck.count()} stuck PROCESSING withdrawals')

    for w in stuck:
        try:
            result = paystack.verify_transfer(w.paystack_reference)
            transfer_status = result.get('status', '')

            if transfer_status == 'success':
                w.status = WithdrawalRequest.Status.COMPLETED
                w.save(update_fields=['status', 'updated_at'])
                logger.info(f'Reconciled COMPLETED: withdrawal={w.id}')

            elif transfer_status in ('failed', 'reversed'):
                with db_tx.atomic():
                    w.status = WithdrawalRequest.Status.FAILED
                    w.rejection_reason = f'Reconciled from Paystack status: {transfer_status}'
                    w.save(update_fields=['status', 'rejection_reason', 'updated_at'])
                    w.wallet.credit(w.amount)  # Refund
                logger.info(f'Reconciled FAILED & refunded: withdrawal={w.id} ₦{w.amount}')

        except PaystackAPIError as e:
            logger.error(f'Reconcile error for withdrawal {w.id}: {e}')
            continue