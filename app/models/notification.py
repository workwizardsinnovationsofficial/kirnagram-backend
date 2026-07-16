from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# 🔹 Notification model
class NotificationCreate(BaseModel):
    user_id: str  # Firebase UID of user who updated profile
    user_name: str  # Full name of user
    user_image: Optional[str] = None  # Profile image of user
    action: str  # "profile_updated", "cover_updated", "bio_updated", "follow", "follow_request", "follow_approved", etc.
    description: str  # "Updated profile photo" or "Updated bio"
    from_user_id: Optional[str] = None  # For follow actions: who initiated the action
    from_user_name: Optional[str] = None  # For follow actions: name of the user who followed/requested
    from_user_image: Optional[str] = None  # For follow actions: image of the user who followed/requested
    timestamp: datetime = None  # Auto-set to current time

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class Notification(BaseModel):
    user_id: str
    user_name: str
    user_image: Optional[str] = None
    action: str
    description: str
    from_user_id: Optional[str] = None
    from_user_name: Optional[str] = None
    from_user_image: Optional[str] = None
    timestamp: datetime

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


# 🔹 Remix Notification Model
class RemixNotification(BaseModel):
    """Model for remix completion notifications."""
    user_id: str
    message: str
    task_id: str
    is_read: bool = False
    created_at: datetime
    remix_data: Optional[dict] = None  # Store remix result data

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}


class NotificationResponse(BaseModel):
    """Response model for notification endpoints."""
    id: str
    user_id: str
    message: str
    task_id: Optional[str] = None
    is_read: bool
    created_at: datetime
    remix_data: Optional[dict] = None

    class Config:
        json_encoders = {datetime: lambda v: v.isoformat()}
