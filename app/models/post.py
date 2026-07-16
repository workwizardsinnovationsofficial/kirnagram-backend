from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


SUPPORTED_POST_RATIOS = {"9:16", "16:9", "1:1"}


def normalize_post_ratio(ratio: Optional[str]) -> str:
    if not ratio:
        return "1:1"
    normalized = str(ratio).strip()
    return normalized if normalized in SUPPORTED_POST_RATIOS else "1:1"


class PostCreate(BaseModel):
    caption: Optional[str] = None
    tags: Optional[List[str]] = []
    ratio: str = "1:1"
    type: Optional[str] = "image"  # "image" or "video"
    image_url: Optional[str] = None
    video_url: Optional[str] = None


class PostResponse(BaseModel):
    post_id: str
    user_id: str
    type: str
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    caption: Optional[str]
    tags: List[str]
    likes_count: int
    comments_count: int
    created_at: datetime
