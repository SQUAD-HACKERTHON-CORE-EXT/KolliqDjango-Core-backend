from django.contrib import admin
from django.urls import path, include
from django.http import JsonResponse
from django.conf.urls.static import static
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from apps.payments.admin import revenue_dashboard_view


def health_check(request):
    return JsonResponse({
        'success': True,
        'data': {'status': 'ok', 'service': 'kolliq-django'},
        'error': None
    })


urlpatterns = [
    path('admin/', admin.site.urls),
    path('admin/revenue/', revenue_dashboard_view, name='revenue-dashboard'),
    path('api/health/', health_check, name='health-check'),
    
    # API Documentation
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    
    # API Endpoints
    path('api/', include('apps.users.urls')),
    path('api/wallets/', include('apps.wallets.urls')),
    path('api/jobs/', include('apps.jobs.urls')),
    path('api/payments/', include('apps.payments.urls')),
    path('api/financial/', include('apps.financial_services.urls')),
    path('api/partner/', include('apps.partner.urls')),
    path('api/marketplace/', include('apps.marketplace.urls')),
    path('api/webhooks/', include('apps.payments.webhook_urls')),
]
