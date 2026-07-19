from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime
from bson import ObjectId
import re
from app.database import db
from app.jwt_auth import create_session_tokens, verify_access_token, verify_token, extract_token_from_header
from app.credits import grant_welcome_bonus_if_eligible
from app.rate_limiter import RateLimiter
from app.otp_manager import OTPManager
from app.password_utils import hash_password, verify_password, validate_password
from app.notification_service import Fast2SMSService, EmailService
from pymongo import ReturnDocument
import os
import httpx


# ============== REQUEST/RESPONSE MODELS ==============

class SignupRequest(BaseModel):
    full_name: str
    email: Optional[str] = None
    mobile: Optional[str] = None
    password: str
    google_profile: Optional[dict] = None


class LoginRequest(BaseModel):
    email: Optional[str] = None
    mobile: Optional[str] = None
    password: Optional[str] = None
    otp: Optional[str] = None
    login_type: str  # "email_password" | "email_otp" | "mobile_password" | "mobile_otp"


class SendOTPRequest(BaseModel):
    email: Optional[str] = None
    mobile: Optional[str] = None


class VerifyOTPRequest(BaseModel):
    email: Optional[str] = None
    mobile: Optional[str] = None
    otp: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: Optional[str] = None
    mobile: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    email: Optional[str] = None
    mobile: Optional[str] = None
    otp: str
    new_password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class GoogleAuthRequest(BaseModel):
    id_token: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    image_name: Optional[str] = None
    dob: Optional[str] = None
    gender: Optional[str] = None
    mobile: Optional[str] = None


class PasswordSetupRequest(BaseModel):
    new_password: str
    confirm_password: str


class UpdateProfileRequest(BaseModel):
    full_name: Optional[str] = None
    mobile: Optional[str] = None
    username: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    image_name: Optional[str] = None


# ============== HELPER FUNCTIONS ==============

async def next_public_user_id() -> str:
    """Generate next public user ID"""
    counter = await db.counters.find_one_and_update(
        {"_id": "public_user_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = int(counter.get("seq", 1))
    return f"k{seq:04d}"


def _normalize_email(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _normalize_mobile(value: Optional[str]) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


# ============== ROUTER ==============

router = APIRouter(prefix="/auth", tags=["Auth"])

PASSWORD_SETUP_REMINDER_HOURS = 12


# --------------------------------------------------
# SIGNUP ENDPOINTS
# --------------------------------------------------

@router.post("/signup/send-email-otp")
async def send_signup_email_otp(request: SendOTPRequest):
    """Send OTP to email for signup"""
    
    if not request.email:
        raise HTTPException(status_code=400, detail="Email is required")
    
    email = _normalize_email(request.email)
    
    # Check if email already exists
    existing_user = await db.users.find_one({"email": email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Generate and store OTP
    otp_data = await OTPManager.create_otp(email, "email")
    
    # Send OTP via email
    email_result = await EmailService.send_otp_email(email, otp_data["otp"])
    
    if not email_result["success"]:
        raise HTTPException(status_code=500, detail="Failed to send OTP email")
    
    return {
        "success": True,
        "message": "OTP sent to email",
        "expires_in_minutes": otp_data["validity_minutes"]
    }


@router.post("/signup/send-mobile-otp")
async def send_signup_mobile_otp(request: SendOTPRequest):
    """Send OTP to mobile for signup"""
    
    if not request.mobile:
        raise HTTPException(status_code=400, detail="Mobile is required")
    
    mobile = _normalize_mobile(request.mobile)
    
    if len(mobile) != 10:
        raise HTTPException(status_code=400, detail="Invalid mobile number")
    
    # Check if mobile already exists
    existing_user = await db.users.find_one({"mobile": mobile})
    if existing_user:
        raise HTTPException(status_code=400, detail="Mobile already registered")
    
    # Generate and store OTP
    otp_data = await OTPManager.create_otp(mobile, "mobile")
    
    # Send OTP via SMS
    sms_result = await Fast2SMSService.send_otp_sms(mobile, otp_data["otp"])
    
    if not sms_result["success"]:
        raise HTTPException(status_code=500, detail=sms_result.get("message", "Failed to send OTP"))
    
    return {
        "success": True,
        "message": "OTP sent to mobile",
        "expires_in_minutes": otp_data["validity_minutes"]
    }


@router.post("/signup/verify-email-otp")
async def verify_email_otp_signup(request: VerifyOTPRequest):
    """Verify email OTP for signup"""
    
    if not request.email or not request.otp:
        raise HTTPException(status_code=400, detail="Email and OTP required")
    
    email = _normalize_email(request.email)
    
    # Verify OTP
    is_verified = await OTPManager.verify_otp(email, request.otp, "email")
    
    if not is_verified:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    
    # Mark as verified for signup
    await db.email_verifications.update_one(
        {"_id": email},
        {"$set": {"verified": True, "verified_at": datetime.utcnow()}},
        upsert=True
    )
    
    return {
        "success": True,
        "message": "Email verified successfully"
    }


@router.post("/signup/verify-mobile-otp")
async def verify_mobile_otp_signup(request: VerifyOTPRequest):
    """Verify mobile OTP for signup"""
    
    if not request.mobile or not request.otp:
        raise HTTPException(status_code=400, detail="Mobile and OTP required")
    
    mobile = _normalize_mobile(request.mobile)
    
    # Verify OTP
    is_verified = await OTPManager.verify_otp(mobile, request.otp, "mobile")
    
    if not is_verified:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")
    
    # Mark as verified for signup
    await db.mobile_verifications.update_one(
        {"_id": mobile},
        {"$set": {"verified": True, "verified_at": datetime.utcnow()}},
        upsert=True
    )
    
    return {
        "success": True,
        "message": "Mobile verified successfully"
    }


@router.post("/signup")
async def signup(request: SignupRequest):
    """Create new user account"""

    # Validate input
    if not request.full_name:
        raise HTTPException(
            status_code=400,
            detail="Full name is required"
        )

    if not validate_password(request.password):
        raise HTTPException(
            status_code=400,
            detail=(
                "Password must be 8-72 characters long and contain at least "
                "one uppercase letter, one lowercase letter, and one number."
            )
        )

    email = _normalize_email(request.email) if request.email else None
    mobile = _normalize_mobile(request.mobile) if request.mobile else None

    # At least one of email or mobile required
    if not email and not mobile:
        raise HTTPException(
            status_code=400,
            detail="Email or mobile is required"
        )

    # Verify Email OTP
    if email:
        email_verification = await db.email_verifications.find_one(
            {"_id": email}
        )

        if not email_verification or not email_verification.get("verified"):
            raise HTTPException(
                status_code=400,
                detail="Email not verified"
            )

    # Verify Mobile OTP
    if mobile:
        mobile_verification = await db.mobile_verifications.find_one(
            {"_id": mobile}
        )

        if not mobile_verification or not mobile_verification.get("verified"):
            raise HTTPException(
                status_code=400,
                detail="Mobile not verified"
            )

    # Check if Email already exists
    if email:
        existing = await db.users.find_one({"email": email})

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Email already registered"
            )

    # Check if Mobile already exists
    if mobile:
        existing = await db.users.find_one({"mobile": mobile})

        if existing:
            raise HTTPException(
                status_code=400,
                detail="Mobile already registered"
            )

    # Create User
    try:
        public_id = await next_public_user_id()

        google_profile = request.google_profile or {}
        user_doc = {
            "public_id": public_id,
            "firebase_uid": None,
            "full_name": request.full_name,
            "email": email,
            "mobile": mobile,
            "password_hash": hash_password(request.password),
            "auth_type": "google" if google_profile else "manual",
            "account_type": "public",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "is_active": True,
            "two_factor_enabled": False,
            "image_name": google_profile.get("picture") or google_profile.get("image_name"),
            "dob": google_profile.get("dob"),
            "gender": google_profile.get("gender"),
        }

        result = await db.users.insert_one(user_doc)

        firebase_uid = str(result.inserted_id)

        await db.users.update_one(
            {"_id": result.inserted_id},
            {
                "$set": {
                    "firebase_uid": firebase_uid
                }
            }
        )

        # Grant welcome bonus immediately for new users
        await grant_welcome_bonus_if_eligible(firebase_uid, user_doc.get("created_at"))

        # Cleanup OTP records
        if email:
            await db.email_verifications.delete_one({"_id": email})

        if mobile:
            await db.mobile_verifications.delete_one({"_id": mobile})

        # Generate JWT Tokens
        tokens = create_session_tokens(
            firebase_uid,
            email=email,
            mobile=mobile
        )

        return {
            "success": True,
            "message": "Account created successfully",
            "user_id": str(result.inserted_id),
            "public_id": public_id,
            **tokens
        }

    except HTTPException:
        raise

    except Exception as e:
        print("Signup Error:", e)

        raise HTTPException(
            status_code=500,
            detail="Unable to create account. Please try again."
        )
# --------------------------------------------------
# LOGIN ENDPOINTS
# --------------------------------------------------

@router.post("/login")
async def login(request: LoginRequest):
    """
    Login endpoint supporting 4 types:
    - email_password: Login with email and password
    - email_otp: Login with email and OTP
    - mobile_password: Login with mobile and password
    - mobile_otp: Login with mobile and OTP
    """
    
    login_type = request.login_type.lower()
    
    if login_type == "email_password":
        return await _login_email_password(request)
    elif login_type == "email_otp":
        return await _login_email_otp(request)
    elif login_type == "mobile_password":
        return await _login_mobile_password(request)
    elif login_type == "mobile_otp":
        return await _login_mobile_otp(request)
    else:
        raise HTTPException(status_code=400, detail="Invalid login_type")


@router.post("/activity-ping")
async def activity_ping(authorization: str = Header(...)):
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded.get("uid")
        if not uid:
            raise HTTPException(status_code=401, detail="Invalid token")

        user = await db.users.find_one(
            {"firebase_uid": uid},
            {
                "_id": 0,
                "firebase_uid": 1,
                "full_name": 1,
                "username": 1,
                "email": 1,
                "mobile": 1,
                "role": 1,
                "account_type": 1,
            },
        )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        now = datetime.utcnow()
        day_start = datetime(now.year, now.month, now.day)
        day_key = day_start.strftime("%Y-%m-%d")

        await db.user_daily_activity.update_one(
            {"user_id": uid, "date_key": day_key},
            {
                "$setOnInsert": {
                    "user_id": uid,
                    "date": day_start,
                    "date_key": day_key,
                    "first_seen_at": now,
                    "created_at": now,
                },
                "$set": {
                    "last_seen_at": now,
                    "updated_at": now,
                    "last_path": "/activity-ping",
                    "name": user.get("full_name") or "",
                    "username": user.get("username") or "",
                    "email": user.get("email") or "",
                    "mobile": user.get("mobile") or "",
                    "role": user.get("role") or "user",
                    "account_type": user.get("account_type") or "normal",
                },
                "$inc": {"hit_count": 1},
            },
            upsert=True,
        )

        return {"ok": True, "user_id": uid, "date_key": day_key, "at": now}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


async def _login_email_password(request: LoginRequest):
    """Login with email and password"""
    
    if not request.email or not request.password:
        raise HTTPException(status_code=400, detail="Email and password required")
    
    email = _normalize_email(request.email)
    
    # Check rate limit
    rate_limit = await RateLimiter.check_rate_limit(email, "email")
    if not rate_limit["allowed"]:
        raise HTTPException(status_code=429, detail=rate_limit["message"])
    
    # Find user
    user = await db.users.find_one({"email": email})
    
    if not user or not user.get("password_hash"):
        await RateLimiter.record_failed_attempt(email, "email")
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    # Persist compatibility firebase_uid for JWT session uid
    if not user.get("firebase_uid"):
        firebase_uid = str(user["_id"])
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"firebase_uid": firebase_uid}}
        )
        user["firebase_uid"] = firebase_uid
    
    # Verify password
    print("=" * 60)
    print("Entered Password :", request.password)
    print("Password Length  :", len(request.password))
    print("Stored Hash      :", user["password_hash"])
    print("Hash Length      :", len(user["password_hash"]))
    print("=" * 60)

    
    if not verify_password(request.password, user["password_hash"]):
        await RateLimiter.record_failed_attempt(email, "email")
        raise HTTPException(status_code=401, detail="Invalid email or password")

    
    # Clear rate limit on successful login
    await RateLimiter.clear_rate_limit(email, "email")
    
    # Create tokens
    tokens = create_session_tokens(str(user["_id"]), email=email, mobile=user.get("mobile"))
    
    # Update last login
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )
    
    return {
        "success": True,
        "message": "Login successful",
        "user_id": str(user["_id"]),
        "public_id": user.get("public_id"),
        "full_name": user.get("full_name"),
        **tokens
    }


async def _login_email_otp(request: LoginRequest):
    """Login with email and OTP"""
    
    if not request.email or not request.otp:
        raise HTTPException(status_code=400, detail="Email and OTP required")
    
    email = _normalize_email(request.email)
    
    # Check rate limit
    rate_limit = await RateLimiter.check_rate_limit(email, "email")
    if not rate_limit["allowed"]:
        raise HTTPException(status_code=429, detail=rate_limit["message"])
    
    # Verify OTP
    is_verified = await OTPManager.verify_otp(email, request.otp, "email")
    
    if not is_verified:
        await RateLimiter.record_failed_attempt(email, "email")
        raise HTTPException(status_code=401, detail="Invalid OTP")
    
    # Find or create user
    user = await db.users.find_one({"email": email})
    
    if not user:
            # Create new user with email
            public_id = await next_public_user_id()
            user_doc = {
                "public_id": public_id,
                "firebase_uid": None,
                "email": email,
                "mobile": None,
                "auth_type": "email_otp",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "is_active": True
            }
            result = await db.users.insert_one(user_doc)
            firebase_uid = str(result.inserted_id)
            await db.users.update_one(
                {"_id": result.inserted_id},
                {"$set": {"firebase_uid": firebase_uid}}
            )
            user = user_doc
            user["_id"] = result.inserted_id
            user["firebase_uid"] = firebase_uid
            await grant_welcome_bonus_if_eligible(firebase_uid, user_doc.get("created_at"))

    if user and not user.get("firebase_uid"):
        firebase_uid = str(user["_id"])
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"firebase_uid": firebase_uid}}
        )
        user["firebase_uid"] = firebase_uid

    await RateLimiter.clear_rate_limit(email, "email")
    
    # Create tokens
    tokens = create_session_tokens(str(user["_id"]), email=email, mobile=user.get("mobile"))
    
    # Update last login
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )
    
    # Clean up OTP
    await OTPManager.mark_otp_as_used(email, "email")
    
    return {
        "success": True,
        "message": "Login successful",
        "user_id": str(user["_id"]),
        "public_id": user.get("public_id"),
        **tokens
    }


async def _login_mobile_password(request: LoginRequest):
    """Login with mobile and password"""
    
    if not request.mobile or not request.password:
        raise HTTPException(status_code=400, detail="Mobile and password required")
    
    mobile = _normalize_mobile(request.mobile)
    
    if len(mobile) != 10:
        raise HTTPException(status_code=400, detail="Invalid mobile number")
    
    # Check rate limit
    rate_limit = await RateLimiter.check_rate_limit(mobile, "mobile")
    if not rate_limit["allowed"]:
        raise HTTPException(status_code=429, detail=rate_limit["message"])
    
    # Find user
    user = await db.users.find_one({"mobile": mobile})
    
    if not user or not user.get("password_hash"):
        await RateLimiter.record_failed_attempt(mobile, "mobile")
        raise HTTPException(status_code=401, detail="Invalid mobile or password")
    
    # Persist compatibility firebase_uid for JWT session uid
    if not user.get("firebase_uid"):
        firebase_uid = str(user["_id"])
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"firebase_uid": firebase_uid}}
        )
        user["firebase_uid"] = firebase_uid
    
    # Verify password
    if not verify_password(request.password, user["password_hash"]):
        await RateLimiter.record_failed_attempt(mobile, "mobile")
        raise HTTPException(status_code=401, detail="Invalid mobile or password")
    
    # Clear rate limit on successful login
    await RateLimiter.clear_rate_limit(mobile, "mobile")
    
    # Create tokens
    tokens = create_session_tokens(str(user["_id"]), email=user.get("email"), mobile=mobile)
    
    # Update last login
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )
    
    return {
        "success": True,
        "message": "Login successful",
        "user_id": str(user["_id"]),
        "public_id": user.get("public_id"),
        "full_name": user.get("full_name"),
        **tokens
    }


async def _login_mobile_otp(request: LoginRequest):
    """Login with mobile and OTP"""
    
    if not request.mobile or not request.otp:
        raise HTTPException(status_code=400, detail="Mobile and OTP required")
    
    mobile = _normalize_mobile(request.mobile)
    
    if len(mobile) != 10:
        raise HTTPException(status_code=400, detail="Invalid mobile number")
    
    # Check rate limit
    rate_limit = await RateLimiter.check_rate_limit(mobile, "mobile")
    if not rate_limit["allowed"]:
        raise HTTPException(status_code=429, detail=rate_limit["message"])
    
    # Verify OTP
    is_verified = await OTPManager.verify_otp(mobile, request.otp, "mobile")
    
    if not is_verified:
        await RateLimiter.record_failed_attempt(mobile, "mobile")
        raise HTTPException(status_code=401, detail="Invalid OTP")
    
    # Find or create user
    user = await db.users.find_one({"mobile": mobile})
    
    if not user:
            # Create new user with mobile
            public_id = await next_public_user_id()
            user_doc = {
                "public_id": public_id,
                "firebase_uid": None,
                "email": None,
                "mobile": mobile,
                "auth_type": "mobile_otp",
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "is_active": True
            }
            result = await db.users.insert_one(user_doc)
            firebase_uid = str(result.inserted_id)
            await db.users.update_one(
                {"_id": result.inserted_id},
                {"$set": {"firebase_uid": firebase_uid}}
            )
            user = user_doc
            user["_id"] = result.inserted_id
            user["firebase_uid"] = firebase_uid

    if user and not user.get("firebase_uid"):
        firebase_uid = str(user["_id"])
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"firebase_uid": firebase_uid}}
        )
        user["firebase_uid"] = firebase_uid

    await RateLimiter.clear_rate_limit(mobile, "mobile")
    
    # Create tokens
    tokens = create_session_tokens(str(user["_id"]), email=user.get("email"), mobile=mobile)
    
    # Update last login
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login": datetime.utcnow()}}
    )
    
    # Clean up OTP
    await OTPManager.mark_otp_as_used(mobile, "mobile")
    
    return {
        "success": True,
        "message": "Login successful",
        "user_id": str(user["_id"]),
        "public_id": user.get("public_id"),
        **tokens
    }


# --------------------------------------------------
# SEND LOGIN OTP ENDPOINTS
# --------------------------------------------------

@router.post("/login/send-email-otp")
async def send_login_email_otp(request: SendOTPRequest):
    """Send OTP to email for login"""
    
    if not request.email:
        raise HTTPException(status_code=400, detail="Email is required")
    
    email = _normalize_email(request.email)
    
    # Check if email exists
    user = await db.users.find_one({"email": email})
    if not user:
        # For security, don't reveal if email exists
        return {
            "success": True,
            "message": "If email exists, OTP has been sent"
        }
    
    # Generate OTP
    otp_data = await OTPManager.create_otp(email, "email")
    
    # Send OTP
    email_result = await EmailService.send_otp_email(email, otp_data["otp"])
    
    if not email_result["success"]:
        raise HTTPException(status_code=500, detail="Failed to send OTP")
    
    return {
        "success": True,
        "message": "OTP sent to email",
        "expires_in_minutes": otp_data["validity_minutes"]
    }


@router.post("/login/send-mobile-otp")
async def send_login_mobile_otp(request: SendOTPRequest):
    """Send OTP to mobile for login"""
    
    if not request.mobile:
        raise HTTPException(status_code=400, detail="Mobile is required")
    
    mobile = _normalize_mobile(request.mobile)
    
    if len(mobile) != 10:
        raise HTTPException(status_code=400, detail="Invalid mobile number")
    
    # Check if mobile exists
    user = await db.users.find_one({"mobile": mobile})
    if not user:
        # For security, don't reveal if mobile exists
        return {
            "success": True,
            "message": "If mobile exists, OTP has been sent"
        }
    
    # Generate OTP
    otp_data = await OTPManager.create_otp(mobile, "mobile")
    
    # Send OTP
    sms_result = await Fast2SMSService.send_otp_sms(mobile, otp_data["otp"])
    
    if not sms_result["success"]:
        raise HTTPException(status_code=500, detail=sms_result.get("message", "Failed to send OTP"))
    
    return {
        "success": True,
        "message": "OTP sent to mobile",
        "expires_in_minutes": otp_data["validity_minutes"]
    }


# --------------------------------------------------
# GOOGLE OAUTH ENDPOINT
# --------------------------------------------------

async def _verify_google_id_token(id_token: str):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://oauth2.googleapis.com/tokeninfo",
                params={"id_token": id_token},
                timeout=10.0,
            )
            response.raise_for_status()
            payload = response.json()

            email_verified = payload.get("email_verified")
            if email_verified not in {"true", True, "1", 1}:
                raise HTTPException(status_code=401, detail="Google email not verified")

            return payload

    except HTTPException:
        raise

    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=401,
            detail="Invalid Google ID token"
        ) from exc

    except Exception as exc:
        raise HTTPException(
            status_code=401,
            detail=f"Failed to verify Google token: {str(exc)}"
        ) from exc


@router.post("/forgot-password")
async def forgot_password(request: ForgotPasswordRequest):
    """Send password reset instructions for a registered email."""

    email = _normalize_email(request.email)

    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    user = await db.users.find_one({"email": email})

    if user:
        otp_data = await OTPManager.create_otp(email, "email")

        email_result = await EmailService.send_otp_email(
            email,
            otp_data["otp"]
        )

        if not email_result["success"]:
            raise HTTPException(
                status_code=500,
                detail="Failed to send reset instructions"
            )

    return {
        "success": True,
        "message": "If the email is registered, reset instructions have been sent"
    }


@router.post("/reset-password")
async def reset_password(request: ResetPasswordRequest):
    """Reset password using email/mobile and OTP"""

    email = _normalize_email(request.email) if request.email else None
    mobile = _normalize_mobile(request.mobile) if request.mobile else None

    # Validate input
    if not email and not mobile:
        raise HTTPException(status_code=400, detail="Email or mobile is required")

    if not request.otp or not request.new_password:
        raise HTTPException(status_code=400, detail="OTP and new password are required")

    if not validate_password(request.new_password):
        raise HTTPException(
            status_code=400,
            detail="Password must be 8-72 characters long and contain at least one uppercase letter, one lowercase letter, and one number."
        )

    # Verify OTP
    identifier = email or mobile
    otp_type = "email" if email else "mobile"
    is_verified = await OTPManager.verify_otp(identifier, request.otp, otp_type)

    if not is_verified:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    # Find user
    query = {}
    if email:
        query["email"] = email
    else:
        query["mobile"] = mobile

    user = await db.users.find_one(query)

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Update password
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": hash_password(request.new_password),
                "updated_at": datetime.utcnow()
            }
        }
    )

    # Clean up OTP
    await OTPManager.mark_otp_as_used(identifier, otp_type)

    return {
        "success": True,
        "message": "Password reset successfully"
    }


@router.post("/forgot-password-send-otp")
async def forgot_password_send_otp(request: SendOTPRequest):
    """Send OTP for forgot password (works for both email and mobile)"""

    email = _normalize_email(request.email) if request.email else None
    mobile = _normalize_mobile(request.mobile) if request.mobile else None

    # At least one required
    if not email and not mobile:
        raise HTTPException(status_code=400, detail="Email or mobile is required")

    # Send email OTP
    if email:
        user = await db.users.find_one({"email": email})
        if not user:
            # For security, don't reveal if email exists
            return {
                "success": True,
                "message": "If email exists, OTP has been sent"
            }

        otp_data = await OTPManager.create_otp(email, "email")
        email_result = await EmailService.send_otp_email(email, otp_data["otp"])

        if not email_result["success"]:
            raise HTTPException(status_code=500, detail="Failed to send OTP")

        return {
            "success": True,
            "message": "OTP sent to email",
            "expires_in_minutes": otp_data["validity_minutes"]
        }

    # Send mobile OTP
    if mobile:
        if len(mobile) != 10:
            raise HTTPException(status_code=400, detail="Invalid mobile number")

        user = await db.users.find_one({"mobile": mobile})
        if not user:
            # For security, don't reveal if mobile exists
            return {
                "success": True,
                "message": "If mobile exists, OTP has been sent"
            }

        otp_data = await OTPManager.create_otp(mobile, "mobile")
        sms_result = await Fast2SMSService.send_otp_sms(mobile, otp_data["otp"])

        if not sms_result["success"]:
            raise HTTPException(status_code=500, detail=sms_result.get("message", "Failed to send OTP"))

        return {
            "success": True,
            "message": "OTP sent to mobile",
            "expires_in_minutes": otp_data["validity_minutes"]
        }


@router.post("/change-password")
async def change_password(
    request: ChangePasswordRequest,
    authorization: str = Header(...)
):
    """Change the current user's password."""

    token = extract_token_from_header(authorization)
    payload = verify_access_token(token)
    user_id = payload.get("sub")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    if not validate_password(request.new_password):
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 6 characters"
        )

    user = await db.users.find_one({"_id": ObjectId(user_id)})

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.get("password_hash") and not verify_password(
        request.old_password,
        user["password_hash"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Old password is incorrect"
        )

    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": hash_password(request.new_password),
                "updated_at": datetime.utcnow(),
            }
        }
    )

    return {
        "success": True,
        "message": "Password updated successfully"
    }


@router.post("/google-login")
async def google_login(request: GoogleAuthRequest):
    """
    Login with Google OAuth
    
    For Google OAuth, we trust that Google has verified the email.
    We reuse an existing saved mobile number when available and only require
    verification for accounts that do not yet have a mobile attached.
    """

    if not request.id_token:
        raise HTTPException(status_code=400, detail="ID token is required")

    try:
        google_payload = await _verify_google_id_token(request.id_token)

        email = _normalize_email(google_payload.get("email") or request.email)
        full_name = google_payload.get("name") or request.full_name
        image_name = request.image_name or google_payload.get("picture")
        dob = request.dob or google_payload.get("birthdate")
        gender = request.gender or google_payload.get("gender")
        mobile = _normalize_mobile(request.mobile) if request.mobile else None

        # For Google OAuth, we trust Google's email verification
        # We don't need to check email_verifications collection
        if not email:
            raise HTTPException(status_code=400, detail="Email from Google is required")

        user = await db.users.find_one({"email": email})

        if user and user.get("mobile"):
            mobile = _normalize_mobile(user.get("mobile"))

        # Existing Google users keep their saved mobile number. We only ask for
        # verification when no mobile is already attached to the account.
        if not mobile:
            return {
                "success": False,
                "needs_mobile_verification": True,
                "email": email,
                "full_name": full_name,
                "image_name": image_name,
                "dob": dob,
                "gender": gender,
                "message": "Mobile verification required"
            }

        # New users still need the submitted mobile to be verified before we
        # create the account.
        if not user:
            mobile_verification = await db.mobile_verifications.find_one({"_id": mobile})
            if not mobile_verification or not mobile_verification.get("verified"):
                return {
                    "success": False,
                    "needs_mobile_verification": True,
                    "email": email,
                    "full_name": full_name,
                    "image_name": image_name,
                    "dob": dob,
                    "gender": gender,
                    "message": "Mobile verification required"
                }

        if not user:
            public_id = await next_public_user_id()

            user_doc = {
                "public_id": public_id,
                "firebase_uid": None,
                "full_name": full_name,
                "email": email,
                "mobile": mobile,
                "auth_type": "google",
                "password_hash": None,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
                "is_active": True,
                "image_name": image_name,
                "dob": dob,
                "gender": gender,
                "needs_password_setup": True,
                "password_setup_notification_sent_at": None,
            }

            result = await db.users.insert_one(user_doc)

            firebase_uid = str(result.inserted_id)

            await db.users.update_one(
                {"_id": result.inserted_id},
                {"$set": {"firebase_uid": firebase_uid}}
            )

            user = user_doc
            user["_id"] = result.inserted_id
            user["firebase_uid"] = firebase_uid

            is_new_user = True

        else:
            is_new_user = False

            if not user.get("firebase_uid"):
                firebase_uid = str(user["_id"])

                await db.users.update_one(
                    {"_id": user["_id"]},
                    {"$set": {"firebase_uid": firebase_uid}}
                )

                user["firebase_uid"] = firebase_uid

        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "last_login": datetime.utcnow(),
                    "image_name": user.get("image_name") or image_name,
                    "dob": user.get("dob") or dob,
                    "gender": user.get("gender") or gender,
                    "mobile": user.get("mobile") or mobile,
                }
            }
        )

        tokens = create_session_tokens(
            str(user["_id"]),
            email=user.get("email"),
            mobile=user.get("mobile"),
        )

        return {
            "success": True,
            "message": "Google login successful",
            "user_id": str(user["_id"]),
            "public_id": user.get("public_id"),
            "full_name": user.get("full_name"),
            "is_new_user": is_new_user,
            "needs_password_setup": user.get("needs_password_setup", False),
            **tokens,
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Google login failed: {str(e)}"
        )


# --------------------------------------------------
# PASSWORD SETUP ENDPOINTS
# --------------------------------------------------


async def _send_password_setup_notification_if_due(user: dict) -> bool:
    email = user.get("email")
    if not email:
        return False

    last_sent = user.get("password_setup_notification_sent_at")
    if last_sent:
        last_sent_dt = last_sent if isinstance(last_sent, datetime) else datetime.fromisoformat(str(last_sent))
        hours_since = (datetime.utcnow() - last_sent_dt).total_seconds() / 3600
        if hours_since < PASSWORD_SETUP_REMINDER_HOURS:
            return False

    now = datetime.utcnow()
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"password_setup_notification_sent_at": now}},
    )

    email_result = await EmailService.send_password_setup_notification(
        email,
        user.get("full_name", "User"),
    )
    return bool(email_result.get("success"))


async def send_due_password_setup_notifications() -> int:
    sent_count = 0
    cursor = db.users.find(
        {
            "needs_password_setup": True,
            "email": {"$nin": [None, ""]},
        }
    )

    async for user in cursor:
        try:
            if await _send_password_setup_notification_if_due(user):
                sent_count += 1
        except Exception:
            continue

    return sent_count

@router.post("/check-password-setup")
async def check_password_setup(authorization: str = Header(...)):
    """Check if user needs to set up password"""
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "success": True,
            "needs_password_setup": user.get("needs_password_setup", False),
            "has_password": bool(user.get("password_hash")),
            "auth_type": user.get("auth_type"),
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Failed to check password setup: {str(e)}")


@router.post("/send-password-setup-notification")
async def send_password_setup_notification(authorization: str = Header(...)):
    """Send password setup notification to user"""
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        sent = await _send_password_setup_notification_if_due(user)
        if not sent:
            raise HTTPException(
                status_code=400,
                detail="Notification already sent in the last 12 hours",
            )
        
        return {
            "success": True,
            "message": "Password setup notification sent",
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send notification: {str(e)}")


@router.post("/setup-password")
async def setup_password(request: PasswordSetupRequest, authorization: str = Header(...)):
    """Set up password for user who signed up via Google"""
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        if request.new_password != request.confirm_password:
            raise HTTPException(status_code=400, detail="Passwords do not match")
        
        if not validate_password(request.new_password):
            raise HTTPException(
                status_code=400,
                detail="Password must be at least 8 characters with uppercase, lowercase, and a number"
            )
        
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Set password and mark setup as complete
        await db.users.update_one(
            {"_id": user["_id"]},
            {
                "$set": {
                    "password_hash": hash_password(request.new_password),
                    "needs_password_setup": False,
                    "updated_at": datetime.utcnow(),
                }
            }
        )
        
        return {
            "success": True,
            "message": "Password set up successfully",
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to set up password: {str(e)}")


# --------------------------------------------------
# TOKEN ENDPOINTS
# --------------------------------------------------

@router.post("/refresh-token")
async def refresh_token(request: RefreshTokenRequest):
    """Refresh access token using refresh token"""

    try:
        payload = verify_token(request.refresh_token)

        if payload.get("type") != "refresh":
            raise HTTPException(
                status_code=401,
                detail="Invalid token type"
            )

        user_id = payload.get("sub")

        user = await db.users.find_one(
            {"_id": ObjectId(user_id)}
        )

        if not user:
            raise HTTPException(
                status_code=401,
                detail="User not found"
            )

        tokens = create_session_tokens(
            user_id,
            email=user.get("email"),
            mobile=user.get("mobile"),
        )

        return {
            "success": True,
            "message": "Token refreshed",
            **tokens,
        }

    except HTTPException:
        raise

    except Exception as e:
        raise HTTPException(
            status_code=401,
            detail=f"Token refresh failed: {str(e)}"
        )


# --------------------------------------------------
# PROFILE ENDPOINTS
# --------------------------------------------------

@router.get("/me")
async def get_current_user(authorization: str = Header(...)):
    """Get current user profile"""
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "success": True,
            "user": {
                "user_id": str(user["_id"]),
                "public_id": user.get("public_id"),
                "full_name": user.get("full_name"),
                "email": user.get("email"),
                "mobile": user.get("mobile"),
                "auth_type": user.get("auth_type"),
                "created_at": user.get("created_at"),
            }
        }
    
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@router.put("/update-profile")
async def update_profile(request: UpdateProfileRequest, authorization: str = Header(...)):
    """Update user profile"""
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        # Update user
        from bson.objectid import ObjectId
        update_data = {}
        
        if request.full_name is not None:
            update_data["full_name"] = request.full_name.strip()

        if request.username is not None:
            username = request.username.strip()
            if username:
                if not re.match(r"^[a-zA-Z0-9._]{3,30}$", username):
                    raise HTTPException(status_code=400, detail="Username must be 3-30 chars and only letters, numbers, dot or underscore")
                existing = await db.users.find_one(
                    {
                        "username": {"$regex": f"^{re.escape(username)}$", "$options": "i"},
                        "firebase_uid": {"$ne": str(user_id)},
                    },
                    {"_id": 1},
                )
                if existing:
                    raise HTTPException(status_code=409, detail="Username already used")
            update_data["username"] = username

        if request.mobile is not None:
            mobile = _normalize_mobile(request.mobile)
            if len(mobile) != 10:
                raise HTTPException(status_code=400, detail="Invalid mobile number")
            existing = await db.users.find_one({"mobile": mobile})
            if existing and str(existing["_id"]) != user_id:
                raise HTTPException(status_code=400, detail="Mobile already in use")
            update_data["mobile"] = mobile

        if request.bio is not None:
            update_data["bio"] = request.bio
        if request.location is not None:
            update_data["location"] = request.location
        if request.website is not None:
            update_data["website"] = request.website
        if request.image_name is not None:
            update_data["image_name"] = request.image_name

        update_data["updated_at"] = datetime.utcnow()
        
        result = await db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="User not found")
        
        return {
            "success": True,
            "message": "Profile updated successfully"
        }
    
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/logout")
async def logout(authorization: str = Header(...)):
    """Logout user (invalidate tokens)"""
    
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        
        # In a real application, you would blacklist the token here
        # For now, we'll just return success
        
        return {
            "success": True,
            "message": "Logged out successfully"
        }
    
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))
