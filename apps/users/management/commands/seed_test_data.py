"""
apps/common/management/commands/seed_test_data.py

Seeds a full test dataset: users across all roles, wallets in different
states, jobs + applications + ratings, and marketplace listings + an
enquiry. Deliberately mixes users with and without a phone number, since
phone is now optional — this exercises the null-safe __str__ fixes and
the Paystack _clean_phone(None) fix in the same pass.

No live Paystack API calls are made — wallet Paystack fields are faked
locally so seeding doesn't hit their sandbox or slow down on network calls.

Run:
    python manage.py seed_test_data
    python manage.py seed_test_data --reset   # wipe seed data first, recreate
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model

from apps.wallets.models import Wallet
from apps.jobs.models import Job, JobApplication, Rating
from apps.marketplace.models import Category, Listing, Enquiry

User = get_user_model()
SEED_PIN = "1234"

# (email, full_name, role, phone_or_None, wallet_balance, has_bank_account)
SEED_USERS = [
    ("worker.test@kolliq.app",   "Amaka Worker",    User.Role.WORKER,   "+2348012345001", Decimal("0.00"),     False),
    ("worker2.test@kolliq.app",  "Tunde Worker",     User.Role.WORKER,   None,             Decimal("0.00"),     False),  # no phone — tests null-safety
    ("trader.test@kolliq.app",   "Bola Trader",      User.Role.TRADER,   "+2348012345002", Decimal("15000.00"), True),
    ("trader2.test@kolliq.app",  "Ngozi Trader",     User.Role.TRADER,   None,             Decimal("3000.00"),  False), # no phone — tests contact gap on listings
    ("employer.test@kolliq.app", "Chidi Employer",   User.Role.EMPLOYER, "+2348012345003", Decimal("50000.00"), True),
    ("admin.test@kolliq.app",    "Admin User",       User.Role.ADMIN,    "+2348012345004", Decimal("0.00"),     False),
]

TEST_BANK_CODE = "044"
TEST_BANK_NAME = "Access Bank"
TEST_ACCOUNT_NUMBER = "0000000000"
TEST_ACCOUNT_NAME = "Test Account Holder"


class Command(BaseCommand):
    help = "Seeds a full test dataset: users, wallets, jobs, applications, ratings, listings."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true", help="Delete existing seed users (and cascade) first.")

    def handle(self, *args, **options):
        if options["reset"]:
            emails = [u[0] for u in SEED_USERS]
            deleted, _ = User.objects.filter(email__in=emails).delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing seed record(s) (cascaded)."))

        users = self._seed_users()
        self._seed_wallets(users)
        self._seed_jobs(users)
        self._seed_marketplace(users)

        self.stdout.write(self.style.SUCCESS("\nSeed complete. All users log in with PIN 1234."))
        self._print_summary()

    # ── USERS ────────────────────────────────────────────────────────────────

    def _seed_users(self):
        users = {}
        for email, full_name, role, phone, _balance, _bank in SEED_USERS:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "full_name": full_name,
                    "role": role,
                    "phone": phone,
                    "is_staff": role == User.Role.ADMIN,
                    "is_superuser": role == User.Role.ADMIN,
                    "location_city": "Lagos",
                    "location_area": "Ile-Ife" if role == User.Role.WORKER else "Yaba",
                },
            )
            if created:
                user.set_pin(SEED_PIN)
                user.set_password(SEED_PIN)
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Created user: {email} ({role}) phone={phone or 'none'}"))
            else:
                self.stdout.write(f"User exists, skipping: {email}")
            users[email] = user
        return users

    # ── WALLETS ──────────────────────────────────────────────────────────────

    def _seed_wallets(self, users):
        for email, _name, _role, _phone, balance, has_bank in SEED_USERS:
            user = users[email]
            wallet, created = Wallet.objects.get_or_create(
                user=user,
                defaults={
                    "balance": balance,
                    "paystack_customer_id": f"CUS_seed_{user.id.hex[:10]}",
                    "paystack_creation_status": "created",
                },
            )
            if created and has_bank:
                wallet.bank_account_number = TEST_ACCOUNT_NUMBER
                wallet.bank_code = TEST_BANK_CODE
                wallet.bank_name = TEST_BANK_NAME
                wallet.bank_account_name = TEST_ACCOUNT_NAME
                wallet.bank_account_verified = True
                wallet.save(update_fields=[
                    "bank_account_number", "bank_code", "bank_name",
                    "bank_account_name", "bank_account_verified", "updated_at",
                ])
            if created:
                self.stdout.write(f"  Wallet for {email}: ₦{wallet.balance}")

    # ── JOBS / APPLICATIONS / RATINGS ───────────────────────────────────────

    def _seed_jobs(self, users):
        employer = users["employer.test@kolliq.app"]
        worker1 = users["worker.test@kolliq.app"]
        worker2 = users["worker2.test@kolliq.app"]

        job_open, _ = Job.objects.get_or_create(
            employer=employer,
            title="Need 2 people for market cleanup — Yaba",
            defaults=dict(
                description="Half-day cleanup job, tools provided.",
                skill_required=Job.SkillRequired.CLEANING,
                workers_needed=2,
                location_area="Yaba Market",
                location_city="Lagos",
                pay_per_worker=Decimal("5000.00"),
                duration_hours=Decimal("4.0"),
                status=Job.Status.OPEN,
            ),
        )

        job_completed, _ = Job.objects.get_or_create(
            employer=employer,
            title="Delivery run — Ikeja to Surulere",
            defaults=dict(
                description="Single delivery, motorbike preferred.",
                skill_required=Job.SkillRequired.DELIVERY,
                workers_needed=1,
                location_area="Ikeja",
                location_city="Lagos",
                pay_per_worker=Decimal("2500.00"),
                duration_hours=Decimal("1.5"),
                status=Job.Status.COMPLETED,
                escrow_reference="KLQ-ESC-SEED-0001",
                escrow_funded=True,
            ),
        )

        # worker1 (has phone) applied + accepted on the open job
        JobApplication.objects.get_or_create(
            job=job_open, worker=worker1,
            defaults=dict(status=JobApplication.Status.ACCEPTED),
        )

        # worker2 (no phone — tests the null-safe __str__ fix) completed the delivery job
        JobApplication.objects.get_or_create(
            job=job_completed, worker=worker2,
            defaults=dict(status=JobApplication.Status.COMPLETED),
        )

        # Rating from employer to worker2 for the completed job
        Rating.objects.get_or_create(
            from_user=employer, to_user=worker2, job=job_completed,
            defaults=dict(stars=5, comment="Fast and reliable, would hire again."),
        )

        self.stdout.write(self.style.SUCCESS(
            f"  Jobs: 1 open ({job_open.title}), 1 completed+rated ({job_completed.title})"
        ))

    # ── MARKETPLACE ──────────────────────────────────────────────────────────

    def _seed_marketplace(self, users):
        trader1 = users["trader.test@kolliq.app"]   # has phone
        trader2 = users["trader2.test@kolliq.app"]  # no phone — tests contact gap
        worker1 = users["worker.test@kolliq.app"]   # will be the enquiry buyer

        category, _ = Category.objects.get_or_create(
            slug="produce", defaults=dict(name="Fresh Produce", icon="🥬"),
        )

        listing_with_contact, _ = Listing.objects.get_or_create(
            seller=trader1, title="Fresh tomatoes — basket",
            defaults=dict(
                category=category,
                description="Farm-fresh, sold by the basket.",
                price=Decimal("8000.00"),
                price_type=Listing.PriceNegotiable.NEGOTIABLE,
                condition=Listing.Condition.NOT_APPLICABLE,
                quantity_available=10,
                unit="per basket",
                location_area="Mile 12 Market",
                location_city="Lagos",
                market_name="Mile 12",
                whatsapp_number=trader1.phone,
                call_number=trader1.phone,
                show_phone=True,
            ),
        )

        # trader2 has no phone — listing created with NO contact info at all.
        # This is the real gap flagged earlier: buyers have no way to reach
        # this seller. Deliberately seeded this way so it's visible in testing.
        listing_no_contact, _ = Listing.objects.get_or_create(
            seller=trader2, title="Used generator — Tiger 2KVA",
            defaults=dict(
                category=category,
                description="Works fine, selling because I upgraded.",
                price=Decimal("45000.00"),
                price_type=Listing.PriceNegotiable.FIXED,
                condition=Listing.Condition.USED_GOOD,
                quantity_available=1,
                unit="",
                location_area="Yaba",
                location_city="Lagos",
                whatsapp_number="",
                call_number="",
                show_phone=False,
            ),
        )

        Enquiry.objects.get_or_create(
            listing=listing_with_contact, buyer=worker1,
            defaults=dict(
                buyer_phone=worker1.phone or "",
                message="Is this still available? Can you do ₦7000?",
                offered_price=Decimal("7000.00"),
                status=Enquiry.Status.OPEN,
            ),
        )

        self.stdout.write(self.style.SUCCESS(
            f"  Listings: '{listing_with_contact.title}' (has contact), "
            f"'{listing_no_contact.title}' (NO contact — gap case), 1 enquiry"
        ))

    # ── SUMMARY ──────────────────────────────────────────────────────────────

    def _print_summary(self):
        self.stdout.write(self.style.SUCCESS("\nTest accounts (PIN 1234 for all):"))
        self.stdout.write("  worker.test@kolliq.app    → ₦0, has phone. Test FundWalletView + BankAccountSaveView.")
        self.stdout.write("  worker2.test@kolliq.app   → ₦0, NO phone. Completed job + received a rating — check __str__ output in admin.")
        self.stdout.write("  trader.test@kolliq.app    → ₦15,000, bank verified, has phone. Test WithdrawalRequestView (auto-approve).")
        self.stdout.write("  trader2.test@kolliq.app   → ₦3,000, NO phone, no bank account. Listing has zero contact info — check that gap.")
        self.stdout.write("  employer.test@kolliq.app  → ₦50,000, bank verified. Owns 1 open + 1 completed job.")
        self.stdout.write("  admin.test@kolliq.app     → /admin/ login via password 1234.")
        self.stdout.write(self.style.SUCCESS(
            "\nRun `python manage.py shell` and check `str(JobApplication.objects.get(worker__email='worker2.test@kolliq.app'))` "
            "and `str(Listing.objects.get(seller__email='trader2.test@kolliq.app'))` to confirm the null-safe __str__ fixes render cleanly."
        ))