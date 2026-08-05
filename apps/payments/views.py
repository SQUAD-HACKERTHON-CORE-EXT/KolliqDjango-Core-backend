"""
apps/payments/views.py
======================
Changes from original:
  - SquadWebhookView replaced with PaystackWebhookView
    (full handler is in apps/payments/webhook_views.py — this file just re-exports it)
  - squad_reference → paystack_reference on Transaction duplicate check
  - process_squad_webhook → process_paystack_webhook
  - InternalWebhookView updated to call process_paystack_webhook
"""

import json
import logging

from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from kolliq.utils import success_response
from .models import Transaction
from .serializers import TransactionSerializer

# The full Paystack webhook handler lives here — import and re-use it
from apps.payments.webhook_views import PaystackWebhookView  # noqa: F401

logger = logging.getLogger(__name__)


# ── Transaction history ───────────────────────────────────────────────────────

class TransactionListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='transactions_list',
        summary='Get transaction history',
        description='Returns the last 50 transactions for the authenticated user, ordered by most recent.',
        request=None,
        responses={
            200: OpenApiResponse(response=TransactionSerializer, description='Transaction history.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        examples=[
            OpenApiExample(
                'Transaction list response',
                value={
                    'transactions': [
                        {'id': 'txn123', 'amount': '5000.00', 'transaction_type': 'credit', 'status': 'success'}
                    ],
                    'count': 1,
                },
                response_only=True,
            ),
        ],
        tags=['Payments'],
    )
    def get(self, request):
        transactions = Transaction.objects.filter(
            user=request.user
        ).order_by('-created_at')[:50]
        return success_response({
            'transactions': TransactionSerializer(transactions, many=True).data,
            'count': transactions.count(),
        })


# ── Paystack Webhook ──────────────────────────────────────────────────────────
# Full implementation is in apps/payments/webhook_views.py
# PaystackWebhookView is imported above and registered in urls.py as:
#   path('webhooks/paystack/', PaystackWebhookView.as_view(), name='paystack-webhook')


# ── Internal Webhook (Node partner service → Django) ─────────────────────────

@method_decorator(csrf_exempt, name='dispatch')
class InternalWebhookView(APIView):
    """
    POST /webhooks/internal/
    Called by the Node partner service to forward Paystack events.
    Secured by a shared secret in the x-internal-secret header.
    """
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id='payments_internal_webhook',
        summary='Internal webhook',
        description=(
            'Internal webhook called by the Node partner service to forward Paystack events. '
            'Secured by a shared secret in the x-internal-secret header.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Event received.'),
            400: OpenApiResponse(description='Invalid JSON payload.'),
            401: OpenApiResponse(description='Invalid or missing internal secret.'),
        },
        tags=['Payments'],
    )
    def post(self, request):
        from django.conf import settings

        internal_secret = request.headers.get('x-internal-secret', '')
        expected = getattr(settings, 'INTERNAL_WEBHOOK_SECRET', '')
        if expected and internal_secret != expected:
            return Response({'status': 'unauthorized'}, status=401)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return Response({'status': 'bad payload'}, status=400)

        # ← renamed from process_squad_webhook
        from .tasks import process_paystack_webhook
        process_paystack_webhook.delay(payload)
        return Response({'status': 'received'}, status=200)