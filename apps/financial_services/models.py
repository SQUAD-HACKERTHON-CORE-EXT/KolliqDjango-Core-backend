from django.db import models
from django.conf import settings
import uuid
from decimal import Decimal
from apps.common.rls import UserOwnedModel, UserOrganizedModel


class DemoFloat(models.Model):
    """
    System wallet that funds simulated loan and insurance disbursements.
    Seeded with ₦500,000 at deploy. Auditable — every transaction tagged.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_disbursed = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    total_repaid = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'demo_float'

    def __str__(self):
        return f"DemoFloat — Balance: ₦{self.balance} | Disbursed: ₦{self.total_disbursed}"

    def disburse(self, amount: Decimal):
        if self.balance < amount:
            raise ValueError(f"Demo float insufficient: have ₦{self.balance}, need ₦{amount}")
        self.balance -= amount
        self.total_disbursed += amount
        self.save(update_fields=['balance', 'total_disbursed', 'updated_at'])

    def receive_repayment(self, amount: Decimal):
        self.balance += amount
        self.total_repaid += amount
        self.save(update_fields=['balance', 'total_repaid', 'updated_at'])


class Loan(UserOwnedModel):

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACTIVE = 'active', 'Active'
        PARTIALLY_REPAID = 'partially_repaid', 'Partially Repaid'
        REPAID = 'repaid', 'Repaid'
        DEFAULTED = 'defaulted', 'Defaulted'
        REJECTED = 'rejected', 'Rejected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='loans'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    interest_rate_monthly = models.DecimalField(
        max_digits=5, decimal_places=2,
        default=Decimal('5.00')
    )
    total_repayable = models.DecimalField(max_digits=10, decimal_places=2)
    amount_repaid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Repayment schedule: [{"due_date": "2024-02-01", "amount": "2625.00", "paid": false}, ...]
    repayment_schedule = models.JSONField(default=list)

    disbursed_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    # Always 'demo_float' in pilot — 'partner_name' when live
    funding_source = models.CharField(max_length=100, default='demo_float')
    disbursement_ref = models.CharField(max_length=200, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'loans'
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status']),
        ]

    def __str__(self):
        return f"Loan({self.user.phone}) ₦{self.amount} [{self.status}]"

    @property
    def outstanding_balance(self):
        return self.total_repayable - self.amount_repaid

    @property
    def max_loan_amount_for_score(self):
        """Loan limit scales with score."""
        score = getattr(self.user, 'economic_score', None)
        if not score:
            return Decimal('10000')
        s = score.score
        if s < 50:
            return Decimal('0')
        elif s < 60:
            return Decimal('10000')
        elif s < 75:
            return Decimal('25000')
        elif s < 90:
            return Decimal('50000')
        return Decimal('100000')


class InsurancePolicy(UserOwnedModel):

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAUSED = 'paused', 'Paused'          # wallet ran dry — auto-pause
        CANCELLED = 'cancelled', 'Cancelled'
        EXPIRED = 'expired', 'Expired'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='insurance_policies'
    )
    daily_premium = models.DecimalField(
        max_digits=8, decimal_places=2,
        default=Decimal('200.00')
    )
    coverage_limit = models.DecimalField(
        max_digits=10, decimal_places=2,
        default=Decimal('50000.00')
    )
    days_active = models.PositiveIntegerField(default=0)
    total_premiums_paid = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    funding_source = models.CharField(max_length=100, default='demo_float')
    activated_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'insurance_policies'

    def __str__(self):
        return f"Insurance({self.user.phone}) ₦{self.daily_premium}/day [{self.status}]"


class InsuranceClaim(UserOwnedModel):

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        AUTO_APPROVED = 'auto_approved', 'Auto Approved'
        MANUAL_REVIEW = 'manual_review', 'Manual Review'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        PAID = 'paid', 'Paid'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    policy = models.ForeignKey(InsurancePolicy, on_delete=models.CASCADE, related_name='claims')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='insurance_claims'
    )
    days_missed = models.PositiveSmallIntegerField()
    reason = models.TextField()
    payout_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    auto_approve_threshold = models.DecimalField(
        max_digits=8, decimal_places=2,
        default=Decimal('5000.00')
    )
    admin_notes = models.TextField(blank=True)
    funding_source = models.CharField(max_length=100, default='demo_float')
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'insurance_claims'

    def __str__(self):
        return f"Claim({self.user.phone}) ₦{self.payout_amount} [{self.status}]"


class SavingsPot(UserOwnedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='savings_pot'
    )
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_deposited = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    total_interest_earned = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    target_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'savings_pots'

    def __str__(self):
        return f"Savings({self.user.phone}) ₦{self.balance}"