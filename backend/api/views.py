"""
API Views for Earnova Backend.
These endpoints act as a drop-in replacement for Firestore operations.
"""
import json
from django.utils import timezone
from django.db import transaction as db_transaction
from django.db.models import Sum, Count, Q
from django.http import JsonResponse
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from .models import UserProfile, Transaction, Withdrawal, AdminProfile
from .serializers import UserProfileSerializer, TransactionSerializer, WithdrawalSerializer
from .firebase_auth import firebase_auth_required, firebase_admin_required


def parse_firestore_update(data):
    """
    Parses a Firestore-style update object, handling FieldValue specials and dot notation for nested fields.
    Example: {'balance': {'_isFieldValue': True, '_method': 'increment', '_value': 5}}
    Example: {'taskCooldowns.fb1': 123456789}
    """
    parsed = {}
    increments = {}
    deletes = []
    nested_updates = {}

    for key, value in data.items():
        # Handle FieldValue objects
        if isinstance(value, dict) and value.get('_isFieldValue'):
            method = value.get('_method')
            if method == 'increment':
                increments[key] = value.get('_value', 0)
            elif method == 'delete':
                deletes.append(key)
            elif method == 'serverTimestamp':
                if '.' in key:
                    nested_updates[key] = timezone.now()
                else:
                    parsed[key] = timezone.now()
            continue

        # Handle dot notation for nested fields
        if '.' in key:
            nested_updates[key] = value
        else:
            parsed[key] = value

    return parsed, increments, deletes, nested_updates


# ==========================================
# USER PROFILE ENDPOINTS (Mirror Users Collection)
# ==========================================

@api_view(['GET', 'POST', 'PATCH'])
@firebase_auth_required
def user_profile_detail(request, uid):
    """
    Handle GET (get doc), POST (set doc), PATCH (update doc) for a user.
    """
    # Ensure a user only accesses their own profile (or is admin)
    request_uid = request.firebase_user.get('uid')
    if request_uid != uid and not getattr(request, 'is_admin', False):
        return Response({'error': 'Unauthorized access to user profile'}, status=403)

    try:
        user_profile = UserProfile.objects.get(uid=uid)
    except UserProfile.DoesNotExist:
        user_profile = None  # Handle below — auto-create if GET, or allow POST/PATCH to create

    if request.method == 'GET':
        if not user_profile:
            # User exists in Firebase Auth but not in DB yet (e.g. partial migration)
            # Auto-create a minimal profile so they can still log in
            firebase_uid = request.firebase_user.get('uid')
            firebase_email = request.firebase_user.get('email', '')
            firebase_name = request.firebase_user.get('name', '')
            name_parts = firebase_name.split(' ', 1) if firebase_name else ['', '']
            import uuid, random, string
            referral_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
            user_profile = UserProfile.objects.create(
                uid=uid,
                email=firebase_email,
                firstname=name_parts[0] if name_parts else '',
                lastname=name_parts[1] if len(name_parts) > 1 else '',
                username=firebase_email.split('@')[0] if firebase_email else uid[:8],
                balance=10.00,
                total_earned=10.00,
                referral_code=referral_code,
                welcome_bonus_given=True,
                status='active',
                role='user',
            )
        serializer = UserProfileSerializer(user_profile)
        return Response(serializer.data)

    elif request.method == 'POST':
        # "set" document (create or overwrite)
        parsed_data, increments, deletes, nested_updates = parse_firestore_update(request.data)
        if 'uid' not in parsed_data:
            parsed_data['uid'] = uid
            
        if user_profile:
            # Overwrite existing
            serializer = UserProfileSerializer(user_profile, data=parsed_data)
        else:
            # Create new
            serializer = UserProfileSerializer(data=parsed_data)
            
        if serializer.is_valid():
            user_profile = serializer.save()
            
            # Handle increments/nested updates if any (rare for set, but possible)
            if increments or nested_updates or deletes:
                user_profile = apply_nested_updates(user_profile, increments, deletes, nested_updates)
                
            return Response(serializer.data, status=status.HTTP_201_CREATED if not user_profile else status.HTTP_200_OK)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == 'PATCH':
        # "update" document
        if not user_profile:
            return Response({'error': 'Document does not exist to update'}, status=404)

        parsed_data, increments, deletes, nested_updates = parse_firestore_update(request.data)

        # Handle direct field updates
        if parsed_data:
            serializer = UserProfileSerializer(user_profile, data=parsed_data, partial=True)
            if serializer.is_valid():
                user_profile = serializer.save()
            else:
                return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        # Handle increments, deletes, and nested updates
        if increments or deletes or nested_updates:
            user_profile = apply_nested_updates(user_profile, increments, deletes, nested_updates)

        return Response(UserProfileSerializer(user_profile).data)

def apply_nested_updates(user_profile, increments, deletes, nested_updates):
    """Helper to apply increments and dot-notation nested updates to a UserProfile."""
    # Handle increments
    for field, val in increments.items():
        if '.' in field:
            # We don't support increments on nested fields yet, but we could
            pass
        elif hasattr(user_profile, field):
            current_val = getattr(user_profile, field)
            try:
                setattr(user_profile, field, current_val + type(current_val)(val))
            except: pass

    # Handle nested updates (e.g. taskCooldowns.fb1 = 123)
    for path, value in nested_updates.items():
        parts = path.split('.')
        if len(parts) == 2:
            base_field, sub_key = parts
            if hasattr(user_profile, base_field):
                current_obj = getattr(user_profile, base_field)
                if isinstance(current_obj, dict):
                    current_obj[sub_key] = value
                    setattr(user_profile, base_field, current_obj)

    # Handle deletes
    for path in deletes:
        parts = path.split('.')
        if len(parts) == 2:
            base_field, sub_key = parts
            if hasattr(user_profile, base_field):
                current_obj = getattr(user_profile, base_field)
                if isinstance(current_obj, dict) and sub_key in current_obj:
                    del current_obj[sub_key]
                    setattr(user_profile, base_field, current_obj)
        elif hasattr(user_profile, path):
            setattr(user_profile, path, None)

    user_profile.save()
    return user_profile


@api_view(['GET'])
@firebase_auth_required
def check_referral_code(request):
    """
    Used during signup to find a user by their referral code.
    Replaces: db.collection('users').where('referralCode', '==', code).get()
    """
    code = request.GET.get('code')
    if not code:
        return Response({'error': 'No referral code provided'}, status=400)

    try:
        user = UserProfile.objects.get(referral_code=code)
        return Response({'uid': user.uid, 'username': user.username})
    except UserProfile.DoesNotExist:
        return Response({'error': 'Invalid referral code'}, status=404)


# ==========================================
# TRANSACTIONS (Mirror users/{uid}/transactions)
# ==========================================

@api_view(['GET', 'POST'])
@firebase_auth_required
def user_transactions(request, uid):
    """
    List user's transactions or add a new one.
    """
    # Ensure authorized
    request_uid = request.firebase_user.get('uid')
    if request_uid != uid and not getattr(request, 'is_admin', False):
        return Response({'error': 'Unauthorized'}, status=403)

    try:
        user_profile = UserProfile.objects.get(uid=uid)
    except UserProfile.DoesNotExist:
        return Response({'error': 'User not found'}, status=404)

    if request.method == 'GET':
        limit = int(request.GET.get('limit', 50))
        txns = Transaction.objects.filter(user=user_profile).order_by('-timestamp')[:limit]
        serializer = TransactionSerializer(txns, many=True)
        return Response(serializer.data)

    elif request.method == 'POST':
        data = request.data
        doc_id = data.get('id') or data.get('doc_id')
        if not doc_id:
            import uuid
            doc_id = f"TXN{uuid.uuid4().hex[:12].upper()}"
            
        data['doc_id'] = doc_id
        
        serializer = TransactionSerializer(data=data)
        if serializer.is_valid():
            serializer.save(user=user_profile)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# ==========================================
# WITHDRAWALS (Mirror withdrawals collection)
# ==========================================

@api_view(['POST'])
@firebase_auth_required
def create_withdrawal(request):
    """
    Adds to global withdrawals collection.
    """
    data = request.data
    uid = request.firebase_user.get('uid')
    
    try:
        user = UserProfile.objects.get(uid=uid)
    except UserProfile.DoesNotExist:
        return Response({'error': 'User not found'}, status=404)

    doc_id = data.get('id', data.get('doc_id'))
    if not doc_id:
        import uuid
        doc_id = f"WD{uuid.uuid4().hex[:12].upper()}"

    withdrawal = Withdrawal.objects.create(
        doc_id=doc_id,
        user=user,
        username=data.get('username', user.username),
        user_email=data.get('userEmail', user.email),
        amount=data.get('amount', 0),
        type=data.get('type', ''),
        status=data.get('status', 'pending'),
        bank=data.get('bank'),
        account_number=data.get('accountNumber'),
        account_name=data.get('accountName'),
        ngn_amount=data.get('ngnAmount'),
        charge=data.get('charge'),
        wallet_address=data.get('walletAddress'),
        network_fee=data.get('networkFee')
    )
    
    serializer = WithdrawalSerializer(withdrawal)
    return Response(serializer.data, status=status.HTTP_201_CREATED)


@api_view(['GET'])
@permission_classes([AllowAny])
def recent_withdrawals(request):
    """
    Used by the live ticker to show recent withdrawals (public).
    """
    limit = int(request.GET.get('limit', 10))
    # For privacy, we could mask names here, but current system uses real/fake mix
    withdrawals = Withdrawal.objects.filter(status='completed').order_by('-timestamp')[:limit]
    serializer = WithdrawalSerializer(withdrawals, many=True)
    return Response(serializer.data)


# ==========================================
# SERVER-SIDE LOGIC (ATOMIC OPERATIONS)
# ==========================================

@api_view(['POST'])
@firebase_auth_required
@db_transaction.atomic
def process_referral(request):
    """
    Atomically process a referral bonus.
    This prevents race conditions and client-side cheating.
    """
    data = request.data
    referred_uid = data.get('referredUid')
    referrer_code = data.get('referrerCode')
    bonus_amount = 4.00

    if not referred_uid or not referrer_code:
        return Response({'error': 'Missing parameters'}, status=400)

    try:
        # Lock referrer row for update
        referrer = UserProfile.objects.select_for_update().get(referral_code=referrer_code)
        
        # Check if this referral was already processed (idempotency)
        txn_exists = Transaction.objects.filter(
            user=referrer, 
            type='referral', 
            referred_user_id=referred_uid
        ).exists()
        
        if txn_exists:
            return Response({'status': 'already_processed'})

        # Update referrer stats
        referrer.balance += type(referrer.balance)(bonus_amount)
        referrer.total_earned += type(referrer.total_earned)(bonus_amount)
        referrer.ref_count += 1
        referrer.ref_earnings += type(referrer.ref_earnings)(bonus_amount)
        referrer.save()

        # Create transaction
        import uuid
        Transaction.objects.create(
            doc_id=f"REF{uuid.uuid4().hex[:10].upper()}",
            user=referrer,
            type='referral',
            title='Referral Bonus',
            amount=bonus_amount,
            description=f'Bonus for referring a new user',
            referred_user_id=referred_uid
        )

        return Response({'status': 'success', 'new_balance': referrer.balance})

    except UserProfile.DoesNotExist:
        return Response({'error': 'Referrer not found'}, status=404)


# ==========================================
# ADMIN ENDPOINTS
# ==========================================

@api_view(['GET'])
@firebase_admin_required
def admin_stats(request):
    """Get total platform statistics for the admin dashboard."""
    total_users = UserProfile.objects.count()
    active_users = UserProfile.objects.filter(status='active').count()
    
    total_balance = UserProfile.objects.aggregate(Sum('balance'))['balance__sum'] or 0
    total_withdrawals = Withdrawal.objects.filter(status='completed').aggregate(Sum('amount'))['amount__sum'] or 0
    pending_withdrawals = Withdrawal.objects.filter(status='pending').count()
    
    return Response({
        'totalUsers': total_users,
        'activeUsers': active_users,
        'totalBalance': total_balance,
        'totalWithdrawals': total_withdrawals,
        'pendingWithdrawalsCount': pending_withdrawals
    })

@api_view(['GET'])
@firebase_admin_required
def admin_users(request):
    """List users for admin panel."""
    users = UserProfile.objects.all().order_by('-created_at')[:100]
    serializer = UserProfileSerializer(users, many=True)
    return Response(serializer.data)

@api_view(['GET'])
@firebase_admin_required
def admin_withdrawals(request):
    """List withdrawals for admin panel."""
    status_filter = request.GET.get('status')
    qs = Withdrawal.objects.all().order_by('-timestamp')
    if status_filter:
        qs = qs.filter(status=status_filter)
        
    withdrawals = qs[:100]
    serializer = WithdrawalSerializer(withdrawals, many=True)
    return Response(serializer.data)

@api_view(['PATCH'])
@firebase_admin_required
@db_transaction.atomic
def admin_handle_withdrawal(request, doc_id):
    """Approve or reject a withdrawal."""
    action = request.data.get('action') # 'approve' or 'reject'
    
    try:
        withdrawal = Withdrawal.objects.select_for_update().get(doc_id=doc_id)
        if withdrawal.status != 'pending':
            return Response({'error': 'Withdrawal is not pending'}, status=400)
            
        if action == 'approve':
            withdrawal.status = 'completed'
            withdrawal.save()
            
            # Update user's transaction if it exists
            if withdrawal.user:
                Transaction.objects.filter(
                    user=withdrawal.user, 
                    type='withdrawal', 
                    status='pending',
                    amount=-withdrawal.amount
                ).update(status='completed')
                
            return Response({'status': 'approved'})
            
        elif action == 'reject':
            withdrawal.status = 'failed'
            withdrawal.save()
            
            # Refund user
            if withdrawal.user:
                user = UserProfile.objects.select_for_update().get(uid=withdrawal.user.uid)
                user.balance += withdrawal.amount
                user.save()
                
                # Update transaction
                Transaction.objects.filter(
                    user=withdrawal.user, 
                    type='withdrawal', 
                    status='pending',
                    amount=-withdrawal.amount
                ).update(status='failed')
                
            return Response({'status': 'rejected', 'refunded': bool(withdrawal.user)})
            
    except Withdrawal.DoesNotExist:
        return Response({'error': 'Withdrawal not found'}, status=404)

@api_view(['POST'])
@firebase_auth_required
def verify_admin(request):
    """Used by signin.html to check if a logging-in user is an admin."""
    email = request.firebase_user.get('email')
    
    try:
        admin = AdminProfile.objects.get(email=email, is_active=True)
        return Response({
            'is_admin': True,
            'role': admin.role,
            'firstname': admin.firstname,
            'lastname': admin.lastname
        })
    except AdminProfile.DoesNotExist:
        return Response({'is_admin': False})
