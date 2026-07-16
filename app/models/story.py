from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime

# 🎬 Story Creation Request
class StoryCreate(BaseModel):
    media_type: str  # "image" or "video"
    duration: int  # duration in seconds (10 for images, max 30 for videos)
    text: Optional[str] = None  # text overlay on story
    emoji_stickers: Optional[List[dict]] = None  # [{"emoji": "❤️", "x": 10, "y": 20}, ...]
    drawing_data: Optional[str] = None  # base64 encoded drawing or canvas data
    music_url: Optional[str] = None  # music URL if added
    visibility: str = "public"  # "public" or "private"



class StoryResponse(BaseModel):
    story_id: str
    user_id: str
    username: str
    user_image: Optional[str] = None

    media_url: str
    media_type: str
    duration: int

    text: Optional[str] = None
    emoji_stickers: Optional[List[dict]] = None
    drawing_data: Optional[str] = None
    music_url: Optional[str] = None

    created_at: datetime
    expires_at: datetime
    views_count: int
    likes_count: int
    liked_by_user: bool
    viewed_by_user: bool




# 📊 Story Stats (for story owner)
class StoryStats(BaseModel):
    story_id: str
    views_count: int
    likes_count: int
    viewers: List[dict]  # [{"user_id": "...", "username": "...", "image": "...", "viewed_at": "..."}, ...]
    likers: List[dict]  # [{"user_id": "...", "username": "...", "image": "..."}, ...]


# ❤️ Like/Unlike Story
class StoryLike(BaseModel):
    story_id: str
    user_id: str
    liked_at: datetime


# 👁️ View Story
class StoryView(BaseModel):
    story_id: str
    user_id: str
    viewed_at: datetime


# 📱 My Stories List (what I've posted)
class MyStoryResponse(BaseModel):
    story_id: str
    media_url: str
    media_type: str
    created_at: datetime
    expires_at: datetime
    views_count: int
    likes_count: int
    remaining_hours: int  # hours until expiry


# 👥 Friends Stories List
class FriendStoriesResponse(BaseModel):
    user_id: str
    username: str
    user_image: Optional[str]
    gender: Optional[str] = None
    stories: List[StoryResponse]
    unviewed_count: int
