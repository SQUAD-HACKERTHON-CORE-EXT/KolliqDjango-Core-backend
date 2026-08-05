from django.contrib import admin
from django.utils import timezone
import logging
from .models import Wallet, WithdrawalRequest

logger = logging.getLogger(__name__)


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = [
        'user', 'paystack_account_number', 'balance',
        'escrow_balance', 'paystack_creation_status', 'created_at'
    ]
    list_filter = ['paystack_creation_status']
    search_fields = ['user__phone', 'paystack_account_number', 'user__full_name']
    readonly_fields = ['id', 'created_at', 'updated_at']

    def get_user_phone(self, obj):
        return obj.user.phone
    get_user_phone.short_description = 'User Phone'


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = [
        'id', 'get_user_phone', 'amount', 'bank_name',
        'bank_account_number', 'status', 'created_at',
    ]
    list_filter = ['status', 'created_at']
    search_fields = ['wallet__user__phone', 'bank_account_number', 'bank_account_name']
    readonly_fields = [
        'id', 'wallet', 'amount', 'bank_account_number', 'bank_code',
        'bank_name', 'bank_account_name', 'paystack_reference',
        'reviewed_by', 'reviewed_at', 'created_at', 'updated_at',
    ]
    actions = ['approve_withdrawals', 'reject_withdrawals']

    def get_user_phone(self, obj):
        return obj.wallet.user.phone
    get_user_phone.short_description = 'User Phone'

    @admin.action(description='✅ Approve selected withdrawals (simulated)')
    def approve_withdrawals(self, request, queryset):
        pending = queryset.filter(status=WithdrawalRequest.Status.PENDING)
        if not pending.exists():
            self.message_user(request, 'No pending withdrawals selected.', level='warning')
            return

        success_count = 0

        for withdrawal in pending:
            withdrawal.status = WithdrawalRequest.Status.PROCESSING
            withdrawal.reviewed_by = request.user
            withdrawal.reviewed_at = timezone.now()
            withdrawal.squad_reference = f"SIM-{str(withdrawal.id).replace('-', '')[:16].upper()}"
            withdrawal.save(update_fields=[
                'status', 'reviewed_by', 'reviewed_at', 'paystack_reference', 'updated_at'
            ])
            success_count += 1
            logger.info(
                f"Withdrawal {withdrawal.id} approved (simulated) — "
                f"amount=₦{withdrawal.amount} user={withdrawal.wallet.user.phone}"
            )

        self.message_user(
            request,
            f'{success_count} withdrawal(s) approved and processing.',
            level='success',
        )

    @admin.action(description='❌ Reject selected withdrawals (refund wallet)')
    def reject_withdrawals(self, request, queryset):
        pending = queryset.filter(status=WithdrawalRequest.Status.PENDING)
        if not pending.exists():
            self.message_user(request, 'No pending withdrawals selected.', level='warning')
            return

        for withdrawal in pending:
            withdrawal.wallet.credit(withdrawal.amount)
            withdrawal.status = WithdrawalRequest.Status.REJECTED
            withdrawal.rejection_reason = f'Rejected by {request.user.phone}'
            withdrawal.reviewed_by = request.user
            withdrawal.reviewed_at = timezone.now()
            withdrawal.save(update_fields=[
                'status', 'rejection_reason',
                'reviewed_by', 'reviewed_at', 'updated_at',
            ])

        self.message_user(
            request,
            f'{pending.count()} withdrawal(s) rejected and wallets refunded.',
        )