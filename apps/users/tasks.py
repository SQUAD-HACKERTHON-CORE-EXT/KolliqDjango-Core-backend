"""
apps/users/tasks.py
====================
Squad → Paystack, BVN removed, and de-duplicated against apps/wallets/tasks.py.

WHY THIS CHANGED STRUCTURALLY:
  The old version stored VA details on the User model (squad_account_number,
  squad_account_status, etc.) AND apps/wallets/tasks.py separately stores the
  same data on the Wallet model. Two independent provisioning paths for the
  same Paystack customer is how you end up with duplicate customers, orphaned
  DVAs, or a User/Wallet pair that disagree about readiness.

  Fix: this file now ONLY handles retry scheduling and backoff. The actual
  Paystack calls live in exactly one place — apps/wallets/tasks.py
  create_wallet_for_user(). This task just re-invokes that, with backoff,
  until it succeeds or exhausts retries.

  All account state (account number, bank name, status) lives on Wallet only.
  The User model no longer needs squad_account_number / squad_account_status
  fields at all — remove them in a migration (see bottom of this file).

TRIGGER:
  Wire this from the same post_save signal that calls create_wallet_for_user,
  OR call it manually from an admin action when you see wallet.paystack_creation_status
  == 'failed' and want to retry with backoff instead of an immediate retry.
"""

import logging
from celery import shared_task
from celery.exceptions import MaxRetriesExceededError

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


def backoff(attempt: int) -> int:
    """Exponential backoff in seconds: 60, 240, 540, 960, 1500"""
    return 60 * (attempt ** 2)


@shared_task(
    bind=True,
    max_retries=MAX_RETRIES,
    name='users.tasks.retry_paystack_wallet_provisioning',
)
def retry_paystack_wallet_provisioning(self, user_id: str):
    """
    Retries Paystack wallet provisioning (customer + DVA) for a user whose
    initial attempt failed. No BVN involved anywhere in this flow.

    Delegates the actual provisioning to apps.wallets.tasks.create_wallet_for_user
    so there is exactly one code path that talks to Paystack.

    On final failure after MAX_RETRIES, marks wallet.paystack_creation_status
    as 'permanently_failed' for manual review.
    """
    from django.contrib.auth import get_user_model
    from apps.wallets.models import Wallet
    from apps.wallets.tasks import create_wallet_for_user

    User = get_user_model()

    # ── Guard: fetch user ──────────────────────────────────────────────────
    try:
        user = User.objects.select_related('wallet').get(id=user_id)
    except User.DoesNotExist:
        logger.warning(f'[Wallet Retry] User {user_id} not found. Aborting.')
        return

    # ── Guard: skip if wallet already provisioned ──────────────────────────
    # Handles the race where another attempt already succeeded.
    wallet = getattr(user, 'wallet', None)
    if wallet and wallet.paystack_creation_status == 'created':
        logger.info(f'[Wallet Retry] User {user_id} already has an active wallet. Skipping.')
        return

    attempt_number = self.request.retries + 1
    logger.info(
        f'[Wallet Retry] Attempting provisioning for user {user_id} '
        f'(attempt {attempt_number}/{MAX_RETRIES})'
    )

    try:
        # Delegate to the single provisioning function — no duplicate Paystack logic here.
        # Calling .run() (not .delay()) executes it synchronously inside this task
        # so we can catch failures and control the backoff ourselves.
        create_wallet_for_user.run(user_id)

        # Re-check status after the call — create_wallet_for_user swallows its
        # own PaystackAPIError and just sets status to 'failed', it doesn't raise
        # by the time we get here unless something unexpected happened.
        wallet = Wallet.objects.get(user=user)
        if wallet.paystack_creation_status != 'created':
            raise RuntimeError(
                f'Provisioning did not complete: status={wallet.paystack_creation_status}'
            )

        logger.info(
            f'[Wallet Retry] Wallet provisioned for user {user_id}: '
            f'{wallet.paystack_account_number}'
        )

    except Exception as exc:
        delay = backoff(attempt_number)
        logger.error(
            f'[Wallet Retry] Attempt {attempt_number}/{MAX_RETRIES} failed for '
            f'user {user_id}: {exc}. Retrying in {delay}s.',
            exc_info=True,
        )

        try:
            raise self.retry(exc=exc, countdown=delay)

        except MaxRetriesExceededError:
            logger.critical(
                f'[Wallet Retry] All {MAX_RETRIES} retries exhausted for user {user_id}. '
                f'Marking permanently_failed. Manual intervention required.'
            )
            Wallet.objects.filter(user=user).update(
                paystack_creation_status='permanently_failed'
            )


# ─────────────────────────────────────────────────────────────────────────────
