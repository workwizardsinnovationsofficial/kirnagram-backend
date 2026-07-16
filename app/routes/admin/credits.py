from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.credits import DEFAULT_CREDIT_SETTINGS, get_credit_settings
from app.database import db

admin_router = APIRouter(prefix="/admin/credits", tags=["Credits Admin"])


class PaidPlan(BaseModel):
    id: str
    name: str
    credits: int
    price: Optional[int] = None
    description: Optional[List[str]] = None


class BurnRates(BaseModel):
    chatgpt: Dict[str, int]
    gemini: Dict[str, int]


class ModelEnabled(BaseModel):
    chatgpt: bool
    gemini: bool


class CreditSettingsUpdate(BaseModel):
    welcome_bonus_enabled: Optional[bool] = None
    welcome_bonus_credits: Optional[int] = None
    welcome_bonus_valid_days: Optional[int] = None
    daily_ad_enabled: Optional[bool] = None
    daily_ad_credits: Optional[int] = None
    daily_ad_limit: Optional[int] = None
    paid_plans: Optional[List[PaidPlan]] = None
    burn_rates: Optional[BurnRates] = None
    model_enabled: Optional[ModelEnabled] = None


@admin_router.get("/settings")
async def get_settings():
    settings = await get_credit_settings()
    return settings


@admin_router.put("/settings")
async def update_settings(payload: CreditSettingsUpdate):
    settings = await get_credit_settings()
    update_data: Dict[str, Any] = {}

    for field, value in payload.dict(exclude_none=True).items():
        update_data[field] = value

    if not update_data:
        raise HTTPException(status_code=400, detail="No changes provided")

    await db.credit_settings.update_one(
        {"_id": settings.get("_id", "global")},
        {"$set": update_data},
        upsert=True,
    )

    updated = DEFAULT_CREDIT_SETTINGS.copy()
    updated.update(settings)
    updated.update(update_data)
    
    # Send notification to all users about credit settings change
    try:
        all_users = await db.users.find({}).to_list(length=None)
        notification_message = "Admin updated credit settings"
        
        # Check what changed to create a specific message
        if "paid_plans" in update_data:
            notification_message = "Credit pricing plans updated"
        elif "daily_ad_credits" in update_data or "daily_ad_enabled" in update_data:
            notification_message = "Daily ad credits settings updated"
        elif "welcome_bonus_credits" in update_data or "welcome_bonus_enabled" in update_data:
            notification_message = "Welcome bonus updated"
        elif "burn_rates" in update_data:
            notification_message = "Credit burn rates updated"
        
        for user in all_users:
            if user.get("firebase_uid"):
                notification_doc = {
                    "user_id": user.get("firebase_uid"),
                    "action": "credit_settings_updated",
                    "description": notification_message,
                    "timestamp": datetime.utcnow(),
                    "read": False,
                    "created_at": datetime.utcnow(),
                }
                try:
                    await db.notifications.insert_one(notification_doc)
                except Exception:
                    # Continue notifying other users even if one fails
                    pass
    except Exception as e:
        # Log but don't fail the settings update if notifications fail
        print(f"Failed to send credit settings notifications: {str(e)}")
    
    return updated
