from django.db import models
from django.conf import settings
import uuid
from decimal import Decimal
from apps.common.rls import UserOwnedManager, UserOrganizedModel


class Job(UserOrganizedModel):

    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        FILLED = 'filled', 'Filled'
        IN_PROGRESS = 'in_progress', 'In Progress'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        DISPUTED = 'disputed', 'Disputed'

    class SkillRequired(models.TextChoices):
        DELIVERY = 'delivery', 'Delivery/Dispatch'
        COOKING = 'cooking', 'Cooking/Catering'
        CONSTRUCTION = 'construction', 'Construction/Labour'
        MARKET = 'market_assistant', 'Market Assistant'
        CLEANING = 'cleaning', 'Cleaning'
        SECURITY = 'security', 'Security'
        TEACHING = 'teaching', 'Teaching/Tutoring'
        OTHER = 'other', 'Other'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    employer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='posted_jobs'
    )

    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    skill_required = models.CharField(max_length=30, choices=SkillRequired.choices)
    workers_needed = models.PositiveIntegerField(default=1)

    # Location
    location_area = models.CharField(max_length=200)
    location_city = models.CharField(max_length=100, default='Lagos')
    location_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    # Pay & Time
    pay_per_worker = models.DecimalField(max_digits=10, decimal_places=2)
    duration_hours = models.DecimalField(max_digits=5, decimal_places=1, null=True, blank=True)
    start_time = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)

    # Source channel (whatsapp / app / ussd)
    source_channel = models.CharField(max_length=20, default='app')

    # Escrow reference
    escrow_reference = models.CharField(max_length=200, blank=True)
    escrow_funded = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'jobs'
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['skill_required']),
            models.Index(fields=['location_city']),
            models.Index(fields=['employer']),
        ]
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} — ₦{self.pay_per_worker} ({self.status})"

    def can_access_record(self, user):
        """Job employer can access job; workers can see open jobs."""
        if user.is_staff or user.is_superuser:
            return True
        # Employer can always access their own job
        if self.employer_id == user.id:
            return True
        # Workers can view open jobs
        if self.status == self.Status.OPEN:
            return True
        # Workers can access jobs they applied to
        return self.applications.filter(worker=user).exists()


class JobApplication(UserOrganizedModel):

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        REJECTED = 'rejected', 'Rejected'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='applications')
    worker = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='applications'
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACCEPTED)
    accepted_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'job_applications'
        unique_together = [['job', 'worker']]

    def __str__(self):
        # phone is optional post phone->email migration — fall back through
        # email, then phone, then raw id so this never prints a bare "None".
        worker_label = self.worker.email or self.worker.phone or str(self.worker.id)
        return f"{worker_label} → {self.job.title} ({self.status})"

    def can_access_record(self, user):
        """Worker and job employer can access application."""
        if user.is_staff or user.is_superuser:
            return True
        return user.id == self.worker_id or user.id == self.job.employer_id


class Rating(UserOrganizedModel):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ratings_given'
    )
    to_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='ratings_received'
    )
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='ratings')
    stars = models.PositiveSmallIntegerField()  # 1-5
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'ratings'
        unique_together = [['from_user', 'to_user', 'job']]

    def __str__(self):
        # Same fallback chain as JobApplication — phone is optional now.
        from_label = self.from_user.email or self.from_user.phone or str(self.from_user.id)
        to_label = self.to_user.email or self.to_user.phone or str(self.to_user.id)
        return f"{from_label} → {to_label}: {self.stars}⭐"

    def can_access_record(self, user):
        """Both the rater and rated user can view the rating."""
        if user.is_staff or user.is_superuser:
            return True
        return user.id == self.from_user_id or user.id == self.to_user_id