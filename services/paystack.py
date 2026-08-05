"""
services/paystack.py
"""

import hmac
import hashlib
import logging
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class PaystackAPIError(Exception):
    def __init__(self, message: str, status_code: int = None, raw: dict = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw = raw or {}


def naira_to_kobo(amount: Decimal) -> int:
    return int(amount * 100)


def kobo_to_naira(kobo: int) -> Decimal:
    return Decimal(str(kobo)) / 100


class PaystackService:
    BASE_URL = settings.PAYSTACK_BASE_URL

    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.dva_bank = getattr(settings, 'PAYSTACK_DVA_BANK', 'wema-bank')
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.secret_key}',
            'Content-Type': 'application/json',
        })

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _post(self, endpoint: str, data: dict) -> dict:
        url = f'{self.BASE_URL}{endpoint}'
        try:
            resp = self.session.post(url, json=data, timeout=30)
            return self._handle(resp, endpoint)
        except requests.exceptions.Timeout:
            raise PaystackAPIError(f'Timeout on POST {endpoint}', status_code=408)
        except requests.exceptions.RequestException as e:
            raise PaystackAPIError(f'Network error on POST {endpoint}: {e}')

    def _idempotency_post(
        self, endpoint: str, data: dict, idempotency_key: str
    ) -> dict:
        """
        POST with Idempotency-Key header.
        Use for /transfer and /transferrecipient.

        If Paystack sees the same key within 24 hours, it returns the
        original response — the transfer is NOT duplicated.

        idempotency_key should be your deterministic reference string,
        e.g. the withdrawal ID or transfer reference.
        """
        url = f'{self.BASE_URL}{endpoint}'
        headers = {'Idempotency-Key': idempotency_key}
        try:
            resp = self.session.post(url, json=data, headers=headers, timeout=30)
            return self._handle(resp, endpoint)
        except requests.exceptions.Timeout:
            raise PaystackAPIError(
                f'Timeout on POST {endpoint} (idempotency_key={idempotency_key})',
                status_code=408,
            )
        except requests.exceptions.RequestException as e:
            raise PaystackAPIError(f'Network error on POST {endpoint}: {e}')

    def _get(self, endpoint: str, params: dict = None) -> dict:
        url = f'{self.BASE_URL}{endpoint}'
        try:
            resp = self.session.get(url, params=params, timeout=30)
            return self._handle(resp, endpoint)
        except requests.exceptions.Timeout:
            raise PaystackAPIError(f'Timeout on GET {endpoint}', status_code=408)
        except requests.exceptions.RequestException as e:
            raise PaystackAPIError(f'Network error on GET {endpoint}: {e}')

    def _handle(self, resp: requests.Response, endpoint: str) -> dict:
        try:
            body = resp.json()
        except Exception:
            raise PaystackAPIError(
                f'Non-JSON response on {endpoint}',
                status_code=resp.status_code,
            )
        if not body.get('status') or resp.status_code not in (200, 201):
            msg = body.get('message', 'Unknown Paystack error')
            logger.error(f'Paystack [{endpoint}] {resp.status_code}: {msg} | {body}')
            raise PaystackAPIError(msg, status_code=resp.status_code, raw=body)
        return body.get('data', body)

    # ── CUSTOMERS ─────────────────────────────────────────────────────────────

    def create_customer(self, email, first_name, last_name, phone=None) -> dict:
        """
        POST /customer
        phone is optional — since phone is no longer required at registration,
        this only includes the key in the payload if a cleaned phone exists.
        """
        payload = {
            'email': email,
            'first_name': first_name,
            'last_name': last_name,
        }
        cleaned_phone = self._clean_phone(phone)
        if cleaned_phone:
            payload['phone'] = cleaned_phone

        result = self._post('/customer', payload)
        logger.info(f'Customer created: {result.get("customer_code")}')
        return result

    def get_customer(self, email_or_code: str) -> dict:
        """GET /customer/{email_or_code}"""
        return self._get(f'/customer/{email_or_code}')

    # ── CHECKOUT / FUNDING (primary funding path — safe in test mode) ──────────

    def initialize_transaction(
        self,
        email: str,
        amount_naira: Decimal,
        reference: str,
        callback_url: str = '',
        metadata: dict = None,
    ) -> dict:
        """
        POST /transaction/initialize
        Starts a Paystack Checkout session. Returns authorization_url
        (frontend redirect) and access_code (inline popup).

        This is the primary wallet-funding path — works fully on
        sk_test_ keys, no DVA/bank-partner dependency. The webhook
        handler credits the wallet on charge.success by matching
        metadata['wallet_id'], not by DVA account number.
        """
        payload = {
            'email': email,
            'amount': naira_to_kobo(amount_naira),
            'reference': reference,
            'currency': 'NGN',
        }
        if callback_url:
            payload['callback_url'] = callback_url
        if metadata:
            payload['metadata'] = metadata

        result = self._post('/transaction/initialize', payload)
        logger.info(f'Transaction initialized: ref={reference} amount=₦{amount_naira}')
        return result

    # ── DEDICATED VIRTUAL ACCOUNTS (dormant until live mode) ────────────────────
    # Not called during signup as of the checkout-funding change. Kept intact
    # for when Kolliq moves to a live Paystack key and DVA sandbox limitations
    # no longer apply. See apps/wallets/tasks.py::provision_pending_dvas.

    def create_virtual_account(
        self, customer_identifier, first_name, last_name, middle_name,
        phone, email, dob='', bvn='', gender='1', address='',
        beneficiary_account='',
    ) -> dict:
        """
        Creates DVA for a user. Signature-compatible with old Squad method.
        Step 1: create/fetch customer. Step 2: create DVA.
        """
        internal_email = email or f'{customer_identifier[:8]}@kolliq.app'
        try:
            customer = self.get_customer(internal_email)
        except PaystackAPIError as e:
            if e.status_code != 404:
                raise
            customer = self.create_customer(
                email=internal_email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
            )
        customer_code = customer.get('customer_code', '')
        dva = self.create_dedicated_account(customer_code=customer_code)
        bank = dva.get('bank', {})
        slug_to_nibss = {
            'wema-bank': '035', 'titan-trust': '102',
            'providus': '101', 'sterling': '232',
        }
        return {
            'virtual_account_number': dva.get('account_number', ''),
            'bank_code': slug_to_nibss.get(self.dva_bank, '035'),
            'bank_name': bank.get('name', self.dva_bank.replace('-', ' ').title()),
            'customer_identifier': customer_code,
            'first_name': first_name,
            'last_name': last_name,
        }

    def create_dedicated_account(
        self, customer_code: str, preferred_bank: str = None,
    ) -> dict:
        """POST /dedicated_account"""
        payload = {
            'customer': customer_code,
            'preferred_bank': preferred_bank or self.dva_bank,
        }
        return self._post('/dedicated_account', payload)

    def list_dedicated_accounts(self, active=True, currency='NGN', customer='') -> list:
        """GET /dedicated_account"""
        params = {'active': str(active).lower(), 'currency': currency}
        if customer:
            params['customer'] = customer
        result = self._get('/dedicated_account', params=params)
        return result if isinstance(result, list) else result.get('data', [])

    def requery_dedicated_account(self, account_number: str) -> dict:
        """GET /dedicated_account/requery — force Paystack to check for missed payments"""
        return self._get('/dedicated_account/requery', params={'account_number': account_number})

    def get_dva_providers(self) -> list:
        """GET /dedicated_account/available_providers"""
        result = self._get('/dedicated_account/available_providers')
        return result if isinstance(result, list) else []

    # ── BANK / ACCOUNT VERIFICATION ───────────────────────────────────────────

    def resolve_account(self, account_number: str, bank_code: str) -> dict:
        """GET /bank/resolve — verify account name before transferring"""
        result = self._get('/bank/resolve', params={
            'account_number': account_number,
            'bank_code': bank_code,
        })
        return {
            'account_name': result.get('account_name', ''),
            'account_number': result.get('account_number', account_number),
        }

    # Kept as alias for backwards compat with existing call sites
    def account_lookup(self, bank_code: str, account_number: str) -> dict:
        return self.resolve_account(account_number, bank_code)

    def list_banks(self, pay_with_bank_transfer=False) -> list:
        """GET /bank"""
        params = {'country': 'nigeria', 'perPage': 200}
        if pay_with_bank_transfer:
            params['pay_with_bank_transfer'] = 'true'
        result = self._get('/bank', params=params)
        return result if isinstance(result, list) else result.get('data', [])

    # ── TRANSFER RECIPIENTS ───────────────────────────────────────────────────

    def create_transfer_recipient(
        self,
        account_name: str,
        account_number: str,
        bank_code: str,
        currency: str = 'NGN',
        idempotency_key: str = '',
    ) -> dict:
        """
        POST /transferrecipient
        With Idempotency-Key — safe to retry.
        Paystack deduplicates by account_number + bank_code anyway,
        but the header gives an extra guarantee.

        idempotency_key: pass the withdrawal_id or wallet_id + bank_code combo.
        """
        payload = {
            'type': 'nuban',
            'name': account_name,
            'account_number': account_number,
            'bank_code': bank_code,
            'currency': currency,
        }
        key = idempotency_key or f'recipient:{account_number}:{bank_code}'
        result = self._idempotency_post('/transferrecipient', payload, key)
        logger.info(
            f'Transfer recipient: {result.get("recipient_code")} '
            f'for ***{account_number[-4:]}'
        )
        return result

    # ── TRANSFERS ─────────────────────────────────────────────────────────────

    def initiate_transfer(
        self,
        amount_naira: Decimal,
        recipient_code: str,
        reference: str,
        reason: str = '',
    ) -> dict:
        """
        POST /transfer with Idempotency-Key = reference.

        The reference IS the idempotency key — Paystack uses it to deduplicate:
        same reference within 24h = same result returned, no new transfer.

        This means our deterministic reference (f'KLQ-{withdrawal_id}') gives us
        both Paystack-level AND internal ledger-level safety for free.

        Status in response: 'pending' | 'success' | 'otp' | 'failed'
        If 'otp': disable OTP in Paystack Dashboard Settings → Transfers
        """
        payload = {
            'source': 'balance',
            'amount': naira_to_kobo(amount_naira),
            'recipient': recipient_code,
            'reference': reference,
            'reason': reason or f'Kolliq withdrawal {reference}',
        }
        # reference doubles as idempotency key — Paystack recommends this
        result = self._idempotency_post('/transfer', payload, idempotency_key=reference)
        logger.info(
            f'Transfer initiated: ref={reference} amount=₦{amount_naira} '
            f'status={result.get("status")}'
        )
        return result

    def verify_transfer(self, reference: str) -> dict:
        """GET /transfer/verify/{reference} — always verify on timeout before retrying"""
        result = self._get(f'/transfer/verify/{reference}')
        logger.info(f'Transfer verified: ref={reference} status={result.get("status")}')
        return result

    def list_transfers(self, per_page=50, page=1) -> list:
        """GET /transfer"""
        result = self._get('/transfer', params={'perPage': per_page, 'page': page})
        return result if isinstance(result, list) else result.get('data', [])

    # ── BALANCE ───────────────────────────────────────────────────────────────

    def get_ledger_balance(self) -> dict:
        """GET /balance"""
        result = self._get('/balance')
        balances = result if isinstance(result, list) else [result]
        ngn = next((b for b in balances if b.get('currency') == 'NGN'), {})
        kobo = int(ngn.get('balance', 0))
        return {
            'balance_naira': kobo_to_naira(kobo),
            'balance_kobo': kobo,
            'currency': 'NGN',
        }

    # ── TRANSACTIONS ──────────────────────────────────────────────────────────

    def list_transactions(self, per_page=50, page=1, customer='', status='') -> list:
        """GET /transaction"""
        params = {'perPage': per_page, 'page': page}
        if customer: params['customer'] = customer
        if status:   params['status'] = status
        result = self._get('/transaction', params=params)
        return result if isinstance(result, list) else result.get('data', [])

    def verify_transaction(self, reference: str) -> dict:
        """GET /transaction/verify/{reference}"""
        return self._get(f'/transaction/verify/{reference}')

    # ── WEBHOOK VERIFICATION ──────────────────────────────────────────────────

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """
        HMAC-SHA512 of raw body bytes.
        Must receive bytes, not parsed dict — call request.body before request.data.
        """
        try:
            expected = hmac.new(
                self.secret_key.encode('utf-8'),
                raw_body,
                hashlib.sha512,
            ).hexdigest()
            return hmac.compare_digest(expected.lower(), signature_header.lower())
        except Exception as e:
            logger.error(f'Webhook signature error: {e}')
            return False

    def parse_dva_webhook(self, payload: dict) -> dict:
        """
        Normalise a DVA-funded charge.success payload into internal format.
        Only relevant once DVA provisioning is live — dormant for now.
        """
        data = payload.get('data', {})
        amount_kobo = int(data.get('amount', 0))
        fee_kobo    = int(data.get('fees', 0))
        customer    = data.get('customer', {})
        auth        = data.get('authorization', {})
        return {
            'transaction_reference':   data.get('reference', ''),
            'virtual_account_number':  auth.get('receiver_bank_account_number', ''),
            'principal_amount':        kobo_to_naira(amount_kobo),
            'settled_amount':          kobo_to_naira(amount_kobo - fee_kobo),
            'fee_charged':             kobo_to_naira(fee_kobo),
            'customer_code':           customer.get('customer_code', ''),
            'customer_email':          customer.get('email', ''),
            'narration':               data.get('message', '') or data.get('reference', ''),
            'currency':                data.get('currency', 'NGN'),
            'channel':                 data.get('channel', 'dedicated_nuban'),
            'paid_at':                 data.get('paid_at', ''),
            'event':                   payload.get('event', 'charge.success'),
            'status':                  data.get('status', ''),
        }

    def parse_checkout_webhook(self, payload: dict) -> dict:
        """
        Normalise a checkout-funded charge.success payload (from
        initialize_transaction) into internal format. This is the funding
        path currently in use — metadata carries wallet_id/purpose since
        there's no DVA account number to match against.
        """
        data = payload.get('data', {})
        amount_kobo = int(data.get('amount', 0))
        customer = data.get('customer', {}) or {}
        metadata = data.get('metadata') or {}
        return {
            'reference':     data.get('reference', ''),
            'amount_naira':  kobo_to_naira(amount_kobo),
            'email':         customer.get('email', ''),
            'metadata':      metadata,
            'channel':       data.get('channel', ''),
            'paid_at':       data.get('paid_at', ''),
            'status':        data.get('status', ''),
        }

    def parse_transfer_webhook(self, payload: dict) -> dict:
        """Normalise transfer.success/failed/reversed payload."""
        data      = payload.get('data', {})
        amount_kobo = int(data.get('amount', 0))
        recipient = data.get('recipient', {})
        return {
            'reference':         data.get('reference', ''),
            'transfer_code':     data.get('transfer_code', ''),
            'status':            data.get('status', ''),
            'amount_naira':      kobo_to_naira(amount_kobo),
            'event':             payload.get('event', ''),
            'failure_reason':    (data.get('failures') or [{}])[0].get('reason', ''),
            'recipient_name':    recipient.get('name', ''),
            'recipient_account': recipient.get('account_number', ''),
        }

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _clean_phone(phone: str = None) -> str:
        """
        Normalises a Nigerian phone number to local 0-prefixed format.
        Returns '' if phone is None/empty — phone is optional post-migration,
        so callers must not assume a non-empty string comes back.
        """
        if not phone:
            return ''
        phone = phone.replace('+234', '0').replace('+', '').replace(' ', '').replace('-', '')
        if phone.startswith('234'):
            phone = '0' + phone[3:]
        return phone[:11]


# ── Nigerian banks (static fallback) ─────────────────────────────────────────

NIGERIAN_BANKS = [
    {'name': 'Access Bank',              'code': '044'},
    {'name': 'Citibank Nigeria',         'code': '023'},
    {'name': 'Ecobank Nigeria',          'code': '050'},
    {'name': 'Fidelity Bank',            'code': '070'},
    {'name': 'First Bank of Nigeria',    'code': '011'},
    {'name': 'First City Monument Bank', 'code': '214'},
    {'name': 'Globus Bank',              'code': '00103'},
    {'name': 'Guaranty Trust Bank',      'code': '058'},
    {'name': 'Heritage Bank',            'code': '030'},
    {'name': 'Keystone Bank',            'code': '082'},
    {'name': 'Kuda Bank',                'code': '50211'},
    {'name': 'Moniepoint MFB',           'code': '50515'},
    {'name': 'OPay',                     'code': '100004'},
    {'name': 'Palmpay',                  'code': '100033'},
    {'name': 'Polaris Bank',             'code': '076'},
    {'name': 'Providus Bank',            'code': '101'},
    {'name': 'Stanbic IBTC Bank',        'code': '221'},
    {'name': 'Standard Chartered Bank',  'code': '068'},
    {'name': 'Sterling Bank',            'code': '232'},
    {'name': 'Titan Trust Bank',         'code': '102'},
    {'name': 'Union Bank of Nigeria',    'code': '032'},
    {'name': 'United Bank for Africa',   'code': '033'},
    {'name': 'Unity Bank',               'code': '215'},
    {'name': 'VFD Microfinance Bank',    'code': '566'},
    {'name': 'Wema Bank',                'code': '035'},
    {'name': 'Zenith Bank',              'code': '057'},
]

_BANK_BY_CODE = {b['code']: b['name'] for b in NIGERIAN_BANKS}


def get_bank_name(bank_code: str) -> str:
    return _BANK_BY_CODE.get(bank_code, bank_code)


def verify_bank_account(bank_code: str, account_number: str) -> dict:
    svc = PaystackService()
    try:
        result = svc.resolve_account(account_number, bank_code)
        if not result.get('account_name'):
            raise ValueError('Could not retrieve account name. Check the account details.')
        return result
    except PaystackAPIError as e:
        raise ValueError(str(e))