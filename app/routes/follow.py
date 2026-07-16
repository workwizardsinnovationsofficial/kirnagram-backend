from fastapi import APIRouter, Header, HTTPException
from datetime import datetime
from bson import ObjectId
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header
from app.firebase import verify_firebase_token
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/follow", tags=["Follow"])


# ================================================================
# HELPER FUNCTIONS gfsjcbjbjdhddcbjkhdchkbkdcnkhdcnkbncb
# ================================================================

async def get_follow_status(follower_uid: str, target_uid: str):
    """
    Get the follow status between two users.
    Returns: "following", "requested", "pending", "none"
    """
    # Check if follower is following target
    following = await db.follows.find_one({
        "follower_id": follower_uid,
        "following_id": target_uid,
        "status": "following"
        
    })

    
    if following:
        return "following"
    
    # Check if there's a follow request
    request = await db.follows.find_one({
        "follower_id": follower_uid,
        "following_id": target_uid,
        "status": "requested"
    })
    
    if request:
        return "requested"
    
    # Check if target is already following follower (for follow back status)
    pending_following = await db.follows.find_one({
        "follower_id": target_uid,
        "following_id": follower_uid,
        "status": "following"
    })

    if pending_following:
        return "pending"

    # Check if target has sent a request to follower (for follow back status)
    pending = await db.follows.find_one({
        "follower_id": target_uid,
        "following_id": follower_uid,
        "status": "requested"
    })
    
    if pending:
        return "pending"
    
    return "none"


async def _resolve_user(identifier: str):
    """Resolve a user document from multiple identifier types safely.
    Accepts ObjectId string, firebase_uid, username, or public_id.
    """
    query_clauses = []
    if ObjectId.is_valid(identifier):
        query_clauses.append({"_id": ObjectId(identifier)})
    # match firebase_uid, username, or public_id
    query_clauses.append({"firebase_uid": identifier})
    query_clauses.append({"username": identifier})
    query_clauses.append({"public_id": identifier})

    user = await db.users.find_one({"$or": query_clauses})
    return user


async def _get_user_firebase_uid(identifier: str):
    user = await _resolve_user(identifier)
    if not user:
        return None
    return user.get("firebase_uid") or str(user.get("_id"))


async def get_active_story_snapshot(target_uid: str):
    """Return whether target user has an active story and the first story id."""
    now = datetime.utcnow()
    first_story = await db.stories.find_one(
        {
            "user_id": target_uid,
            "expires_at": {"$gt": now}
        },
        sort=[("created_at", 1)]
    )

    if not first_story:
        return False, None

    return True, str(first_story.get("_id"))


async def create_notification(to_user_id: str, from_user_id: str, action: str, description: str):
    """Helper to create follow notifications"""
    from_user = await _resolve_user(from_user_id)
    to_user = await _resolve_user(to_user_id)
    if not from_user or not to_user:
        raise HTTPException(status_code=404, detail="Notification user not found")

    to_user_fid = to_user.get("firebase_uid") or str(to_user.get("_id"))
    from_user_fid = from_user.get("firebase_uid") or str(from_user.get("_id"))

    # Get current follow status for context
    follow_status = await get_follow_status(to_user_fid, from_user_fid)
    
    notif_doc = {
        "user_id": to_user_fid,
        "from_user_id": from_user_fid,
        "from_user_name": from_user.get("full_name", "User"),
        "from_user_username": from_user.get("username"),
        "from_user_image": from_user.get("image_name"),
        "from_user_gender": from_user.get("gender"),
        "action": action,
        "description": description,
        "follow_status": follow_status,
        "timestamp": datetime.utcnow()
    }
    
    result = await db.notifications.insert_one(notif_doc)
    return str(result.inserted_id)


@router.get("/story-status/{target_uid}")
async def get_story_status(target_uid: str, authorization: str = Header(...)):
    """Return active story status for a target user using follow router."""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]
        decoded = verify_firebase_token(token)
        viewer_uid = decoded["uid"]

        target_user = await _resolve_user(target_uid)
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        target_uid = target_user.get("firebase_uid") or str(target_user.get("_id"))
        account_type = target_user.get("account_type", "public")

        viewer_user = await _resolve_user(viewer_uid)
        viewer_uid = viewer_user.get("firebase_uid") if viewer_user else viewer_uid

        if target_uid != viewer_uid and account_type == "private":
            follow_doc = await db.follows.find_one({
                "follower_id": viewer_uid,
                "following_id": target_uid,
                "status": "following"
            })
            if not follow_doc:
                return {
                    "target_uid": target_uid,
                    "can_view": False,
                    "has_active_story": False,
                    "first_story_id": None,
                }

        has_active_story, first_story_id = await get_active_story_snapshot(target_uid)
        return {
            "target_uid": target_uid,
            "can_view": True,
            "has_active_story": has_active_story,
            "first_story_id": first_story_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch story status: {str(e)}")


# ================================================================
# GET USER PROFILE (for viewing other users)
# ================================================================

@router.get("/{user_id}")
async def get_user_profile(user_id: str, authorization: str = Header(...)):
    """
    Get another user's profile with follow relationship info.
    user_id can be firebase_uid or username.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        # Resolve target user by any identifier
        target_user = await _resolve_user(user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        target_uid = target_user.get("firebase_uid") or str(target_user.get("_id"))

        is_creator = False
        creator_app = await db.ai_creator_applications.find_one({"user_id": target_uid, "status": "approved"})
        if creator_app:
            is_creator = True
        target_user["is_creator"] = is_creator

        has_active_story, first_story_id = await get_active_story_snapshot(target_uid)
        target_user["has_active_story"] = has_active_story
        target_user["first_story_id"] = first_story_id
        
        # If viewing own profile, just return it
        if current_user_uid == target_uid:
            target_user.pop("_id", None)
            return target_user
        
        current_user = await _resolve_user(current_user_uid)
        if not current_user:
            raise HTTPException(status_code=404, detail="Current user not found")

        current_user_uid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        # For other users' profiles, check privacy
        account_type = target_user.get("account_type", "public")
        is_following = await db.follows.find_one({
            "follower_id": current_user_uid,
            "following_id": target_uid,
            "status": "following"
        })
        
        # If private account and not following, hide sensitive info
        if account_type == "private" and not is_following:
            target_user = {
                "firebase_uid": target_user.get("firebase_uid"),
                "username": target_user.get("username"),
                "full_name": target_user.get("full_name"),
                "gender": target_user.get("gender"),  # include gender for fallback avatar icons
                "image_name": target_user.get("image_name"),
                "cover_image": target_user.get("cover_image"),  # allow cover preview in private mode
                "account_type": "private",
                "is_creator": is_creator,
                "followers_count": await db.follows.count_documents({
                    "following_id": target_uid,
                    "status": "following"
                }),
                "following_count": await db.follows.count_documents({
                    "follower_id": target_uid,
                    "status": "following"
                }),
                "posts_count": await db.posts.count_documents({"user_id": target_uid, "is_prompt_post": {"$ne": True}}),
                "prompts_count": await db.posts.count_documents({"user_id": target_uid, "is_prompt_post": True})
            }
            # Private account stories are hidden from non-followers.
            target_user["has_active_story"] = False
            target_user["first_story_id"] = None
        else:
            # Public account or already following - show all info
            target_user["followers_count"] = await db.follows.count_documents({
                "following_id": target_uid,
                "status": "following"
            })
            target_user["following_count"] = await db.follows.count_documents({
                "follower_id": target_uid,
                "status": "following"
            })
            target_user["posts_count"] = await db.posts.count_documents({"user_id": target_uid, "is_prompt_post": {"$ne": True}})
            target_user["prompts_count"] = await db.posts.count_documents({"user_id": target_uid, "is_prompt_post": True})
        
        # Add follow relationship status
        follow_status = await get_follow_status(current_user_uid, target_uid)
        target_user["follow_status"] = follow_status
        target_user.pop("_id", None)
        
        return target_user
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(e)}")


# ================================================================
# SEARCH USERS (for discover page)
# ================================================================

@router.get("/search/{query}")
async def search_users(query: str, authorization: str = Header(...)):
    """
    Search for users by username or full name.
    Returns all profiles (public and private) for discovery.
    Private status is indicated in the account_type field.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        # Find users matching query
        regex_pattern = {"$regex": query, "$options": "i"}
        users = await db.users.find(
            {
                "$or": [
                    {"username": regex_pattern},
                    {"full_name": regex_pattern}
                ],
                "firebase_uid": {"$ne": current_user_uid}  # Exclude current user
            },
            {"_id": 0}
        ).limit(50).to_list(length=50)
        
        result = []
        for user in users:
            target_uid = user.get("firebase_uid")
            account_type = user.get("account_type", "public")
            
            # Include all profiles (both public and private) for discovery
            # Private indicator will be shown in UI
            
            # Add follow status
            follow_status = await get_follow_status(current_user_uid, target_uid)
            user["follow_status"] = follow_status
            
            # Add count fields for consistent response
            user["followers_count"] = await db.follows.count_documents({
                "following_id": target_uid,
                "status": "following"
            })
            user["following_count"] = await db.follows.count_documents({
                "follower_id": target_uid,
                "status": "following"
            })
            user["posts_count"] = await db.posts.count_documents({"user_id": target_uid, "is_prompt_post": {"$ne": True}})
            user["prompts_count"] = await db.posts.count_documents({"user_id": target_uid, "is_prompt_post": True})

            # Public users always expose story ring. Private users expose it only to followers.
            can_show_story_ring = account_type != "private" or follow_status == "following"
            if can_show_story_ring:
                has_active_story, first_story_id = await get_active_story_snapshot(target_uid)
                user["has_active_story"] = has_active_story
                user["first_story_id"] = first_story_id
            else:
                user["has_active_story"] = False
                user["first_story_id"] = None
            
            result.append(user)
        
        return {
            "total": len(result),
            "users": result
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Search failed: {str(e)}")


# ================================================================
# FOLLOW ACTIONS
# ================================================================

@router.post("/send-request/{target_uid}")
async def send_follow_request(target_uid: str, authorization: str = Header(...)):
    """
    Send a follow request or follow immediately (Instagram-like rules):
    - Target public  → follow immediately (regardless of follower privacy)
    - Target private → create a pending request
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        # Get both users (resolve by flexible identifiers)
        current_user = await _resolve_user(current_user_uid)
        target_user = await _resolve_user(target_uid)
        
        if not current_user or not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Get account types for both users
        current_account_type = current_user.get("account_type", "public")
        target_account_type = target_user.get("account_type", "public")
        
        # Use firebase_uid values for follow records
        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))
        target_fid = target_user.get("firebase_uid") or str(target_user.get("_id"))

        # Prevent duplicate relationships/requests
        already_following = await db.follows.find_one({
            "follower_id": current_fid,
            "following_id": target_fid,
            "status": "following"
        })
        if already_following:
            return {
                "status": "success",
                "message": "Already following",
                "follow_status": "following"
            }

        existing_request = await db.follows.find_one({
            "follower_id": current_fid,
            "following_id": target_fid,
            "status": "requested"
        })
        if existing_request:
            return {
                "status": "success",
                "message": "Follow request already sent",
                "follow_status": "requested"
            }

        # ================================================================
        # CASE 1: Both users are PUBLIC
        # User A (public) → User B (public)
        # Action: Instant follow, no request needed
        # ================================================================
        if current_account_type == "public" and target_account_type == "public":
            await db.follows.delete_many({
                "follower_id": current_fid,
                "following_id": target_fid
            })

            follow_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "following",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(follow_doc)

            # ✅ UPDATE user.following array for story feed (store firebase_uids)
            await db.users.update_one(
                {"_id": current_user.get("_id")},
                {"$addToSet": {"following": target_fid}}
            )
            # ✅ UPDATE user.followers array
            await db.users.update_one(
                {"_id": target_user.get("_id")},
                {"$addToSet": {"followers": current_fid}}
            )

            await create_notification(
                to_user_id=target_uid,
                from_user_id=current_user_uid,
                action="started_following",
                description=f"{current_user.get('full_name', 'User')} started following you"
            )

            return {
                "status": "success",
                "message": "Now following (Case 1: Both Public)",
                "follow_status": "following"
            }
        
        # ================================================================
        # CASE 2: Both users are PRIVATE
        # User A (private) → User B (private)
        # Action: Send follow request
        # ================================================================
        elif current_account_type == "private" and target_account_type == "private":
            request_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "requested",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(request_doc)

            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="follow_request",
                description=f"{current_user.get('full_name', 'User')} requested to follow you"
            )
            
            return {
                "status": "success",
                "message": "Follow request sent (Case 2: Both Private)",
                "follow_status": "requested"
            }
        
        # ================================================================
        # CASE 3: Sender is PUBLIC, Target is PRIVATE
        # User A (public) → User B (private)
        # Action: Send follow request (target is private)
        # ================================================================
        elif current_account_type == "public" and target_account_type == "private":
            request_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "requested",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(request_doc)

            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="follow_request",
                description=f"{current_user.get('full_name', 'User')} requested to follow you"
            )
            
            return {
                "status": "success",
                "message": "Follow request sent (Case 3: Public → Private)",
                "follow_status": "requested"
            }
        
        # ================================================================
        # CASE 4: Sender is PRIVATE, Target is PUBLIC
        # User A (private) → User B (public)
        # Action: Instant follow (target is public)
        # ================================================================
        else:  # current_account_type == "private" and target_account_type == "public"
            await db.follows.delete_many({
                "follower_id": current_fid,
                "following_id": target_fid
            })

            follow_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "following",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(follow_doc)
            
            # ✅ UPDATE user.following array for story feed (store firebase_uids)
            await db.users.update_one(
                {"_id": current_user.get("_id")},
                {"$addToSet": {"following": target_fid}}
            )
            # ✅ UPDATE user.followers array
            await db.users.update_one(
                {"_id": target_user.get("_id")},
                {"$addToSet": {"followers": current_fid}}
            )

            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="started_following",
                description=f"{current_user.get('full_name', 'User')} started following you"
            )

            return {
                "status": "success",
                "message": "Now following (Case 4: Private → Public)",
                "follow_status": "following"
            }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Follow request failed: {str(e)}")


@router.post("/approve-request/{from_user_id}")
async def approve_follow_request(from_user_id: str, authorization: str = Header(...)):
    """
    Approve a follow request.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        # Get users
        from_user = await _resolve_user(from_user_id)
        current_user = await _resolve_user(current_user_uid)
        
        if not from_user or not current_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        from_user_fid = from_user.get("firebase_uid") or str(from_user.get("_id"))
        current_user_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        # Find the follow request
        follow_request = await db.follows.find_one({
            "follower_id": from_user_fid,
            "following_id": current_user_fid,
            "status": "requested"
        })
        
        if not follow_request:
            raise HTTPException(status_code=400, detail="No follow request found")
        
        # Approve the request - change to following
        await db.follows.update_one(
            {"follower_id": from_user_fid, "following_id": current_user_fid},
            {"$set": {"status": "following"}}
        )
        
        # Update arrays for both users
        await db.users.update_one(
            {"_id": current_user.get("_id")},
            {"$addToSet": {"followers": from_user_fid}}
        )
        await db.users.update_one(
            {"_id": from_user.get("_id")},
            {"$addToSet": {"following": current_user_fid}}
        )
        
        # Notify the requester
        await create_notification(
            to_user_id=from_user_fid,
            from_user_id=current_user_fid,
            action="follow_approved",
            description=f"{current_user.get('full_name', 'User')} approved your follow request"
        )
        
        return {
            "status": "success",
            "message": "Follow request approved"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to approve request: {str(e)}")


@router.post("/reject-request/{from_user_id}")
async def reject_follow_request(from_user_id: str, authorization: str = Header(...)):
    """
    Reject a follow request.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        from_user = await _resolve_user(from_user_id)
        current_user = await _resolve_user(current_user_uid)
        if not from_user or not current_user:
            raise HTTPException(status_code=404, detail="User not found")

        from_user_fid = from_user.get("firebase_uid") or str(from_user.get("_id"))
        current_user_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        # Find and delete the follow request
        result = await db.follows.delete_one({
            "follower_id": from_user_fid,
            "following_id": current_user_fid,
            "status": "requested"
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=400, detail="No follow request found")
        
        return {
            "status": "success",
            "message": "Follow request rejected"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to reject request: {str(e)}")


@router.post("/follow-back/{target_uid}")
async def follow_back(target_uid: str, authorization: str = Header(...)):
    """
    Follow back a user who is following you.
    Handles all 4 cases for follow back behavior.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        # Get both users
        current_user = await _resolve_user(current_user_uid)
        target_user = await _resolve_user(target_uid)
        
        if not current_user or not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))
        target_fid = target_user.get("firebase_uid") or str(target_user.get("_id"))

        # Get account types for both users
        current_account_type = current_user.get("account_type", "public")
        target_account_type = target_user.get("account_type", "public")
        
        # Check if already following
        already_following = await db.follows.find_one({
            "follower_id": current_fid,
            "following_id": target_fid,
            "status": "following"
        })
        
        if already_following:
            return {
                "status": "success",
                "message": "Already following",
                "follow_status": "following"
            }

        # ================================================================
        # CASE 1: Both users are PUBLIC
        # User B (public) follows back User A (public)
        # Action: Instant follow, no request needed
        # ================================================================
        if current_account_type == "public" and target_account_type == "public":
            await db.follows.delete_many({
                "follower_id": current_fid,
                "following_id": target_fid
            })

            follow_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "following",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(follow_doc)
            
            await db.users.update_one(
                {"_id": current_user.get("_id")},
                {"$addToSet": {"following": target_fid}}
            )
            await db.users.update_one(
                {"_id": target_user.get("_id")},
                {"$addToSet": {"followers": current_fid}}
            )

            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="started_following",
                description=f"{current_user.get('full_name', 'User')} started following you"
            )
            
            return {
                "status": "success",
                "message": "Now following (Case 1: Both Public)",
                "follow_status": "following"
            }

        # ================================================================
        # CASE 2: Both users are PRIVATE
        # User B (private) follows back User A (private)
        # Action: Send follow request (target is private)
        # ================================================================
        elif current_account_type == "private" and target_account_type == "private":
            existing_request = await db.follows.find_one({
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "requested"
            })
            if existing_request:
                return {
                    "status": "success",
                    "message": "Request already sent",
                    "follow_status": "requested"
                }

            request_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "requested",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(request_doc)
            
            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="follow_request",
                description=f"{current_user.get('full_name', 'User')} requested to follow you"
            )
            
            return {
                "status": "success",
                "message": "Follow request sent (Case 2: Both Private)",
                "follow_status": "requested"
            }

        # ================================================================
        # CASE 3: Sender is PUBLIC, Target is PRIVATE
        # User B (public) follows back User A (private)
        # Action: Send follow request (target is private)
        # ================================================================
        elif current_account_type == "public" and target_account_type == "private":
            existing_request = await db.follows.find_one({
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "requested"
            })
            if existing_request:
                return {
                    "status": "success",
                    "message": "Request already sent",
                    "follow_status": "requested"
                }

            request_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "requested",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(request_doc)
            
            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="follow_request",
                description=f"{current_user.get('full_name', 'User')} requested to follow you"
            )
            
            return {
                "status": "success",
                "message": "Follow request sent (Case 3: Public → Private)",
                "follow_status": "requested"
            }

        # ================================================================
        # CASE 4: Sender is PRIVATE, Target is PUBLIC
        # User B (private) follows back User A (public)
        # Action: Instant follow (target is public)
        # ================================================================
        else:  # current_account_type == "private" and target_account_type == "public"
            await db.follows.delete_many({
                "follower_id": current_fid,
                "following_id": target_fid
            })

            follow_doc = {
                "follower_id": current_fid,
                "following_id": target_fid,
                "status": "following",
                "created_at": datetime.utcnow()
            }
            await db.follows.insert_one(follow_doc)
            
            await db.users.update_one(
                {"_id": current_user.get("_id")},
                {"$addToSet": {"following": target_fid}}
            )
            await db.users.update_one(
                {"_id": target_user.get("_id")},
                {"$addToSet": {"followers": current_fid}}
            )

            await create_notification(
                to_user_id=target_fid,
                from_user_id=current_fid,
                action="started_following",
                description=f"{current_user.get('full_name', 'User')} started following you"
            )
            
            return {
                "status": "success",
                "message": "Now following (Case 4: Private → Public)",
                "follow_status": "following"
            }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Follow back failed: {str(e)}")


@router.delete("/unfollow/{target_uid}")
async def unfollow(target_uid: str, authorization: str = Header(...)):
    """
    Unfollow a user or cancel a follow request.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]

        current_user = await _resolve_user(current_user_uid)
        if not current_user:
            raise HTTPException(status_code=404, detail="User not found")
        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        target_user = await _resolve_user(target_uid)
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found")
        target_fid = target_user.get("firebase_uid") or str(target_user.get("_id"))

        # Delete follow relationship
        result = await db.follows.delete_one({
            "follower_id": current_fid,
            "following_id": target_fid
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=400, detail="Not following this user")
        
        # ✅ UPDATE user.following array
        await db.users.update_one(
            {"_id": current_user.get("_id")},
            {"$pull": {"following": target_fid}}
        )
        # ✅ UPDATE user.followers array
        await db.users.update_one(
            {"_id": target_user.get("_id")},
            {"$pull": {"followers": current_fid}}
        )
        
        return {
            "status": "success",
            "message": "Unfollowed successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Unfollow failed: {str(e)}")


@router.get("/followers/{user_id}")
async def get_followers(user_id: str, authorization: str = Header(...)):
    """Get list of followers for a user"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        
        # Find target user
        target_user = await _resolve_user(user_id)
        
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        target_uid = target_user.get("firebase_uid") or str(target_user.get("_id"))
        
        # Get followers
        followers = await db.follows.find(
            {"following_id": target_uid, "status": "following"}
        ).to_list(length=None)
        
        followers_data = []
        for follow in followers:
            follower_user = await _resolve_user(follow["follower_id"])
            if follower_user:
                follower_user.pop("_id", None)
                followers_data.append(follower_user)
        
        return {
            "total": len(followers_data),
            "followers": followers_data
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Failed to get followers: {str(e)}")


@router.get("/following/{user_id}")
async def get_following(user_id: str, authorization: str = Header(...)):
    """Get list of users that a user is following"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        
        # Find target user
        target_user = await _resolve_user(user_id)
        
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")
        
        target_uid = target_user.get("firebase_uid") or str(target_user.get("_id"))
        
        # Get following
        following = await db.follows.find(
            {"follower_id": target_uid, "status": "following"}
        ).to_list(length=None)
        
        following_data = []
        for follow in following:
            following_user = await _resolve_user(follow["following_id"])
            if following_user:
                following_user.pop("_id", None)
                following_data.append(following_user)
        
        return {
            "total": len(following_data),
            "following": following_data
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Failed to get following: {str(e)}")


@router.get("/pending-requests")
async def get_pending_requests(authorization: str = Header(...)):
    """Get list of pending follow requests for the current user"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]

        current_user = await _resolve_user(current_user_uid)
        if not current_user:
            raise HTTPException(status_code=404, detail="User not found")

        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        # Get pending requests
        requests = await db.follows.find(
            {"following_id": current_fid, "status": "requested"}
        ).to_list(length=None)
        
        requests_data = []
        for request in requests:
            requester = await _resolve_user(request["follower_id"])
            if requester:
                requester.pop("_id", None)
                requests_data.append(requester)
        
        return {
            "total": len(requests_data),
            "pending_requests": requests_data
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Failed to get pending requests: {str(e)}")


@router.delete("/remove-follower/{follower_uid}")
async def remove_follower(follower_uid: str, authorization: str = Header(...)):
    """
    Remove a follower (Instagram-style).
    Deletes the follow relationship where follower_uid is following the current user.
    """
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        current_user_uid = decoded["uid"]
        
        follower_user = await _resolve_user(follower_uid)
        if not follower_user:
            raise HTTPException(status_code=404, detail="Follower not found")

        follower_fid = follower_user.get("firebase_uid") or str(follower_user.get("_id"))
        current_user = await _resolve_user(current_user_uid)
        if not current_user:
            raise HTTPException(status_code=404, detail="Current user not found")

        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))

        result = await db.follows.delete_one({
            "follower_id": follower_fid,
            "following_id": current_fid
        })
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=400, detail="Follower relationship not found")
        
        return {
            "status": "success",
            "message": "Follower removed successfully"
        }
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to remove follower: {str(e)}")

# ================================================================
# PRIVACY-AWARE FOLLOW ENDPOINTS (For Stories)
# ================================================================

# 👥 FOLLOW USER (with privacy handling)
@router.post("/user/{target_user_id}")
async def follow_user_privacy(
    target_user_id: str,
    authorization: str = Header(...)
):
    """Follow a user or send follow request if account is private"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        if user_id == target_user_id:
            raise HTTPException(status_code=400, detail="Cannot follow yourself")
        
        target_user = await _resolve_user(target_user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="User not found")

        current_user = await _resolve_user(user_id)
        if not current_user:
            raise HTTPException(status_code=404, detail="Current user not found")

        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))
        target_fid = target_user.get("firebase_uid") or str(target_user.get("_id"))
        account_type = target_user.get("account_type", "public")
        following = current_user.get("following", [])

        if target_fid in following:
            raise HTTPException(status_code=400, detail="Already following")

        if account_type == "private":
            follow_requests = target_user.get("follow_requests", [])
            if current_fid in follow_requests:
                raise HTTPException(status_code=400, detail="Request already sent")

            await db.users.update_one(
                {"_id": target_user.get("_id")},
                {"$addToSet": {"follow_requests": current_fid}}
            )
            return {"status": "pending", "message": "Follow request sent"}
        else:
            await db.users.update_one(
                {"_id": current_user.get("_id")},
                {"$addToSet": {"following": target_fid}}
            )
            await db.users.update_one(
                {"_id": target_user.get("_id")},
                {"$addToSet": {"followers": current_fid}}
            )
            return {"status": "approved", "message": "Now following"}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to follow: {str(e)}")


# 🚫 UNFOLLOW USER
@router.post("/user/{target_user_id}/unfollow")
async def unfollow_user_privacy(
    target_user_id: str,
    authorization: str = Header(...)
):
    """Unfollow a user"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        # Resolve target and use firebase_uids in arrays
        target_user = await _resolve_user(target_user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="Target user not found")

        current_user = await _resolve_user(user_id)
        if not current_user:
            raise HTTPException(status_code=404, detail="Current user not found")

        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))
        target_fid = target_user.get("firebase_uid") or str(target_user.get("_id"))

        await db.users.update_one(
            {"_id": current_user.get("_id")},
            {"$pull": {"following": target_fid}}
        )

        await db.users.update_one(
            {"_id": target_user.get("_id")},
            {"$pull": {"followers": current_fid}}
        )

        await db.follows.delete_one({"follower_id": current_fid, "following_id": target_fid})

        return {"message": "Unfollowed"}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to unfollow: {str(e)}")


# 📨 GET FOLLOW REQUESTS
@router.get("/requests")
async def get_follow_requests(authorization: str = Header(...)):
    """Get pending follow requests"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        current_user = await _resolve_user(user_id)
        if not current_user:
            raise HTTPException(status_code=404, detail="User not found")

        follow_requests = current_user.get("follow_requests", [])
        requesters = []

        for requester_id in follow_requests:
            requester = await _resolve_user(requester_id)
            if requester:
                requesters.append({
                    "user_id": requester.get("firebase_uid") or str(requester.get("_id")),
                    "username": requester.get("username"),
                    "image_name": requester.get("image_name")
                })

        return {"requests": requesters, "count": len(requesters)}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get requests: {str(e)}")


# ✅ APPROVE FOLLOW REQUEST
@router.post("/requests/{requester_id}/approve")
async def approve_follow_request(
    requester_id: str,
    authorization: str = Header(...)
):
    """Approve a follow request"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        current_user = await _resolve_user(user_id)
        if not current_user:
            raise HTTPException(status_code=404, detail="User not found")

        requester = await _resolve_user(requester_id)
        if not requester:
            raise HTTPException(status_code=404, detail="Requester not found")

        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))
        requester_fid = requester.get("firebase_uid") or str(requester.get("_id"))
        follow_requests = current_user.get("follow_requests", [])

        if requester_fid not in follow_requests:
            raise HTTPException(status_code=404, detail="Request not found")

        await db.users.update_one(
            {"_id": current_user.get("_id")},
            {
                "$pull": {"follow_requests": requester_fid},
                "$addToSet": {"followers": requester_fid, "approved_followers": requester_fid}
            }
        )

        await db.users.update_one(
            {"_id": requester.get("_id")},
            {"$addToSet": {"following": current_fid}}
        )

        return {"message": "Request approved"}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to approve: {str(e)}")


# ❌ REJECT FOLLOW REQUEST
@router.post("/requests/{requester_id}/reject")
async def reject_follow_request(
    requester_id: str,
    authorization: str = Header(...)
):
    """Reject a follow request"""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header")
        
        token = authorization.split(" ")[1]
        decoded = verify_firebase_token(token)
        user_id = decoded["uid"]
        
        current_user = await _resolve_user(user_id)
        if not current_user:
            raise HTTPException(status_code=404, detail="User not found")

        requester = await _resolve_user(requester_id)
        if not requester:
            raise HTTPException(status_code=404, detail="Requester not found")

        current_fid = current_user.get("firebase_uid") or str(current_user.get("_id"))
        requester_fid = requester.get("firebase_uid") or str(requester.get("_id"))
        follow_requests = current_user.get("follow_requests", [])

        if requester_fid not in follow_requests:
            raise HTTPException(status_code=404, detail="Request not found")

        await db.users.update_one(
            {"_id": current_user.get("_id")},
            {"$pull": {"follow_requests": requester_fid}}
        )

        return {"message": "Request rejected"}
    
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to reject: {str(e)}")