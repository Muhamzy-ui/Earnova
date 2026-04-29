"""
Migration script to copy all data from Firebase Firestore to PostgreSQL via Django ORM.
Updated to handle large datasets with pagination and robust error handling.
"""
import os
import sys
import django
from datetime import datetime, timezone as dt_timezone

# Setup Django if run standalone
if not os.environ.get('DJANGO_SETTINGS_MODULE'):
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'earnova_project.settings')
    django.setup()

from django.utils import timezone
from api.models import UserProfile, Transaction, Withdrawal, AdminProfile
from api.firebase_auth import get_firebase_app
from firebase_admin import firestore
import time

def parse_fs_timestamp(ts_data):
    ts = timezone.now()
    if ts_data:
        if hasattr(ts_data, 'timestamp'):
            ts = datetime.fromtimestamp(ts_data.timestamp(), tz=dt_timezone.utc)
        elif isinstance(ts_data, dict) and 'toDate' in ts_data:
            try:
                ts = datetime.fromisoformat(ts_data['toDate'].replace('Z', '+00:00'))
            except: pass
        elif isinstance(ts_data, str):
            try:
                ts = datetime.fromisoformat(ts_data.replace('Z', '+00:00'))
            except: pass
    return ts

def migrate():
    print("Starting migration from Firestore to PostgreSQL...")
    
    app = get_firebase_app()
    if not app:
        print("ERROR: Firebase not initialized. Check FIREBASE_CREDENTIALS_PATH in .env")
        return
        
    db = firestore.client()
    
    # 1. Migrate Admins
    print("\n--- Migrating Admins ---")
    try:
        admins = db.collection('admins').stream()
        admin_count = 0
        for admin_doc in admins:
            data = admin_doc.to_dict()
            email = data.get('email')
            if not email:
                email = f"{admin_doc.id}@admin.com" if not '@' in admin_doc.id else admin_doc.id
                    
            AdminProfile.objects.update_or_create(
                email=email,
                defaults={
                    'uid': data.get('uid', admin_doc.id),
                    'firstname': data.get('firstname', 'Admin'),
                    'lastname': data.get('lastname', ''),
                    'role': data.get('role', 'admin'),
                    'is_active': True
                }
            )
            admin_count += 1
        print(f"Migrated {admin_count} admins.")
    except Exception as e:
        print(f"Error migrating admins: {e}")

    # 2. Migrate Users (with batching)
    print("\n--- Migrating Users ---")
    users_ref = db.collection('users')
    
    user_count = 0
    txn_count = 0
    batch_size = 500
    last_doc = None
    has_more = True
    
    while has_more:
        try:
            query = users_ref.limit(batch_size)
            if last_doc:
                query = query.start_after(last_doc)
                
            docs = list(query.stream())
            if not docs:
                has_more = False
                break
                
            for user_doc in docs:
                uid = user_doc.id
                data = user_doc.to_dict()
                
                # Parse dates
                created_at = timezone.now()
                last_login = timezone.now()
                flash_bonus = None
                
                if 'createdAt' in data and data['createdAt']:
                    created_at = parse_fs_timestamp(data['createdAt'])
                if 'lastLogin' in data and data['lastLogin']:
                    last_login = parse_fs_timestamp(data['lastLogin'])
                if 'flashBonusLastClaimed' in data and data['flashBonusLastClaimed']:
                    flash_bonus = parse_fs_timestamp(data['flashBonusLastClaimed'])
                
                # Create/Update User
                user_profile, created = UserProfile.objects.update_or_create(
                    uid=uid,
                    defaults={
                        'firstname': data.get('firstname', ''),
                        'lastname': data.get('lastname', ''),
                        'username': data.get('username', ''),
                        'email': data.get('email', ''),
                        'phone': data.get('phone', ''),
                        'region': data.get('region', ''),
                        'balance': data.get('balance', 0),
                        'total_earned': data.get('totalEarned', 0),
                        'tasks_completed': data.get('tasksCompleted', 0),
                        'task_cooldowns': data.get('taskCooldowns', {}),
                        'ref_count': data.get('refCount', 0),
                        'ref_earnings': data.get('refEarnings', 0),
                        'referral_code': data.get('referralCode', ''),
                        'referred_by': data.get('referredBy', ''),
                        'welcome_bonus_given': data.get('welcomeBonusGiven', False),
                        'flash_bonus_last_claimed': flash_bonus,
                        'promo_code': data.get('promoCode', ''),
                        'status': data.get('status', 'active'),
                        'role': data.get('role', 'user'),
                        'created_at': created_at,
                        'last_login': last_login,
                    }
                )
                user_count += 1
                
                # Migrate User's Transactions (fetch all at once since usually small per user)
                txns_ref = users_ref.document(uid).collection('transactions')
                txns = txns_ref.stream()
                
                for txn_doc in txns:
                    t_data = txn_doc.to_dict()
                    ts = parse_fs_timestamp(t_data.get('timestamp'))
                    
                    Transaction.objects.update_or_create(
                        doc_id=txn_doc.id,
                        defaults={
                            'user': user_profile,
                            'type': t_data.get('type', 'earning'),
                            'title': t_data.get('title', ''),
                            'amount': t_data.get('amount', 0),
                            'status': t_data.get('status', 'completed'),
                            'date': t_data.get('date', ''),
                            'description': t_data.get('description', ''),
                            'referred_user_id': t_data.get('referredUserId', ''),
                            'timestamp': ts,
                            'bank': t_data.get('bank', ''),
                            'account_number': t_data.get('accountNumber', ''),
                            'account_name': t_data.get('accountName', ''),
                            'wallet_address': t_data.get('walletAddress', ''),
                            'network_fee': t_data.get('networkFee'),
                            'ngn_amount': t_data.get('ngnAmount'),
                            'charge': t_data.get('charge'),
                            'deduction_pending': t_data.get('deductionPending', False),
                        }
                    )
                    txn_count += 1
                    
            last_doc = docs[-1]
            print(f"  Processed {user_count} users, {txn_count} transactions...")
            time.sleep(1) # Prevent rate limiting
            
        except Exception as e:
            print(f"Error in users batch: {e}. Retrying in 5s...")
            time.sleep(5)
            # Will retry the same batch since last_doc hasn't changed
            
    print(f"Migrated {user_count} users and {txn_count} user transactions.")

    # 3. Migrate Global Withdrawals
    print("\n--- Migrating Global Withdrawals ---")
    try:
        wd_ref = db.collection('withdrawals')
        withdrawals = wd_ref.stream()
        wd_count = 0
        
        for wd_doc in withdrawals:
            data = wd_doc.to_dict()
            
            uid = data.get('userId')
            user = None
            if uid:
                try:
                    user = UserProfile.objects.get(uid=uid)
                except UserProfile.DoesNotExist:
                    pass
                    
            ts = parse_fs_timestamp(data.get('timestamp'))
                
            Withdrawal.objects.update_or_create(
                doc_id=wd_doc.id,
                defaults={
                    'user': user,
                    'username': data.get('username', ''),
                    'user_email': data.get('userEmail', ''),
                    'amount': data.get('amount', 0),
                    'type': data.get('type', ''),
                    'status': data.get('status', 'pending'),
                    'bank': data.get('bank', ''),
                    'account_number': data.get('accountNumber', ''),
                    'account_name': data.get('accountName', ''),
                    'ngn_amount': data.get('ngnAmount'),
                    'charge': data.get('charge'),
                    'wallet_address': data.get('walletAddress', ''),
                    'network_fee': data.get('networkFee'),
                    'timestamp': ts,
                }
            )
            wd_count += 1
            
        print(f"Migrated {wd_count} global withdrawals.")
    except Exception as e:
        print(f"Error migrating withdrawals: {e}")
        
    print("\n✅ Migration complete!")

if __name__ == '__main__':
    migrate()
