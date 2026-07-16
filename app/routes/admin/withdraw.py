from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.database import db
from bson import ObjectId

admin_router = APIRouter(prefix="/admin/withdraw", tags=["Withdraw Admin"])

WITHDRAW_SETTINGS_ID = "withdraw_settings"
DEFAULT_MIN_WITHDRAW_AMOUNT = 100


class WithdrawSettingsPayload(BaseModel):
    min_withdraw_amount: int


async def get_withdraw_settings_doc():
    settings = await db.settings.find_one({"_id": WITHDRAW_SETTINGS_ID})
    if settings:
        return settings

    defaults = {
        "_id": WITHDRAW_SETTINGS_ID,
        "min_withdraw_amount": DEFAULT_MIN_WITHDRAW_AMOUNT,
        "updated_at": datetime.utcnow(),
    }
    await db.settings.insert_one(defaults)
    return defaults


@admin_router.get("/settings")
async def get_withdraw_settings():
    settings = await get_withdraw_settings_doc()
    return {
        "min_withdraw_amount": int(settings.get("min_withdraw_amount", DEFAULT_MIN_WITHDRAW_AMOUNT) or DEFAULT_MIN_WITHDRAW_AMOUNT)
    }


@admin_router.put("/settings")
async def update_withdraw_settings(payload: WithdrawSettingsPayload):
    amount = int(payload.min_withdraw_amount or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="min_withdraw_amount must be greater than 0")

    await db.settings.update_one(
        {"_id": WITHDRAW_SETTINGS_ID},
        {"$set": {"min_withdraw_amount": amount, "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return {"success": True, "min_withdraw_amount": amount}

@admin_router.get("/requests")
async def get_withdraw_requests():
    # Get all withdrawal requests
    requests = await db.withdraw_requests.find().sort("created_at", -1).to_list(None)
    settings = await get_withdraw_settings_doc()
    min_withdraw_amount = int(settings.get("min_withdraw_amount", DEFAULT_MIN_WITHDRAW_AMOUNT) or DEFAULT_MIN_WITHDRAW_AMOUNT)

    result = []
    for req in requests:
        user_id = req.get("user_id")

        # Fetch user profile for full details
        profile = await db.users.find_one({"firebase_uid": user_id})
        creator_app = await db.ai_creator_applications.find_one({"user_id": user_id})

        full_name = (
            (profile or {}).get("full_name")
            or (creator_app or {}).get("full_name")
            or ""
        )
        username = (profile or {}).get("username", "")
        email = (
            (profile or {}).get("email")
            or (creator_app or {}).get("email")
            or ""
        )
        mobile = (
            (profile or {}).get("mobile")
            or (creator_app or {}).get("mobile")
            or ""
        )
        gender = (profile or {}).get("gender", "")

        # Aggregate creator stats
        prompts = await db.ai_creator_prompts.find({"user_id": user_id}).to_list(None)
        total_prompts = len(prompts)
        total_remixes = sum(len(p.get("remixes", [])) for p in prompts)
        total_likes = sum(len(p.get("likes", [])) for p in prompts)
        total_views = sum(len(p.get("views", [])) for p in prompts)
        # Calculate total earned from remix IDs attached to creator prompts.
        remix_object_ids = []
        for prompt in prompts:
            for remix_id in prompt.get("remixes", []):
                if isinstance(remix_id, ObjectId):
                    remix_object_ids.append(remix_id)
                elif isinstance(remix_id, str) and ObjectId.is_valid(remix_id):
                    remix_object_ids.append(ObjectId(remix_id))

        all_remixes = []
        if remix_object_ids:
            all_remixes = await db.ai_creator_remixes.find({"_id": {"$in": remix_object_ids}}).to_list(None)

        total_earned = sum(int(remix.get("payout_per_remix", 1) or 1) for remix in all_remixes)
        total_withdrawn = await db.withdraw_requests.aggregate([
            {"$match": {"user_id": user_id, "status": {"$in": ["approved", "paid"]}}},
            {"$group": {"_id": None, "sum": {"$sum": "$amount"}}}
        ]).to_list(1)
        total_withdrawn = total_withdrawn[0]["sum"] if total_withdrawn else 0
        remaining = max(0, total_earned - total_withdrawn)

        if isinstance(req.get("_id"), ObjectId):
            req["_id"] = str(req.get("_id"))
        result.append({
            **req,
            "full_name": full_name,
            "username": username,
            "email": email,
            "mobile": mobile,
            "gender": gender,
            "min_withdraw_amount": min_withdraw_amount,
            "total_prompts": total_prompts,
            "total_remixes": total_remixes,
            "total_likes": total_likes,
            "total_views": total_views,
            "total_earned": total_earned,
            "total_withdrawn": total_withdrawn,
            "remaining": remaining,
        })
    return result


@admin_router.post("/requests/{request_id}/approve")
async def approve_withdraw_request(request_id: str):
    if not ObjectId.is_valid(request_id):
        raise HTTPException(status_code=400, detail="Invalid request id")

    request_doc = await db.withdraw_requests.find_one({"_id": ObjectId(request_id)})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Withdraw request not found")
    if request_doc.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be approved")

    await db.withdraw_requests.update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": "approved", "updated_at": datetime.utcnow(), "reason": None}},
    )
    return {"success": True, "request_id": request_id, "status": "approved"}


@admin_router.post("/requests/{request_id}/reject")
async def reject_withdraw_request(request_id: str, reason: Optional[str] = None):
    if not ObjectId.is_valid(request_id):
        raise HTTPException(status_code=400, detail="Invalid request id")

    request_doc = await db.withdraw_requests.find_one({"_id": ObjectId(request_id)})
    if not request_doc:
        raise HTTPException(status_code=404, detail="Withdraw request not found")
    if request_doc.get("status") != "pending":
        raise HTTPException(status_code=400, detail="Only pending requests can be rejected")

    await db.withdraw_requests.update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": "rejected", "updated_at": datetime.utcnow(), "reason": reason or "Rejected by admin"}},
    )
    return {"success": True, "request_id": request_id, "status": "rejected"}
