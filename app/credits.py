from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from app.database import db

DEFAULT_CREDIT_SETTINGS: Dict[str, Any] = {
    "_id": "global",
    "welcome_bonus_enabled": True,
    "welcome_bonus_credits": 10,
    "welcome_bonus_valid_days": 1,
    "daily_ad_enabled": True,
    "daily_ad_credits": 2,
    "daily_ad_limit": 1,
    "paid_plans": [
        {"id": "plan_1", "name": "Plan 1", "credits": 100, "price": 19, "description": ["","","",""]},
        {"id": "plan_2", "name": "Plan 2", "credits": 250, "price": 49, "description": ["","","",""]},
        {"id": "plan_3", "name": "Plan 3", "credits": 500, "price": 99, "description": ["","","",""]},
        {"id": "plan_4", "name": "Plan 4", "credits": 1000, "price": 199, "description": ["","","",""]},
        {"id": "plan_5", "name": "Plan 5", "credits": 2000, "price": 499, "description": ["","","",""]},
        {"id": "plan_6", "name": "Plan 6", "credits": 5000, "price": 999, "description": ["","","",""]},
        {"id": "plan_7", "name": "Plan 7", "credits": 10000, "price": 1999, "description": ["","","",""]},
    ],
    "burn_rates": {
        "chatgpt": {"low": 2, "medium": 4, "high": 6},
        "gemini": {"fast": 2, "standard": 4, "ultra": 6},
    },
    "model_enabled": {
        "chatgpt": True,
        "gemini": True,
    },
}


def _utcnow() -> datetime:
    return datetime.utcnow()


async def get_credit_settings() -> Dict[str, Any]:
    settings = await db.credit_settings.find_one({"_id": "global"})
    if settings:
        return settings
    await db.credit_settings.insert_one(DEFAULT_CREDIT_SETTINGS)
    return DEFAULT_CREDIT_SETTINGS.copy()


async def ensure_wallet(user_id: str) -> Dict[str, Any]:
    wallet = await db.credit_wallets.find_one({"user_id": user_id})
    if wallet:
        return wallet
    now = _utcnow()
    wallet = {
        "user_id": user_id,
        "balance": 0,
        "welcome_bonus_claimed_at": None,
        "last_daily_claim_at": None,
        "daily_claim_count": 0,
        "daily_claim_date": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.credit_wallets.insert_one(wallet)
    return wallet


async def record_transaction(
    user_id: str,
    amount: int,
    tx_type: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = _utcnow()
    doc = {
        "user_id": user_id,
        "amount": amount,
        "type": tx_type,
        "meta": meta or {},
        "created_at": now,
    }
    result = await db.credit_transactions.insert_one(doc)
    doc["_id"] = str(result.inserted_id)
    return doc


async def grant_welcome_bonus_if_eligible(user_id: str, created_at: Optional[datetime]) -> Optional[int]:
    settings = await get_credit_settings()
    if not settings.get("welcome_bonus_enabled"):
        return None

    wallet = await ensure_wallet(user_id)
    if wallet.get("welcome_bonus_claimed_at"):
        return None

    valid_days = int(settings.get("welcome_bonus_valid_days", 0) or 0)
    if not created_at or valid_days <= 0:
        return None

    deadline = created_at + timedelta(days=valid_days)
    if _utcnow() > deadline:
        return None

    amount = int(settings.get("welcome_bonus_credits", 0) or 0)
    if amount <= 0:
        return None

    now = _utcnow()
    await db.credit_wallets.update_one(
        {"user_id": user_id},
        {
            "$inc": {"balance": amount},
            "$set": {"welcome_bonus_claimed_at": now, "updated_at": now},
        },
    )
    await record_transaction(user_id, amount, "welcome_bonus", {"source": "auto"})
    return amount
