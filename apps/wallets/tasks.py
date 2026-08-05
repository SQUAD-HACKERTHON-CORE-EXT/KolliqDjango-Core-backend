"""
apps/wallets/tasks.py
=====================
BVN REMOVED — Kolliq no longer collects or transmits BVN anywhere.

Consequence: Paystack DVA creation works ONLY if your business category
is NOT one of: Betting, Financial Services, General Services.

If Kolliq is registered under General Services with Paystack, you must
either:
  (a) ask Paystack to reclassify you (Gig Economy / Marketplace categories
      don't require BVN), or
  (b) accept that DVA creation may fail until reclassified — wallet status
      will be 'failed' and you'll see the reason in logs.

Most marketplace/gig platforms ARE NOT classified as General Services —
that category is specifically for fintech-style apps (lending, savings-as-
a-service, etc). Worth checking your Paystack dashboard business category.
"""

from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def create_wallet_for_user(self, user_id: str):
    """
    Creates Wallet DB record + provisions Paystack Customer + DVA.
    Triggered by post_save signal on User creation.

    No BVN involved. Just email, first_name, last_name, phone.
    """
    from django.contrib.auth import get_user_model
    from services.paystack import PaystackService, PaystackAPIError
    from apps.wallets.models import Wallet

    User = get_user_model()

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f'create_wallet_for_user: User {user_id} not found')
        return

    wallet, _ = Wallet.objects.get_or_create(user=user)
    if wallet.paystack_creation_status == 'created':
        logger.info(f'Wallet already provisioned for {user_id}')
        return

    paystack = PaystackService()

    name_parts  = (user.full_name or 'Kolliq User').strip().split()
    first_name  = name_parts[0]
    last_name   = name_parts[-1] if len(name_parts) >= 2 else first_name

    # Use the user's real signup email as the Paystack customer identifier.
    # Fallback only exists as a safety net for the (shouldn't-happen) case
    # of a User row somehow missing an email — email is required + unique
    # at registration, so this should rarely if ever trigger.
    customer_email = user.email or f'{str(user.id)[:8]}@kolliq.app'

    try:
        # ── Step 1: Create or fetch Paystack customer ─────────────────────────
        try:
            customer = paystack.get_customer(customer_email)
            customer_code = customer.get('customer_code', '')
        except PaystackAPIError as exc:
            if exc.status_code != 404:
                raise
            customer = paystack.create_customer(
                email=customer_email,
                first_name=first_name,
                last_name=last_name,
                phone=user.phone,
            )
            customer_code = customer.get('customer_code', '')

        wallet.paystack_customer_id = customer_code
        wallet.save(update_fields=['paystack_customer_id', 'updated_at'])

        # ── Step 2: Create DVA immediately — no BVN step ──────────────────────
        _provision_dva(wallet, customer_code, paystack)

    except PaystackAPIError as exc:
        wallet.paystack_creation_status = 'failed'
        wallet.save(update_fields=['paystack_creation_status', 'updated_at'])
        logger.error(f'Wallet provisioning failed for {user_id}: {exc} | raw={exc.raw}')

        # If Paystack rejects due to business category requiring validation,
        # don't retry endlessly — log clearly for manual review
        if 'validat' in str(exc).lower() or 'bvn' in str(exc).lower():
            logger.error(
                f'DVA creation requires customer validation (BVN) for user {user_id}. '
                f'Kolliq does not collect BVN. Check your Paystack business category — '
                f'it may need to be changed from General Services to a category '
                f'that does not require validation (e.g. Marketplace, Gig Economy).'
            )
            return  # don't retry — this won't resolve itself

        raise self.retry(exc=exc)


def _provision_dva(wallet, customer_code: str, paystack):
    """Creates Paystack DVA and saves details to the wallet."""
    from django.conf import settings

    dva = paystack.create_dedicated_account(
        customer_code=customer_code,
        preferred_bank=getattr(settings, 'PAYSTACK_DVA_BANK', 'wema-bank'),
    )

    bank_info = dva.get('bank', {})
    account_number = dva.get('account_number', '')
    bank_name = bank_info.get('name', 'Wema Bank')
    dva_id = str(dva.get('id', ''))

    customer_data = dva.get('customer', {})
    full_name = (
        f"{customer_data.get('first_name', '')} "
        f"{customer_data.get('last_name', '')}".strip()
    )

    wallet.paystack_account_number  = account_number
    wallet.paystack_account_name    = full_name or wallet.paystack_account_name
    wallet.paystack_bank_name       = bank_name
    wallet.paystack_account_ref     = account_number
    wallet.paystack_dva_id          = dva_id
    wallet.paystack_creation_status = 'created'
    wallet.save(update_fields=[
        'paystack_account_number',
        'paystack_account_name',
        'paystack_bank_name',
        'paystack_account_ref',
        'paystack_dva_id',
        'paystack_creation_status',
        'updated_at',
    ])

    logger.info(f'DVA provisioned: {account_number} @ {bank_name} for {customer_code}')