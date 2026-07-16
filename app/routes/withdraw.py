from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from bson import ObjectId

from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token

router = APIRouter(prefix="/withdraw", tags=["Withdraw"])

WITHDRAW_SETTINGS_ID = "withdraw_settings"
DEFAULT_MIN_WITHDRAW_AMOUNT = 100


class WithdrawRequestPayload(BaseModel):
    upiId: str
    amount: int


def _get_user_id_from_auth_header(authorization: str) -> str:
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    scheme, token = authorization.split(" ", 1)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    try:
        decoded = verify_firebase_token(token)
        return decoded["uid"]
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(exc)}")


async def get_withdraw_settings() -> Dict[str, Any]:
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


async def _compute_creator_earnings(user_id: str) -> Dict[str, int]:
    prompts = await db.ai_creator_prompts.find({"user_id": user_id}).to_list(None)
    total_prompts = len(prompts)

    # Earnings belong to prompt owner, so compute from remix IDs attached to creator prompts.
    remix_object_ids = []
    for prompt in prompts:
        for remix_id in prompt.get("remixes", []):
            if isinstance(remix_id, ObjectId):
                remix_object_ids.append(remix_id)
            elif isinstance(remix_id, str) and ObjectId.is_valid(remix_id):
                remix_object_ids.append(ObjectId(remix_id))

    total_remixes = len(remix_object_ids)
    all_remixes = []
    if remix_object_ids:
        all_remixes = await db.ai_creator_remixes.find({"_id": {"$in": remix_object_ids}}).to_list(None)

    # Each remix stores payout_per_remix at creation time (historical lock).
    total_earned = sum(int(remix.get("payout_per_remix", 1) or 1) for remix in all_remixes)

    withdrawn_rows = await db.withdraw_requests.aggregate([
        {"$match": {"user_id": user_id, "status": {"$in": ["approved", "paid"]}}},
        {"$group": {"_id": None, "sum": {"$sum": "$amount"}}},
    ]).to_list(1)
    total_withdrawn = withdrawn_rows[0]["sum"] if withdrawn_rows else 0

    return {
        "totalPrompts": total_prompts,
        "totalRemixes": total_remixes,
        "totalEarnings": total_earned,
        "totalWithdrawn": int(total_withdrawn or 0),
        "availableBalance": max(0, int(total_earned or 0) - int(total_withdrawn or 0)),
    }


@router.get("/min-withdraw")
async def get_min_withdraw_amount():
    settings = await get_withdraw_settings()
    return {"minWithdrawAmount": int(settings.get("min_withdraw_amount", DEFAULT_MIN_WITHDRAW_AMOUNT) or DEFAULT_MIN_WITHDRAW_AMOUNT)}


@router.get("/summary")
async def get_withdraw_summary(authorization: str = Header(...)):
    user_id = _get_user_id_from_auth_header(authorization)
    earnings = await _compute_creator_earnings(user_id)
    settings = await get_withdraw_settings()
    min_withdraw_amount = int(settings.get("min_withdraw_amount", DEFAULT_MIN_WITHDRAW_AMOUNT) or DEFAULT_MIN_WITHDRAW_AMOUNT)

    return {
        **earnings,
        "minWithdrawAmount": min_withdraw_amount,
        "canWithdraw": earnings["availableBalance"] >= min_withdraw_amount,
    }


@router.post("/request")
async def create_withdraw_request(payload: WithdrawRequestPayload, authorization: str = Header(...)):
    user_id = _get_user_id_from_auth_header(authorization)

    upi_id = (payload.upiId or "").strip()
    amount = int(payload.amount or 0)
    if not upi_id:
        raise HTTPException(status_code=400, detail="UPI ID is required")

    settings = await get_withdraw_settings()
    min_withdraw_amount = int(settings.get("min_withdraw_amount", DEFAULT_MIN_WITHDRAW_AMOUNT) or DEFAULT_MIN_WITHDRAW_AMOUNT)
    if amount < min_withdraw_amount:
        raise HTTPException(status_code=400, detail=f"Minimum withdraw amount is Rs {min_withdraw_amount}")

    earnings = await _compute_creator_earnings(user_id)
    if amount > earnings["availableBalance"]:
        raise HTTPException(status_code=400, detail="Insufficient withdrawable balance")

    existing_pending = await db.withdraw_requests.find_one({"user_id": user_id, "status": "pending"})
    if existing_pending:
        raise HTTPException(status_code=400, detail="You already have a pending withdraw request")

    now = datetime.utcnow()
    result = await db.withdraw_requests.insert_one({
        "user_id": user_id,
        "upi_id": upi_id,
        "amount": amount,
        "status": "pending",
        "reason": None,
        "created_at": now,
        "updated_at": now,
    })

    return {
        "success": True,
        "requestId": str(result.inserted_id),
        "amount": amount,
        "minWithdrawAmount": min_withdraw_amount,
        "availableBalance": earnings["availableBalance"] - amount,
    }


@router.get("/history")
async def get_withdraw_history(authorization: str = Header(...)):
    user_id = _get_user_id_from_auth_header(authorization)
    requests = await db.withdraw_requests.find(
        {"user_id": user_id}
    ).sort("created_at", -1).to_list(None)

    return {
        "history": [
            {
                "id": str(r["_id"]),
                "amount": r.get("amount", 0),
                "upi_id": r.get("upi_id", ""),
                "status": r.get("status", "pending"),
                "reason": r.get("reason"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
            for r in requests
        ]
    }