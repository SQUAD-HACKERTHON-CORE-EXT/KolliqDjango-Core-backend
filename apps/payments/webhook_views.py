'''
apps/payments/webhook_views.py
=========================================
No BVN handlers (removed — Kolliq doesn't need them).
No platform payout routing (removed — withdrawals settled manually via Dashboard).
All balance changes route through services/ledger.py for locking + idempotency.

FUNDING PATH: checkout (initialize_transaction), not DVA.
Users fund their wallet via Paystack Checkout — charge.success webhooks
for that flow carry metadata.purpose == 'wallet_funding' and
metadata.wallet_id, which we match directly. No DVA account number
matching is needed for this path.

The DVA-matching branch (_credit_personal_wallet / escrow VA matching)
stays intact and dormant — it activates automatically once DVA
provisioning resumes in live mode (apps/wallets/tasks.py::provision_pending_dvas),
since a DVA-funded charge.success has no metadata.purpose and falls
through to that branch instead.

Handles:
  charge.success      → checkout wallet funding (active), or
                         escrow DVA match / personal DVA top-up (dormant)
  transfer.success    → user withdrawal completed
  transfer.failed     → user withdrawal failed, wallet refunded
  transfer.reversed   → same as failed
'''
import json
import logging

from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

from services.paystack import PaystackService

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class PaystackWebhookView(APIView):
    permission_classes = []
    authentication_classes = []

    def post(self, request):
        raw_body = request.body  # bytes — must read before any parsing

        signature = request.headers.get('X-Paystack-Signature', '')
        if not signature:
            logger.warning('Webhook received with no signature header')
            return Response(status=status.HTTP_400_BAD_REQUEST)

        paystack = PaystackService()
        if not paystack.verify_webhook_signature(raw_body, signature):
            logger.warning('Webhook signature FAILED')
            return Response(status=status.HTTP_401_UNAUTHORIZED)

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            return Response(status=status.HTTP_400_BAD_REQUEST)

        event = payload.get('event', '')
        logger.info(f'Paystack webhook: event={event}')

        handlers = {
            'charge.success':   self._on_charge_success,
            'transfer.success': self._on_transfer_success,
            'transfer.failed':  self._on_transfer_failed,
            'transfer.reversed': self._on_transfer_reversed,
        }
        handler = handlers.get(event)
        if handler:
            try:
                handler(paystack, payload)
            except Exception as e:
                logger.error(f'Webhook handler error [{event}]: {e}', exc_info=True)

        return Response({'status': 'ok'}, status=status.HTTP_200_OK)

    def _on_charge_success(self, paystack: PaystackService, payload: dict):
        """
        Routes charge.success by funding type:
          1. Checkout wallet funding (metadata.purpose == 'wallet_funding') — active path
          2. Escrow DVA match / personal DVA top-up — dormant until live-mode DVA
        """
        data = payload.get('data', {})
        metadata = data.get('metadata') or {}

        if metadata.get('purpose') == 'wallet_funding':
            self._credit_wallet_from_checkout(paystack, payload)
            return

        # ── Dormant DVA path — only reachable once DVA provisioning resumes ────
        from django.conf import settings
        from services.escrow import match_escrow_payment_to_job

        parsed = paystack.parse_dva_webhook(payload)
        va_number = parsed['virtual_account_number']
        amount    = parsed['principal_amount']
        narration = parsed['narration']
        ref       = parsed['transaction_reference']

        escrow_va = getattr(settings, 'KOLLIQ_ESCROW_DVA_NUMBER', '')

        if va_number == escrow_va:
            matched = match_escrow_payment_to_job(
                narration=narration, amount=amount, paystack_reference=ref,
            )
            if not matched:
                logger.warning(
                    f'Unmatched escrow payment: VA={va_number} ₦{amount} '
                    f'narration="{narration}" ref={ref} — manual review needed'
                )
        elif va_number:
            self._credit_personal_wallet(va_number, amount, ref, parsed)
        else:
            logger.warning(
                f'charge.success with no metadata.purpose and no VA number — '
                f'ref={ref} amount=₦{amount} — cannot route, ignoring'
            )

    def _credit_wallet_from_checkout(self, paystack: PaystackService, payload: dict):
        """
        Credits a wallet funded via Paystack Checkout (initialize_transaction).
        Matches by metadata.wallet_id set when the transaction was initialized —
        NOT by account number, since checkout has no persistent DVA.
        """
        from apps.wallets.models import Wallet
        from services.ledger import credit_inbound

        parsed = paystack.parse_checkout_webhook(payload)
        metadata = parsed['metadata']
        wallet_id = metadata.get('wallet_id')
        ref = parsed['reference']
        amount = parsed['amount_naira']

        if not wallet_id:
            logger.warning(f'Checkout charge.success missing wallet_id in metadata — ref={ref}')
            return

        try:
            wallet = Wallet.objects.get(id=wallet_id)
        except Wallet.DoesNotExist:
            logger.warning(f'Checkout funding: wallet {wallet_id} not found — ref={ref}')
            return

        credit_inbound(
            wallet_id=str(wallet.id),
            amount=amount,
            paystack_reference=ref,
            narration='Wallet funding via checkout',
            channel=parsed.get('channel', ''),
            paid_at=parsed.get('paid_at', ''),
        )
        logger.info(f'Wallet funded via checkout: wallet={wallet.id} amount=₦{amount} ref={ref}')

    def _credit_personal_wallet(self, va_number, amount, ref, parsed):
        """
        Top up a user's wallet from their personal DVA — via ledger, locked +
        idempotent. Dormant until live-mode DVA provisioning resumes.
        """
        from apps.wallets.models import Wallet
        from services.ledger import credit_inbound

        try:
            wallet = Wallet.objects.get(paystack_account_number=va_number)
        except Wallet.DoesNotExist:
            logger.warning(f'No wallet found for DVA {va_number} — skipping credit')
            return

        credit_inbound(
            wallet_id=str(wallet.id),
            amount=amount,
            paystack_reference=ref,
            narration=parsed.get('narration', ''),
            channel=parsed.get('channel', 'dedicated_nuban'),
            paid_at=parsed.get('paid_at', ''),
        )

    def _on_transfer_success(self, paystack: PaystackService, payload: dict):
        from apps.wallets.models import WithdrawalRequest
        from services.ledger import complete_withdrawal

        parsed = paystack.parse_transfer_webhook(payload)
        ref = parsed['reference']

        try:
            w = WithdrawalRequest.objects.get(paystack_reference=ref)
        except WithdrawalRequest.DoesNotExist:
            logger.warning(f'transfer.success: no withdrawal found for ref={ref}')
            return

        if w.status != WithdrawalRequest.Status.COMPLETED:
            w.status = WithdrawalRequest.Status.COMPLETED
            w.save(update_fields=['status', 'updated_at'])
            complete_withdrawal(withdrawal_request_id=str(w.id), paystack_reference=ref)
            logger.info(f'Withdrawal COMPLETED: id={w.id} ref={ref}')

    def _on_transfer_failed(self, paystack: PaystackService, payload: dict):
        from apps.wallets.models import WithdrawalRequest
        from services.ledger import reverse_withdrawal

        parsed = paystack.parse_transfer_webhook(payload)
        ref = parsed['reference']
        reason = parsed.get('failure_reason') or 'Transfer failed'

        try:
            w = WithdrawalRequest.objects.get(
                paystack_reference=ref,
                status__in=[WithdrawalRequest.Status.PENDING, WithdrawalRequest.Status.PROCESSING],
            )
        except WithdrawalRequest.DoesNotExist:
            logger.warning(f'transfer.failed: no active withdrawal found for ref={ref}')
            return

        w.status = WithdrawalRequest.Status.FAILED
        w.rejection_reason = reason
        w.save(update_fields=['status', 'rejection_reason', 'updated_at'])

        reverse_withdrawal(
            wallet_id=str(w.wallet_id),
            amount=w.amount,
            withdrawal_request_id=str(w.id),
            reason=reason,
        )
        logger.info(f'Withdrawal FAILED & refunded: id={w.id} ₦{w.amount} reason={reason}')

    def _on_transfer_reversed(self, paystack: PaystackService, payload: dict):
        self._on_transfer_failed(paystack, payload)