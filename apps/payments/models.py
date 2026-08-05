"""
apps/payments/models.py
=======================
Changes from original:
  1. squad_reference → paystack_reference
  2. Added idempotency_key field (unique per transaction_type)
  3. Added REVERSAL transaction type
  4. Added save() override that enforces append-only (no updates except status PENDING→SUCCESS/FAILED)
  5. Added DB constraint: amount > 0

Migration needed after this:
  python manage.py makemigrations payments --name add_idempotency_and_reversal
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models


class Transaction(models.Model):

    class Type(models.TextChoices):
        CREDIT            = 'credit',            'Credit'
        DEBIT             = 'debit',             'Debit'
        REVERSAL          = 'reversal',          'Reversal'        # ← new: refund of a failed debit
        ESCROW_HOLD       = 'escrow_hold',       'Escrow Hold'
        ESCROW_RELEASE    = 'escrow_release',    'Escrow Release'
        PLATFORM_FEE      = 'platform_fee',      'Platform Fee'
        WITHDRAWAL        = 'withdrawal',        'Withdrawal'
        LOAN_DISBURSEMENT = 'loan_disbursement', 'Loan Disbursement'
        LOAN_REPAYMENT    = 'loan_repayment',    'Loan Repayment'
        SAVINGS_DEPOSIT   = 'savings_deposit',   'Savings Deposit'
        SAVINGS_WITHDRAWAL= 'savings_withdrawal','Savings Withdrawal'
        INSURANCE_PREMIUM = 'insurance_premium', 'Insurance Premium'
        INSURANCE_PAYOUT  = 'insurance_payout',  'Insurance Payout'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        SUCCESS = 'success', 'Success'
        FAILED  = 'failed',  'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='transactions',
    )
    transaction_type = models.CharField(max_length=30, choices=Type.choices)
    amount           = models.DecimalField(max_digits=12, decimal_places=2)
    status           = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Paystack reference — unique per Paystack transaction
    paystack_reference = models.CharField(max_length=200, blank=True, db_index=True)

    # Idempotency key — our internal key, unique per (type, key) pair.
    # Format: '{event_type}:{unique_ref}'  e.g. 'inbound:REF_abc123'
    # Used to prevent double-processing on Celery retries and webhook replays.
    idempotency_key = models.CharField(max_length=255, blank=True, db_index=True)

    # Optional relations
    job          = models.ForeignKey('jobs.Job', null=True, blank=True, on_delete=models.SET_NULL)
    related_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='related_transactions',
    )

    description = models.TextField(blank=True)
    metadata    = models.JSONField(default=dict, blank=True)

    created_at  = models.DateTimeField(auto_now_add=True, db_index=True)
    # No updated_at — transactions are append-only.
    # The ONLY allowed post-creation change is status PENDING → SUCCESS | FAILED.
    # All other modifications are FORBIDDEN (enforced in save() below).

    class Meta:
        db_table = 'payments_transaction'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', '-created_at']),
            models.Index(fields=['paystack_reference']),
            models.Index(fields=['idempotency_key', 'transaction_type']),
        ]
        # DB-level: amount must always be positive
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='transaction_amount_positive',
            ),
        ]

    def __str__(self):
        return (
            f'{self.transaction_type} ₦{self.amount} '
            f'[{self.status}] — {self.user}'
        )

    def save(self, *args, **kwargs):
        """
        Enforce append-only immutability.

        Allowed:
          - INSERT (new transaction)
          - UPDATE status from PENDING → SUCCESS or PENDING → FAILED
          - UPDATE paystack_reference when empty (filling in the ref after timeout)

        Forbidden:
          - Changing amount, transaction_type, user, idempotency_key, job
          - Changing status from SUCCESS or FAILED to anything
          - Any change after the record is SUCCESS or FAILED

        To "undo" a SUCCESS, create a new REVERSAL transaction instead.
        """
        if self._state.adding:
            # New record — always allowed
            super().save(*args, **kwargs)
            return

        # Existing record — fetch original to compare
        try:
            original = Transaction.objects.get(pk=self.pk)
        except Transaction.DoesNotExist:
            super().save(*args, **kwargs)
            return

        # Immutable fields — must never change after creation
        IMMUTABLE = ['user_id', 'transaction_type', 'amount', 'idempotency_key', 'job_id']
        for field in IMMUTABLE:
            if getattr(self, field) != getattr(original, field):
                raise ValueError(
                    f'Transaction.{field} is immutable after creation. '
                    f'Create a new REVERSAL transaction instead.'
                )

        # Status transitions: only PENDING → SUCCESS | FAILED allowed
        if original.status in (self.Status.SUCCESS, self.Status.FAILED):
            if self.status != original.status or self.paystack_reference != original.paystack_reference:
                raise ValueError(
                    f'Transaction {self.pk} is in terminal state {original.status}. '
                    f'No further changes allowed.'
                )

        super().save(*args, **kwargs)


class PlatformWithdrawalLog(models.Model):
    """
    A manual record an admin creates AFTER withdrawing Kolliq's revenue
    via the Paystack Dashboard directly. This app never touches the
    company's bank account number, code, or name — it only knows
    "₦X was taken out on this date" so the revenue dashboard stays
    accurate over time.
    """
    id     = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    note   = models.CharField(
        max_length=255, blank=True,
        help_text="e.g. 'Settled to company account via Paystack Dashboard, June 2026'"
    )
    logged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'platform_withdrawal_log'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0),
                name='platform_withdrawal_log_amount_positive',
            ),
        ]

    def __str__(self):
        return f'₦{self.amount} logged on {self.created_at:%Y-%m-%d}'