# ================= IMAGE PROXY ENDPOINT (for CORS) =====================
import httpx
from fastapi.responses import StreamingResponse
from fastapi import APIRouter as _APIRouter, HTTPException
posts_router = _APIRouter(prefix="/posts", tags=["Posts"])

@posts_router.get("/image-proxy")
async def image_proxy(url: str):
    """Proxy remote images to avoid CORS issues (for story upload, etc)"""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return StreamingResponse(
                resp.aiter_bytes(),
                media_type=resp.headers.get("content-type", "image/jpeg")
            )
    except Exception as e:
        logger.error(f"Image proxy error: {str(e)}")
        raise HTTPException(status_code=404, detail="Image not found")
from fastapi import APIRouter, UploadFile, File, Header, HTTPException, Query
from app.r2 import s3, BUCKET_NAME, PUBLIC_BASE
from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token
from app.database import db
from typing import List
from app.models.story import (
    StoryCreate, StoryResponse, MyStoryResponse, 
    FriendStoriesResponse, StoryStats
)
from datetime import datetime, timedelta
from io import BytesIO
import logging
from bson import ObjectId
import json
from app.models.story import StoryResponse, FriendStoriesResponse
from app.services.media_processing import process_uploaded_image_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stories", tags=["Stories"])


async def ensure_user_public_id(user_doc: dict) -> str | None:
    """Return existing public_id or assign one for legacy users that don't have it yet."""
    if not user_doc:
        return None

    existing = (user_doc.get("public_id") or "").strip()
    if existing:
        return existing

    firebase_uid = user_doc.get("firebase_uid")
    if not firebase_uid:
        return None

    from app.routes.auth import next_public_user_id

    public_id = await next_public_user_id()
    await db.users.update_one({"_id": ObjectId(firebase_uid)}, {"$set": {"public_id": public_id}})
    user_doc["public_id"] = public_id
    return public_id

# 🖼️ Helper function to get full profile image URL
def get_profile_image_url(image_name: str) -> str:
    """Convert image_name to full URL, with default fallback"""
    if not image_name:
        # Return default profile image using ui-avatars.com (more reliable)
        return "https://ui-avatars.com/api/?name=User&background=6366f1&color=fff&size=128"
    # If it's already a full URL, return as-is
    if image_name.startswith("http"):
        return image_name
    # Otherwise, combine with PUBLIC_BASE
    return f"{PUBLIC_BASE}/{image_name}"


async def create_story_notification(
    to_user_id: str,
    from_user: dict,
    story: dict,
    description: str,
):
    if to_user_id == from_user.get("firebase_uid"):
        return

    notif_doc = {
        "user_id": to_user_id,
        "from_user_id": from_user.get("firebase_uid"),
        "from_user_name": from_user.get("full_name", "User"),
        "from_user_username": from_user.get("username"),
        "from_user_image": from_user.get("image_name"),
        "from_user_gender": from_user.get("gender"),
        "action": "story_liked",
        "description": description,
        "story_id": str(story.get("_id")),
        "story_owner_id": story.get("user_id"),
        "story_media": story.get("media_url"),
        "timestamp": datetime.utcnow(),
    }

    await db.notifications.insert_one(notif_doc)


# 📤 Helper function to upload story media to R2
async def upload_story_media(file: UploadFile, user_id: str, media_type: str) -> str:
    """Upload optimized story media to R2 and return a public URL."""
    try:
        logger.info(f"📤 Starting story media upload for user: {user_id}")

        if not file.filename or "." not in file.filename:
            raise ValueError("Invalid filename: no extension found")

        ext = file.filename.split(".")[-1].lower()
        logger.info(f"📄 File extension: {ext}")

        valid_image_exts = ['jpg', 'jpeg', 'png', 'webp', 'gif']
        valid_video_exts = ['mp4', 'webm', 'mov']

        if media_type == 'image' and ext not in valid_image_exts:
            raise ValueError(f"Invalid image format: {ext}")
        if media_type == 'video' and ext not in valid_video_exts:
            raise ValueError(f"Invalid video format: {ext}")

        file_content = await file.read()
        logger.info(f"📦 File size: {len(file_content)} bytes")
        if len(file_content) > 12 * 1024 * 1024:
            raise ValueError("File is too large. Please upload a file smaller than 12MB")

        if media_type == 'image':
            processed_bytes, content_type, output_ext = process_uploaded_image_bytes(
                file_content,
                file.filename,
                content_type=file.content_type,
                max_width=1600,
                max_height=1600,
                quality=85,
                target_size_kb=900,
            )
            ext = output_ext
            content_type = content_type
            file_content = processed_bytes
        else:
            content_type = file.content_type or f"{media_type}/{ext}"

        timestamp = int(datetime.utcnow().timestamp() * 1000)
        filename = f"stories/{user_id}/{timestamp}.{ext}"
        logger.info(f"📁 R2 path: {filename}")

        try:
            s3.upload_fileobj(
                BytesIO(file_content),
                BUCKET_NAME,
                filename,
                ExtraArgs={"ContentType": content_type}
            )
            logger.info(f"✅ File uploaded to R2 successfully")
        except Exception as upload_error:
            logger.error(f"❌ R2 Upload failed: {str(upload_error)}")
            raise ValueError(f"Failed to upload to R2: {str(upload_error)}")

        base_url = PUBLIC_BASE.rstrip("/")
        public_url = f"{base_url}/{filename}"
        logger.info(f"🔗 Public URL: {public_url}")

        return public_url

    except Exception as e:
        logger.error(f"❌ Error uploading story media: {str(e)}", exc_info=True)
        raise ValueError(f"Failed to upload story media: {str(e)}")


# 🏥 HEALTH CHECK
@router.get("/health")
async def health_check():
    """Check if stories API is working"""
    try:
        # Test database connection
        test_doc = await db.stories.find_one({})
        return {
            "status": "ok",
            "message": "Stories API is healthy",
            "database": "connected",
            "stories_count": await db.stories.count_documents({})
        }
    except Exception as e:
        logger.error(f"❌ Health check failed: {str(e)}")
        return {
            "status": "error",
            "message": f"Health check failed: {str(e)}",
            "database": "disconnected"
        }


# 🔍 DEBUG - Check user's following/followers
@router.get("/debug/user-relationships")
async def debug_user_relationships(authorization: str = Header(...)):
    """Debug endpoint to check user's following/followers"""
    try:
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            return {"error": "User not found"}
        
        following = user.get("following", [])
        followers = user.get("followers", [])
        
        # Get details of people user is following
        following_details = []
        for uid in following:
            u = await db.users.find_one({"_id": ObjectId(uid)})
            if u:
                following_details.append({
                    "uid": uid,
                    "username": u.get("username"),
                    "account_type": u.get("account_type", "public")
                })
        
        # Get details of followers
        followers_details = []
        for uid in followers:
            u = await db.users.find_one({"_id": ObjectId(uid)})
            if u:
                followers_details.append({
                    "uid": uid,
                    "username": u.get("username")
                })
        
        return {
            "user_id": user_id,
            "username": user.get("username"),
            "following_count": len(following),
            "followers_count": len(followers),
            "following": following_details,
            "followers": followers_details
        }
    except Exception as e:
        return {"error": str(e)}


# � DEBUG - Verify token
@router.get("/debug/verify-token")
async def debug_verify_token(authorization: str = Header(...)):
    """Debug endpoint to verify Firebase token"""
    try:
        if not authorization or " " not in authorization:
            return {"status": "error", "message": "Invalid authorization header format"}
        
        token = authorization.split(" ")[1]
        logger.info(f"🔍 Verifying token (first 50 chars): {token[:50]}...")
        
        decoded = verify_firebase_token(token)
        
        return {
            "status": "ok",
            "message": "Token verified successfully",
            "user_id": decoded.get("uid"),
            "email": decoded.get("email")
        }
    except Exception as e:
        logger.error(f"❌ Token verification failed: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"Token verification failed: {str(e)}"
        }



async def check_story_access(
    story_id: str,
    viewer_user_id: str
) -> tuple[bool, str]:
    """
    Story access control with public/private account support

    RULES:
    1. Story owner can always view their own stories
    2. Story must not be expired
    3. PUBLIC account: anyone can view their stories
    4. PRIVATE account: only followers can view their stories
    """

    try:
        # 1️⃣ Fetch story
        story = await db.stories.find_one({"_id": ObjectId(story_id)})
        if not story:
            return False, "Story not found"

        owner_id = story["user_id"]

        # 2️⃣ Owner always allowed
        if viewer_user_id == owner_id:
            return True, "Owner access"

        # 3️⃣ Expiry check
        if story.get("expires_at") < datetime.utcnow():
            return False, "Story expired"

        # 4️⃣ Check story owner's account type
        owner = await db.users.find_one({"_id": ObjectId(owner_id)})
        if not owner:
            return False, "Story owner not found"

        # 5️⃣ PUBLIC account: anyone can view
        if owner.get("account_type") != "private":
            return True, "Public account access"

        # 6️⃣ PRIVATE account: must be following
        follow = await db.follows.find_one({
            "follower_id": viewer_user_id,
            "following_id": owner_id,
            "status": "following"
        })

        if not follow:
            return False, "You must follow this user to view their stories"

        # ✅ ALL GOOD
        return True, "Access granted"

    except Exception as e:
        logger.error(
            f"❌ check_story_access failed: {str(e)}",
            exc_info=True
        )
        return False, "Story access check failed"


# 🎬 CREATE STORY
@router.post("/create")
async def create_story(
    file: UploadFile = File(...),
    media_type: str = Query(...),  # "image" or "video"
    duration: int = Query(...),     # seconds
    text: str = Query(None),
    emoji_stickers: str = Query(None),  # JSON string
    drawing_data: str = Query(None),
    music_url: str = Query(None),
    visibility: str = Query("public"),
    authorization: str = Header(...)
):
    """Create and upload a story"""
    try:
        # 1️⃣ Verify user
        if not authorization or " " not in authorization:
            logger.error(f"❌ Invalid authorization header format: {authorization}")
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        logger.info(f"🔍 Verifying token for story creation...")
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        logger.info(f"✅ User authenticated: {user_id}")
        
        # 2️⃣ Validate duration
        if media_type == "image" and duration > 10:
            raise ValueError("Image stories have a maximum duration of 10 seconds")
        if media_type == "video" and duration > 120:
            raise ValueError("Video stories have a maximum duration of 120 seconds (2 minutes)")
        
        # 3️⃣ Upload media to R2
        media_url = await upload_story_media(file, user_id, media_type)
        
        # 4️⃣ Parse emoji stickers if provided
        emoji_stickers_list = []
        if emoji_stickers:
            try:
                emoji_stickers_list = json.loads(emoji_stickers)
            except:
                emoji_stickers_list = []
        
        # 5️⃣ Create story document
        now = datetime.utcnow()
        expires_at = now + timedelta(hours=24)
        
        story_doc = {
            "_id": ObjectId(),
            "user_id": user_id,
            "media_url": media_url,
            "media_type": media_type,
            "duration": duration,
            "text": text or None,
            "emoji_stickers": emoji_stickers_list or None,
            "drawing_data": drawing_data or None,
            "music_url": music_url or None,
            "visibility": visibility,
            "created_at": now,
            "expires_at": expires_at,
            "views": [],  # [{"user_id": "...", "viewed_at": "..."}, ...]
            "likes": []   # [{"user_id": "...", "liked_at": "..."}, ...]
        }
        
        # 6️⃣ Save to MongoDB
        result = await db.stories.insert_one(story_doc)
        story_id = str(result.inserted_id)
        
        # Get user info for better logging
        user_doc = await db.users.find_one({"_id": ObjectId(user_id)})
        username = user_doc.get("username", "Unknown") if user_doc else "Unknown"
        
        logger.info(f"✅ Story created successfully:")
        logger.info(f"   📖 Story ID: {story_id}")
        logger.info(f"   👤 User: @{username} ({user_id})")
        logger.info(f"   📷 Media: {media_type}")
        logger.info(f"   🔗 URL: {media_url}")
        logger.info(f"   ⏱️ Duration: {duration}s")
        logger.info(f"   📅 Expires: {expires_at}")
        
        # 7️⃣ Get follower count for distribution info
        if user_doc:
            followers = user_doc.get("followers", [])
            logger.info(f"   📤 Will be delivered to {len(followers)} followers")
        
        return {
            "story_id": story_id,
            "media_url": media_url,
            "message": "Story created successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error creating story: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Failed to create story: {str(e)}")


# 👁️ VIEW STORY (Track view)
@router.post("/view/{story_id}")
async def view_story(
    story_id: str,
    authorization: str = Header(...)
):
    """Mark story as viewed by user (with privacy check)"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        # Check privacy access
        has_access, reason = await check_story_access(story_id, user_id)
        if not has_access:
            logger.warning(f"⚠️ User {user_id} denied access to story {story_id}: {reason}")
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")
        
        # Find story
        story = await db.stories.find_one({"_id": ObjectId(story_id)})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        # Check if user already viewed
        already_viewed = any(v["user_id"] == user_id for v in story.get("views", []))
        
        if not already_viewed:
            await db.stories.update_one(
                {"_id": ObjectId(story_id)},
                {"$push": {"views": {"user_id": user_id, "viewed_at": datetime.utcnow()}}}
            )
            logger.info(f"✅ Story view tracked: {user_id} viewed {story_id}")
        
        return {"message": "Story view tracked"}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error viewing story: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Failed to track view: {str(e)}")


# ❤️ LIKE STORY
@router.post("/like/{story_id}")
async def like_story(
    story_id: str,
    authorization: str = Header(...)
):
    """Like/unlike a story (with privacy check)"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        # Check privacy access
        has_access, reason = await check_story_access(story_id, user_id)
        if not has_access:
            logger.warning(f"⚠️ User {user_id} denied access to like story {story_id}: {reason}")
            raise HTTPException(status_code=403, detail=f"Access denied: {reason}")
        
        story = await db.stories.find_one({"_id": ObjectId(story_id)})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        # Check if already liked
        already_liked = any(l["user_id"] == user_id for l in story.get("likes", []))
        
        if already_liked:
            # Unlike
            await db.stories.update_one(
                {"_id": ObjectId(story_id)},
                {"$pull": {"likes": {"user_id": user_id}}}
            )
            logger.info(f"👍 Story unliked: {user_id} unliked {story_id}")
            liked = False
        else:
            # Like
            await db.stories.update_one(
                {"_id": ObjectId(story_id)},
                {"$push": {"likes": {"user_id": user_id, "liked_at": datetime.utcnow()}}}
            )
            logger.info(f"❤️ Story liked: {user_id} liked {story_id}")
            liked = True

            # Create notification for story owner
            story_owner_id = story.get("user_id")
            if story_owner_id and story_owner_id != user_id:
                liker_user = await db.users.find_one({"_id": ObjectId(user_id)})
                if liker_user:
                    description = f"{liker_user.get('full_name', 'User')} liked your story"
                    await create_story_notification(
                        to_user_id=story_owner_id,
                        from_user=liker_user,
                        story=story,
                        description=description,
                    )
        
        return {"liked": liked}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error liking story: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Failed to like story: {str(e)}")


# 🗑️ DELETE STORY
@router.delete("/delete/{story_id}")
async def delete_story(
    story_id: str,
    authorization: str = Header(...)
):
    """Delete a story (only by owner)"""
    try:
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        story = await db.stories.find_one({"_id": ObjectId(story_id)})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        if story["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="You can only delete your own stories")
        
        await db.stories.delete_one({"_id": ObjectId(story_id)})
        
        logger.info(f"✅ Story deleted: {story_id}")
        return {"message": "Story deleted successfully"}
    
    except Exception as e:
        logger.error(f"❌ Error deleting story: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


# 📊 GET MY STORIES
@router.get(   "/my-stories",
    response_model=List[MyStoryResponse]
)
async def get_my_stories(authorization: str = Header(...)):
    """Get all stories posted by current user"""
    try:
        # 1️⃣ Validate and extract token
        if not authorization or " " not in authorization:
            logger.error(f"❌ Invalid authorization header format: {authorization}")
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = authorization.split(" ")[1]
        logger.info(f"🔍 Attempting to verify token...")
        
        # 2️⃣ Verify Firebase token
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        logger.info(f"✅ User authenticated: {user_id}")
        
        # 3️⃣ Find all non-expired stories by user
        now = datetime.utcnow()
        stories = await db.stories.find({
            "user_id": user_id,
            "expires_at": {"$gt": now}
        }).sort("created_at", 1).to_list(None)
        
        logger.info(f"📊 Found {len(stories)} active stories for user {user_id}")
        
        # 4️⃣ Format response
        my_stories = []
        for story in stories:
            remaining_hours = int((story["expires_at"] - now).total_seconds() / 3600)
            my_stories.append({
                "story_id": str(story["_id"]),
                "media_url": story["media_url"],
                "media_type": story["media_type"],
                "created_at": story["created_at"],
                "expires_at": story["expires_at"],
                "views_count": len(story.get("views", [])),
                "likes_count": len(story.get("likes", [])),
                "remaining_hours": remaining_hours
            })
        
        logger.info(f"✅ Returning {len(my_stories)} stories")
        return my_stories
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching my stories: {str(e)}", exc_info=True)
        raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")


@router.get("/user/{target_user_id}/active")
async def get_user_active_story(
    target_user_id: str,
    authorization: str = Header(...)
):
    """Return active story status for a specific user, respecting private-account visibility."""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")

        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        viewer_id = decoded["uid"]

        target_user = await db.users.find_one({"_id": ObjectId(target_user_id)})
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        can_view = viewer_id == target_user_id
        if not can_view and target_user.get("account_type") != "private":
            can_view = True

        if not can_view:
            follow_doc = await db.follows.find_one({
                "follower_id": viewer_id,
                "following_id": target_user_id,
                "status": "following"
            })
            can_view = follow_doc is not None

        if not can_view:
            return {
                "target_user_id": target_user_id,
                "can_view": False,
                "has_active_story": False,
                "first_story_id": None,
                "stories": []
            }

        now = datetime.utcnow()
        stories = await db.stories.find({
            "user_id": target_user_id,
            "expires_at": {"$gt": now}
        }).sort("created_at", 1).to_list(None)

        first_story_id = str(stories[0]["_id"]) if stories else None

        return {
            "target_user_id": target_user_id,
            "can_view": True,
            "has_active_story": len(stories) > 0,
            "first_story_id": first_story_id,
            "stories": [
                {
                    "story_id": str(story["_id"]),
                    "created_at": story.get("created_at"),
                    "expires_at": story.get("expires_at")
                }
                for story in stories
            ]
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error checking active story for user={target_user_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))

@router.get(
    "/feed",
    response_model=List[FriendStoriesResponse]
)
async def get_stories_feed(authorization: str = Header(...)):
    try:
        # 🔐 AUTH
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")

        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]

        logger.info(f"🔍 FEED REQUEST from user: {user_id}")

        # 👤 CURRENT USER
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # ============================================================
        # STEP 1: GET FOLLOWING (SINGLE SOURCE OF TRUTH)
        # ============================================================
        follow_docs = await db.follows.find({
            "follower_id": user_id,
            "status": "following"
        }).to_list(None)

        following_ids = [doc["following_id"] for doc in follow_docs]

        logger.info(f"📋 Following IDs: {following_ids}")

        # ============================================================
        # STEP 2: ALLOWED STORY OWNERS
        # ============================================================
        # Own stories always allowed
        allowed_story_users = [user_id]

        # If you follow someone → you can see their stories
        allowed_story_users.extend(following_ids)

        logger.info(f"✅ Allowed story users: {allowed_story_users}")

        # ============================================================
        # STEP 3: FETCH STORIES
        # ============================================================
        now = datetime.utcnow()

        stories = await db.stories.find({
            "user_id": {"$in": allowed_story_users},
            "expires_at": {"$gt": now}
        }).sort("created_at", -1).to_list(None)

        logger.info(f"📊 Found {len(stories)} stories")

        # ============================================================
        # STEP 4: GROUP STORIES
        # ============================================================
        user_stories = {}

        for story in stories:
            owner_id = story["user_id"]

            if owner_id not in user_stories:
                owner = await db.users.find_one({"_id": ObjectId(owner_id)})
                if not owner:
                    continue

                owner_public_id = await ensure_user_public_id(owner)

                user_stories[owner_id] = {
                    "user_id": owner_id,
                    "username": owner.get("username", "Unknown"),
                    "public_id": owner_public_id,
                    "full_name": owner.get("full_name"),
                    "user_image": get_profile_image_url(owner.get("image_name")),
                    "gender": owner.get("gender"),
                    "stories": [],
                    "unviewed_count": 0
                }

            viewed = any(v["user_id"] == user_id for v in story.get("views", []))
            liked = any(l["user_id"] == user_id for l in story.get("likes", []))

            user_stories[owner_id]["stories"].append({
                "story_id": str(story["_id"]),
                "user_id": owner_id,
                "username": user_stories[owner_id]["username"],
                "public_id": user_stories[owner_id]["public_id"],
                "full_name": user_stories[owner_id]["full_name"],
                "user_image": user_stories[owner_id]["user_image"],
                "media_url": story["media_url"],
                "media_type": story["media_type"],
                "duration": story["duration"],
                "text": story.get("text"),
                "emoji_stickers": story.get("emoji_stickers"),
                "drawing_data": story.get("drawing_data"),
                "music_url": story.get("music_url"),
                "created_at": story["created_at"],
                "expires_at": story["expires_at"],
                "views_count": len(story.get("views", [])),
                "likes_count": len(story.get("likes", [])),
                "liked_by_user": liked,
                "viewed_by_user": viewed
            })

            if not viewed:
                user_stories[owner_id]["unviewed_count"] += 1

        # Ensure each user's stories are ordered oldest -> newest
        for owner_id, payload in user_stories.items():
            payload["stories"].sort(key=lambda s: s.get("created_at"))

        return list(user_stories.values())

    except Exception as e:
        logger.error("❌ Story feed error", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


# 📊 GET STORY STATS (for story owner)
@router.get("/stats/{story_id}",
    response_model=StoryStats
)
async def get_story_stats(
    story_id: str,
    authorization: str = Header(...)
):
    """Get views and likes for a story"""
    try:
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        # Validate story_id format
        if not story_id or len(story_id) != 24:
            raise HTTPException(status_code=400, detail="Invalid story ID format")
        
        try:
            story_oid = ObjectId(story_id)
        except Exception as e:
            logger.error(f"❌ Invalid ObjectId: {story_id} - {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid story ID format")
        
        story = await db.stories.find_one({"_id": story_oid})
        if not story:
            raise HTTPException(status_code=404, detail="Story not found")
        
        if story["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Can only view stats for your own stories")
        
        # Get viewer details
        viewers = []
        for view in story.get("views", []):
            viewer_user = await db.users.find_one({"firebase_uid": view["user_id"]})
            if viewer_user:
                viewers.append({
                    "user_id": view["user_id"],
                    "username": viewer_user.get("username", "Unknown"),
                    "public_id": viewer_user.get("public_id"),
                    "full_name": viewer_user.get("full_name"),
                    "image": get_profile_image_url(viewer_user.get("image_name")),
                    "viewed_at": view["viewed_at"]
                })
        
        # Get liker details
        likers = []
        for like in story.get("likes", []):
            liker_user = await db.users.find_one({"firebase_uid": like["user_id"]})
            if liker_user:
                likers.append({
                    "user_id": like["user_id"],
                    "username": liker_user.get("username", "Unknown"),
                    "public_id": liker_user.get("public_id"),
                    "full_name": liker_user.get("full_name"),
                    "image": get_profile_image_url(liker_user.get("image_name"))
                })
        
        return {
            "story_id": story_id,
            "views_count": len(viewers),
            "likes_count": len(likers),
            "viewers": viewers,
            "likers": likers
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching story stats: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


# 📖 GET USER STORIES WITH FULL DETAILS (with permission check)
@router.get("/user/{target_user_id}/stories")
async def get_user_stories(
    target_user_id: str,
    authorization: str = Header(...)
):
    """Get full story details for a specific user with permission checking"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")

        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        viewer_id = decoded["uid"]

        logger.info(f"🔍 Fetching stories for user: {target_user_id} by viewer: {viewer_id}")

        # Get target user
        target_user = await db.users.find_one({"_id": ObjectId(target_user_id)})
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        target_public_id = await ensure_user_public_id(target_user)

        # Check permission to view stories
        can_view = viewer_id == target_user_id  # Own stories always visible
        
        if not can_view and target_user.get("account_type") != "private":
            # Public account - anyone can view
            can_view = True

        if not can_view:
            # Private account - check if following
            follow_doc = await db.follows.find_one({
                "follower_id": viewer_id,
                "following_id": target_user_id,
                "status": "following"
            })
            can_view = follow_doc is not None

        if not can_view:
            raise HTTPException(
                status_code=403, 
                detail="Cannot view stories from this private account"
            )

        # Fetch active stories
        now = datetime.utcnow()
        stories = await db.stories.find({
            "user_id": target_user_id,
            "expires_at": {"$gt": now}
        }).sort("created_at", 1).to_list(None)

        if not stories:
            return {
                "user_id": target_user_id,
                "username": target_user.get("username", "Unknown"),
                "public_id": target_public_id,
                "full_name": target_user.get("full_name"),
                "user_image": get_profile_image_url(target_user.get("image_name")),
                "gender": target_user.get("gender"),
                "stories": [],
                "unviewed_count": 0
            }

        # Build detailed story list
        detailed_stories = []
        unviewed_count = 0

        for story in stories:
            viewed = any(v["user_id"] == viewer_id for v in story.get("views", []))
            liked = any(l["user_id"] == viewer_id for l in story.get("likes", []))

            detailed_stories.append({
                "story_id": str(story["_id"]),
                "user_id": target_user_id,
                "username": target_user.get("username", "Unknown"),
                "public_id": target_public_id,
                "full_name": target_user.get("full_name"),
                "user_image": get_profile_image_url(target_user.get("image_name")),
                "media_url": story["media_url"],
                "media_type": story["media_type"],
                "duration": story["duration"],
                "text": story.get("text"),
                "emoji_stickers": story.get("emoji_stickers"),
                "drawing_data": story.get("drawing_data"),
                "music_url": story.get("music_url"),
                "created_at": story["created_at"],
                "expires_at": story["expires_at"],
                "views_count": len(story.get("views", [])),
                "likes_count": len(story.get("likes", [])),
                "liked_by_user": liked,
                "viewed_by_user": viewed
            })

            if not viewed:
                unviewed_count += 1

        return {
            "user_id": target_user_id,
            "username": target_user.get("username", "Unknown"),
            "public_id": target_public_id,
            "full_name": target_user.get("full_name"),
            "user_image": get_profile_image_url(target_user.get("image_name")),
            "gender": target_user.get("gender"),
            "stories": detailed_stories,
            "unviewed_count": unviewed_count
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Error fetching user stories: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))
