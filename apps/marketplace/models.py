"""
Kolliq Marketplace — traders advertise goods, buyers browse and express interest.

Designed for Amina and traders like her. No complex inventory system —
just a listing with price, photo (optional), location, and a contact mechanism.
Builds more transaction history → higher Economic Identity Score.
"""
from django.db import models
from django.conf import settings
import uuid


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    icon = models.CharField(max_length=50, blank=True)   # emoji or icon name
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'marketplace_categories'
        verbose_name_plural = 'categories'
        ordering = ['name']

    def __str__(self):
        return self.name


class Listing(models.Model):

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        SOLD = 'sold', 'Sold'
        PAUSED = 'paused', 'Paused'
        REMOVED = 'removed', 'Removed'

    class Condition(models.TextChoices):
        NEW = 'new', 'New'
        USED_GOOD = 'used_good', 'Used — Good'
        USED_FAIR = 'used_fair', 'Used — Fair'
        NOT_APPLICABLE = 'na', 'Not Applicable'   # for food, produce

    class PriceNegotiable(models.TextChoices):
        FIXED = 'fixed', 'Fixed Price'
        NEGOTIABLE = 'negotiable', 'Negotiable'
        OPEN = 'open', 'Make an Offer'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='listings'
    )
    category = models.ForeignKey(
        Category,
        on_delete=models.SET_NULL,
        null=True,
        related_name='listings'
    )

    title = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    price_type = models.CharField(
        max_length=15,
        choices=PriceNegotiable.choices,
        default=PriceNegotiable.FIXED
    )
    condition = models.CharField(
        max_length=15,
        choices=Condition.choices,
        default=Condition.NOT_APPLICABLE
    )
    quantity_available = models.PositiveIntegerField(default=1)
    unit = models.CharField(
        max_length=50,
        blank=True,
        help_text='e.g. per kg, per bag, per piece, per basket'
    )

    # Location
    location_area = models.CharField(max_length=200)
    location_city = models.CharField(max_length=100, default='Lagos')
    location_lat = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    location_lng = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    market_name = models.CharField(max_length=200, blank=True)

    # Contact
    whatsapp_number = models.CharField(max_length=20, blank=True)
    call_number = models.CharField(max_length=20, blank=True)
    show_phone = models.BooleanField(default=True)

    # Status + metrics
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.ACTIVE
    )
    views_count = models.PositiveIntegerField(default=0)
    enquiries_count = models.PositiveIntegerField(default=0)
    is_featured = models.BooleanField(default=False)
    is_flagged = models.BooleanField(default=False)
    flag_reason = models.TextField(blank=True)

    # Channel that created this listing
    source_channel = models.CharField(
        max_length=20,
        choices=[('app', 'App'), ('ussd', 'USSD'), ('whatsapp', 'WhatsApp')],
        default='app'
    )

    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'marketplace_listings'
        indexes = [
            models.Index(fields=['status', '-created_at']),
            models.Index(fields=['seller']),
            models.Index(fields=['category']),
            models.Index(fields=['location_city']),
            models.Index(fields=['is_featured', 'status']),
        ]
        ordering = ['-is_featured', '-created_at']

    def __str__(self):
        # phone is optional post phone->email migration — fall back through
        # email, then phone, then raw id so this never prints a bare "None".
        seller_label = self.seller.email or self.seller.phone or str(self.seller.id)
        return f"{self.title} — ₦{self.price} ({seller_label})"

    def increment_views(self):
        self.views_count += 1
        self.save(update_fields=['views_count'])


class ListingImage(models.Model):
    """
    Up to 4 images per listing.
    Stored on Cloudflare R2 (free 10GB — more than enough for pilot).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey(
        Listing,
        on_delete=models.CASCADE,
        related_name='images'
    )
    image_url = models.URLField(max_length=500)
    is_primary = models.BooleanField(default=False)
    upload_order = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'marketplace_listing_images'
        ordering = ['upload_order']

    def __str__(self):
        return f"Image for {self.listing.title}"


class Enquiry(models.Model):
    """
    Buyer expresses interest in a listing.
    Not a transaction — just a contact event.
    But it DOES build the seller's transaction history → score.
    """
    class Status(models.TextChoices):
        OPEN = 'open', 'Open'
        RESPONDED = 'responded', 'Responded'
        CLOSED = 'closed', 'Closed'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    listing = models.ForeignKey(
        Listing,
        on_delete=models.CASCADE,
        related_name='enquiries'
    )
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='enquiries_made',
        null=True,
        blank=True    # allow anonymous (USSD) enquiries
    )
    buyer_phone = models.CharField(max_length=20, blank=True)
    message = models.TextField(blank=True)
    offered_price = models.DecimalField(
        max_digits=12, decimal_places=2,
        null=True, blank=True
    )
    status = models.CharField(
        max_length=15,
        choices=Status.choices,
        default=Status.OPEN
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'marketplace_enquiries'
        ordering = ['-created_at']

    def __str__(self):
        return f"Enquiry on {self.listing.title} from {self.buyer_phone or 'anon'}"


class SavedListing(models.Model):
    """Buyer bookmarks a listing."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='saved_listings'
    )
    listing = models.ForeignKey(
        Listing,
        on_delete=models.CASCADE,
        related_name='saved_by'
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'marketplace_saved_listings'
        unique_together = [['user', 'listing']]