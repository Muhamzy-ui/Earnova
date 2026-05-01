"""
API Views for Earnova Backend.
These endpoints act as a drop-in replacement for Firestore operations.
"""
import json
from decimal import Decimal
from django.utils import timezone
from django.db import transaction as db_transaction
from django.db.models import Sum, Count, Q
from django.http import JsonResponse
from rest_framework import viewsets, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from .models import UserProfile, Transaction, Withdrawal, AdminProfile, PlatformSettings
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
        else:
            # AUTO-HEAL broken profiles created during bug phase!
            needs_save = False
            if not user_profile.email:
                user_profile.email = request.firebase_user.get('email', '')
                needs_save = True
            if not user_profile.username or user_profile.username == 'USER':
                if user_profile.email:
                    user_profile.username = user_profile.email.split('@')[0]
                else:
                    user_profile.username = uid[:8]
                needs_save = True
            if not user_profile.referral_code:
                import random, string
                user_profile.referral_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
                needs_save = True
            # Only give 10.00 bonus if they are literally at 0/0 and never got it
            if getattr(user_profile, 'balance', 0) == 0 and getattr(user_profile, 'total_earned', 0) == 0:
                user_profile.balance = 10.00
                user_profile.total_earned = 10.00
                user_profile.welcome_bonus_given = True
                needs_save = True
                
            if needs_save:
                user_profile.save()

        serializer = UserProfileSerializer(user_profile)
        return Response(serializer.data)

    elif request.method == 'POST':
        # "set" document (create or overwrite)
        parsed_data, increments, deletes, nested_updates = parse_firestore_update(request.data)
        
        # Manually extract fields to bypass DRF's finicky validation
        uid_val = parsed_data.get('uid', uid)
        
        defaults = {
            'email': parsed_data.get('email', ''),
            'firstname': parsed_data.get('firstname', ''),
            'lastname': parsed_data.get('lastname', ''),
            'username': parsed_data.get('username', ''),
            'phone': parsed_data.get('phone', ''),
            'region': parsed_data.get('region', ''),
            'balance': parsed_data.get('balance', 0.00),
            'total_earned': parsed_data.get('totalEarned', 0.00),
            'tasks_completed': parsed_data.get('tasksCompleted', 0),
            'ref_count': parsed_data.get('refCount', 0),
            'ref_earnings': parsed_data.get('refEarnings', 0.00),
            'referral_code': parsed_data.get('referralCode', None),
            'referred_by': parsed_data.get('referredBy', None),
            'welcome_bonus_given': parsed_data.get('welcomeBonusGiven', False),
            'promo_code': parsed_data.get('promoCode', None),
            'status': parsed_data.get('status', 'active'),
            'role': parsed_data.get('role', 'user'),
        }

        # Apply server timestamps if provided
        if 'createdAt' in parsed_data and isinstance(parsed_data['createdAt'], str):
            defaults['created_at'] = parsed_data['createdAt']
        if 'lastLogin' in parsed_data and isinstance(parsed_data['lastLogin'], str):
            defaults['last_login'] = parsed_data['lastLogin']

        # Handle taskCooldowns mapping
        if 'taskCooldowns' in parsed_data:
            defaults['task_cooldowns'] = parsed_data['taskCooldowns']

        try:
            # Create or update bypassing serializer
            user_profile, created = UserProfile.objects.update_or_create(
                uid=uid_val,
                defaults=defaults
            )
            
            # Apply increments
            if increments or nested_updates or deletes:
                user_profile = apply_nested_updates(user_profile, increments, deletes, nested_updates)
                
            # Securely process referral bonus on the backend if this is a new user
            if created and user_profile.referred_by:
                try:
                    referrer = UserProfile.objects.get(referral_code=user_profile.referred_by)
                    # Don't allow self-referral
                    if referrer.uid != user_profile.uid:
                        referrer.ref_count = getattr(referrer, 'ref_count', 0) + 1
                        referrer.ref_earnings = getattr(referrer, 'ref_earnings', 0) + Decimal('4.00')
                        referrer.balance = getattr(referrer, 'balance', 0) + Decimal('4.00')
                        referrer.total_earned = getattr(referrer, 'total_earned', 0) + Decimal('4.00')
                        referrer.save()
                        
                        # Add transaction to referrer's history
                        Transaction.objects.create(
                            user=referrer,
                            type='referral',
                            title='Referral Bonus',
                            amount=Decimal('4.00'),
                            status='completed',
                            description=f'Referred user: {user_profile.username}',
                            timestamp=timezone.now()
                        )
                except Exception as e:
                    print("Referral processing error:", str(e))
                
            serializer = UserProfileSerializer(user_profile)
            return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
            
        except Exception as e:
            print("Manual save error:", str(e))
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)

    elif request.method == 'PATCH':
        # "update" document
        if not user_profile:
            return Response({'error': 'Document does not exist to update'}, status=404)

        parsed_data, increments, deletes, nested_updates = parse_firestore_update(request.data)
        
        # Translate JS camelCase fields to Django snake_case manually
        if 'refCount' in parsed_data: parsed_data['ref_count'] = parsed_data.pop('refCount')
        if 'refEarnings' in parsed_data: parsed_data['ref_earnings'] = parsed_data.pop('refEarnings')
        if 'totalEarned' in parsed_data: parsed_data['total_earned'] = parsed_data.pop('totalEarned')
        if 'tasksCompleted' in parsed_data: parsed_data['tasks_completed'] = parsed_data.pop('tasksCompleted')
        if 'referralCode' in parsed_data: parsed_data['referral_code'] = parsed_data.pop('referralCode')
        if 'referredBy' in parsed_data: parsed_data['referred_by'] = parsed_data.pop('referredBy')

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
    FIELD_MAPPING = {
        'refCount': 'ref_count',
        'refEarnings': 'ref_earnings',
        'totalEarned': 'total_earned',
        'tasksCompleted': 'tasks_completed',
        'taskCooldowns': 'task_cooldowns',
        'balance': 'balance' # just in case
    }

    # Handle increments
    for field, val in increments.items():
        mapped_field = FIELD_MAPPING.get(field, field)
        if '.' in mapped_field:
            # We don't support increments on nested fields yet, but we could
            pass
        elif hasattr(user_profile, mapped_field):
            current_val = getattr(user_profile, mapped_field)
            try:
                setattr(user_profile, mapped_field, current_val + type(current_val)(val))
            except: pass

    # Handle nested updates (e.g. taskCooldowns.fb1 = 123)
    for path, value in nested_updates.items():
        parts = path.split('.')
        if len(parts) == 2:
            base_field, sub_key = parts
            
            if base_field == 'taskCooldowns':
                base_field = 'task_cooldowns'
                
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
        txns = Transaction.objects.filter(user=user_profile)
        
        tx_type = request.GET.get('type')
        if tx_type:
            txns = txns.filter(type=tx_type)
            
        txns = txns.order_by('-timestamp')[:limit]
        serializer = TransactionSerializer(txns, many=True)
        return Response(serializer.data)

    elif request.method == 'POST':
        data = request.data
        doc_id = data.get('id') or data.get('doc_id')
        if not doc_id:
            import uuid
            doc_id = f"TXN{uuid.uuid4().hex[:12].upper()}"
            
        try:
            # Manually extract to avoid DRF validation choking on Firebase FieldValues
            txn = Transaction.objects.create(
                doc_id=doc_id,
                user=user_profile,
                type=data.get('type', 'bonus'),
                title=data.get('title', ''),
                amount=data.get('amount', 0.00),
                status=data.get('status', 'completed'),
                date=data.get('date', ''),
                description=data.get('description', ''),
                referred_user_id=data.get('referred_user_id') or data.get('referredUserId'),
                bank=data.get('bank'),
                account_number=data.get('account_number') or data.get('accountNumber'),
                account_name=data.get('account_name') or data.get('accountName'),
                wallet_address=data.get('wallet_address') or data.get('walletAddress'),
                network_fee=data.get('network_fee') or data.get('networkFee'),
                ngn_amount=data.get('ngn_amount') or data.get('ngnAmount'),
                charge=data.get('charge'),
            )
            serializer = TransactionSerializer(txn)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        except Exception as e:
            print("Transaction create error:", str(e))
            return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)


# ==========================================
# WITHDRAWALS (Mirror withdrawals collection)
# ==========================================

@api_view(['POST'])
@firebase_auth_required
@db_transaction.atomic
def create_withdrawal(request):
    """
    Checks user balance, deducts it, and adds to global withdrawals collection.
    """
    data = request.data
    uid = request.firebase_user.get('uid')
    
    try:
        user = UserProfile.objects.select_for_update().get(uid=uid)
    except UserProfile.DoesNotExist:
        return Response({'error': 'User not found'}, status=404)

    # Get amount and validate
    try:
        amount = float(data.get('amount', 0))
    except (ValueError, TypeError):
        return Response({'error': 'Invalid amount'}, status=400)

    if amount <= 0:
        return Response({'error': 'Amount must be greater than zero'}, status=400)

    # CHECK BALANCE AND PENDING WITHDRAWALS
    pending_withdrawals_sum = Withdrawal.objects.filter(
        user=user, status='pending'
    ).aggregate(Sum('amount'))['amount__sum'] or 0
    
    available_balance = user.balance - type(user.balance)(pending_withdrawals_sum)

    if amount > available_balance:
        return Response({'error': 'You have a pending on going payment. Insufficient funds, top up your tasks.'}, status=400)

    # DEDUCT BALANCE IMMEDIATELY
    user.balance -= type(user.balance)(amount)
    user.save()

    doc_id = data.get('id', data.get('doc_id'))
    if not doc_id:
        import uuid
        doc_id = f"WD{uuid.uuid4().hex[:12].upper()}"

    # Create withdrawal record
    withdrawal = Withdrawal.objects.create(
        doc_id=doc_id,
        user=user,
        username=data.get('username', user.username),
        user_email=data.get('userEmail', user.email),
        amount=amount,
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
    
    # Create a transaction record for the user's history
    Transaction.objects.create(
        user=user,
        type='withdrawal',
        title=f"Withdrawal ({data.get('type', 'Standard')})",
        amount=-amount,
        status='pending',
        description=f"Withdrawal of ${amount} to {data.get('bank') or data.get('walletAddress')}"
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
        
    limit = int(request.GET.get('limit', 100))
    if limit > 1000: limit = 1000
    withdrawals = qs[:limit]
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
    
    # HARDCODED FAILSAFE: Ensure the main admin can always log in
    if email == 'admin@gmail.com' or email == 'mojisolafolash@gmail.com':
        return Response({
            'is_admin': True,
            'email': email,
            'role': 'Super Admin',
            'firstname': 'Admin',
            'lastname': 'User'
        })
        
    try:
        admin = AdminProfile.objects.get(email=email, is_active=True)
        return Response({
            'is_admin': True,
            'email': email,
            'role': admin.role,
            'firstname': admin.firstname,
            'lastname': admin.lastname
        })
    except AdminProfile.DoesNotExist:
        # Check if the user has an 'admin' role in their UserProfile
        try:
            user = UserProfile.objects.get(email=email, role='admin')
            return Response({
                'is_admin': True,
                'email': email,
                'role': 'Admin',
                'firstname': user.firstname,
                'lastname': user.lastname
            })
        except UserProfile.DoesNotExist:
            return Response({'is_admin': False})


# ==========================================
# PLATFORM SETTINGS (Mirror settings collection)
# ==========================================

@api_view(['GET', 'POST', 'PATCH'])
def platform_settings_detail(request, doc_id):
    """
    GET  /settings/{doc_id}/ - Read a settings document
    POST /settings/{doc_id}/ - Create or overwrite a settings document (admin set)
    PATCH /settings/{doc_id}/ - Merge-update a settings document
    """
    try:
        settings_obj, _ = PlatformSettings.objects.get_or_create(
            doc_id=doc_id,
            defaults={'data': {}}
        )
    except Exception as e:
        # Table may not exist yet (migration pending) — return empty doc gracefully
        print(f"Settings DB error for {doc_id}: {e}")
        if request.method == 'GET':
            return Response({'exists': False, 'data': {}, 'doc_id': doc_id})
        return Response({'error': 'Settings table not ready. Run migrations.'}, status=503)

    if request.method == 'GET':
        return Response({'exists': True, 'data': settings_obj.data, 'doc_id': doc_id})

    elif request.method == 'POST':
        # Full overwrite (set)
        clean_data = {k: v for k, v in request.data.items()
                      if not (isinstance(v, dict) and v.get('_isFieldValue'))}
        settings_obj.data = clean_data
        settings_obj.save()
        return Response({'exists': True, 'data': settings_obj.data, 'doc_id': doc_id})

    elif request.method == 'PATCH':
        # Merge update
        merged = dict(settings_obj.data)
        for k, v in request.data.items():
            if isinstance(v, dict) and v.get('_isFieldValue'):
                continue
            merged[k] = v
        settings_obj.data = merged
        settings_obj.save()
        return Response({'exists': True, 'data': settings_obj.data, 'doc_id': doc_id})



# ==========================================
# WITHDRAWAL SET-BY-ID (for bythr.html and cryptopay.html)
# ==========================================

@api_view(['POST', 'PATCH'])
@firebase_auth_required
@db_transaction.atomic
def withdrawal_by_id(request, doc_id):
    """
    POST/PATCH /withdrawals/{doc_id}/ - Create or merge-update a specific withdrawal record.
    Checks and deducts balance on creation.
    """
    uid = request.firebase_user.get('uid')
    
    # Lock user for balance safety
    try:
        user = UserProfile.objects.select_for_update().get(uid=uid)
    except UserProfile.DoesNotExist:
        return Response({'error': 'User not found'}, status=404)

    data = {k: v for k, v in request.data.items()
            if not (isinstance(v, dict) and v.get('_isFieldValue'))}

    # Get amount for validation
    try:
        amount = float(data.get('amount', 0))
    except (ValueError, TypeError):
        amount = 0

    # Try to get or create
    try:
        withdrawal = Withdrawal.objects.select_for_update().get(doc_id=doc_id)
        created = False
    except Withdrawal.DoesNotExist:
        # CHECK BALANCE AND PENDING WITHDRAWALS ON CREATION
        if amount <= 0:
            return Response({'error': 'Invalid amount'}, status=400)
            
        pending_withdrawals_sum = Withdrawal.objects.filter(
            user=user, status='pending'
        ).aggregate(Sum('amount'))['amount__sum'] or 0
        
        available_balance = user.balance - type(user.balance)(pending_withdrawals_sum)

        if amount > available_balance:
            return Response({'error': 'You have a pending on going payment. Insufficient funds, top up your tasks.'}, status=400)
            
        # DEDUCT BALANCE
        user.balance -= type(user.balance)(amount)
        user.save()

        withdrawal = Withdrawal.objects.create(
            doc_id=doc_id,
            user=user,
            username=data.get('username', user.username),
            user_email=data.get('userEmail', user.email),
            amount=amount,
            type=data.get('type', ''),
            status=data.get('status', 'pending'),
            bank=data.get('bank') or (data.get('userBankDetails') or {}).get('bank'),
            account_number=data.get('accountNumber') or (data.get('userBankDetails') or {}).get('account'),
            account_name=data.get('accountName') or (data.get('userBankDetails') or {}).get('name'),
            ngn_amount=data.get('amountNGN') or data.get('ngnAmount'),
            charge=data.get('transferCharge') or data.get('charge'),
            wallet_address=data.get('walletAddress'),
            network_fee=data.get('networkFee'),
        )
        
        # Create a transaction record for the user's history if new
        Transaction.objects.create(
            user=user,
            type='withdrawal',
            title=f"Withdrawal ({data.get('type', 'Standard')})",
            amount=-amount,
            status='pending',
            description=f"Withdrawal of ${amount} to {withdrawal.bank or withdrawal.wallet_address}"
        )
        created = True

    if not created:
        # Merge update
        if data.get('status'): withdrawal.status = data['status']
        if data.get('amount'): withdrawal.amount = data['amount']
        # Note: We don't deduct balance on merge updates to avoid double deduction
        withdrawal.save()

    from .serializers import WithdrawalSerializer
    return Response(WithdrawalSerializer(withdrawal).data,
                    status=201 if created else 200)


# ==========================================
# TRANSACTION BY ID (for bythr.html merge-update)
# ==========================================

@api_view(['GET', 'PATCH'])
@firebase_auth_required
def transaction_by_id(request, uid, doc_id):
    """
    PATCH /users/{uid}/transactions/{doc_id}/ - Merge-update a specific transaction
    (e.g. mark receiptUploaded=True after payment proof is submitted)
    """
    request_uid = request.firebase_user.get('uid')
    if request_uid != uid and not getattr(request, 'is_admin', False):
        return Response({'error': 'Unauthorized'}, status=403)

    try:
        user = UserProfile.objects.get(uid=uid)
    except UserProfile.DoesNotExist:
        return Response({'error': 'User not found'}, status=404)

    if request.method == 'GET':
        try:
            txn = Transaction.objects.get(user=user, doc_id=doc_id)
            from .serializers import TransactionSerializer
            return Response(TransactionSerializer(txn).data)
        except Transaction.DoesNotExist:
            return Response({'exists': False}, status=404)

    elif request.method == 'PATCH':
        data = {k: v for k, v in request.data.items()
                if not (isinstance(v, dict) and v.get('_isFieldValue'))}

        txn, created = Transaction.objects.get_or_create(
            user=user,
            doc_id=doc_id,
            defaults={
                'type': data.get('type', 'withdrawal'),
                'title': data.get('title', 'Withdrawal'),
                'amount': data.get('amount', 0),
                'status': data.get('status', 'pending'),
                'date': data.get('date', ''),
                'description': data.get('description', ''),
            }
        )

        if not created:
            # Merge update: only update fields that are provided
            allowed = ['status', 'description', 'receipt_uploaded', 'receipt_file']
            if data.get('receiptUploaded') is not None:
                txn.description = (txn.description or '') + ' • Receipt uploaded'
            if data.get('status'): txn.status = data['status']
            txn.save()

        from .serializers import TransactionSerializer
        return Response(TransactionSerializer(txn).data, status=201 if created else 200)
