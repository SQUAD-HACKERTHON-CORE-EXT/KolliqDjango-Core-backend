"""
apps/payments/tasks.py
======================
Changes from original:
  - process_squad_webhook → process_paystack_webhook
  - squad_reference → paystack_reference on all Transaction lookups
  - Webhook parsing now uses PaystackService.parse_dva_webhook()
    instead of SquadService.parse_webhook_payload()
  - Duplicate detection uses paystack_reference field
"""

from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def process_paystack_webhook(self, payload: dict):
    """
    Celery task that processes a Paystack webhook payload.
    Called by:
      - PaystackWebhookView (apps/payments/webhook_views.py) for direct Paystack events
      - InternalWebhookView (apps/payments/views.py) for Node-forwarded events

    The full event dispatch logic lives in webhook_views.py.
    This task exists as a fallback for async processing of the same events
    (e.g. when the Node service forwards events and you want them queued).

    Duplicate detection: checks paystack_reference before processing.
    """
    from services.paystack import PaystackService
    from services.escrow import match_escrow_payment_to_job
    from apps.payments.models import Transaction
    from apps.wallets.models import Wallet
    from django.conf import settings
    from decimal import Decimal

    event = payload.get('event', '')
    logger.info(f'process_paystack_webhook: event={event}')

    if event == 'charge.success':
        paystack = PaystackService()
        parsed = paystack.parse_dva_webhook(payload)

        tx_ref = parsed['transaction_reference']
        amount = parsed['principal_amount']
        narration = parsed['narration']
        va_number = parsed['virtual_account_number']

        # ── Duplicate check ───────────────────────────────────────────────────
        if tx_ref and Transaction.objects.filter(paystack_reference=tx_ref).exists():
            logger.info(f'Paystack webhook duplicate ignored: {tx_ref}')
            return

        # ── Route: escrow DVA or personal DVA ────────────────────────────────
        escrow_va = getattr(settings, 'KOLLIQ_ESCROW_DVA_NUMBER', '')

        if va_number == escrow_va:
            matched = match_escrow_payment_to_job(
                narration=narration,
                amount=amount,
                paystack_reference=tx_ref,
            )
            if not matched:
                logger.warning(
                    f'Unmatched escrow payment in task: VA={va_number} '
                    f'₦{amount} narration="{narration}"'
                )
        else:
            # Personal DVA top-up
            try:
                wallet = Wallet.objects.select_related('user').get(
                    paystack_account_number=va_number
                )
            except Wallet.DoesNotExist:
                logger.warning(f'No wallet found for DVA {va_number}')
                return

            from django.db import transaction as db_tx
            with db_tx.atomic():
                wallet.credit(amount)
                Transaction.objects.create(
                    user=wallet.user,
                    transaction_type=Transaction.Type.CREDIT,
                    amount=amount,
                    status=Transaction.Status.SUCCESS,
                    paystack_reference=tx_ref,
                    description='Wallet top-up via bank transfer',
                    metadata={
                        'channel': parsed.get('channel'),
                        'narration': narration,
                        'paid_at': parsed.get('paid_at'),
                    },
                )
            logger.info(f'Wallet top-up processed: user={wallet.user_id} ₦{amount}')

    elif event in ('transfer.success', 'transfer.failed', 'transfer.reversed'):
        # These are handled in webhook_views.py directly.
        # If they arrive here via the internal webhook route, delegate back.
        from apps.payments.webhook_views import PaystackWebhookView
        handler_map = {
            'transfer.success':  '_on_transfer_success',
            'transfer.failed':   '_on_transfer_failed',
            'transfer.reversed': '_on_transfer_reversed',
        }
        view = PaystackWebhookView()
        from services.paystack import PaystackService
        getattr(view, handler_map[event])(PaystackService(), payload)

    else:
        logger.debug(f'process_paystack_webhook: unhandled event={event}')


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def release_escrow_for_job(self, job_id: str, worker_id: str):
    """
    Celery task to release escrow for a completed job.
    Called by JobCompleteView after employer confirms job done.
    Delegates to the escrow engine (services/escrow.py).
    """
    from services.escrow import release_escrow

    try:
        release_escrow(job_id=job_id, worker_id=worker_id)
        logger.info(f'Escrow released: job={job_id} worker={worker_id}')
    except Exception as exc:
        logger.error(f'release_escrow_for_job failed: job={job_id} worker={worker_id} {exc}')
        raise self.retry(exc=exc)