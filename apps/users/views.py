import logging

from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiResponse, OpenApiExample
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError

from apps.users.models import User, PinResetOTP
from apps.users.serializers import (
    UserSerializer,
    UserCreateSerializer,
    LoginSerializer,
    ChangePinSerializer,
    ResetPinRequestSerializer,
    ResetPinConfirmSerializer,
)
from services.email_service import EmailService

logger = logging.getLogger(__name__)
_email = EmailService()
BEARER_SECURITY = [{"bearerAuth": []}]


def _user_response(user, status_code=status.HTTP_200_OK):
    refresh = RefreshToken.for_user(user)
    return Response(
        {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": UserSerializer(user).data,
        },
        status=status_code,
    )


def _send_otp_email(email: str, otp: str) -> None:
    subject = "Your Kolliq PIN Reset Code"
    message = (
        f"Your reset OTP is {otp}. "
        "It expires in 10 minutes. Do not share it with anyone."
    )
    result = _email.send_email(email, subject, message)
    if not result.get("success"):
        logger.error("EmailService failed to deliver OTP to %s — response: %s", email, result)
        raise RuntimeError("Email delivery failed")


class RegisterView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Register a new user",
        description=(
            "Creates a new user account. Returns access + refresh JWT tokens "
            "and the full user profile on success. "
            "Returns 409 if the email (or phone, if supplied) is already registered."
        ),
        request=UserCreateSerializer,
        responses={
            201: OpenApiResponse(response=UserSerializer, description="Account created."),
            400: OpenApiResponse(description="Validation error."),
            409: OpenApiResponse(description="Email or phone number already registered."),
        },
        examples=[
            OpenApiExample(
                "Worker registration",
                value={
                    "email": "amaka.obi@example.com",
                    "phone": "+2348012345678",
                    "pin": "123456",
                    "full_name": "Amaka Obi",
                    "role": "worker",
                    "gender": "female",
                    "location_city": "Lagos",
                },
                request_only=True,
            ),
        ],
        tags=["Auth"],
    )
    def post(self, request):
        serializer = UserCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        email = data["email"]
        phone = data.get("phone") or None

        if User.objects.filter(email=email).exists():
            return Response(
                {"detail": "An account with this email already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        if phone and User.objects.filter(phone=phone).exists():
            return Response(
                {"detail": "An account with this phone number already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        pin = data.pop("pin", None)
        user = User(
            email=email,
            phone=phone,
            full_name=data.get("full_name", ""),
            role=data.get("role", User.Role.WORKER),
            gender=data.get("gender"),
            date_of_birth=data.get("date_of_birth"),
            address=data.get("address"),
            location_area=data.get("location_area", ""),
            location_city=data.get("location_city", "Lagos"),
            skills=data.get("skills", []),
            languages=data.get("languages", []),
            has_vehicle=data.get("has_vehicle", False),
            vehicle_type=data.get("vehicle_type", "none"),
            availability=data.get("availability", User.Availability.FULL_DAY),
            trade_category=data.get("trade_category", ""),
            market_name=data.get("market_name", ""),
            weekly_income_range=data.get("weekly_income_range", ""),
            business_name=data.get("business_name", ""),
            channel=data.get("channel", "app"),
        )

        if pin:
            user.set_pin(pin)
        user.save()

        logger.info("New user registered: %s (role=%s)", email, user.role)
        return _user_response(user, status_code=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Login with email + PIN",
        description=(
            "Authenticates a user with their email and PIN. "
            "Returns access + refresh JWT tokens and the full user profile."
        ),
        request=LoginSerializer,
        responses={
            200: OpenApiResponse(response=UserSerializer, description="Login successful."),
            400: OpenApiResponse(description="Validation error."),
            401: OpenApiResponse(description="Invalid email or PIN."),
            403: OpenApiResponse(description="Account is deactivated."),
        },
        examples=[
            OpenApiExample(
                "Login example",
                value={"email": "amaka.obi@example.com", "pin": "1234"},
                request_only=True,
            ),
        ],
        tags=["Auth"],
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]
        pin = serializer.validated_data["pin"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"detail": "Invalid email or PIN."}, status=status.HTTP_401_UNAUTHORIZED)

        if not user.is_active:
            return Response({"detail": "This account has been deactivated."}, status=status.HTTP_403_FORBIDDEN)

        if not user.check_pin(pin):
            return Response({"detail": "Invalid email or PIN."}, status=status.HTTP_401_UNAUTHORIZED)

        user.last_login = timezone.now()
        user.save(update_fields=["last_login"])

        logger.info("User logged in: %s", email)
        return _user_response(user)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Logout",
        description=(
            "Blacklists the provided refresh token. "
            "Send the refresh token in the request body."
        ),
        request=None,
        responses={
            200: OpenApiResponse(description="Logged out successfully."),
            400: OpenApiResponse(description="Refresh token missing or invalid."),
            401: OpenApiResponse(description="Not authenticated."),
        },
        examples=[
            OpenApiExample(
                "Logout body",
                value={"refresh": "<your_refresh_token>"},
                request_only=True,
            ),
        ],
        tags=["Auth"],
    )
    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"detail": "Refresh token is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except TokenError:
            return Response({"detail": "Token is invalid or already blacklisted."}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"detail": "Logged out successfully."}, status=status.HTTP_200_OK)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get current user identity",
        description="Returns id, email, phone, full_name, and role of the authenticated user.",
        request=None,
        responses={
            200: OpenApiResponse(description="Current user identity."),
            401: OpenApiResponse(description="Not authenticated."),
        },
        examples=[
            OpenApiExample(
                "Identity response",
                value={
                    "id": "b04f4f06-82b3-4b1c-8401-eef8aa0d5abf",
                    "email": "amaka.obi@example.com",
                    "phone": "+2348012345678",
                    "full_name": "Amaka Obi",
                    "role": "worker",
                },
                response_only=True,
            ),
        ],
        tags=["Auth"],
    )
    def get(self, request):
        user = request.user
        return Response(
            {
                "id": user.id,
                "email": user.email,
                "phone": user.phone,
                "full_name": user.full_name,
                "role": user.role,
            },
            status=status.HTTP_200_OK,
        )


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    UPDATABLE_FIELDS = [
        "full_name", "phone", "gender", "date_of_birth",
        "address", "location_area", "location_city",
        "location_lat", "location_lng", "skills", "languages",
        "has_vehicle", "vehicle_type", "availability",
        "trade_category", "market_name", "weekly_income_range", "business_name",
    ]

    @extend_schema(
        summary="Get full user profile",
        description="Returns the complete profile of the authenticated user.",
        request=None,
        responses={
            200: OpenApiResponse(response=UserSerializer, description="Full user profile."),
            401: OpenApiResponse(description="Not authenticated."),
        },
        tags=["Profile"],
    )
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    @extend_schema(
        summary="Update user profile",
        description=(
            "Partially updates the authenticated user's profile. Only whitelisted fields "
            "accepted. Email is intentionally excluded — changing login email is a separate "
            "verified flow."
        ),
        request=UserSerializer,
        responses={
            200: OpenApiResponse(response=UserSerializer, description="Updated user profile."),
            400: OpenApiResponse(description="Non-updatable or invalid field supplied."),
            401: OpenApiResponse(description="Not authenticated."),
        },
        tags=["Profile"],
    )
    def patch(self, request):
        user = request.user
        data = request.data

        unknown_keys = set(data.keys()) - set(self.UPDATABLE_FIELDS)
        if unknown_keys:
            return Response(
                {"detail": f"Field(s) not updatable: {', '.join(unknown_keys)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        changed_fields = []
        for field in self.UPDATABLE_FIELDS:
            if field in data:
                setattr(user, field, data[field])
                changed_fields.append(field)

        if changed_fields:
            user.save(update_fields=changed_fields)

        return Response(UserSerializer(user).data)


class ChangePinView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Change PIN",
        description="Changes the authenticated user's PIN. Requires current PIN for verification.",
        request=ChangePinSerializer,
        responses={
            200: OpenApiResponse(description="PIN changed successfully."),
            400: OpenApiResponse(description="Current PIN incorrect or validation failed."),
            401: OpenApiResponse(description="Not authenticated."),
        },
        examples=[
            OpenApiExample(
                "Change PIN",
                value={"current_pin": "1234", "new_pin": "5678"},
                request_only=True,
            ),
        ],
        tags=["Auth"],
    )
    def post(self, request):
        serializer = ChangePinSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        data = serializer.validated_data

        if not user.check_pin(data["current_pin"]):
            return Response({"detail": "Current PIN is incorrect."}, status=status.HTTP_400_BAD_REQUEST)

        user.set_pin(data["new_pin"])
        user.save(update_fields=["pin"])
        logger.info("PIN changed for user: %s", user.email)
        return Response({"detail": "PIN changed successfully."}, status=status.HTTP_200_OK)


class ResetPinRequestView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Request PIN reset OTP",
        description=(
            "Sends a 6-digit OTP via email. Always returns 200 to prevent user enumeration. "
            "OTP expires in 10 minutes."
        ),
        request=ResetPinRequestSerializer,
        responses={
            200: OpenApiResponse(description="OTP sent if email is registered."),
            400: OpenApiResponse(description="Validation error."),
            503: OpenApiResponse(description="Email delivery failed."),
        },
        examples=[
            OpenApiExample("Request OTP", value={"email": "amaka.obi@example.com"}, request_only=True),
        ],
        tags=["PIN Reset"],
    )
    def post(self, request):
        serializer = ResetPinRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        email = serializer.validated_data["email"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"detail": "If that email is registered, an OTP has been sent."}, status=status.HTTP_200_OK)

        otp_record = PinResetOTP.create_for_user(user)

        try:
            _send_otp_email(email, otp_record.otp)
        except Exception:
            logger.exception("Failed to send OTP email to %s", email)
            return Response({"detail": "Could not send OTP. Please try again later."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        return Response({"detail": "If that email is registered, an OTP has been sent."}, status=status.HTTP_200_OK)


class ResetPinConfirmView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Confirm PIN reset with OTP",
        description="Verifies OTP and sets new PIN. OTP must be unused and within 10-minute window.",
        request=ResetPinConfirmSerializer,
        responses={
            200: OpenApiResponse(description="PIN reset successfully."),
            400: OpenApiResponse(description="Invalid or expired OTP."),
        },
        examples=[
            OpenApiExample(
                "Confirm reset",
                value={"email": "amaka.obi@example.com", "otp": "482910", "new_pin": "5678"},
                request_only=True,
            ),
        ],
        tags=["PIN Reset"],
    )
    def post(self, request):
        serializer = ResetPinConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        email = data["email"]
        otp_value = data["otp"]
        new_pin = data["new_pin"]

        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            return Response({"detail": "Invalid or expired OTP."}, status=status.HTTP_400_BAD_REQUEST)

        otp_record = (
            PinResetOTP.objects
            .filter(user=user, is_used=False)
            .order_by("-created_at")
            .first()
        )

        if not otp_record or not otp_record.is_valid or otp_record.otp != otp_value:
            return Response({"detail": "Invalid or expired OTP."}, status=status.HTTP_400_BAD_REQUEST)

        otp_record.is_used = True
        otp_record.save(update_fields=["is_used"])

        user.set_pin(new_pin)
        user.save(update_fields=["pin"])

        logger.info("PIN reset completed for user: %s", email)
        return Response({"detail": "PIN reset successfully."}, status=status.HTTP_200_OK)