"""
Earnova Database Models
Mirrors the Firebase Firestore collections:
  - users/{uid}                    → UserProfile
  - users/{uid}/transactions       → Transaction
  - withdrawals                    → Withdrawal
  - admins                         → AdminProfile
"""
from django.db import models
from django.utils import timezone
import json


class UserProfile(models.Model):
    """
    Maps to Firestore: users/{uid}
    Stores all user data including balance, referrals, and task progress.
    """
    uid = models.CharField(max_length=128, unique=True, primary_key=True, db_index=True)
    firstname = models.CharField(max_length=100, blank=True, default='')
    lastname = models.CharField(max_length=100, blank=True, default='')
    username = models.CharField(max_length=100, blank=True, default='')
    email = models.EmailField(blank=True, default='')
    phone = models.CharField(max_length=30, blank=True, default='')
    region = models.CharField(max_length=50, blank=True, default='')

    # Financial
    balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_earned = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Tasks
    tasks_completed = models.IntegerField(default=0)
    task_cooldowns = models.JSONField(default=dict, blank=True)

    # Referrals
    ref_count = models.IntegerField(default=0)
    ref_earnings = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    referral_code = models.CharField(max_length=30, unique=True, blank=True, null=True, db_index=True)
    referred_by = models.CharField(max_length=30, blank=True, null=True)

    # Bonuses
    welcome_bonus_given = models.BooleanField(default=False)
    flash_bonus_last_claimed = models.DateTimeField(null=True, blank=True)

    # Account
    promo_code = models.CharField(max_length=50, blank=True, null=True)
    status = models.CharField(max_length=20, default='active')
    role = models.CharField(max_length=20, default='user')
    created_at = models.DateTimeField(default=timezone.now)
    last_login = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'users'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.username} ({self.uid[:8]}...)"

    def to_firestore_format(self):
        """
        Returns data in the same format the frontend expects
        (matching the old Firestore document structure).
        """
        return {
            'uid': self.uid,
            'firstname': self.firstname,
            'lastname': self.lastname,
            'username': self.username,
            'email': self.email,
            'phone': self.phone,
            'region': self.region,
            'balance': float(self.balance),
            'totalEarned': float(self.total_earned),
            'tasksCompleted': self.tasks_completed,
            'taskCooldowns': self.task_cooldowns or {},
            'refCount': self.ref_count,
            'refEarnings': float(self.ref_earnings),
            'referralCode': self.referral_code,
            'referredBy': self.referred_by,
            'welcomeBonusGiven': self.welcome_bonus_given,
            'flashBonusLastClaimed': self.flash_bonus_last_claimed.isoformat() if self.flash_bonus_last_claimed else None,
            'promoCode': self.promo_code,
            'status': self.status,
            'role': self.role,
            'createdAt': self.created_at.isoformat() if self.created_at else None,
            'lastLogin': self.last_login.isoformat() if self.last_login else None,
        }


class Transaction(models.Model):
    """
    Maps to Firestore: users/{uid}/transactions
    Stores all financial transactions for each user.
    """
    TYPE_CHOICES = [
        ('earning', 'Earning'),
        ('bonus', 'Bonus'),
        ('referral', 'Referral'),
        ('withdrawal', 'Withdrawal'),
    ]
    STATUS_CHOICES = [
        ('completed', 'Completed'),
        ('pending', 'Pending'),
        ('failed', 'Failed'),
    ]

    id = models.AutoField(primary_key=True)
    doc_id = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='transactions')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    title = models.CharField(max_length=200, blank=True, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='completed')
    date = models.CharField(max_length=50, blank=True, default='')
    description = models.TextField(blank=True, default='')
    referred_user_id = models.CharField(max_length=128, blank=True, null=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    # Extra withdrawal fields
    bank = models.CharField(max_length=100, blank=True, null=True)
    account_number = models.CharField(max_length=30, blank=True, null=True)
    account_name = models.CharField(max_length=100, blank=True, null=True)
    wallet_address = models.CharField(max_length=200, blank=True, null=True)
    network_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    ngn_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    charge = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    deduction_pending = models.BooleanField(default=False)

    class Meta:
        db_table = 'transactions'
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.type}: {self.title} ({self.amount})"

    def to_firestore_format(self):
        """Returns data in the format the frontend expects."""
        data = {
            'id': self.doc_id,
            'type': self.type,
            'title': self.title,
            'amount': float(self.amount),
            'status': self.status,
            'date': self.date,
            'description': self.description,
            'timestamp': self.timestamp.isoformat(),
        }
        if self.referred_user_id:
            data['referredUserId'] = self.referred_user_id
        if self.bank:
            data['bank'] = self.bank
        if self.account_number:
            data['accountNumber'] = self.account_number
        if self.account_name:
            data['accountName'] = self.account_name
        if self.wallet_address:
            data['walletAddress'] = self.wallet_address
        if self.network_fee is not None:
            data['networkFee'] = float(self.network_fee)
        if self.ngn_amount is not None:
            data['ngnAmount'] = float(self.ngn_amount)
        if self.charge is not None:
            data['charge'] = float(self.charge)
        if self.deduction_pending:
            data['deductionPending'] = True
        return data


class Withdrawal(models.Model):
    """
    Maps to Firestore: withdrawals (global collection)
    Used for the live ticker and admin panel.
    """
    doc_id = models.CharField(max_length=64, unique=True, db_index=True)
    user = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name='withdrawals', null=True, blank=True)
    username = models.CharField(max_length=100, blank=True, default='')
    user_email = models.EmailField(blank=True, default='')
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    type = models.CharField(max_length=50, blank=True, default='')
    status = models.CharField(max_length=20, default='pending')

    # Bank details
    bank = models.CharField(max_length=100, blank=True, null=True)
    account_number = models.CharField(max_length=30, blank=True, null=True)
    account_name = models.CharField(max_length=100, blank=True, null=True)
    ngn_amount = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    charge = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)

    # Crypto details
    wallet_address = models.CharField(max_length=200, blank=True, null=True)
    network_fee = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = 'withdrawals'
        ordering = ['-timestamp']

    def __str__(self):
        return f"Withdrawal: {self.username} - ${self.amount}"

    def to_firestore_format(self):
        """Returns data in format suitable for frontend/ticker."""
        return {
            'id': self.doc_id,
            'userId': self.user.uid if self.user else None,
            'userEmail': self.user_email,
            'amount': float(self.amount),
            'type': self.type,
            'bank': self.bank,
            'accountNumber': self.account_number,
            'accountName': self.account_name,
            'ngnAmount': float(self.ngn_amount) if self.ngn_amount else None,
            'charge': float(self.charge) if self.charge else None,
            'walletAddress': self.wallet_address,
            'networkFee': float(self.network_fee) if self.network_fee else None,
            'status': self.status,
            'timestamp': {
                'toDate': self.timestamp.isoformat(),
                '_seconds': int(self.timestamp.timestamp()),
            },
        }


class AdminProfile(models.Model):
    """
    Maps to Firestore: admins/{doc_id}
    Stores admin account data.
    """
    uid = models.CharField(max_length=128, blank=True, default='')
    email = models.EmailField(unique=True)
    firstname = models.CharField(max_length=100, blank=True, default='Admin')
    lastname = models.CharField(max_length=100, blank=True, default='')
    role = models.CharField(max_length=20, default='admin')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'admins'

    def __str__(self):
        return f"Admin: {self.email}"


class PlatformSettings(models.Model):
    """
    Maps to Firestore: settings/{doc_id}
    Stores admin-configurable platform settings (bank, crypto, general, etc.)
    """
    doc_id = models.CharField(max_length=50, unique=True, primary_key=True)
    data = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'platform_settings'

    def __str__(self):
        return f"Settings: {self.doc_id}"
