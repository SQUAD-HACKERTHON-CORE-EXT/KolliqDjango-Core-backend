from django.urls import path
from .views import TransactionListView
 
urlpatterns = [
    path('transactions/', TransactionListView.as_view(), name='transaction-list'),
    # Webhook routes are in webhook_urls.py, mounted at /webhooks/paystack/
]
