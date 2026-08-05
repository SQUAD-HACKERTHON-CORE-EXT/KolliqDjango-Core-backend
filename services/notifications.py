"""
Notification helpers — thin wrappers around EmailService for specific platform events.
All notification functions are designed to be called from Celery tasks.
"""
from celery import shared_task
from services.email_service import EmailService
import logging

logger = logging.getLogger(__name__)


@shared_task
def notify_employer_acceptance(application_id: str):
    """Tell employer a worker has accepted their job."""
    from apps.jobs.models import Application  # adjust import path to your actual app
    try:
        application = Application.objects.select_related('job__employer', 'worker').get(id=application_id)
    except Application.DoesNotExist:
        logger.error(f"notify_employer_acceptance: Application {application_id} not found")
        return

    job = application.job
    worker = application.worker
    employer = job.employer

    subject = "Kolliq: Job Application Accepted"
    message = (
        f"{worker.full_name or worker.email} has accepted your job '{job.title}'. "
        f"Contact: {worker.email}. "
        f"Reply 'done {str(job.id)[:8]}' when complete."
    )
    email_service = EmailService()
    email_service.send_email(employer.email, subject, message)
    logger.info(f"Notified employer {employer.email} of acceptance by {worker.email}")


@shared_task
def notify_worker_payment(user_id: str, amount: str, new_balance: str):
    """Tell worker their payment landed."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"notify_worker_payment: User {user_id} not found")
        return

    subject = "Kolliq: Payment Received"
    message = (
        f"₦{amount} has been added to your wallet. "
        f"New balance: ₦{new_balance}. "
        f"Keep completing jobs to grow your score!"
    )
    email_service = EmailService()
    email_service.send_email(user.email, subject, message)


@shared_task
def notify_trader_payment_received(user_id: str, amount: str, new_balance: str, sender: str):
    """Tell trader they received payment."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"notify_trader_payment_received: User {user_id} not found")
        return

    subject = "Kolliq: Payment Received"
    message = (
        f"You received ₦{amount} from {sender}. "
        f"Balance: ₦{new_balance}."
    )
    email_service = EmailService()
    email_service.send_email(user.email, subject, message)


@shared_task
def notify_loan_disbursed(user_id: str, amount: str, repayment_date: str):
    """Tell user their loan has been disbursed."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        logger.error(f"notify_loan_disbursed: User {user_id} not found")
        return

    subject = "Kolliq: Loan Disbursed"
    message = (
        f"₦{amount} loan has been added to your wallet. "
        f"First repayment due: {repayment_date}. "
        f"Repay on time to grow your score and increase your limit."
    )
    email_service = EmailService()
    email_service.send_email(user.email, subject, message)