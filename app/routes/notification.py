from fastapi import APIRouter, Header, HTTPException
from datetime import datetime, timedelta
from bson import ObjectId
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token
from app.models.notification import NotificationCreate

router = APIRouter(prefix="/notifications", tags=["Notifications"])


@router.get("/recent")
async def get_recent_notifications(
    hours: int = 24,
    limit: int = 50,
    authorization: str = Header(...)
):
    """Get recent notifications for the authenticated user only."""
    try:
        # Extract token
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        # Verify user exists
        user = await db.users.find_one({"_id": ObjectId(uid)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Calculate time window
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)

        # Get notifications for this user in the last N hours
        notifications = await db.notifications.find(
            {"user_id": uid, "timestamp": {"$gte": cutoff_time}}
        ).sort("timestamp", -1).limit(limit).to_list(length=limit)

        # Convert ObjectId to string for JSON serialization
        result = []
        for notif in notifications:
            notif["_id"] = str(notif["_id"])
            if "timestamp" in notif and isinstance(notif["timestamp"], datetime):
                # Return ISO 8601 with explicit Z to avoid timezone ambiguity
                notif["timestamp"] = notif["timestamp"].isoformat() + "Z"
            
            # Calculate follow_status dynamically for follow-related notifications
            if notif.get("from_user_id") and notif.get("action") in ["started_following", "follow_request", "follow_approved"]:
                from app.routes.follow import get_follow_status
                from_user_id = notif["from_user_id"]
                notif["follow_status"] = await get_follow_status(uid, from_user_id)

                # Special handling for follow request lifecycle to mirror Instagram behavior
                if notif.get("action") == "follow_request":
                    # Check current state of the underlying follow relation from from_user -> current user
                    pending_request = await db.follows.find_one({
                        "follower_id": from_user_id,
                        "following_id": uid,
                        "status": "requested"
                    })
                    approved_follow = await db.follows.find_one({
                        "follower_id": from_user_id,
                        "following_id": uid,
                        "status": "following"
                    })

                    if pending_request:
                        # Still pending: keep as follow_request with Confirm/Delete buttons
                        pass
                    elif approved_follow:
                        # Request was approved: present as a "started_following" to enable "Follow Back"
                        notif["action"] = "started_following"
                        # Refresh description to a consistent "started following" message
                        from_user = await db.users.find_one({"_id": ObjectId(from_user_id)})
                        from_name = from_user.get("full_name", "User") if from_user else "User"
                        notif["description"] = f"{from_name} started following you"
                    else:
                        # Request no longer exists (likely rejected/cancelled) -> skip showing this notification
                        continue
            
            result.append(notif)

        return {
            "total": len(result),
            "hours": hours,
            "notifications": result
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(e)}")


@router.post("/create")
async def create_notification(
    notification: NotificationCreate,
    authorization: str = Header(...)
):
    """
    Internal endpoint to create a notification when profile is updated
    """
    try:
        # Extract token
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        # Ensure notification is for the authenticated user
        if notification.user_id != uid:
            raise HTTPException(status_code=403, detail="Cannot create notifications for other users")

        # Create notification document
        notif_doc = {
            "user_id": notification.user_id,
            "user_name": notification.user_name,
            "user_image": notification.user_image,
            "action": notification.action,
            "description": notification.description,
            "timestamp": datetime.utcnow()
        }

        result = await db.notifications.insert_one(notif_doc)

        return {
            "success": True,
            "notification_id": str(result.inserted_id),
            # Append Z to ensure clients treat it as UTC
            "timestamp": notif_doc["timestamp"].isoformat() + "Z"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create notification: {str(e)}")


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    authorization: str = Header(...)
):
    """Delete a notification by ID for the current user."""
    try:
        # Extract token
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")
        
        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        # Verify user exists
        user = await db.users.find_one({"_id": ObjectId(uid)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Convert string ID to ObjectId
        try:
            obj_id = ObjectId(notification_id)
        except:
            raise HTTPException(status_code=400, detail="Invalid notification ID format")

        # Delete only if the notification belongs to the current user
        result = await db.notifications.delete_one({"_id": obj_id, "user_id": uid})

        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Notification not found")

        return {
            "success": True,
            "message": "Notification deleted successfully",
            "notification_id": notification_id
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to delete notification: {str(e)}")


@router.delete("/clear/all")
async def clear_notifications(authorization: str = Header(...)):
    """Delete all notifications for the current user."""
    try:
        if not authorization or " " not in authorization:
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        parts = authorization.split(" ")
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise HTTPException(status_code=401, detail="Invalid authorization header format")

        token = parts[1]
        decoded = verify_firebase_token(token)
        uid = decoded["uid"]

        # Ensure user exists
        user = await db.users.find_one({"_id": ObjectId(uid)})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        result = await db.notifications.delete_many({"user_id": uid})

        return {
            "success": True,
            "deleted": result.deleted_count
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to clear notifications: {str(e)}")
