from fastapi import APIRouter, Header, HTTPException
from app.database import db
from app.credits import grant_welcome_bonus_if_eligible
from app.firebase import verify_firebase_token
from app.jwt_auth import extract_token_from_header
from app.models.user import UserCreate
from app.models.google_user import GoogleUser
from app.models.otp import SendEmailOtpRequest, VerifyEmailOtpRequest
from app.services.email_otp_service import (
    consume_verified_email_for_signup,
    send_email_otp,
    verify_email_otp,
)
from datetime import datetime
from typing import Optional
from pymongo import ReturnDocument


router = APIRouter(prefix="/auth", tags=["Auth"])


def _normalize_email(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _normalize_mobile(value: Optional[str]) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


async def _is_ai_creator_identifier_blocked(email: Optional[str] = None, mobile: Optional[str] = None) -> bool:
    checks = []
    normalized_email = _normalize_email(email)
    normalized_mobile = _normalize_mobile(mobile)
    if normalized_email:
        checks.append({"kind": "email", "value": normalized_email})
    if normalized_mobile:
        checks.append({"kind": "mobile", "value": normalized_mobile})
    if not checks:
        return False
    blocked = await db.ai_creator_blocklist.find_one({"$or": checks, "active": True})
    return bool(blocked)


async def next_public_user_id() -> str:
    counter = await db.counters.find_one_and_update(
        {"_id": "public_user_id"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = int(counter.get("seq", 1))
    return f"k{seq:04d}"


# --------------------------------------------------
# TEST MongoDB CONNECTION
# --------------------------------------------------
@router.get("/test-db")
async def test_database():
    try:
        # Try to count documents
        count = await db.users.count_documents({})
        
        # Try to insert a test document
        test_doc = {
            "test": True,
            "timestamp": datetime.utcnow()
        }
        result = await db.users.insert_one(test_doc)
        
        # Delete the test document
        await db.users.delete_one({"_id": result.inserted_id})
        
        return {
            "status": "connected",
            "users_count": count,
            "test_insert": "success"
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


# --------------------------------------------------
# CHECK USER
# --------------------------------------------------
@router.post("/check-user")
async def check_user(data: dict):
    email = data.get("email")
    mobile = data.get("mobile")

    email_exists = await db.users.find_one({"email": email})
    mobile_exists = await db.users.find_one({"mobile": mobile})

    blocked_for_ai_creator = await _is_ai_creator_identifier_blocked(email=email, mobile=mobile)

    return {
        "emailExists": bool(email_exists),
        "mobileExists": bool(mobile_exists),
        "aiCreatorBlocked": blocked_for_ai_creator,
    }


@router.post("/send-email-otp")
async def send_signup_email_otp(payload: SendEmailOtpRequest):
    email = payload.email

    if await _is_ai_creator_identifier_blocked(email=email):
        raise HTTPException(status_code=403, detail="This email is blocked for AI Creator due to policy violation")

    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already in use")

    return await send_email_otp(email)


@router.post("/verify-email-otp")
async def verify_signup_email_otp(payload: VerifyEmailOtpRequest):
    email = payload.email

    if await _is_ai_creator_identifier_blocked(email=email):
        raise HTTPException(status_code=403, detail="This email is blocked for AI Creator due to policy violation")

    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already in use")

    return await verify_email_otp(email, payload.otp)

# --------------------------------------------------
# MANUAL SIGNUP
# --------------------------------------------------
@router.post("/signup")
async def signup(
    user: UserCreate,
    authorization: str = Header(...)
):
    try:
        print("🔵 Signup endpoint called")
        token = extract_token_from_header(authorization)
        decoded = verify_firebase_token(token)

        uid = decoded["uid"]
        email = decoded["email"]
        print(f"🔵 Firebase UID: {uid}, Email: {email}")

        if await _is_ai_creator_identifier_blocked(email=email, mobile=user.mobile):
            raise HTTPException(status_code=403, detail="This email/mobile is blocked for AI Creator due to policy violation")

        # Enforce OTP verification even if frontend validation is bypassed.
        await consume_verified_email_for_signup(email)

        existing = await db.users.find_one({"firebase_uid": uid})
        if existing:
            print("⚠️ User already exists")
            return {"message": "User already exists"}

        public_id = await next_public_user_id()

        user_doc = {
            "firebase_uid": uid,
            "public_id": public_id,
            "full_name": user.full_name,
            "email": email,
            "mobile": user.mobile,
            "image_name": user.image_name,
            "provider": "manual",
            "account_type": "public",  # Default to public
            "created_at": datetime.utcnow()
        }
        print(f"🔵 Inserting user document: {user_doc}")
        
        result = await db.users.insert_one(user_doc)
        print(f"✅ User created with ID: {result.inserted_id}")

        await grant_welcome_bonus_if_eligible(uid, user_doc.get("created_at"))

        return {"message": "Manual account created", "success": True}

    except Exception as e:
        print(f"❌ Signup error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=401, detail=str(e))


# --------------------------------------------------
# GOOGLE LOGIN (🔥 THIS WAS MISSING)
# --------------------------------------------------
@router.post("/google-login")
async def google_login(
    user: GoogleUser,
    authorization: str = Header(...)
):
    try:
        print("🔵 Google login endpoint called")
        token = extract_token_from_header(authorization)
        decoded = verify_firebase_token(token)

        uid = decoded["uid"]
        email = decoded["email"]
        print(f"🔵 Google user - UID: {uid}, Email: {email}")

        if await _is_ai_creator_identifier_blocked(email=email):
            raise HTTPException(status_code=403, detail="This email is blocked for AI Creator due to policy violation")

        existing = await db.users.find_one({"firebase_uid": uid})

        if existing:
            print("✅ Existing Google user found")

            if not existing.get("public_id"):
                public_id = await next_public_user_id()
                await db.users.update_one(
                    {"firebase_uid": uid},
                    {"$set": {"public_id": public_id}}
                )

            incoming_image = user.image_name
            existing_image = existing.get("image_name")
            if incoming_image and (
                not existing_image or
                "default" in str(existing_image) or
                "placeholder" in str(existing_image)
            ):
                await db.users.update_one(
                    {"firebase_uid": uid},
                    {"$set": {"image_name": incoming_image}}
                )
            return {
                "message": "Google login successful",
                "newUser": False
            }

        public_id = await next_public_user_id()

        user_doc = {
            "firebase_uid": uid,
            "public_id": public_id,
            "full_name": user.full_name,
            "email": email,
            "mobile": None,
            "image_name": user.image_name,
            "provider": "google",
            "account_type": "public",  # Default to public
            "created_at": datetime.utcnow()
        }
        print(f"🔵 Inserting Google user: {user_doc}")
        
        result = await db.users.insert_one(user_doc)
        print(f"✅ Google user created with ID: {result.inserted_id}")

        await grant_welcome_bonus_if_eligible(uid, user_doc.get("created_at"))

        return {
            "message": "Google account created",
            "newUser": True
        }

    except Exception as e:
        print(f"❌ Google login error: {str(e)}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=401, detail=str(e))
    


# --------------------------------------------------
# GET EMAIL BY MOBILE
# --------------------------------------------------
@router.post("/get-email-by-mobile")
async def get_email_by_mobile(data: dict):
    mobile = data.get("mobile")

    user = await db.users.find_one({"mobile": mobile})
    if not user:
        return {"exists": False}

    return {
        "exists": True,
        "email": user["email"]
    }


@router.post("/sync-user")
async def sync_user(
    user: UserCreate,
    authorization: str = Header(...)
):
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)

    uid = decoded["uid"]
    email = decoded["email"]

    if await _is_ai_creator_identifier_blocked(email=email, mobile=user.mobile):
        raise HTTPException(status_code=403, detail="This email/mobile is blocked for AI Creator due to policy violation")

    existing = await db.users.find_one({"firebase_uid": uid})
    if existing:
        return {"message": "User already exists"}

    public_id = await next_public_user_id()

    user_doc = {
        "firebase_uid": uid,
        "public_id": public_id,
        "email": email,
        "full_name": user.full_name,
        "mobile": user.mobile,
        "username": user.username,
        "bio": user.bio,
        "location": user.location,
        "website": user.website,
        "image_name": user.image_name,
        "provider": user.provider,
        "followers": [],
        "following": [],
        "created_at": datetime.utcnow(),
        "two_factor_enabled": False  # Default to False
    }

    await db.users.insert_one(user_doc)
    await grant_welcome_bonus_if_eligible(uid, user_doc.get("created_at"))

    return {"message": "User saved"}


@router.post("/activity-ping")
async def activity_ping(authorization: str = Header(...)):
    try:
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


# --------------------------------------------------
# TEMPORARY USER MANAGEMENT
# --------------------------------------------------
@router.post("/ensure-temporary-user")
async def ensure_temporary_user(authorization: str = Header(...)):
    """Ensure user has a temporary username if they don't have one."""
    try:
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        from app.utils.generate_user_id import assign_temporary_username

        username = await assign_temporary_username(uid, db)

        return {
            "username": username,
            "is_temporary": username.startswith("temp_")
        }

    except Exception as e:
        print("ENSURE TEMPORARY USER ERROR:", str(e))
        raise HTTPException(status_code=500, detail=f"Failed to ensure temporary user: {str(e)}")


@router.post("/update-username")
async def update_username(request: dict, authorization: str = Header(...)):
    """Update user username after profile completion."""
    try:
        token = extract_token_from_header(authorization)
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        new_username = request.get("username", "").strip()
        if not new_username:
            raise HTTPException(status_code=400, detail="Username is required")

        from app.utils.generate_user_id import update_username as update_username_util

        result = await update_username_util(uid, new_username, db)

        if not result["success"]:
            raise HTTPException(status_code=400, detail=result["error"])

        return {
            "message": "Username updated successfully",
            "username": result["username"]
        }

    except HTTPException:
        raise
    except Exception as e:
        print("UPDATE USERNAME ERROR:", str(e))
        raise HTTPException(status_code=500, detail=f"Failed to update username: {str(e)}")


@router.get("/check-username/{username}")
async def check_username_availability(username: str):
    """Check if a username is available."""
    try:
        from app.utils.generate_user_id import validate_username

        if not validate_username(username):
            return {
                "available": False,
                "error": "Invalid username format. Use 3-30 characters, only letters, numbers, underscore, and dot."
            }

        # Check if username exists
        existing = await db.users.find_one({"username": username})
        if existing:
            return {"available": False, "error": "Username already taken"}

        return {"available": True}

    except Exception as e:
        print("CHECK USERNAME ERROR:", str(e))
        raise HTTPException(status_code=500, detail=f"Failed to check username: {str(e)}")
