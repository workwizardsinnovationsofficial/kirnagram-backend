from pydantic import BaseModel
from typing import Optional, List

# 🔹 Used when creating user (manual / google / facebook)
class UserCreate(BaseModel):
    # 🔐 Identity (from Firebase)
    full_name: Optional[str] = None
    mobile: Optional[str] = None

    # 🧑 Editable profile fields
    username: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None

    # 🆔 Public profile ID (auto-generated k0001)
    public_id: Optional[str] = None

    # 🖼 Images
    image_name: Optional[str] = None

    # 🔑 Auth provider
    provider: Optional[str] = None  # "manual" | "google" | "facebook"

    # 🤝 Social (SAFE default)
    followers: Optional[List[str]] = None
    following: Optional[List[str]] = None
    account_type: Optional[str] = "public"  # "public" or "private"
    approved_followers: Optional[List[str]] = None  # For private accounts: approved followers
    follow_requests: Optional[List[str]] = None  # For private accounts: pending follow requests


# 🔹 Used for Edit Profile (PUT /profile/update)
class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    dob: Optional[str] = None
    username: Optional[str] = None
    bio: Optional[str] = None
    location: Optional[str] = None
    website: Optional[str] = None
    website_name: Optional[str] = None
    mobile: Optional[str] = None
    email: Optional[str] = None
    public_id: Optional[str] = None
    gender: Optional[str] = None  # "male" or "female"
    image_name: Optional[str] = None  # profile image url
    cover_image: Optional[str] = None  # cover image url
    account_type: Optional[str] = None  # "public" or "private"
    # Social media fields
    instagram: Optional[str] = None
    youtube: Optional[str] = None
    facebook: Optional[str] = None
    x: Optional[str] = None
    linkedin: Optional[str] = None
    whatsapp: Optional[str] = None
    skip_notification: Optional[bool] = False  # Skip notification creation (for image uploads)

# 🔹 Follow Request Models
class FollowRequestModel(BaseModel):
    user_id: str
    username: str
    profile_image: Optional[str] = None
    status: str  # "pending" or "approved"


class ApproveFollowRequestModel(BaseModel):
    action: str  # "approve" or "reject"