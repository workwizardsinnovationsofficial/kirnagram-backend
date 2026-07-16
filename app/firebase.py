from fastapi import HTTPException
from app.jwt_auth import verify_access_token


def verify_firebase_token(token: str):
    """Compatibility shim that verifies backend JWTs and returns a Firebase-like payload.

    Many legacy routes call `verify_firebase_token`. To avoid changing all callers
    at once, this shim keeps the same function name but delegates to `verify_access_token`.
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
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(exc)}") from exc
