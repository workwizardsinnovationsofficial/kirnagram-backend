import jwt
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from fastapi import HTTPException
import secrets

# JWT Configuration
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "your-super-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24
REFRESH_TOKEN_EXPIRE_DAYS = 7


def create_access_token(data: Dict[str, Any], expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token"""
    to_encode = data.copy()
    
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    
    to_encode.update({"exp": expire})
    
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_refresh_token(user_id: str) -> str:
    """Create JWT refresh token"""
    data = {
        "sub": user_id,
        "type": "refresh"
    }
    
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    to_encode.update({"exp": expire})
    
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt


def create_session_tokens(user_id: str, email: Optional[str] = None, mobile: Optional[str] = None) -> Dict[str, str]:
    """Create both access and refresh tokens for session management"""
    
    access_token_data = {
        "sub": user_id,
        "uid": user_id,
        "type": "access",
        "email": email,
        "mobile": mobile,
        "session_id": secrets.token_urlsafe(16)  # Unique session ID
    }
    
    access_token = create_access_token(access_token_data)
    refresh_token = create_refresh_token(user_id)
    
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer"
    }


def verify_token(token: str) -> Dict[str, Any]:
    """Verify JWT token and return decoded data"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token has expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


def verify_access_token(token: str) -> Dict[str, Any]:
    """Verify JWT access token"""
    payload = verify_token(token)
    
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid token type")
    
    return payload


def get_user_id_from_token(token: str) -> str:
    """Extract user_id from token"""
    payload = verify_access_token(token)
    return payload.get("sub")


def extract_token_from_header(authorization: str) -> str:
    """Extract token from Authorization header"""
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    return parts[1]


def get_user_id_from_authorization_header(authorization: str) -> str:
    """Extract and verify user_id from Authorization header"""
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing user ID")
        
        return user_id
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(exc)}")
