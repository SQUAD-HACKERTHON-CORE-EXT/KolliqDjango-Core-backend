from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from kolliq.permissions import IsAuthenticatedOrInternalSecret, resolve_user
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from django.shortcuts import get_object_or_404
from django.db.models import Q
from django.utils import timezone
from kolliq.utils import success_response, error_response
from .models import Listing, ListingImage, Enquiry, Category, SavedListing
from .serializers import (
    ListingCreateSerializer, ListingListSerializer, ListingDetailSerializer,
    ListingUpdateSerializer, EnquiryCreateSerializer, EnquirySerializer,
    ListingImageCreateSerializer, CategorySerializer,
)
import logging

logger = logging.getLogger(__name__)

PAGE_SIZE = 20
BEARER_SECURITY = [{"bearerAuth": []}]


# ══════════════════════════════════════════════════════════════════
# CATEGORIES
# ══════════════════════════════════════════════════════════════════

class CategoryListView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id='marketplace_categories_list',
        summary='Get categories',
        description='Get list of active marketplace categories. No authentication required.',
        request=None,
        responses={
            200: OpenApiResponse(response=CategorySerializer, description='List of active categories.'),
        },
        tags=['Marketplace'],
    )
    def get(self, request):
        categories = Category.objects.filter(is_active=True)
        return success_response({'categories': CategorySerializer(categories, many=True).data})


# ══════════════════════════════════════════════════════════════════
# LISTINGS — BROWSE
# ══════════════════════════════════════════════════════════════════

class ListingFeedView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id='marketplace_listings_list',
        summary='Get listings feed',
        description=(
            'Paginated public feed of active listings. No authentication required. '
            'Filter by category, city, search query, price range, condition, or seller. '
            'Featured listings float to the top.'
        ),
        request=None,
        responses={
            200: OpenApiResponse(response=ListingListSerializer, description='Paginated listings.'),
        },
        examples=[
            OpenApiExample(
                'Response',
                value={'listings': [], 'total': 0, 'page': 1, 'pages': 1},
                response_only=True,
            ),
        ],
        tags=['Marketplace'],
    )
    def get(self, request):
        qs = Listing.objects.filter(
            status=Listing.Status.ACTIVE
        ).select_related('seller', 'category').prefetch_related('images', 'saved_by')

        category_slug = request.query_params.get('category')
        city = request.query_params.get('city')
        search = request.query_params.get('q')
        min_price = request.query_params.get('min_price')
        max_price = request.query_params.get('max_price')
        seller_id = request.query_params.get('seller_id')
        condition = request.query_params.get('condition')

        if category_slug:
            qs = qs.filter(category__slug=category_slug)
        if city:
            qs = qs.filter(location_city__icontains=city)
        if search:
            qs = qs.filter(
                Q(title__icontains=search) |
                Q(description__icontains=search) |
                Q(market_name__icontains=search)
            )
        if min_price:
            qs = qs.filter(price__gte=min_price)
        if max_price:
            qs = qs.filter(price__lte=max_price)
        if seller_id:
            qs = qs.filter(seller_id=seller_id)
        if condition:
            qs = qs.filter(condition=condition)

        try:
            page = max(1, int(request.query_params.get('page', 1)))
        except ValueError:
            page = 1

        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        total = qs.count()
        listings = qs[start:end]

        return success_response({
            'listings': ListingListSerializer(listings, many=True, context={'request': request}).data,
            'total': total,
            'page': page,
            'pages': (total + PAGE_SIZE - 1) // PAGE_SIZE,
        })


class ListingDetailView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        operation_id='marketplace_listings_retrieve',
        summary='Get listing details',
        description='Get detailed information for a specific listing. No authentication required.',
        request=None,
        responses={
            200: OpenApiResponse(response=ListingDetailSerializer, description='Listing details.'),
            404: OpenApiResponse(description='Listing not found or not active.'),
        },
        tags=['Marketplace'],
    )
    def get(self, request, listing_id):
        listing = get_object_or_404(
            Listing.objects.select_related('seller', 'category').prefetch_related('images'),
            id=listing_id,
            status__in=[Listing.Status.ACTIVE, Listing.Status.PAUSED]
        )

        from apps.marketplace.tasks import increment_listing_views
        increment_listing_views.delay(str(listing_id))

        return success_response(ListingDetailSerializer(listing, context={'request': request}).data)


# ══════════════════════════════════════════════════════════════════
# LISTINGS — MANAGE (seller)
# ══════════════════════════════════════════════════════════════════

class ListingCreateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_listings_create',
        summary='Create listing',
        description='Create a new marketplace listing. Triggers score recalculation.',
        request=ListingCreateSerializer,
        responses={
            201: OpenApiResponse(response=ListingDetailSerializer, description='Listing created.'),
            400: OpenApiResponse(description='Validation error.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        tags=['Marketplace'],
    )
    def post(self, request):
        serializer = ListingCreateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return error_response(serializer.errors)

        listing = serializer.save()

        from apps.scoring.tasks import recalculate_score
        recalculate_score.delay(str(request.user.id))

        return success_response(
            ListingDetailSerializer(listing, context={'request': request}).data,
            status=201
        )


class ListingUpdateView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_listings_update',
        summary='Update listing',
        description='Partially update an existing listing. Only the listing owner can update.',
        request=ListingUpdateSerializer,
        responses={
            200: OpenApiResponse(response=ListingDetailSerializer, description='Updated listing.'),
            400: OpenApiResponse(description='Validation error.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Listing not found or not owned by user.'),
        },
        tags=['Marketplace'],
    )
    def patch(self, request, listing_id):
        listing = get_object_or_404(Listing, id=listing_id, seller=request.user)
        serializer = ListingUpdateSerializer(listing, data=request.data, partial=True)
        if not serializer.is_valid():
            return error_response(serializer.errors)

        listing = serializer.save()
        return success_response(ListingDetailSerializer(listing, context={'request': request}).data)


class ListingPurchaseView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_listings_purchase',
        summary='Purchase listing',
        description=(
            'Buy a listing using Kolliq wallet balance. '
            'Pure internal ledger transfer — no external payment call. '
            'Platform takes a 2% fee; seller receives the remainder. '
            'Seller is notified via email on purchase.'
        ),
        request=None,
        responses={
            201: OpenApiResponse(description='Purchase completed successfully.'),
            400: OpenApiResponse(description='Insufficient balance, invalid quantity, or own listing.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Listing not found or wallet not ready.'),
        },
        examples=[
            OpenApiExample('Request', value={'quantity': 1, 'message': 'I will pick up tomorrow'}, request_only=True),
            OpenApiExample(
                'Response',
                value={
                    'reference': 'MKT-listing-buyer',
                    'quantity': 1,
                    'total_paid': '5000.00',
                    'platform_fee': '100.00',
                    'seller_received': '4900.00',
                    'your_new_balance': '10000.00',
                    'status': 'completed',
                },
                response_only=True,
            ),
        ],
        tags=['Marketplace'],
    )
    def post(self, request, listing_id):
        from apps.marketplace.models import Listing, Enquiry
        from apps.payments.models import Transaction
        from django.db import transaction as db_transaction
        from decimal import Decimal

        buyer = request.user
        listing = get_object_or_404(Listing, id=listing_id, status='active')

        if listing.seller == buyer:
            return error_response('You cannot purchase your own listing.')

        quantity = int(request.data.get('quantity', 1))
        if quantity < 1:
            return error_response('Quantity must be at least 1.')

        if quantity > listing.quantity_available:
            return error_response(f'Only {listing.quantity_available} available.')

        total = listing.price * quantity
        PLATFORM_CUT = (total * Decimal('0.02')).quantize(Decimal('0.01'))
        seller_receives = total - PLATFORM_CUT

        try:
            buyer_wallet = buyer.wallet
        except Exception:
            return error_response('Your wallet is not ready.', status=404)

        if buyer_wallet.balance < total:
            return error_response(
                f'Insufficient balance. Need ₦{total}, you have ₦{buyer_wallet.balance}. Top up your wallet first.'
            )

        seller = listing.seller
        try:
            seller_wallet = seller.wallet
        except Exception:
            return error_response("Seller's wallet is not ready.")

        reference = f"MKT-{listing.id}-{buyer.id}"[:40]

       
        with db_transaction.atomic():
            from apps.wallets.models import Wallet

            buyer_wallet  = Wallet.objects.select_for_update().get(user=buyer)
            seller_wallet = Wallet.objects.select_for_update().get(user=seller)
            # ← No platform wallet lock needed anymore

            if buyer_wallet.balance < total:
                return error_response(f'Insufficient balance: ₦{buyer_wallet.balance}.')

            buyer_wallet.debit(total)
            seller_wallet.credit(seller_receives)
            # ← FIX: PLATFORM_CUT is no longer credited to any wallet.
            # It simply stays inside the gap between what buyer paid and what
            # seller received — recorded below as a PLATFORM_FEE transaction
            # so services.platform_revenue can account for it.

            listing.quantity_available -= quantity
            if listing.quantity_available == 0:
                listing.status = 'sold'
            listing.save(update_fields=['quantity_available', 'status', 'updated_at'])

            Transaction.objects.create(
                user=buyer,
                transaction_type=Transaction.Type.DEBIT,
                amount=total,
                status=Transaction.Status.SUCCESS,
                related_user=seller,
                description=f'Purchase: {listing.title} x{quantity}',
                metadata={'type': 'marketplace_purchase', 'listing_id': str(listing.id), 'quantity': quantity, 'reference': reference},
            )
            Transaction.objects.create(
                user=seller,
                transaction_type=Transaction.Type.CREDIT,
                amount=seller_receives,
                status=Transaction.Status.SUCCESS,
                related_user=buyer,
                description=f'Sale: {listing.title} x{quantity}',
                metadata={'type': 'marketplace_sale', 'listing_id': str(listing.id), 'platform_fee': str(PLATFORM_CUT), 'reference': reference},
            )
            Transaction.objects.create(
                user=seller,
                transaction_type=Transaction.Type.PLATFORM_FEE,
                amount=PLATFORM_CUT,
                status=Transaction.Status.SUCCESS,
                related_user=buyer,
                description=f'Marketplace fee (2%): {listing.title}',
                metadata={'type': 'marketplace_platform_fee', 'fee_type': 'marketplace', 'listing_id': str(listing.id), 'reference': reference},
            )

        # FIX: services/africas_talking.py was removed when Kolliq migrated
        # from SMS to email notifications — this used to import ATService,
        # which no longer exists and would raise ModuleNotFoundError the
        # first time anyone completed a purchase (after the wallet debit/
        # credit had already committed). Now uses EmailService instead,
        # matching every other notification path in the codebase.
        if seller.email:
            from services.email_service import EmailService
            email_service = EmailService()
            email_service.send_email(
                seller.email,
                'Kolliq: Item Sold',
                f"{buyer.display_name} bought {quantity}x '{listing.title}' for ₦{seller_receives}. "
                f"Balance: ₦{seller_wallet.balance}."
            )
        else:
            logger.warning(f'Seller {seller.id} has no email — sale notification skipped for {reference}')

        from apps.scoring.tasks import recalculate_score
        recalculate_score.delay(str(seller.id))

        return success_response({
            'reference': reference,
            'listing': listing.title,
            'quantity': quantity,
            'total_paid': str(total),
            'platform_fee': str(PLATFORM_CUT),
            'seller_received': str(seller_receives),
            'your_new_balance': str(buyer_wallet.balance),
            'status': 'completed',
            'message': f'Purchase complete! ₦{total} paid. Contact {seller.display_name} to arrange pickup.',
        }, status=201)


class ListingDeleteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_listings_delete',
        summary='Delete listing',
        description='Soft-delete a listing by marking it as removed. Only the listing owner can delete.',
        request=None,
        responses={
            200: OpenApiResponse(description='Listing removed.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Listing not found or not owned by user.'),
        },
        tags=['Marketplace'],
    )
    def delete(self, request, listing_id):
        listing = get_object_or_404(Listing, id=listing_id, seller=request.user)
        listing.status = Listing.Status.REMOVED
        listing.save(update_fields=['status', 'updated_at'])
        return success_response({'message': 'Listing removed.'})


class MyListingsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_my_listings_list',
        summary='Get my listings',
        description='Get all listings posted by the authenticated user. Filter by status via ?status= query param.',
        request=None,
        responses={
            200: OpenApiResponse(response=ListingListSerializer, description='User listings.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        tags=['Marketplace'],
    )
    def get(self, request):
        status_filter = request.query_params.get('status')
        qs = Listing.objects.filter(seller=request.user).prefetch_related('images')
        if status_filter:
            qs = qs.filter(status=status_filter)
        qs = qs.order_by('-created_at')
        return success_response({
            'listings': ListingListSerializer(qs, many=True, context={'request': request}).data,
            'count': qs.count(),
        })


# ══════════════════════════════════════════════════════════════════
# IMAGES
# ══════════════════════════════════════════════════════════════════

class ListingImageAddView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_listing_images_create',
        summary='Add listing image',
        description='Add an image URL to a listing. Frontend uploads to Cloudflare R2 first, then sends the URL here.',
        request=ListingImageCreateSerializer,
        responses={
            201: OpenApiResponse(description='Image added.'),
            400: OpenApiResponse(description='Validation error.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Listing not found or not owned by user.'),
        },
        tags=['Marketplace'],
    )
    def post(self, request, listing_id):
        listing = get_object_or_404(Listing, id=listing_id, seller=request.user)
        data = {**request.data, 'listing': str(listing.id)}
        serializer = ListingImageCreateSerializer(data=data, context={'request': request})
        if not serializer.is_valid():
            return error_response(serializer.errors)

        if serializer.validated_data.get('is_primary'):
            listing.images.filter(is_primary=True).update(is_primary=False)

        image = serializer.save()
        return success_response({
            'image_id': str(image.id),
            'image_url': image.image_url,
            'is_primary': image.is_primary,
        }, status=201)

    @extend_schema(
        operation_id='marketplace_listing_images_delete',
        summary='Delete listing image',
        description='Delete an image from a listing using ?image_id= query param.',
        request=None,
        responses={
            200: OpenApiResponse(description='Image removed.'),
            400: OpenApiResponse(description='image_id query param missing.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Image or listing not found.'),
        },
        tags=['Marketplace'],
    )
    def delete(self, request, listing_id):
        image_id = request.query_params.get('image_id')
        if not image_id:
            return error_response('image_id query param required.')
        listing = get_object_or_404(Listing, id=listing_id, seller=request.user)
        image = get_object_or_404(ListingImage, id=image_id, listing=listing)
        image.delete()
        return success_response({'message': 'Image removed.'})


# ══════════════════════════════════════════════════════════════════
# ENQUIRIES
# ══════════════════════════════════════════════════════════════════

class EnquiryCreateView(APIView):
    permission_classes = [IsAuthenticatedOrInternalSecret]

    @extend_schema(
        operation_id='marketplace_enquiries_create',
        summary='Create enquiry',
        description=(
            'Send an enquiry to a listing seller. Seller is notified via email. '
            'Response includes both `seller_phone` and `seller_email` as contact fallbacks — '
            'phone is optional on Kolliq accounts, so a seller who registered without one would '
            'otherwise leave the buyer with no way to reach them. Both fields collapse to '
            '"Contact via platform" if the seller has `show_phone` disabled.'
        ),
        request=EnquiryCreateSerializer,
        responses={
            201: OpenApiResponse(description='Enquiry sent.'),
            400: OpenApiResponse(description='Validation error.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        tags=['Marketplace'],
    )
    def post(self, request):
        user, err = resolve_user(request)
        if err:
            return err
        serializer = EnquiryCreateSerializer(data=request.data, context={'request': request, 'user': user})
        
        if not serializer.is_valid():
            return error_response(serializer.errors)

        enquiry = serializer.save()

        from apps.marketplace.tasks import notify_seller_new_enquiry
        notify_seller_new_enquiry.delay(str(enquiry.id))

        # phone is optional post phone->email migration — a phone-less
        # seller previously left the buyer with seller_phone: null and no
        # other way to reach them. email is now always included as a
        # fallback when show_phone is true; when show_phone is false the
        # buyer is still directed to the platform, unchanged.
        seller = enquiry.listing.seller
        return success_response({
            'enquiry_id': str(enquiry.id),
            'listing_title': enquiry.listing.title,
            'seller_phone': (
                seller.phone
                if enquiry.listing.show_phone
                else 'Contact via platform'
            ),
            'seller_email': (
                seller.email
                if enquiry.listing.show_phone
                else 'Contact via platform'
            ),
            'message': 'Enquiry sent! The seller will be notified.',
        }, status=201)


class MyEnquiriesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_my_enquiries_list',
        summary='Get my enquiries',
        description='Get all enquiries sent by the authenticated user.',
        request=None,
        responses={
            200: OpenApiResponse(response=EnquirySerializer, description='Sent enquiries.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        tags=['Marketplace'],
    )
    def get(self, request):
        enquiries = Enquiry.objects.filter(
            buyer=request.user
        ).select_related('listing').order_by('-created_at')
        return success_response({
            'enquiries': EnquirySerializer(enquiries, many=True).data,
            'count': enquiries.count(),
        })


class SellerEnquiriesView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_seller_enquiries_list',
        summary='Get received enquiries',
        description='Get enquiries received on the authenticated seller\'s listings. Filter by ?listing_id= query param.',
        request=None,
        responses={
            200: OpenApiResponse(response=EnquirySerializer, description='Received enquiries.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        tags=['Marketplace'],
    )
    def get(self, request):
        listing_id = request.query_params.get('listing_id')
        qs = Enquiry.objects.filter(
            listing__seller=request.user
        ).select_related('listing').order_by('-created_at')
        if listing_id:
            qs = qs.filter(listing_id=listing_id)
        return success_response({
            'enquiries': EnquirySerializer(qs, many=True).data,
            'count': qs.count(),
        })


class EnquiryRespondView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_enquiries_respond',
        summary='Mark enquiry as responded',
        description='Mark an enquiry as responded. Only the listing seller can respond.',
        request=None,
        responses={
            200: OpenApiResponse(description='Enquiry marked as responded.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Enquiry not found or not owned by seller.'),
        },
        tags=['Marketplace'],
    )
    def patch(self, request, enquiry_id):
        enquiry = get_object_or_404(Enquiry, id=enquiry_id, listing__seller=request.user)
        enquiry.status = Enquiry.Status.RESPONDED
        enquiry.save(update_fields=['status', 'updated_at'])
        return success_response({'status': 'responded'})


# ══════════════════════════════════════════════════════════════════
# SAVED LISTINGS
# ══════════════════════════════════════════════════════════════════

class SaveListingView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_listings_save',
        summary='Save or unsave listing',
        description='Toggle saved status for a listing. Calling again on a saved listing removes it.',
        request=None,
        responses={
            201: OpenApiResponse(description='Listing saved.'),
            200: OpenApiResponse(description='Listing removed from saved.'),
            401: OpenApiResponse(description='Not authenticated.'),
            404: OpenApiResponse(description='Listing not found.'),
        },
        tags=['Marketplace'],
    )
    def post(self, request, listing_id):
        listing = get_object_or_404(Listing, id=listing_id)
        saved, created = SavedListing.objects.get_or_create(user=request.user, listing=listing)
        if not created:
            saved.delete()
            return success_response({'saved': False, 'message': 'Removed from saved.'})
        return success_response({'saved': True, 'message': 'Saved!'}, status=201)


class SavedListingsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id='marketplace_saved_listings_list',
        summary='Get saved listings',
        description='Get all active listings saved by the authenticated user.',
        request=None,
        responses={
            200: OpenApiResponse(response=ListingListSerializer, description='Saved listings.'),
            401: OpenApiResponse(description='Not authenticated.'),
        },
        tags=['Marketplace'],
    )
    def get(self, request):
        saved = SavedListing.objects.filter(
            user=request.user
        ).select_related('listing__seller', 'listing__category').prefetch_related(
            'listing__images'
        ).order_by('-created_at')
        listings = [s.listing for s in saved if s.listing.status == 'active']
        return success_response({
            'listings': ListingListSerializer(listings, many=True, context={'request': request}).data,
            'count': len(listings),
        })