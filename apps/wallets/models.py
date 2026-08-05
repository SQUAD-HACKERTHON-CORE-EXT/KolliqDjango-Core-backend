'''
apps/wallets/models.py — FINAL, all decisions merged
========================================================
- No BVN anywhere (status choice removed)
- 'permanently_failed' status added (from retry task)
- large_amount_override_approved field added (from money safety / admin)
- DB constraints: all balances >= 0, withdrawal amount > 0
'''

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.common.rls import UserOwnedModel


class Wallet(UserOwnedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='wallet',
    )

    paystack_account_number  = models.CharField(max_length=20, blank=True)
    paystack_account_name    = models.CharField(max_length=200, blank=True)
    paystack_bank_name       = models.CharField(max_length=100, blank=True, default='Wema Bank')
    paystack_customer_id     = models.CharField(max_length=100, blank=True)
    paystack_account_ref     = models.CharField(max_length=200, blank=True, unique=True, null=True)
    paystack_dva_id          = models.CharField(max_length=50, blank=True)
    paystack_recipient_code  = models.CharField(max_length=100, blank=True)

    balance          = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    escrow_balance   = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    savings_balance  = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))

    bank_account_number    = models.CharField(max_length=10, blank=True)
    bank_code              = models.CharField(max_length=10, blank=True)
    bank_name              = models.CharField(max_length=100, blank=True)
    bank_account_name      = models.CharField(max_length=150, blank=True)
    bank_account_verified  = models.BooleanField(default=False)
    bank_account_updated_at = models.DateTimeField(blank=True, null=True)

    is_active = models.BooleanField(default=True)
    paystack_creation_status = models.CharField(
        max_length=20,
        choices=[
            ('pending',             'Pending'),
            ('created',             'Created'),
            ('failed',              'Failed'),
            ('permanently_failed',  'Permanently Failed'),
        ],
        default='pending',
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'wallets'
        constraints = [
            models.CheckConstraint(condition=models.Q(balance__gte=0), name='wallet_balance_non_negative'),
            models.CheckConstraint(condition=models.Q(escrow_balance__gte=0), name='wallet_escrow_balance_non_negative'),
            models.CheckConstraint(condition=models.Q(savings_balance__gte=0), name='wallet_savings_balance_non_negative'),
        ]

    def __str__(self):
        return f'Wallet({self.user.phone}) — ₦{self.balance}'

    @property
    def wallet_ready(self) -> bool:
        return self.paystack_creation_status == 'created'

    def credit(self, amount: Decimal, save: bool = True):
        """Always call inside db_transaction.atomic() + select_for_update().
        Prefer services.ledger functions over calling this directly."""
        self.balance += Decimal(str(amount))
        if save:
            self.save(update_fields=['balance', 'updated_at'])

    def debit(self, amount: Decimal, save: bool = True):
        amount = Decimal(str(amount))
        if self.balance < amount:
            raise ValueError(f'Insufficient wallet balance. Available: ₦{self.balance}, Requested: ₦{amount}')
        self.balance -= amount
        if save:
            self.save(update_fields=['balance', 'updated_at'])


class WithdrawalRequest(models.Model):
    class Status(models.TextChoices):
        PENDING    = 'pending',    'Pending'
        APPROVED   = 'approved',   'Approved'
        REJECTED   = 'rejected',   'Rejected'
        PROCESSING = 'processing', 'Processing'
        COMPLETED  = 'completed',  'Completed'
        FAILED     = 'failed',     'Failed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name='withdrawals')
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    bank_account_number = models.CharField(max_length=10)
    bank_code            = models.CharField(max_length=10)
    bank_name             = models.CharField(max_length=100)
    bank_account_name    = models.CharField(max_length=150)

    status              = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    paystack_reference  = models.CharField(max_length=200, blank=True, db_index=True)
    rejection_reason    = models.TextField(blank=True)
    # Set True only when an admin explicitly approves an amount above the
    # auto-transfer ceiling (services.money_safety.MAX_AUTO_TRANSFER_NAIRA)
    large_amount_override_approved = models.BooleanField(default=False)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_withdrawals',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'withdrawal_requests'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name='withdrawal_amount_positive'),
        ]

    def __str__(self):
        return f'Withdrawal ₦{self.amount} — {self.wallet.user.phone} [{self.status}]'