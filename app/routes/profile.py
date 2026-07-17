from fastapi import APIRouter, Header, HTTPException
from bson import ObjectId
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header
from app.firebase import verify_firebase_token
from app.models.user import UserUpdate
from app.models.otp import SendEmailOtpRequest, VerifyEmailOtpRequest
from app.routes.auth_new import next_public_user_id
from datetime import datetime, timedelta
import logging
import re
from app.database import users_collection
from app.config import MOBILE_VERIFICATION_WINDOW_MINUTES, EMAIL_CHANGE_VERIFICATION_WINDOW_MINUTES
from app.services.otp_service import normalize_mobile
from app.services.email_otp_service import send_profile_email_otp, verify_profile_email_otp

router = APIRouter(prefix="/profile", tags=["Profile"])

# Configure logging if not already configured (safe for import)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s"
)
logger = logging.getLogger("kirnagram.profile")

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._]{3,30}$")
FULL_NAME_COOLDOWN_DAYS = 14



@router.get("/me")
async def get_my_profile(authorization: str = Header(...)):
    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")

        if not user_id:
            logger.warning("Invalid token payload: missing 'sub' field")
            raise HTTPException(status_code=401, detail="Invalid token")

        user = await db.users.find_one(
            {"_id": ObjectId(user_id)}
        )

        if not user:
            logger.info(f"User not found for user_id={user_id} in /profile/me")
            raise HTTPException(status_code=404, detail="User not found")

        # Ensure legacy accounts get a public_id
        if not user.get("public_id"):
            public_id = await next_public_user_id()
            await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"public_id": public_id}})
            user["public_id"] = public_id

        # Check if user is a creator (approved in ai_creator_applications)
        is_creator = False
        creator_app = await db.ai_creator_applications.find_one({"user_id": user_id, "status": "approved"})
        if creator_app:
            is_creator = True

        # Add is_creator to the returned user dict
        user["is_creator"] = is_creator

        # Convert ObjectId to string for JSON serialization
        user["_id"] = str(user["_id"])

        logger.info(f"Profile fetched for user_id={user_id}")
        return user

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token verification failed in /profile/me: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(e)}")
@router.get("/users/suggested")
async def get_suggested_users(authorization: str = Header(...)):
    if not authorization or " " not in authorization:
        logger.warning("Invalid authorization header in /profile/users/suggested")
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing user ID")
        
        logger.info(f"Suggested users requested by user_id={user_id}")

        # 1️⃣ Get current user document to access firebase_uid
        current_user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not current_user:
            raise HTTPException(status_code=404, detail="Current user not found")
        
        current_user_firebase_uid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        # 2️⃣ Get list of users current user already follows
        following_docs = await db.follows.find({
            "follower_id": current_user_firebase_uid,
            "status": "following"
        }).to_list(None)

        following_firebase_uids = [doc["following_id"] for doc in following_docs]

        # 3️⃣ Exclude:
        # - current user (by firebase_uid)
        # - already followed users (by firebase_uid)
        exclude_firebase_uids = following_firebase_uids + [current_user_firebase_uid]

        # Find users that are not followed and not current user.
        # Use a single $and condition to exclude both firebase_uid and _id values.
        exclude_object_ids = [ObjectId(uid) for uid in exclude_firebase_uids if ObjectId.is_valid(uid)]
        cursor = db.users.find({
            "$and": [
                {"firebase_uid": {"$nin": exclude_firebase_uids}},
                {"_id": {"$nin": exclude_object_ids}}
            ]
        }).limit(10)

        users = await cursor.to_list(length=10)

        result = []
        for user in users:
            firebase_uid = user.get("firebase_uid") or str(user.get("_id"))
            result.append({
                "firebase_uid": firebase_uid,
                "username": user.get("username"),
                "full_name": user.get("full_name"),
                "image_name": user.get("image_name"),
                "gender": user.get("gender"),
            })

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get suggested users: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to get suggested users: {str(e)}")


@router.get("/user/{user_id}")
async def get_user_profile(user_id: str, authorization: str = Header(...)):
    try:
        if not authorization or " " not in authorization:
            logger.warning("Invalid authorization header in /profile/user/{user_id}")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("Invalid authorization header format in /profile/user/{user_id} (not Bearer)")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]
        decoded = verify_firebase_token(token)
        viewer_id = decoded["uid"]

        target = await db.users.find_one(
            {"firebase_uid": user_id},
            {"_id": 0}
        )

        if not target:
            logger.info(f"User not found for uid={user_id} in /profile/user/{user_id}")
            raise HTTPException(status_code=404, detail="User not found")

        if target.get("account_type") == "private" and viewer_id != user_id:
            follow = await db.follows.find_one({
                "follower_id": viewer_id,
                "following_id": user_id,
                "status": "following"
            })
            if not follow:
                logger.info(f"Access denied to private account uid={user_id} by viewer={viewer_id}")
                raise HTTPException(status_code=403, detail="Private account")

        is_creator = False
        creator_app = await db.ai_creator_applications.find_one({"user_id": user_id, "status": "approved"})
        if creator_app:
            is_creator = True

        logger.info(f"Profile fetched for uid={user_id} by viewer={viewer_id}")
        return {
            "firebase_uid": target.get("firebase_uid"),
            "public_id": target.get("public_id"),
            "username": target.get("username"),
            "full_name": target.get("full_name"),
            "image_name": target.get("image_name"),
            "gender": target.get("gender"),
            "account_type": target.get("account_type"),
            "is_creator": is_creator,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Token verification failed in /profile/user/{user_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(e)}")


@router.get("/username/{username}")
async def get_user_by_username(username: str, authorization: str = Header(...)):
    """Lookup user by username and return their firebase_uid"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]
        decoded = verify_firebase_token(token)

        # Find user by username (case-insensitive)
        user = await db.users.find_one(
            {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
            {"firebase_uid": 1, "_id": 0}
        )

        if not user:
            logger.info(f"Username not found: {username}")
            raise HTTPException(status_code=404, detail="User not found")

        logger.info(f"Username lookup successful: {username} -> {user.get('firebase_uid')}")
        return {"firebase_uid": user.get("firebase_uid")}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Username lookup failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Username lookup failed: {str(e)}")


@router.get("/username-availability")
async def username_availability(username: str, authorization: str = Header(...)):
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        candidate = (username or "").strip()
        if not candidate:
            return {"available": False, "reason": "Username is required"}
        if not USERNAME_PATTERN.match(candidate):
            return {
                "available": False,
                "reason": "Use 3-30 letters, numbers, dot or underscore",
            }

        existing = await db.users.find_one(
            {
                "username": {"$regex": f"^{re.escape(candidate)}$", "$options": "i"},
                "firebase_uid": {"$ne": uid},
            },
            {"_id": 1},
        )

        return {"available": existing is None}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Username availability check failed", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Username check failed: {str(e)}")


def _extract_uid(authorization: str) -> str:
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    token = parts[1]
    payload = verify_access_token(token)
    uid = payload.get("sub")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return uid


@router.post("/send-email-otp")
async def send_profile_email_otp_route(payload: SendEmailOtpRequest, authorization: str = Header(...)):
    uid = _extract_uid(authorization)
    user = await db.users.find_one({"firebase_uid": uid}, {"email": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    normalized_email = payload.email.strip().lower()
    existing = await db.users.find_one(
        {"email": normalized_email, "firebase_uid": {"$ne": uid}},
        {"_id": 1},
    )
    if existing:
        raise HTTPException(status_code=400, detail="Email already in use")

    return await send_profile_email_otp(normalized_email)


@router.post("/verify-email-otp")
async def verify_profile_email_otp_route(payload: VerifyEmailOtpRequest, authorization: str = Header(...)):
    uid = _extract_uid(authorization)
    normalized_email = payload.email.strip().lower()

    existing = await db.users.find_one(
        {"email": normalized_email, "firebase_uid": {"$ne": uid}},
        {"_id": 1},
    )
    if existing:
        raise HTTPException(status_code=400, detail="Email already in use")

    result = await verify_profile_email_otp(normalized_email, payload.otp)
    await db.users.update_one(
        {"firebase_uid": uid},
        {
            "$set": {
                "email_change_verified": {
                    "email": normalized_email,
                    "verified_at": datetime.utcnow(),
                }
            }
        },
    )
    return result


@router.put("/update")
async def update_profile(
    data: UserUpdate,
    authorization: str = Header(...)
):
    try:
        # Extract token from "Bearer {token}" format
        if not authorization or " " not in authorization:
            logger.warning("Invalid authorization header format in /profile/update")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("Invalid authorization header format in /profile/update (not Bearer)")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        # Build update data. Allow explicit nulls (e.g., removing image/cover) while
        # ignoring fields that were not provided.
        update_data = data.dict(exclude_unset=True)
        # Remove skip_notification from update data (it's not a user field)
        skip_notification = update_data.pop("skip_notification", False)
        unset_data = {}

        current_user = await db.users.find_one({"firebase_uid": uid})
        if not current_user:
            raise HTTPException(status_code=404, detail="User not found")

        username = update_data.get("username")
        if isinstance(username, str):
            cleaned_username = username.strip()
            update_data["username"] = cleaned_username

            if cleaned_username:
                if not USERNAME_PATTERN.match(cleaned_username):
                    raise HTTPException(
                        status_code=400,
                        detail="Username must be 3-30 chars and only letters, numbers, dot or underscore",
                    )

                existing = await db.users.find_one(
                    {
                        "username": {"$regex": f"^{re.escape(cleaned_username)}$", "$options": "i"},
                        "firebase_uid": {"$ne": uid},
                    },
                    {"_id": 1},
                )
                if existing:
                    raise HTTPException(status_code=409, detail="Username already used")

                current_username = (current_user.get("username") or "").strip()
                if cleaned_username != current_username:
                    username_change_count = int(current_user.get("username_change_count", 0) or 0)
                    update_data["username_change_count"] = username_change_count + 1

        public_id = update_data.get("public_id")
        if isinstance(public_id, str):
            cleaned_public_id = public_id.strip().lower()
            update_data["public_id"] = cleaned_public_id

            if cleaned_public_id:
                if not re.match(r"^k\d{4}$", cleaned_public_id):
                    raise HTTPException(
                        status_code=400,
                        detail="Public ID must be in format k0001 (lowercase k + 4 digits)",
                    )

                existing_pid = await db.users.find_one(
                    {
                        "public_id": {"$regex": f"^{re.escape(cleaned_public_id)}$", "$options": "i"},
                        "firebase_uid": {"$ne": uid},
                    },
                    {"_id": 1},
                )
                if existing_pid:
                    raise HTTPException(status_code=409, detail="Public ID already used")

        full_name = update_data.get("full_name")
        if isinstance(full_name, str):
            cleaned_full_name = full_name.strip()
            current_full_name = (current_user.get("full_name") or "").strip()
            if cleaned_full_name and cleaned_full_name != current_full_name:
                existing_updated_at = current_user.get("full_name_updated_at")
                if isinstance(existing_updated_at, str):
                    try:
                        existing_updated_at = datetime.fromisoformat(existing_updated_at.replace("Z", "+00:00"))
                    except ValueError:
                        existing_updated_at = None
                if isinstance(existing_updated_at, datetime):
                    elapsed = datetime.utcnow() - existing_updated_at
                    if elapsed < timedelta(days=FULL_NAME_COOLDOWN_DAYS):
                        remaining_seconds = (timedelta(days=FULL_NAME_COOLDOWN_DAYS) - elapsed).total_seconds()
                        remaining_days = max(1, int((remaining_seconds + 86399) // 86400))
                        raise HTTPException(
                            status_code=403,
                            detail={
                                "code": "FULL_NAME_COOLDOWN",
                                "message": f"You can change full name again in {remaining_days} day(s)",
                                "remaining_days": remaining_days,
                            },
                        )
                update_data["full_name"] = cleaned_full_name
                update_data["full_name_updated_at"] = datetime.utcnow()
            else:
                update_data["full_name"] = cleaned_full_name

        mobile = update_data.get("mobile")
        if isinstance(mobile, str):
            normalized_mobile = normalize_mobile(mobile)
            if len(normalized_mobile) != 10:
                raise HTTPException(status_code=400, detail="Invalid mobile number")
            current_mobile = current_user.get("mobile")
            update_data["mobile"] = normalized_mobile
            existing_mobile = await db.users.find_one({"mobile": normalized_mobile, "firebase_uid": {"$ne": uid}}, {"_id": 1})
            if existing_mobile:
                raise HTTPException(status_code=400, detail="Mobile already in use")

            if current_mobile and current_mobile != normalized_mobile:
                mobile_change_verified = current_user.get("mobile_change_verified") or {}
                verified_mobile = mobile_change_verified.get("mobile")
                verified_at = mobile_change_verified.get("verified_at")

                if verified_mobile != current_mobile:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "MOBILE_CHANGE_OTP_REQUIRED",
                            "message": "Please verify your current mobile number before updating to a new number",
                        },
                    )

                if not isinstance(verified_at, datetime) or datetime.utcnow() - verified_at > timedelta(minutes=MOBILE_VERIFICATION_WINDOW_MINUTES):
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "MOBILE_CHANGE_OTP_EXPIRED",
                            "message": "Current mobile verification expired. Please verify your current number again.",
                        },
                    )

        email = update_data.get("email")
        if isinstance(email, str):
            cleaned_email = email.strip().lower()
            current_email = (current_user.get("email") or "").strip().lower()
            if cleaned_email:
                if current_email and current_email != cleaned_email:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "EMAIL_ALREADY_SET",
                            "message": "Email is already set and cannot be changed.",
                        },
                    )

                if not current_email:
                    existing_email = await db.users.find_one({"email": cleaned_email, "firebase_uid": {"$ne": uid}}, {"_id": 1})
                    if existing_email:
                        raise HTTPException(status_code=400, detail="Email already in use")

                update_data["email"] = cleaned_email
            else:
                if current_email:
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "code": "EMAIL_CANNOT_BE_CLEARED",
                            "message": "Email is already set and cannot be cleared.",
                        },
                    )
                update_data.pop("email", None)

        # Explicitly allow other profile fields to update without extra OTP verification
        for field in ["bio", "location", "website", "website_name", "gender", "image_name", "cover_image", "account_type", "instagram", "youtube", "facebook", "x", "linkedin", "whatsapp"]:
            if field in update_data:
                update_data[field] = update_data[field]

        update_ops = {}
        if update_data:
            update_ops["$set"] = update_data
        if unset_data:
            update_ops["$unset"] = unset_data

        if update_ops:
            await db.users.update_one(
                {"firebase_uid": uid},
                update_ops
            )

        logger.info(f"Profile updated for uid={uid} fields={list(update_data.keys())}")

        # Only create notification if skip_notification is False (final save)
        if not skip_notification:
            # Get updated user info for notification
            updated_user = await db.users.find_one({"firebase_uid": uid})
            # Create notification for profile update
            notification_doc = {
                "user_id": uid,
                "from_user_id": uid,
                "from_user_name": updated_user.get("full_name", "User"),
                "from_user_username": updated_user.get("username"),
                "from_user_image": updated_user.get("image_name"),
                "from_user_gender": updated_user.get("gender"),
                "action": "profile_updated",
                "description": "Updated their profile",
                "follow_status": "none",
                "timestamp": datetime.utcnow()
            }
            await db.notifications.insert_one(notification_doc)
            logger.info(f"Profile update notification created for uid={uid}")

        return {"message": "Profile updated successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Profile update failed for uid={uid if 'uid' in locals() else '?'}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Profile update failed: {str(e)}")


@router.get("/about/{user_id}")
async def get_about_account(user_id: str, authorization: str = Header(...)):
    """Get 'About this account' information including joined date and change counts"""
    try:
        if not authorization or " " not in authorization:
            logger.warning("Invalid authorization header in /profile/about/{user_id}")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("Invalid authorization header format in /profile/about/{user_id} (not Bearer)")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]
        decoded = verify_firebase_token(token)
        viewer_id = decoded["uid"]

        # Fetch target user by multiple identifiers for compatibility with old records.
        target_user = await db.users.find_one({
            "$or": [
                {"firebase_uid": user_id},
                {"uid": user_id},
                {"user_id": user_id},
                {"username": user_id},
            ]
        })
        if not target_user:
            logger.warning(f"User not found for identifier={user_id} in /profile/about/{user_id}; returning defaults")
            return {
                "firebase_uid": user_id,
                "username": None,
                "full_name": None,
                "image_name": None,
                "gender": None,
                "joined_date": None,
                "full_name_change_count": 0,
                "username_change_count": 0,
                "bio": None,
                "location": None,
                "website": None,
            }

        # Get joined date
        joined_date = target_user.get("created_at")
        if isinstance(joined_date, str):
            joined_date = datetime.fromisoformat(joined_date.replace('Z', '+00:00'))
        
        # Get change counts
        full_name_change_count = int(target_user.get("full_name_change_count", 0) or 0)
        username_change_count = int(target_user.get("username_change_count", 0) or 0)

        logger.info(f"About account fetched for uid={user_id} by viewer={viewer_id}")
        
        return {
            "firebase_uid": user_id,
            "username": target_user.get("username"),
            "full_name": target_user.get("full_name"),
            "image_name": target_user.get("image_name"),
            "gender": target_user.get("gender"),
            "joined_date": joined_date.isoformat() if joined_date else None,
            "full_name_change_count": full_name_change_count,
            "username_change_count": username_change_count,
            "bio": target_user.get("bio"),
            "location": target_user.get("location"),
            "website": target_user.get("website"),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"About account fetch failed for uid={user_id if 'user_id' in locals() else '?'}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"About fetch failed: {str(e)}")


@router.get("/stats")
async def get_profile_stats(authorization: str = Header(...)):
    try:
        # Extract token from "Bearer {token}" format
        if not authorization or " " not in authorization:
            logger.warning("Invalid authorization header format in /profile/stats")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            logger.warning("Invalid authorization header format in /profile/stats (not Bearer)")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        posts = await db.posts.count_documents({"user_id": uid, "is_prompt_post": {"$ne": True}})
        prompts = await db.posts.count_documents({"user_id": uid, "is_prompt_post": True})
        followers = await db.follows.count_documents({"following_id": uid, "status": "following"})
        following = await db.follows.count_documents({"follower_id": uid, "status": "following"})

        logger.info(f"Stats fetched for uid={uid}")
        return {
            "posts": posts,
            "prompts": prompts,
            "followers": followers,
            "following": following
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Stats fetch failed for uid={uid if 'uid' in locals() else '?'}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Stats fetch failed: {str(e)}")


@router.get("/creators/all")
async def get_all_creators(authorization: str = Header(...)):
    """Get all approved AI creators with their total remix counts."""
    try:
        if not authorization or " " not in authorization:
            logger.warning("Invalid authorization header in /profile/creators/all")
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        
        # Get all approved AI creator applications
        creator_apps = await db.ai_creator_applications.find(
            {"status": "approved"}
        ).to_list(None)
        
        creator_ids = [app["user_id"] for app in creator_apps]
        
        # Get user profiles for these creators
        creators = await db.users.find(
            {"firebase_uid": {"$in": creator_ids}}
        ).to_list(None)
        
        result = []
        for creator in creators:
            creator_uid = creator.get("firebase_uid")

            # Rank creators by remix usage on their prompts (creator performance),
            # aligned with the earnings logic used across the app.
            creator_prompts = await db.ai_creator_prompts.find(
                {"user_id": creator_uid},
                {"_id": 0, "remix_count": 1, "remixes": 1},
            ).to_list(length=None)

            total_remixes = 0
            for prompt in creator_prompts:
                remix_count = int(prompt.get("remix_count") or 0)
                if remix_count <= 0:
                    remixes_arr = prompt.get("remixes", [])
                    remix_count = len(remixes_arr) if isinstance(remixes_arr, list) else 0
                total_remixes += remix_count
            
            result.append({
                "firebase_uid": creator_uid,
                "username": creator.get("username"),
                "public_id": creator.get("public_id"),
                "full_name": creator.get("full_name"),
                "image_name": creator.get("image_name"),
                "gender": creator.get("gender"),
                "is_creator": True,
                "total_remix_count": total_remixes
            })
        
        logger.info(f"Returned {len(result)} AI creators with remix counts")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch creators: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch creators: {str(e)}")


# 🔍 DEBUG - Change account type to PUBLIC
@router.post("/debug/make-public")
async def debug_make_public(authorization: str = Header(...)):
    """DEBUG: Change current user's account to PUBLIC"""
    try:
        if not authorization or " " not in authorization:
            logger.warning("Invalid authorization header in /profile/debug/make-public")
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]
        result = await db.users.update_one(
            {"firebase_uid": uid},
            {"$set": {"account_type": "public"}}
        )
        user = await db.users.find_one({"firebase_uid": uid})
        logger.info(f"Account type set to PUBLIC for uid={uid}")
        return {
            "status": "success",
            "message": "Account changed to PUBLIC",
            "modified_count": result.modified_count,
            "user": {
                "username": user.get("username"),
                "account_type": user.get("account_type"),
                "following": user.get("following", []),
                "followers": user.get("followers", [])
            }
        }
    except Exception as e:
        logger.error(f"Failed to set account public for uid={uid if 'uid' in locals() else '?'}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed: {str(e)}")

@router.post("/debug/sync-follow-arrays")
async def debug_sync_follow_arrays(authorization: str = Header(...)):
    """
    DEBUG / ADMIN TOOL

    Rebuild users.following and users.followers arrays
    from the db.follows collection.

    WHY THIS EXISTS:
    - Stories feed depends on users.following
    - Follow system mainly uses db.follows
    - This endpoint fixes mismatches

    ⚠️ Should be used only for debugging or migration
    """
    try:
        # 🔐 Verify token
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")

        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        caller_uid = decoded["uid"]

        logger.info(f"🔧 Sync-follow-arrays triggered by user: {caller_uid}")

        # 📦 Get all users
        users = await db.users.find({}).to_list(None)
        updated_count = 0

        for user in users:
            uid = user.get("firebase_uid")
            if not uid:
                continue

            # 👉 Users this user is FOLLOWING
            following_docs = await db.follows.find({
                "follower_id": uid,
                "status": "following"
            }).to_list(None)

            following_list = [doc["following_id"] for doc in following_docs]

            # 👉 Users who FOLLOW this user
            followers_docs = await db.follows.find({
                "following_id": uid,
                "status": "following"
            }).to_list(None)

            followers_list = [doc["follower_id"] for doc in followers_docs]

            # 🔄 Update user document
            await db.users.update_one(
                {"firebase_uid": uid},
                {
                    "$set": {
                        "following": following_list,
                        "followers": followers_list
                    }
                }
            )

            updated_count += 1

            logger.info(
                f"✅ Synced user {uid} | "
                f"following={len(following_list)} | "
                f"followers={len(followers_list)}"
            )

        return {
            "status": "success",
            "message": "Synced following/followers arrays from follows collection",
            "users_updated": updated_count
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("❌ Failed to sync follow arrays", exc_info=True)
        raise HTTPException(
            status_code=400,
            detail=f"Sync failed: {str(e)}"
        )