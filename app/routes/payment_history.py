from datetime import datetime
from fastapi import APIRouter, Header, HTTPException
from bson import ObjectId
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token
from app.credits import get_credit_settings

router = APIRouter(prefix="/payment", tags=["Payment History"])


def _iso(dt):
    """Convert datetime to ISO format string"""
    if not dt:
        return None
    if isinstance(dt, datetime):
        return dt.isoformat() + "Z"
    return dt


def get_user_id_from_header(authorization: str) -> str:
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


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


async def _compute_withdrawable_balance(user_id: str) -> int:
    prompts = await db.ai_creator_prompts.find({"user_id": user_id}).to_list(None)

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

    total_earned = sum(_to_int(remix.get("payout_per_remix", 1), 1) for remix in all_remixes)

    withdrawn_rows = await db.withdraw_requests.aggregate([
        {"$match": {"user_id": user_id, "status": {"$in": ["approved", "paid"]}}},
        {"$group": {"_id": None, "sum": {"$sum": "$amount"}}},
    ]).to_list(1)
    total_withdrawn = _to_int(withdrawn_rows[0].get("sum", 0), 0) if withdrawn_rows else 0

    return max(0, total_earned - total_withdrawn)


@router.get("/history")
async def get_payment_history(authorization: str = Header(...)):
    """Get all payment transactions (credits purchases, ad payments, withdrawals)"""
    user_id = get_user_id_from_header(authorization)

    try:
        # Fetch credit transactions (only purchases are monetary)
        credit_txs = await db.credit_transactions.find(
            {"user_id": user_id}
        ).sort("created_at", -1).to_list(length=100)

        # Fetch ad payments
        ad_payments = await db.ad_payments.find(
            {"publisher_id": user_id}
        ).sort("created_at", -1).to_list(length=100)

        # Fetch withdrawal history
        withdrawals = await db.withdraw_requests.find(
            {"user_id": user_id}
        ).sort("created_at", -1).to_list(length=100)

        settings = await get_credit_settings()
        paid_plans = settings.get("paid_plans", [])
        credits_to_price = {
            _to_int(plan.get("credits", 0), 0): _to_int(plan.get("price", 0), 0)
            for plan in paid_plans
            if _to_int(plan.get("credits", 0), 0) > 0
        }

        # Combine and format all transactions
        transactions = []

        # Add credit purchases in INR
        for tx in credit_txs:
            tx_type = str(tx.get("type", "")).lower()
            if tx_type != "purchase":
                continue

            purchased_credits = _to_int(tx.get("amount", 0), 0)
            meta = tx.get("meta") or {}
            amount_inr = _to_int(meta.get("amount_paid", 0), 0)
            if amount_inr <= 0:
                amount_inr = credits_to_price.get(purchased_credits, 0)

            transactions.append({
                "id": str(tx.get("_id", "")),
                "type": "Credit Purchase",
                "category": "credits",
                "amount": amount_inr,
                "timestamp": _iso(tx.get("created_at")),
                "status": "completed",
                "icon": "ShoppingCart",
                "description": f"Purchased {purchased_credits} credits"
            })

        # Add ad payments
        for payment in ad_payments:
            amount_paise = _to_int(payment.get("amount", 0), 0)
            amount_inr = int(round(amount_paise / 100))
            transactions.append({
                "id": str(payment.get("_id", "")),
                "type": "Ad Campaign Payment",
                "category": "ads",
                "amount": -abs(amount_inr),
                "timestamp": _iso(payment.get("created_at")),
                "status": payment.get("status", "pending"),
                "icon": "Megaphone",
                "description": f"Ad: {payment.get('ad_name', '')} - {payment.get('duration_days', 0)} days"
            })

        # Add withdrawals
        for withdrawal in withdrawals:
            status_icon = {
                "pending": "Clock",
                "approved": "CheckCircle2",
                "paid": "CheckCircle2",
                "rejected": "XCircle"
            }.get(withdrawal.get("status", "pending"), "Clock")

            withdrawal_amount = _to_int(withdrawal.get("amount", 0), 0)

            transactions.append({
                "id": str(withdrawal.get("_id", "")),
                "type": "Withdrawal",
                "category": "withdrawals",
                "amount": -abs(withdrawal_amount),
                "timestamp": _iso(withdrawal.get("created_at")),
                "status": withdrawal.get("status", "pending"),
                "icon": status_icon,
                "description": f"Withdrawal to {withdrawal.get('upi_id', '')} - {withdrawal.get('status', 'pending')}"
            })

        # Sort by timestamp descending
        transactions.sort(key=lambda x: x["timestamp"] or "", reverse=True)

        # Calculate totals by category
        totals = {
            "credits_purchased": sum(
                max(0, _to_int(t.get("amount", 0), 0))
                for t in transactions
                if t.get("category") == "credits"
            ),
            "ads_spent": sum(
                abs(_to_int(t.get("amount", 0), 0))
                for t in transactions
                if t.get("category") == "ads" and str(t.get("status", "")).lower() in {"completed", "paid"}
            ),
            "withdrawn": sum(
                abs(_to_int(t.get("amount", 0), 0))
                for t in transactions
                if t.get("category") == "withdrawals" and str(t.get("status", "")).lower() == "paid"
            ),
            "withdrawable": await _compute_withdrawable_balance(user_id),
        }

        return {
            "transactions": transactions[:100],  # Return last 100 combined transactions
            "totals": totals,
            "count": len(transactions)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch payment history: {str(e)}")
