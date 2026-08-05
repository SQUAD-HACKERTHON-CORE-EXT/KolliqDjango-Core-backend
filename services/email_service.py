"""
Email service — sends transactional emails via Gmail SMTP (or any SMTP
backend configured in Django settings).


Gmail requires an "App Password" (2FA must be enabled on the account) —
regular account passwords are rejected by SMTP auth.
"""
import logging

from django.conf import settings
from django.core.mail import send_mail
from django.core.mail import BadHeaderError

logger = logging.getLogger(__name__)


class EmailService:

    def __init__(self):
        self.from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', settings.EMAIL_HOST_USER)

    def send_email(self, to_email: str, subject: str, message: str, html_message: str = None) -> dict:
        """
        Send a single email.
        to_email: recipient address
        """
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=self.from_email,
                recipient_list=[to_email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info(f"Email sent to {to_email} — subject: {subject}")
            return {'success': True}
        except BadHeaderError as e:
            logger.error(f"Invalid header in email to {to_email}: {e}")
            return {'success': False, 'error': 'Invalid header'}
        except Exception as e:
            logger.error(f"Email send failed to {to_email}: {e}")
            return {'success': False, 'error': str(e)}

    def send_bulk_email(self, to_emails: list, subject: str, message: str, html_message: str = None) -> dict:
        """Send same message to multiple recipients (BCC-style, one send per recipient to avoid exposing addresses)."""
        results = []
        for email in to_emails:
            results.append(self.send_email(email, subject, message, html_message))
        return {'results': results, 'success': all(r['success'] for r in results)}