from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from bson import ObjectId
from app.config import razorpay_client, RAZORPAY_KEY_ID

from app.credits import ensure_wallet, get_credit_settings, grant_welcome_bonus_if_eligible, record_transaction
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header

router = APIRouter(prefix="/credits", tags=["Credits"])


def _utcnow() -> datetime:
    return datetime.utcnow()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return dt.isoformat() + "Z"


def _date_key(dt: datetime) -> str:
    return dt.date().isoformat()


def get_user_id_from_header(authorization: str) -> str:
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header format")

    try:
        token = extract_token_from_header(authorization)
        payload = verify_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token: missing user ID")
        return user_id
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {str(exc)}")


async def _refresh_daily_claim_state(wallet: Dict[str, Any]) -> Dict[str, Any]:
    now = _utcnow()
    today = _date_key(now)
    if wallet.get("daily_claim_date") != today:
        await db.credit_wallets.update_one(
            {"user_id": wallet.get("user_id")},
            {"$set": {"daily_claim_date": today, "daily_claim_count": 0, "updated_at": now}},
        )
        wallet["daily_claim_date"] = today
        wallet["daily_claim_count"] = 0
    return wallet


def _build_daily_claim_summary(settings: Dict[str, Any], wallet: Dict[str, Any]) -> Dict[str, Any]:
    enabled = bool(settings.get("daily_ad_enabled", True))
    credits = int(settings.get("daily_ad_credits", 0) or 0)
    limit = int(settings.get("daily_ad_limit", 0) or 0)
    count = int(wallet.get("daily_claim_count", 0) or 0)

    remaining = max(0, limit - count) if enabled else 0
    next_available_at = None

    if remaining <= 0 and enabled and limit > 0:
        last_claim = wallet.get("last_daily_claim_at")
        if isinstance(last_claim, datetime):
            next_available_at = _iso(last_claim + timedelta(hours=24))
        else:
            tomorrow = _utcnow().date() + timedelta(days=1)
            next_available_at = datetime.combine(tomorrow, datetime.min.time()).isoformat() + "Z"

    return {
        "enabled": enabled,
        "credits": credits,
        "limit_per_day": limit,
        "remaining": remaining,
        "next_available_at": next_available_at,
    }


@router.get("/summary")
async def get_credits_summary(authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)
    settings = await get_credit_settings()

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    await grant_welcome_bonus_if_eligible(user_id, user.get("created_at"))

    wallet = await ensure_wallet(user_id)
    wallet = await _refresh_daily_claim_state(wallet)

    daily_claim = _build_daily_claim_summary(settings, wallet)

    recent = await db.credit_transactions.find(
        {"user_id": user_id}
    ).sort("created_at", -1).limit(5).to_list(length=5)

    recent_activity = []
    for tx in recent:
        tx["_id"] = str(tx.get("_id"))
        tx["created_at"] = _iso(tx.get("created_at"))
        recent_activity.append(tx)

    return {
        "balance": wallet.get("balance", 0),
        "last_daily_claim_at": _iso(wallet.get("last_daily_claim_at")),
        "welcome_bonus_claimed_at": _iso(wallet.get("welcome_bonus_claimed_at")),
        "welcome_bonus": {
            "enabled": bool(settings.get("welcome_bonus_enabled", True)),
            "credits": int(settings.get("welcome_bonus_credits", 0) or 0),
            "valid_days": int(settings.get("welcome_bonus_valid_days", 0) or 0),
        },
        "daily_claim": daily_claim,
        "paid_plans": settings.get("paid_plans", []),
        "burn_rates": settings.get("burn_rates", {}),
        "model_enabled": settings.get("model_enabled", {}),
        "recent_activity": recent_activity,
    }


@router.post("/claim-daily")
async def claim_daily_credits(authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)
    settings = await get_credit_settings()

    if not settings.get("daily_ad_enabled", True):
        raise HTTPException(status_code=400, detail="Daily claim disabled")

    wallet = await ensure_wallet(user_id)
    wallet = await _refresh_daily_claim_state(wallet)

    limit = int(settings.get("daily_ad_limit", 0) or 0)
    if limit <= 0:
        raise HTTPException(status_code=400, detail="Daily claim not available")

    count = int(wallet.get("daily_claim_count", 0) or 0)
    if count >= limit:
        raise HTTPException(status_code=400, detail="Daily claim already used")

    amount = int(settings.get("daily_ad_credits", 0) or 0)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Daily claim amount is zero")

    now = _utcnow()
    await db.credit_wallets.update_one(
        {"user_id": user_id},
        {
            "$inc": {"balance": amount, "daily_claim_count": 1},
            "$set": {"last_daily_claim_at": now, "daily_claim_date": _date_key(now), "updated_at": now},
        },
    )

    await record_transaction(user_id, amount, "daily_ad", {"source": "ad"})

    updated = await db.credit_wallets.find_one({"user_id": user_id})

    return {
        "amount": amount,
        "balance": updated.get("balance", 0) if updated else amount,
        "last_daily_claim_at": _iso(now),
    }


@router.post("/burn")
async def burn_credits(payload: Dict[str, Any], authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)
    settings = await get_credit_settings()

    model = str(payload.get("model", "")).lower()
    quality = str(payload.get("quality", "")).lower()
    prompt_id = payload.get("prompt_id")

    if not model or not quality:
        raise HTTPException(status_code=400, detail="Model and quality are required")

    if not settings.get("model_enabled", {}).get(model, True):
        raise HTTPException(status_code=400, detail="Model disabled")

    rate = settings.get("burn_rates", {}).get(model, {}).get(quality)
    if rate is None:
        raise HTTPException(status_code=400, detail="Unsupported quality")

    wallet = await ensure_wallet(user_id)
    balance = int(wallet.get("balance", 0) or 0)
    rate = int(rate)

    if balance < rate:
        raise HTTPException(status_code=400, detail="Not enough credits")

    now = _utcnow()
    await db.credit_wallets.update_one(
        {"user_id": user_id},
        {"$inc": {"balance": -rate}, "$set": {"updated_at": now}},
    )

    await record_transaction(
        user_id,
        -rate,
        "burn",
        {"model": model, "quality": quality, "prompt_id": prompt_id},
    )

    # 📢 Send notification about credit burn
    try:
        notification_doc = {
            "user_id": user_id,
            "action": "credits_burned",
            "description": f"You spent {rate} credits for {model.capitalize()} ({quality.capitalize()})",
            "timestamp": now,
            "read": False,
            "created_at": now,
        }
        await db.notifications.insert_one(notification_doc)
    except Exception:
        pass  # Don't fail if notification fails

    updated = await db.credit_wallets.find_one({"user_id": user_id})

    return {
        "amount": rate,
        "balance": updated.get("balance", 0) if updated else balance - rate,
    }


# 🔹 Create Order API
class CreateOrderRequest(BaseModel):
    amount: int


@router.post("/create-order")
async def create_order(data: CreateOrderRequest, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    try:
        if not RAZORPAY_KEY_ID:
            raise HTTPException(status_code=500, detail="Razorpay not configured")

        order = razorpay_client.order.create({
            "amount": data.amount * 100,
            "currency": "INR",
            "payment_capture": 1
        })

        return {
            "order_id": order["id"],
            "amount": order["amount"]
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# 🔹 Verify Payment + Add Credits
@router.post("/verify-payment")
async def verify_payment(payload: dict, authorization: str = Header(...)):

    user_id = get_user_id_from_header(authorization)

    try:
        # 1️⃣ Validate required fields
        if not payload.get("razorpay_order_id") or \
           not payload.get("razorpay_payment_id") or \
           not payload.get("razorpay_signature"):
            raise HTTPException(status_code=400, detail="Missing payment fields")

        # 2️⃣ Verify signature
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": payload["razorpay_order_id"],
            "razorpay_payment_id": payload["razorpay_payment_id"],
            "razorpay_signature": payload["razorpay_signature"]
        })

        # 3️⃣ Fetch payment
        payment = razorpay_client.payment.fetch(payload["razorpay_payment_id"])

        if payment["status"] != "captured":
            raise HTTPException(status_code=400, detail="Payment not captured")

        # 4️⃣ Fetch order
        order = razorpay_client.order.fetch(payload["razorpay_order_id"])

        if order["amount"] != payment["amount"]:
            raise HTTPException(status_code=400, detail="Amount mismatch")

        # 5️⃣ Convert to INR
        amount_paid = int(payment["amount"] / 100)

        # 6️⃣ Get plan securely from backend settings
        settings = await get_credit_settings()

        plan = next(
            (p for p in settings.get("paid_plans", [])
             if int(p["price"]) == amount_paid),
            None
        )

        if not plan:
            raise HTTPException(status_code=400, detail="Invalid plan")

        credits = int(plan["credits"])

        # 7️⃣ Prevent duplicate processing
        existing = await db.credit_transactions.find_one({
            "meta.order_id": payload["razorpay_order_id"]
        })

        if existing:
            return {"message": "Already processed"}

        # 8️⃣ Ensure wallet exists
        await ensure_wallet(user_id)

        # 9️⃣ Add credits
        await db.credit_wallets.update_one(
            {"user_id": user_id},
            {"$inc": {"balance": credits}}
        )

        # 🔟 Record transaction
        await record_transaction(user_id, credits, "purchase", {
            "order_id": payload["razorpay_order_id"],
            "amount_paid": amount_paid,
            "credits": credits,
        })

        # 📢 Send notification about successful credit purchase
        try:
            notification_doc = {
                "user_id": user_id,
                "action": "credits_purchased",
                "description": f"Payment successful! You received {credits} credits",
                "timestamp": _utcnow(),
                "read": False,
                "created_at": _utcnow(),
            }
            await db.notifications.insert_one(notification_doc)
        except Exception:
            pass  # Don't fail if notification fails

        return {"message": "Payment verified & credits added"}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

