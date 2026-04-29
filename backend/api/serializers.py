"""
Serializers for Earnova API.
These convert Django models to JSON and validate incoming data.
"""
from rest_framework import serializers
from .models import UserProfile, Transaction, Withdrawal, AdminProfile


class UserProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserProfile
        fields = '__all__'
        read_only_fields = ['uid', 'created_at']

    def to_representation(self, instance):
        """Convert to the Firestore format the frontend expects."""
        return instance.to_firestore_format()


class TransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Transaction
        fields = '__all__'
        read_only_fields = ['id', 'timestamp']

    def to_representation(self, instance):
        """Convert to the Firestore format the frontend expects."""
        return instance.to_firestore_format()


class WithdrawalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Withdrawal
        fields = '__all__'
        read_only_fields = ['timestamp']

    def to_representation(self, instance):
        """Convert to format suitable for admin panel and ticker."""
        return {
            'id': self.instance.doc_id if hasattr(self, 'instance') and self.instance else instance.doc_id, # doc_id is the primary identifier for frontend
            'userId': instance.user.uid if instance.user else None,
            'userEmail': instance.user_email,
            'amount': float(instance.amount),
            'type': instance.type,
            'bank': instance.bank,
            'accountNumber': instance.account_number,
            'accountName': instance.account_name,
            'ngnAmount': float(instance.ngn_amount) if instance.ngn_amount else None,
            'charge': float(instance.charge) if instance.charge else None,
            'walletAddress': instance.wallet_address,
            'networkFee': float(instance.network_fee) if instance.network_fee else None,
            'status': instance.status,
            'timestamp': {
                'toDate': instance.timestamp.isoformat(),
                '_seconds': int(instance.timestamp.timestamp()),
            },
        }


class AdminProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdminProfile
        fields = '__all__'
