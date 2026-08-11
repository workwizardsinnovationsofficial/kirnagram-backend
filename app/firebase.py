import os

from fastapi import HTTPException
from dotenv import load_dotenv

from app.jwt_auth import verify_access_token

import firebase_admin
from firebase_admin import credentials, auth


# Load backend .env
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)


# ---------------------------------------------------------
# Firebase Admin SDK initialization
# ---------------------------------------------------------

FIREBASE_ADMIN_KEY_PATH = os.path.join(
    BASE_DIR,
    "adminFirebaseKey.json",
)


def _get_firebase_app():
    """
    Return the existing Firebase Admin app or initialize it
    using the admin service-account key.
    """
    try:
        return firebase_admin.get_app()
    except ValueError:
        try:
            cred = credentials.Certificate(FIREBASE_ADMIN_KEY_PATH)
            return firebase_admin.initialize_app(cred)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to initialize Firebase Admin SDK: {exc}"
            ) from exc


# ---------------------------------------------------------
# Existing backend JWT compatibility function
# ---------------------------------------------------------

def verify_firebase_token(token: str):
    """
    Compatibility shim for existing routes.

    Existing application routes use the backend's own HS256 JWT.
    Keep this function unchanged so normal application
    authentication is not broken.
    """
    try:
        payload = verify_access_token(token)

        return {
            "uid": payload.get("sub"),
            "email": payload.get("email"),
            "mobile": payload.get("mobile"),
            "session_id": payload.get("session_id"),
        }

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid token: {str(exc)}",
        ) from exc


# ---------------------------------------------------------
# Real Firebase ID-token verification
# ---------------------------------------------------------

def verify_admin_firebase_token(token: str):
    """
    Verify a Firebase Authentication ID token.

    This is used by admin dashboard requests, where the
    browser sends a Firebase RS256 ID token.
    """
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Missing Firebase ID token",
        )

    try:
        _get_firebase_app()

        decoded = auth.verify_id_token(
            token,
            check_revoked=True,
        )

        return {
            "uid": decoded.get("uid") or decoded.get("sub"),
            "email": decoded.get("email"),
            "email_verified": decoded.get("email_verified"),
            "firebase": decoded,
        }

    except auth.ExpiredIdTokenError:
        raise HTTPException(
            status_code=401,
            detail="Firebase token has expired",
        )

    except auth.RevokedIdTokenError:
        raise HTTPException(
            status_code=401,
            detail="Firebase token has been revoked",
        )

    except auth.InvalidIdTokenError as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Invalid Firebase token: {str(exc)}",
        )

    except HTTPException:
        raise

    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Firebase token verification failed: {str(exc)}",
        ) from exc
