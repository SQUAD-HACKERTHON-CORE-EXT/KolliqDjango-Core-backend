from django.urls import path
from .webhook_views import PaystackWebhookView
from .views import InternalWebhookView
 
urlpatterns = [
    path('',          PaystackWebhookView.as_view(),  name='paystack-webhook'),
    path('internal/', InternalWebhookView.as_view(),  name='internal-webhook'),
]
