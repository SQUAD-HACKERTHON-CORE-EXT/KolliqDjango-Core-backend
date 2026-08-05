from django.urls import path
from .views import (
    WalletDetailView,
    NigerianBankListView,
    BankAccountDetailView,
    BankAccountVerifyView,
    BankAccountSaveView,
    WithdrawalRequestView,
    FundWalletView,
)
 
urlpatterns = [
    # Wallet info
    path('me/',                   WalletDetailView.as_view(),       name='wallet-detail'),
 
    # Live bank list (fetched from Paystack, cached 24h)
    path('banks/',                NigerianBankListView.as_view(),   name='bank-list'),
 
    # Bank account management
    path('bank-account/',         BankAccountDetailView.as_view(),  name='bank-account-detail'),
    path('bank-account/verify/',  BankAccountVerifyView.as_view(),  name='bank-account-verify'),
    path('bank-account/save/',    BankAccountSaveView.as_view(),    name='bank-account-save'),
 
    # Withdrawals
    path('withdraw/',             WithdrawalRequestView.as_view(),  name='withdrawal'),
    path('fund/',                 FundWalletView.as_view(),  name='fund-wallet'),
]
