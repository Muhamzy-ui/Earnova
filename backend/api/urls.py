from django.urls import path
from . import views

urlpatterns = [
    # User endpoints
    path('users/<str:uid>/', views.user_profile_detail, name='user-detail'),
    path('users/by-referral/check/', views.check_referral_code, name='check-referral'),
    
    # Transactions
    path('users/<str:uid>/transactions/', views.user_transactions, name='user-transactions'),
    path('users/<str:uid>/transactions/<str:doc_id>/', views.transaction_by_id, name='transaction-by-id'),
    
    # Server-side logic
    path('referral/process/', views.process_referral, name='process-referral'),
    
    # Withdrawals
    path('withdrawals/', views.create_withdrawal, name='create-withdrawal'),
    path('withdrawals/<str:doc_id>/', views.withdrawal_by_id, name='withdrawal-by-id'),
    path('withdrawals/recent/', views.recent_withdrawals, name='recent-withdrawals'),
    
    # Platform Settings
    path('settings/<str:doc_id>/', views.platform_settings_detail, name='settings-detail'),

    # Admin
    path('admin/verify/', views.verify_admin, name='verify-admin'),
    path('admin/stats/', views.admin_stats, name='admin-stats'),
    path('admin/users/', views.admin_users, name='admin-users'),
    path('admin/withdrawals/', views.admin_withdrawals, name='admin-withdrawals'),
    path('admin/withdrawals/<str:doc_id>/', views.admin_handle_withdrawal, name='admin-handle-withdrawal'),
    path('admin/backfill-referrals/', views.backfill_referrals, name='backfill_referrals'),
]
