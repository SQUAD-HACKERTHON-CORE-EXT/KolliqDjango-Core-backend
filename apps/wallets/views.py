'''
apps/wallets/views.py — FINAL, all decisions merged, fully documented
========================================================
- NigerianBankListView: live from Paystack, cached 24h
- BankAccountVerifyView/SaveView: validate against live bank list
- WithdrawalRequestView: auto-approve under ₦200,000, pend + SMS admin over it
- FundWalletView: checkout-based funding (Paystack test-mode safe, no DVA needed)
'''

import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.db import transaction as db_transaction
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView

from kolliq.permissions import IsAuthenticatedOrInternalSecret, resolve_user
from kolliq.utils import success_response, error_response
from services.banks import get_bank_list, get_bank_name, get_bank_by_code
from services.paystack import verify_bank_account, PaystackService, PaystackAPIError
from services.money_safety import MAX_AUTO_TRANSFER_NAIRA
from .models import Wallet, WithdrawalRequest
from apps.wallets.serializers import (
    BankAccountVerifySerializer,
    BankAccountSaveSerializer,
    BankAccountDetailSerializer,
    WithdrawalRequestSerializer,
    WithdrawalDetailSerializer,
)

logger = logging.getLogger(__name__)
MINIMUM_WITHDRAWAL = Decimal('2500.00')
MINIMUM_FUNDING = Decimal('100.00')


class WalletDetailView(APIView):
    permission_classes = [IsAuthenticatedOrInternalSecret]

    @extend_schema(
        operation_id='wallet_detail',
        summary='Get wallet details',
        description=(
            'Returns the authenticated user\'s wallet balance and Paystack account info. '
            '`account_number`/`bank_name` will be empty until DVA provisioning resumes in live '
            'mode — funding currently happens via checkout (`/wallets/fund/`), not a persistent '
            'account number. Treat an empty `account_number` as "use Fund Wallet", not an error.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Wallet details.'),
            404: OpenApiResponse(description='Wallet not yet created — retry shortly, provisioning runs async on signup.'),
        },
        examples=[
            OpenApiExample(
                'Response',
                value={
                    'success': True,
                    'data': {
                        'account_number': '',
                        'account_name': '',
                        'bank_name': '',
                        'balance': '5000.00',
                        'escrow_balance': '0.00',
                        'savings_balance': '0.00',
                        'wallet_ready': True,
                        'wallet_status': 'created',
                    },
                    'error': None,
                },
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def get(self, request):
        user, err = resolve_user(request)
        if err:
            return err
        try:
            wallet = user.wallet
        except Wallet.DoesNotExist:
            return error_response('Wallet not yet created. Please try again shortly.', status=404)

        return success_response({
            'account_number':  wallet.paystack_account_number,
            'account_name':    wallet.paystack_account_name,
            'bank_name':       wallet.paystack_bank_name,
            'balance':         str(wallet.balance),
            'escrow_balance':  str(wallet.escrow_balance),
            'savings_balance': str(wallet.savings_balance),
            # NOTE: 'created' now means "Paystack customer exists," not
            # "DVA/account number exists" — DVA is dormant, funding happens
            # via checkout instead. account_number/bank_name will be empty
            # until DVA provisioning resumes in live mode. Frontend should
            # treat empty account_number as "no persistent account yet,
            # use Fund Wallet" rather than a broken/failed state.
            'wallet_ready':    wallet.paystack_creation_status == 'created',
            'wallet_status':   wallet.paystack_creation_status,
        })


class FundWalletView(APIView):
    """
    POST /api/wallets/fund/
    Body: {"amount": "5000.00"}

    Starts a Paystack Checkout session for wallet funding. Returns
    authorization_url for the frontend to redirect the user to (or
    access_code for Paystack's inline popup). The wallet is credited
    later via the charge.success webhook, matched by metadata.wallet_id
    — NOT here, and NOT via any DVA.

    This is the funding path used while Paystack is on a test key —
    works fully in sandbox, no bank-partner DVA dependency.
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='wallet_fund',
        summary='Fund wallet via Paystack Checkout',
        description=(
            'Starts a Paystack Checkout session. Returns `authorization_url` — redirect the '
            'user there (or open it in a webview/browser tab) to complete payment. '
            '**The wallet balance does NOT update from this response.** Crediting happens '
            'asynchronously when Paystack fires a `charge.success` webhook after payment '
            'completes. Poll `GET /wallets/` or listen for a push notification to detect the '
            'balance update — do not assume funding succeeded just because this call returned 200.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(description='Checkout session started.'),
            400: OpenApiResponse(description='Invalid amount, below minimum, or no email on account.'),
            401: OpenApiResponse(description='Not authenticated.'),
            503: OpenApiResponse(description='Could not reach Paystack — retry.'),
        },
        examples=[
            OpenApiExample('Request', value={'amount': '5000.00'}, request_only=True),
            OpenApiExample(
                'Response',
                value={
                    'authorization_url': 'https://checkout.paystack.com/abc123xyz',
                    'access_code': 'abc123xyz',
                    'reference': 'KLQ-FUND-a1b2c3d4e5f6',
                },
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def post(self, request):
        amount_raw = request.data.get('amount')
        try:
            amount = Decimal(str(amount_raw))
        except (InvalidOperation, TypeError):
            return Response({'detail': 'Invalid amount.'}, status=status.HTTP_400_BAD_REQUEST)

        if amount < MINIMUM_FUNDING:
            return Response(
                {'detail': f'Minimum funding amount is ₦{MINIMUM_FUNDING:,.0f}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user
        if not user.email:
            return Response(
                {'detail': 'Your account has no email on file. Please update your profile first.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        wallet, _ = Wallet.objects.get_or_create(user=user)

        reference = f'KLQ-FUND-{uuid.uuid4().hex[:12]}'
        paystack = PaystackService()

        try:
            result = paystack.initialize_transaction(
                email=user.email,
                amount_naira=amount,
                reference=reference,
                metadata={
                    'user_id': str(user.id),
                    'wallet_id': str(wallet.id),
                    'purpose': 'wallet_funding',
                },
            )
        except PaystackAPIError as e:
            logger.error(f'FundWalletView: initialize_transaction failed for user {user.id}: {e}')
            return Response(
                {'detail': 'Could not start payment. Please try again.'},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response({
            'authorization_url': result.get('authorization_url'),
            'access_code':       result.get('access_code'),
            'reference':         reference,
        }, status=status.HTTP_200_OK)


class NigerianBankListView(APIView):
    permission_classes = []

    @extend_schema(
        operation_id='wallets_bank_list',
        summary='List Nigerian banks',
        description=(
            'Public endpoint — no authentication required. Returns bank names and codes for '
            'the bank-account verify/save flow. Cached 24h server-side; pass `?refresh=true` '
            'to force a fresh fetch from Paystack. Pass `?transfer_only=true` to restrict the '
            'list to banks that currently support Paystack transfers (use this for the '
            'withdrawal bank-selection UI specifically, since not every bank in the general '
            'list supports payouts).'
        ),
        request=None,
        responses={200: OpenApiResponse(description='List of banks.')},
        examples=[
            OpenApiExample(
                'Response',
                value={'count': 2, 'banks': [{'name': 'Access Bank', 'code': '044'}, {'name': 'Zenith Bank', 'code': '057'}]},
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def get(self, request):
        force_refresh = request.query_params.get('refresh', '').lower() == 'true'
        transfer_only = request.query_params.get('transfer_only', '').lower() == 'true'

        if transfer_only:
            try:
                from services.paystack import PaystackService
                raw = PaystackService().list_banks(pay_with_bank_transfer=True)
                banks = [{'name': b['name'], 'code': b['code']} for b in raw if b.get('code') and b.get('active', True)]
                banks.sort(key=lambda b: b['name'])
            except Exception as e:
                logger.warning(f'transfer_only bank fetch failed: {e}')
                banks = get_bank_list(force_refresh=force_refresh)
        else:
            banks = get_bank_list(force_refresh=force_refresh)

        return Response({'count': len(banks), 'banks': banks})


class BankAccountDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='wallets_bank_account_detail',
        summary='Get saved bank account',
        description='Returns the withdrawal bank account currently saved on the user\'s wallet, if any.',
        request=None,
        responses={
            200: OpenApiResponse(response=BankAccountDetailSerializer, description='Bank account details, or has_bank_account: false if none saved.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Wallet not found.'),
        },
        examples=[
            OpenApiExample(
                'No account saved',
                value={'has_bank_account': False, 'detail': 'No bank account saved yet.'},
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def get(self, request):
        wallet = getattr(request.user, 'wallet', None)
        if not wallet:
            return Response({'detail': 'Wallet not found.'}, status=404)
        if not wallet.bank_account_number:
            return Response({'has_bank_account': False, 'detail': 'No bank account saved yet.'})

        serializer = BankAccountDetailSerializer({
            'bank_account_number':     wallet.bank_account_number,
            'bank_code':               wallet.bank_code,
            'bank_name':               wallet.bank_name,
            'bank_account_name':       wallet.bank_account_name,
            'bank_account_verified':   wallet.bank_account_verified,
            'bank_account_updated_at': wallet.bank_account_updated_at,
        })
        return Response({'has_bank_account': True, **serializer.data})


class BankAccountVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='wallets_bank_account_verify',
        summary='Verify a bank account number',
        description=(
            'Step 1 of 2 for adding a withdrawal bank account — call this BEFORE save. Resolves '
            'the account number against the live bank (via Paystack `/bank/resolve`) and returns '
            'the real account holder name for the user to confirm. This is a real lookup, not a '
            'mocked sandbox response — a fake/nonexistent account number will genuinely fail '
            'even in test mode. For repeatable testing use Paystack\'s documented test account: '
            'Zenith Bank (code `057`), account number `0000000000`.'
        ),
        request=BankAccountVerifySerializer,
        responses={
            200: OpenApiResponse(description='Account resolved — show the name to the user for confirmation, then call /bank-accounts/save/.'),
            400: OpenApiResponse(description='Unknown bank code or validation error.'),
            401: OpenApiResponse(description='Not authenticated.'),
            422: OpenApiResponse(description='Account could not be resolved — number/bank combination is invalid.'),
            503: OpenApiResponse(description='Bank verification service unavailable — retry.'),
        },
        examples=[
            OpenApiExample('Request', value={'bank_code': '057', 'account_number': '0000000000'}, request_only=True),
            OpenApiExample(
                'Response',
                value={
                    'verified': True,
                    'account_name': 'JOHN DOE',
                    'account_number': '0000000000',
                    'bank_code': '057',
                    'bank_name': 'Zenith Bank',
                    'message': 'Account found: JOHN DOE. Please confirm before saving.',
                },
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def post(self, request):
        serializer = BankAccountVerifySerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        bank_code = serializer.validated_data['bank_code']
        account_number = serializer.validated_data['account_number']

        bank = get_bank_by_code(bank_code)
        if not bank:
            return Response(
                {'detail': f'Unknown bank code: {bank_code}. Fetch the current list from /wallets/banks/.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = verify_bank_account(bank_code, account_number)
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
        except Exception as e:
            logger.error(f'Bank verify error for user {request.user.id}: {e}')
            return Response({'detail': 'Bank verification service unavailable. Please try again.'}, 
            status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({
            'verified': True,
            'account_name': result['account_name'],
            'account_number': account_number,
            'bank_code': bank_code,
            'bank_name': bank['name'],
            'message': f"Account found: {result['account_name']}. Please confirm before saving.",
        })


class BankAccountSaveView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='wallets_bank_account_save',
        summary='Save a verified bank account',
        description=(
            'Step 2 of 2 — call this after /bank-accounts/verify/, passing back the exact '
            '`account_name` that verify returned. Saving overwrites any previously saved bank '
            'account and resets the cached Paystack transfer recipient, so the next withdrawal '
            'creates a fresh recipient against the new account. This is required before '
            '`/wallets/withdraw/` will succeed — an unverified wallet gets a 400 on withdrawal.'
        ),
        request=BankAccountSaveSerializer,
        responses={
            200: OpenApiResponse(description='Bank account saved — withdrawals are now enabled.'),
            400: OpenApiResponse(description='Validation error.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Wallet not found.'),
        },
        examples=[
            OpenApiExample(
                'Request',
                value={'bank_code': '057', 'account_number': '0000000000', 'bank_account_name': 'JOHN DOE'},
                request_only=True,
            ),
            OpenApiExample(
                'Response',
                value={
                    'success': True,
                    'bank_name': 'Zenith Bank',
                    'account_number': '0000000000',
                    'account_name': 'JOHN DOE',
                    'message': 'Bank account saved. You can now withdraw funds.',
                },
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def post(self, request):
        serializer = BankAccountSaveSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        wallet = getattr(request.user, 'wallet', None)
        if not wallet:
            return Response({'detail': 'Wallet not found.'}, status=404)

        bank_name = get_bank_name(data['bank_code'])

        wallet.bank_account_number     = data['account_number']
        wallet.bank_code               = data['bank_code']
        wallet.bank_name               = bank_name
        wallet.bank_account_name       = data['bank_account_name']
        wallet.bank_account_verified   = True
        wallet.bank_account_updated_at = timezone.now()
        wallet.paystack_recipient_code = ''

        wallet.save(update_fields=[
            'bank_account_number', 'bank_code', 'bank_name', 'bank_account_name',
            'bank_account_verified', 'bank_account_updated_at',
            'paystack_recipient_code', 'updated_at',
        ])

        return Response({
            'success': True, 'bank_name': bank_name,
            'account_number': data['account_number'],
            'account_name': data['bank_account_name'],
            'message': 'Bank account saved. You can now withdraw funds.',
        })


class WithdrawalRequestView(APIView):
    """
    amount <  ₦200,000 → auto-approved, processed immediately
    amount >= ₦200,000 → PENDING, admin SMS-notified, must review manually
    """
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='wallets_withdraw_create',
        summary='Request a withdrawal',
        description=(
            f'Requires a verified bank account (see /bank-accounts/save/) and a minimum amount '
            f'of ₦{MINIMUM_WITHDRAWAL:,.0f}. Wallet balance is debited immediately on request, '
            f'before Paystack transfer confirms — this reserves the funds. Two outcomes:\n\n'
            f'- **Under the auto-approve threshold**: `status: "approved"`, a background task '
            f'fires the real Paystack transfer immediately. Show the user "processing", not "done" '
            f'— final settlement is confirmed later via webhook (`status` moves to `processing` then '
            f'`completed`).\n'
            f'- **At or above the threshold**: `status: "pending"`, held for manual admin review. '
            f'Show the user this will take longer.\n\n'
            f'Poll `GET /wallets/withdraw/` to track status transitions: '
            f'`pending`/`approved` → `processing` → `completed` or `failed` (auto-refunded to '
            f'wallet on failure).'
        ),
        request=WithdrawalRequestSerializer,
        responses={
            201: OpenApiResponse(description='Withdrawal request created.'),
            400: OpenApiResponse(description='No bank account, below minimum, or insufficient balance.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Wallet not found.'),
        },
        examples=[
            OpenApiExample('Request', value={'amount': '3000.00'}, request_only=True),
            OpenApiExample(
                'Auto-approved response',
                value={
                    'success': True,
                    'withdrawal_id': '12fa3018-60fa-48bb-9478-d1f05a5144c5',
                    'amount': '3000.00',
                    'bank_name': 'Zenith Bank',
                    'account_number': '0000000000',
                    'account_name': 'JOHN DOE',
                    'status': 'approved',
                    'requires_admin_review': False,
                    'message': 'Withdrawal received and is being processed.',
                },
                response_only=True,
            ),
        ],
        tags=['Wallets'],
    )
    def post(self, request):
        serializer = WithdrawalRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        amount = serializer.validated_data['amount']
        wallet = getattr(request.user, 'wallet', None)

        if not wallet:
            return Response({'detail': 'Wallet not found.'}, status=404)
        if not wallet.bank_account_verified:
            return Response({'detail': 'Please add and verify a bank account before withdrawing.'}, 
            status=status.HTTP_400_BAD_REQUEST)
        if amount < MINIMUM_WITHDRAWAL:
            return Response({'detail': f'Minimum withdrawal is ₦{MINIMUM_WITHDRAWAL:,.0f}.'}, 
            status=status.HTTP_400_BAD_REQUEST)

        requires_review = amount >= MAX_AUTO_TRANSFER_NAIRA

        try:
            with db_transaction.atomic():
                withdrawal = WithdrawalRequest.objects.create(
                    wallet=wallet, amount=amount,
                    bank_account_number=wallet.bank_account_number,
                    bank_code=wallet.bank_code,
                    bank_name=wallet.bank_name,
                    bank_account_name=wallet.bank_account_name,
                    status=WithdrawalRequest.Status.PENDING if requires_review else WithdrawalRequest.Status.APPROVED,
                )
                from services.ledger import debit_for_withdrawal
                debit_for_withdrawal(wallet_id=str(wallet.id), amount=amount, withdrawal_request_id=str(withdrawal.id))
        except ValueError as e:
            return Response({'detail': str(e)}, status=status.HTTP_400_BAD_REQUEST)

        if requires_review:
            from apps.wallets.admin_notify import notify_admin_large_withdrawal
            notify_admin_large_withdrawal.delay(str(withdrawal.id))
            message = 'Withdrawal requires admin review due to its size. You will be notified once approved.'
        else:
            from apps.wallets.payout_tasks import process_withdrawal
            process_withdrawal.delay(str(withdrawal.id))
            message = 'Withdrawal received and is being processed.'

        return Response({
            'success': True, 'withdrawal_id': str(withdrawal.id), 'amount': str(amount),
            'bank_name': wallet.bank_name, 'account_number': wallet.bank_account_number,
            'account_name': wallet.bank_account_name, 'status': withdrawal.status,
            'requires_admin_review': requires_review, 'message': message,
        }, status=status.HTTP_201_CREATED)

    @extend_schema(
        operation_id='wallets_withdraw_list',
        summary='List withdrawal history',
        description='Returns the authenticated user\'s most recent 20 withdrawal requests, most recent first.',
        request=None,
        responses={
            200: OpenApiResponse(response=WithdrawalDetailSerializer, description='Withdrawal history.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Wallet not found.'),
        },
        tags=['Wallets'],
    )
    def get(self, request):
        wallet = getattr(request.user, 'wallet', None)
        if not wallet:
            return Response({'detail': 'Wallet not found.'}, status=404)
        withdrawals = wallet.withdrawals.all()[:20]
        return Response({'withdrawals': WithdrawalDetailSerializer(withdrawals, many=True).data})