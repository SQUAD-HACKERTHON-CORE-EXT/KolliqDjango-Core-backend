"""
apps/wallets/admin_notify.py — Admin Alert System for Large Withdrawals
==========================================================================
Sends an EMAIL the moment a withdrawal of ₦200,000+ is requested, so a
human knows to go review it in the admin panel.

Settings needed:
  ADMIN_NOTIFY_EMAILS = ['founder@kolliq.app', 'ops@kolliq.app']

  Plus standard Django email settings (SMTP example):
    EMAIL_BACKEND       = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST          = 'smtp.gmail.com'          # or your provider
    EMAIL_PORT          = 587
    EMAIL_USE_TLS       = True
    EMAIL_HOST_USER     = 'your-sending-address@gmail.com'
    EMAIL_HOST_PASSWORD = 'your-app-password'
    DEFAULT_FROM_EMAIL   = 'Kolliq Alerts <alerts@kolliq.app>'

  For production, a transactional provider (SendGrid, Mailgun, Postmark,
  Resend) is more reliable than raw SMTP and has its own Django backend
  package — swap EMAIL_BACKEND accordingly, the send_mail() call below
  doesn't change either way.
"""

from celery import shared_task
import logging

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def notify_admin_large_withdrawal(self, withdrawal_id: str):
    """
    Emails every address in settings.ADMIN_NOTIFY_EMAILS when a withdrawal
    requires manual review (amount >= MAX_AUTO_TRANSFER_NAIRA).
    """
    from django.conf import settings
    from django.core.mail import send_mail
    from apps.wallets.models import WithdrawalRequest

    try:
        withdrawal = WithdrawalRequest.objects.select_related('wallet__user').get(id=withdrawal_id)
    except WithdrawalRequest.DoesNotExist:
        logger.error(f'notify_admin_large_withdrawal: {withdrawal_id} not found')
        return

    admin_emails = getattr(settings, 'ADMIN_NOTIFY_EMAILS', [])
    if not admin_emails:
        logger.warning(
            f'No ADMIN_NOTIFY_EMAILS configured — large withdrawal {withdrawal_id} '
            f'(₦{withdrawal.amount}) is pending review but NO ONE WAS NOTIFIED. '
            f'Set ADMIN_NOTIFY_EMAILS in settings immediately.'
        )
        return

    user = withdrawal.wallet.user
    subject = f'[Kolliq] Withdrawal review needed — ₦{withdrawal.amount:,.2f}'
    body = (
        f'A withdrawal requires manual review.\n\n'
        f'Withdrawal ID: {str(withdrawal.id)[:8]}\n'
        f'Amount: ₦{withdrawal.amount:,.2f}\n'
        f'Requested by: {user.full_name or user.phone}\n'
        f'Bank: {withdrawal.bank_name} — {withdrawal.bank_account_number}\n\n'
        f'Log into the admin panel to approve or reject:\n'
        f'{getattr(settings, "ADMIN_PANEL_URL", "")}/admin/wallets/withdrawalrequest/'
    )

    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=getattr(settings, 'DEFAULT_FROM_EMAIL', None),
            recipient_list=admin_emails,
            fail_silently=False,
        )
        logger.info(f'Admin notified by email: {admin_emails} re: withdrawal {withdrawal_id}')
    except Exception as e:
        logger.error(f'Failed to email admins about withdrawal {withdrawal_id}: {e}', exc_info=True)
        raise self.retry(exc=e)