from django.contrib import admin
from django.shortcuts import render
from django.contrib.admin.views.decorators import staff_member_required

from .models import PlatformWithdrawalLog
from services.platform_revenue import get_available_platform_revenue, get_revenue_breakdown


@admin.register(PlatformWithdrawalLog)
class PlatformWithdrawalLogAdmin(admin.ModelAdmin):
        """
        After withdrawing via the Paystack Dashboard, log it here so the
        revenue dashboard stays accurate. Just an amount and a note —
        never any bank details.
        """
        list_display = ['amount', 'note', 'logged_by', 'created_at']
        readonly_fields = ['created_at']

        def save_model(self, request, obj, form, change):
            if not obj.logged_by_id:
                obj.logged_by = request.user
            super().save_model(request, obj, form, change)


@staff_member_required
def revenue_dashboard_view(request):
        """
        GET /admin/revenue/
        Read-only. Shows how much Kolliq has earned and how much is still
        sitting in the Paystack balance, available to settle via Dashboard.
        """
        available = get_available_platform_revenue()
        breakdown = get_revenue_breakdown()
        return render(request, 'admin/revenue_dashboard.html', {
            'available': available,
            'breakdown': breakdown,
        })
