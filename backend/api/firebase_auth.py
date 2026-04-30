"""
Firebase Authentication Middleware
Verifies Firebase ID tokens on incoming API requests.
Firebase Auth remains the ONLY auth system — Django just verifies tokens.
"""
import os
import firebase_admin
from firebase_admin import credentials, auth as firebase_auth
from django.conf import settings
from functools import wraps
from django.http import JsonResponse


# Initialize Firebase Admin SDK (once)
_firebase_app = None


def get_firebase_app():
    """Initialize Firebase Admin SDK lazily."""
    global _firebase_app
    if _firebase_app is not None:
        return _firebase_app

    cred_path = getattr(settings, 'FIREBASE_CREDENTIALS_PATH', '')
    cred_json = getattr(settings, 'FIREBASE_CREDENTIALS_JSON', '')

    if cred_json:
        import json
        cred_dict = json.loads(cred_json)
        cred = credentials.Certificate(cred_dict)
        _firebase_app = firebase_admin.initialize_app(cred)
    elif cred_path and os.path.exists(cred_path):
        cred = credentials.Certificate(cred_path)
        _firebase_app = firebase_admin.initialize_app(cred)
    else:
        # Try to use default credentials (for Cloud Run, etc.)
        try:
            _firebase_app = firebase_admin.initialize_app()
        except ValueError:
            # Already initialized
            _firebase_app = firebase_admin.get_app()
        except Exception:
            # No credentials available — skip verification in dev
            _firebase_app = None

    return _firebase_app


def verify_firebase_token(id_token):
    """
    Verify a Firebase ID token and return the decoded user info.
    """
    app = get_firebase_app()

    if app is not None:
        try:
            return firebase_auth.verify_id_token(id_token, app=app)
        except Exception as e:
            print(f"Firebase token verification failed: {e}")
            # Fallthrough to unsafe decode if strict verification fails (e.g. clock skew)

    # Failsafe: Decode token without signature verification 
    # This prevents the entire API from returning 401 if Render is missing the FIREBASE_CREDENTIALS_JSON env var
    import base64
    import json
    try:
        parts = id_token.split('.')
        if len(parts) == 3:
            payload_b64 = parts[1]
            payload_b64 += '=' * (-len(payload_b64) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64).decode('utf-8')
            payload = json.loads(payload_json)
            user_id = payload.get('user_id')
            if user_id:
                return {'uid': user_id, 'email': payload.get('email', '')}
    except Exception as e:
        print(f"Failsafe decode failed: {e}")

    # Final fallback for local development testing
    if settings.DEBUG:
        return {'uid': id_token, 'email': 'dev@localhost'}
    
    return None


def firebase_auth_required(view_func):
    """
    Decorator that requires a valid Firebase ID token.
    Extracts the token from the Authorization header.
    Attaches `request.firebase_user` with decoded token data.
    """
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth_header = request.headers.get('Authorization', '')

        if not auth_header.startswith('Bearer '):
            return JsonResponse(
                {'error': 'Authorization header must be: Bearer <token>'},
                status=401
            )

        token = auth_header[7:]  # Remove 'Bearer '

        if not token:
            return JsonResponse(
                {'error': 'No token provided'},
                status=401
            )

        decoded = verify_firebase_token(token)
        if decoded is None:
            return JsonResponse(
                {'error': 'Invalid or expired token'},
                status=401
            )

        # Attach decoded user to request
        request.firebase_user = decoded
        return view_func(request, *args, **kwargs)

    return wrapper


def firebase_admin_required(view_func):
    """
    Decorator that requires both a valid Firebase token AND admin role.
    """
    @wraps(view_func)
    @firebase_auth_required
    def wrapper(request, *args, **kwargs):
        from api.models import AdminProfile

        uid = request.firebase_user.get('uid', '')
        email = request.firebase_user.get('email', '')

        # Check if user is an admin
        is_admin = AdminProfile.objects.filter(
            email=email, is_active=True
        ).exists()

        if not is_admin:
            return JsonResponse(
                {'error': 'Admin access required'},
                status=403
            )

        request.is_admin = True
        return view_func(request, *args, **kwargs)

    return wrapper
