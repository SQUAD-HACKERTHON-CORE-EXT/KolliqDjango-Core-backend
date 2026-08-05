from django.conf import settings
import uuid
from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
import random
import string
from django.utils import timezone


class UserManager(BaseUserManager):
    """Custom manager — email is the unique identifier, not username."""

    def create_user(self, email, phone=None, pin=None, **extra_fields):
        if not email:
            raise ValueError("Email is required.")
        email = self.normalize_email(email)
        user = self.model(email=email, phone=phone, **extra_fields)
        if pin:
            user.set_pin(pin)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, phone=None, pin=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(email, phone=phone, pin=pin, **extra_fields)


class User(AbstractBaseUser):
    """
    Custom user model.
    - Email is the login identifier.
    - PIN is hashed (not password) — use set_pin / check_pin.
    - JWT-based auth via djangorestframework-simplejwt.
    - Phone is optional profile data only — not used for auth or delivery.
    """

    # ------------------------------------------------------------------ #
    #  Choices
    # ------------------------------------------------------------------ #
    class Role(models.TextChoices):
        WORKER = 'worker', 'Worker'
        TRADER = 'trader', 'Trader'
        EMPLOYER = 'employer', 'Employer'
        ADMIN = 'admin', 'Admin'

    class Availability(models.TextChoices):
        FULL_DAY = 'full_day', 'Full Day'
        MORNING = 'morning', 'Morning'
        AFTERNOON = 'afternoon', 'Afternoon'
        EVENING = 'evening', 'Evening'
        WEEKENDS = 'weekends', 'Weekends'

    class Gender(models.TextChoices):
        MALE = 'M', 'Male'
        FEMALE = 'F', 'Female'
        OTHER = 'O', 'Other'

    # ------------------------------------------------------------------ #
    #  Core identity
    # ------------------------------------------------------------------ #
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    phone = models.CharField(max_length=20, unique=True, blank=True, null=True, db_index=True)
    pin = models.CharField(max_length=128, blank=True)  # Hashed PIN

    # ------------------------------------------------------------------ #
    #  Profile
    # ------------------------------------------------------------------ #
    full_name = models.CharField(max_length=255, blank=True, default='')
    middle_name = models.CharField(max_length=255, blank=True, default='')
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.WORKER)
    gender = models.CharField(max_length=1, choices=Gender.choices, blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)

    # ------------------------------------------------------------------ #
    #  Location
    # ------------------------------------------------------------------ #
    location_area = models.CharField(max_length=255, blank=True, default='')
    location_city = models.CharField(max_length=255, blank=True, default='Lagos')
    location_lat = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)
    location_lng = models.DecimalField(max_digits=9, decimal_places=6, blank=True, null=True)

    # ------------------------------------------------------------------ #
    #  Work profile
    # ------------------------------------------------------------------ #
    skills = models.JSONField(default=list, blank=True)
    languages = models.JSONField(default=list, blank=True)
    has_vehicle = models.BooleanField(default=False)
    vehicle_type = models.CharField(max_length=50, blank=True, default='none')
    availability = models.CharField(
        max_length=20, choices=Availability.choices, default=Availability.FULL_DAY
    )
    trade_category = models.CharField(max_length=255, blank=True, default='')
    market_name = models.CharField(max_length=255, blank=True, default='')
    weekly_income_range = models.CharField(max_length=100, blank=True, default='')
    business_name = models.CharField(max_length=255, blank=True, default='')

    # ------------------------------------------------------------------ #
    #  Meta / acquisition
    # ------------------------------------------------------------------ #
    channel = models.CharField(max_length=50, blank=True, default='app')
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    is_staff = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    onboarding_complete = models.BooleanField(default=False)

    # ------------------------------------------------------------------ #
    #  Timestamps
    # ------------------------------------------------------------------ #
    last_login = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # ------------------------------------------------------------------ #
    #  Auth config
    # ------------------------------------------------------------------ #
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        db_table = 'users'
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.full_name or self.email} ({self.role})"

    # ------------------------------------------------------------------ #
    #  PIN helpers  (NOT Django's password field)
    # ------------------------------------------------------------------ #
    def set_pin(self, raw_pin: str):
        """Hash and store a PIN."""
        self.pin = make_password(raw_pin)

    def check_pin(self, raw_pin: str) -> bool:
        """Return True if raw_pin matches the stored hash."""
        return check_password(raw_pin, self.pin)

    # ------------------------------------------------------------------ #
    #  Django permission shim (required by AbstractBaseUser)
    # ------------------------------------------------------------------ #
    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser


class PinResetOTP(models.Model):
    """
    Stores a short-lived OTP for the PIN reset flow.

    Flow:
        POST /api/auth/reset-pin/request/  → generates & emails OTP
        POST /api/auth/reset-pin/confirm/  → verifies OTP, sets new PIN

    One active OTP per user at a time — requesting a new OTP invalidates
    any previous ones (is_used=True on the old ones).
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='pin_reset_otps',
    )
    otp = models.CharField(max_length=8)
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'PIN Reset OTP'
        verbose_name_plural = 'PIN Reset OTPs'

    def __str__(self):
        return f"OTP for {self.user} — {'used' if self.is_used else 'active'}"

    @staticmethod
    def generate_otp(length: int = 6) -> str:
        return ''.join(random.choices(string.digits, k=length))

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at

    @property
    def is_valid(self) -> bool:
        return not self.is_used and not self.is_expired

    @classmethod
    def create_for_user(cls, user, expiry_minutes: int = 10) -> 'PinResetOTP':
        """
        Invalidates all previous OTPs for this user and creates a fresh one.
        expiry_minutes defaults to 10 — adjust in settings if needed.
        """
        cls.objects.filter(user=user, is_used=False).update(is_used=True)

        otp_value = cls.generate_otp()
        expires_at = timezone.now() + timezone.timedelta(minutes=expiry_minutes)

        return cls.objects.create(
            user=user,
            otp=otp_value,
            expires_at=expires_at,
        )