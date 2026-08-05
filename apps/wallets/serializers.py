from rest_framework import serializers
from decimal import Decimal
from .models import Wallet, WithdrawalRequest
from services.paystack import NIGERIAN_BANKS, get_bank_name


def get_bank_by_code(code):
    return next((b for b in NIGERIAN_BANKS if b['code'] == code), None)


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = [
            'id',
            'paystack_account_number',
            'paystack_account_name',
            'paystack_bank_name',
            'balance',
            'escrow_balance',
            'savings_balance',
            'paystack_creation_status',
            'created_at',
        ]
        read_only_fields = fields


class WalletPublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = [
            'paystack_account_number',
            'paystack_account_name',
            'paystack_bank_name',
            'paystack_creation_status',
        ]
        read_only_fields = fields


class NigerianBankSerializer(serializers.Serializer):
    code = serializers.CharField()
    name = serializers.CharField()


class BankAccountVerifySerializer(serializers.Serializer):
    bank_code      = serializers.CharField(max_length=10)
    account_number = serializers.CharField(min_length=10, max_length=10)

    def validate_bank_code(self, value):
        bank = get_bank_by_code(value)
        if not bank:
            raise serializers.ValidationError(
                f"Invalid bank code '{value}'. Use GET /wallets/banks/ for valid codes."
            )
        return value

    def validate_account_number(self, value):
        if not value.isdigit():
            raise serializers.ValidationError('Account number must contain digits only.')
        return value


class BankAccountSaveSerializer(serializers.Serializer):
    bank_code         = serializers.CharField(max_length=10)
    account_number    = serializers.CharField(min_length=10, max_length=10)
    bank_account_name = serializers.CharField(max_length=150)
    confirm           = serializers.BooleanField()

    def validate_bank_code(self, value):
        bank = get_bank_by_code(value)
        if not bank:
            raise serializers.ValidationError(f"Invalid bank code '{value}'.")
        return value

    def validate_account_number(self, value):
        if not value.isdigit():
            raise serializers.ValidationError('Account number must contain digits only.')
        return value

    def validate_confirm(self, value):
        if not value:
            raise serializers.ValidationError(
                'You must confirm the account details before saving.'
            )
        return value


class BankAccountDetailSerializer(serializers.Serializer):
    bank_account_number    = serializers.CharField()
    bank_code              = serializers.CharField()
    bank_name              = serializers.CharField()
    bank_account_name      = serializers.CharField()
    bank_account_verified  = serializers.BooleanField()
    bank_account_updated_at = serializers.DateTimeField()


class WithdrawalRequestSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2)

    def validate_amount(self, value):
        if value < Decimal('2500.00'):
            raise serializers.ValidationError(
                'Minimum withdrawal amount is ₦2,500.'
            )
        return value


class WithdrawalDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = [
            'id', 'amount', 'bank_account_number', 'bank_code',
            'bank_name', 'bank_account_name', 'status',
            'rejection_reason', 'created_at', 'updated_at',
        ]
        read_only_fields = fields