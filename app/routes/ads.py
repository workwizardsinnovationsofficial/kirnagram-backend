from datetime import datetime, timedelta
from typing import List, Literal, Optional
from uuid import uuid4
import random

from bson import ObjectId
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field, model_validator
from pymongo import ReturnDocument

from app.database import db
from app.firebase import verify_firebase_token
from app.config import razorpay_client, RAZORPAY_KEY_ID

user_router = APIRouter(prefix="/ads", tags=["Ads"])
admin_router = APIRouter(prefix="/admin/ads", tags=["Admin Ads"])


class PublisherApplicationCreate(BaseModel):
    full_name: str = Field(min_length=2)
    business_name: str = Field(min_length=2)
    business_type: str = Field(min_length=2)
    registration_type: Literal["registered", "unregistered"]
    gst: Optional[str] = None
    cin: Optional[str] = None
    legal_name: Optional[str] = None
    msme: Optional[str] = None
    target_audience: str = Field(min_length=2)
    target_region: List[str] = Field(min_items=1)
    govt_id: Optional[str] = None

    @model_validator(mode="after")
    def validate_registration_details(self):
        if self.registration_type == "registered":
            optional_ids = [
                (self.gst or "").strip(),
                (self.cin or "").strip(),
                (self.legal_name or "").strip(),
                (self.msme or "").strip(),
            ]
            if not any(optional_ids):
                raise ValueError("For registered businesses, add at least one of GST/CIN/Legal Name/MSME")
        return self


class PublisherApplicationStatusUpdate(BaseModel):
    status: Literal["approved", "rejected"]
    admin_note: Optional[str] = None


class CampaignHomeVisibilityUpdate(BaseModel):
    enabled: bool


class CampaignPlacementVisibilityUpdate(BaseModel):
    home_banner_enabled: Optional[bool] = None
    discover_banner_enabled: Optional[bool] = None
    claims_banner_enabled: Optional[bool] = None
    feed_inline_enabled: Optional[bool] = None


class CampaignCreate(BaseModel):
    ad_name: str = Field(min_length=2)
    business_name: str = Field(min_length=2)
    description: Optional[str] = None
    logo_url: Optional[str] = None
    photo_preview_url: Optional[str] = None
    video_preview_url: Optional[str] = None
    video_duration_seconds: Optional[int] = None
    website_url: Optional[str] = None
    target_audience: str = Field(min_length=2)
    target_region: List[str] = Field(min_items=1)
    budget_mode: Literal["custom", "package"]
    ad_type: Literal["standard", "full"]
    custom_budget: Optional[float] = None
    package_name: Optional[str] = None
    package_duration_days: Optional[int] = None
    package_total_budget: Optional[float] = None

    @model_validator(mode="after")
    def validate_budget_mode(self):
        mode = self.budget_mode
        if self.video_duration_seconds and self.video_duration_seconds > 120:
            raise ValueError("Video preview duration must be 120 seconds or less")

        if mode == "custom":
            if not self.custom_budget or self.custom_budget <= 0:
                raise ValueError("custom_budget must be greater than 0")
        else:
            if not self.package_name:
                raise ValueError("package_name is required for package mode")
            if not self.package_duration_days or self.package_duration_days <= 0:
                raise ValueError("package_duration_days must be greater than 0")
            if not self.package_total_budget or self.package_total_budget <= 0:
                raise ValueError("package_total_budget must be greater than 0")
        return self


class BusinessProfileUpdate(BaseModel):
    business_name: str = Field(min_length=2)
    about: Optional[str] = None
    whatsapp: Optional[str] = None
    website: Optional[str] = None
    address: Optional[str] = None
    headquarters: Optional[str] = None


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    scheme, token = authorization.split(" ", 1)
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    return token


def _serialize_application(doc: dict) -> dict:
    return {
        "_id": str(doc.get("_id")),
        "user_id": doc.get("user_id"),
        "status": doc.get("status", "pending"),
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
        "reviewed_at": doc.get("reviewed_at").isoformat() if doc.get("reviewed_at") else None,
        "admin_note": doc.get("admin_note"),
        "user": {
            "firebase_uid": doc.get("user", {}).get("firebase_uid"),
            "username": doc.get("user", {}).get("username"),
            "full_name": doc.get("user", {}).get("full_name"),
            "email": doc.get("user", {}).get("email"),
            "mobile": doc.get("user", {}).get("mobile"),
            "image_name": doc.get("user", {}).get("image_name"),
        },
        "application": {
            "full_name": doc.get("application", {}).get("full_name"),
            "business_name": doc.get("application", {}).get("business_name"),
            "business_type": doc.get("application", {}).get("business_type"),
            "govt_id": doc.get("application", {}).get("govt_id"),
            "registration_type": doc.get("application", {}).get("registration_type"),
            "gst": doc.get("application", {}).get("gst"),
            "cin": doc.get("application", {}).get("cin"),
            "legal_name": doc.get("application", {}).get("legal_name"),
            "msme": doc.get("application", {}).get("msme"),
            "target_audience": doc.get("application", {}).get("target_audience"),
            "target_region": doc.get("application", {}).get("target_region", []),
        },
    }


def _serialize_campaign(doc: dict) -> dict:
    now = datetime.utcnow()
    ends_at = doc.get("ends_at")
    days_left = 0
    if isinstance(ends_at, datetime):
        days_left = max(0, (ends_at - now).days)

    home_banner_enabled = bool(doc.get("home_banner_enabled", doc.get("home_enabled", False)))
    discover_banner_enabled = bool(doc.get("discover_banner_enabled", False))
    claims_banner_enabled = bool(doc.get("claims_banner_enabled", False))
    feed_inline_enabled = bool(doc.get("feed_inline_enabled", False))

    return {
        "_id": str(doc.get("_id")),
        "publisher_id": doc.get("publisher_id"),
        "ad_name": doc.get("ad_name"),
        "business_name": doc.get("business_name"),
        "description": doc.get("description"),
        "logo_url": doc.get("logo_url"),
        "photo_preview_url": doc.get("photo_preview_url"),
        "video_preview_url": doc.get("video_preview_url"),
        "video_duration_seconds": doc.get("video_duration_seconds"),
        "website_url": doc.get("website_url"),
        "target_audience": doc.get("target_audience"),
        "target_region": doc.get("target_region", []),
        "budget_mode": doc.get("budget_mode"),
        "ad_type": doc.get("ad_type"),
        "custom_budget": doc.get("custom_budget"),
        "daily_price": doc.get("daily_price"),
        "package_name": doc.get("package_name"),
        "package_duration_days": doc.get("package_duration_days"),
        "package_total_budget": doc.get("package_total_budget"),
        "started_at": doc.get("started_at").isoformat() if doc.get("started_at") else None,
        "ends_at": ends_at.isoformat() if isinstance(ends_at, datetime) else None,
        "status": doc.get("status", "active"),
        "home_enabled": home_banner_enabled,
        "home_banner_enabled": home_banner_enabled,
        "discover_banner_enabled": discover_banner_enabled,
        "claims_banner_enabled": claims_banner_enabled,
        "feed_inline_enabled": feed_inline_enabled,
        "days_left": days_left,
        "metrics": {
            "views": doc.get("metrics", {}).get("views", 0),
            "detail_clicks": doc.get("metrics", {}).get("detail_clicks", 0),
        },
        "created_at": doc.get("created_at").isoformat() if doc.get("created_at") else None,
        "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
    }


async def _get_latest_application(user_id: str) -> Optional[dict]:
    return await db.publisher_applications.find_one(
        {"user_id": user_id},
        sort=[("created_at", -1)],
    )


async def _require_approved_publisher(user_id: str) -> dict:
    latest = await _get_latest_application(user_id)
    if not latest or latest.get("status") != "approved":
        raise HTTPException(status_code=403, detail="Only approved publishers can access this resource")
    return latest


@user_router.post("/publisher-applications")
async def submit_publisher_application(
    payload: PublisherApplicationCreate,
    authorization: str = Header(...),
):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing_pending = await db.publisher_applications.find_one({
        "user_id": user_id,
        "status": "pending",
    })
    if existing_pending:
        raise HTTPException(status_code=400, detail="A pending application already exists")

    now = datetime.utcnow()
    doc = {
        "user_id": user_id,
        "status": "pending",
        "admin_note": None,
        "user": {
            "firebase_uid": user.get("firebase_uid"),
            "username": user.get("username"),
            "full_name": user.get("full_name") or user.get("name") or payload.full_name,
            "email": user.get("email"),
            "mobile": user.get("mobile"),
            "image_name": user.get("image_name"),
        },
        "application": {
            "full_name": payload.full_name.strip(),
            "business_name": payload.business_name.strip(),
            "business_type": payload.business_type.strip(),
            "govt_id": (payload.govt_id or "").strip() or None,
            "registration_type": payload.registration_type,
            "gst": (payload.gst or "").strip() or None,
            "cin": (payload.cin or "").strip() or None,
            "legal_name": (payload.legal_name or "").strip() or None,
            "msme": (payload.msme or "").strip() or None,
            "target_audience": payload.target_audience.strip(),
            "target_region": [r.strip() for r in payload.target_region if r and r.strip()],
        },
        "created_at": now,
        "updated_at": now,
        "reviewed_at": None,
    }

    result = await db.publisher_applications.insert_one(doc)

    return {
        "success": True,
        "application_id": str(result.inserted_id),
        "status": "pending",
    }


@user_router.get("/publisher-applications/me")
async def my_publisher_application(authorization: str = Header(...)):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    app = await _get_latest_application(user_id)
    if not app:
        return {"exists": False, "status": "none"}

    return {
        "exists": True,
        "status": app.get("status", "pending"),
        "application": _serialize_application(app),
    }


@user_router.get("/publisher-access")
async def publisher_access(authorization: str = Header(...)):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    app = await _get_latest_application(user_id)
    status = app.get("status") if app else "none"
    return {
        "is_publisher": status == "approved",
        "status": status,
    }


@admin_router.get("/publisher-applications")
async def list_publisher_applications(
    status: Literal["all", "pending", "approved", "rejected"] = Query(default="all"),
):
    query = {} if status == "all" else {"status": status}
    cursor = db.publisher_applications.find(query).sort("created_at", -1)

    items = []
    async for doc in cursor:
        items.append(_serialize_application(doc))

    return items


@admin_router.post("/publisher-applications/{application_id}/status")
async def update_publisher_application_status(
    application_id: str,
    payload: PublisherApplicationStatusUpdate,
):
    if not ObjectId.is_valid(application_id):
        raise HTTPException(status_code=400, detail="Invalid application id")

    app = await db.publisher_applications.find_one({"_id": ObjectId(application_id)})
    if not app:
        raise HTTPException(status_code=404, detail="Application not found")

    now = datetime.utcnow()
    await db.publisher_applications.update_one(
        {"_id": ObjectId(application_id)},
        {
            "$set": {
                "status": payload.status,
                "admin_note": payload.admin_note,
                "reviewed_at": now,
                "updated_at": now,
            }
        },
    )

    await db.notifications.insert_one(
        {
            "user_id": app.get("user_id"),
            "type": "publisher_application_status",
            "status": payload.status,
            "message": (
                "Your publisher application was approved"
                if payload.status == "approved"
                else "Your publisher application was rejected"
            ),
            "timestamp": now,
            "admin_note": payload.admin_note,
        }
    )

    return {
        "success": True,
        "application_id": application_id,
        "status": payload.status,
    }


@admin_router.get("/campaigns")
async def list_all_campaigns_for_admin():
    items = []
    cursor = db.ad_campaigns.find({}).sort("created_at", -1)
    async for doc in cursor:
        item = _serialize_campaign(doc)
        publisher = await db.users.find_one(
            {"firebase_uid": item.get("publisher_id")},
            {"_id": 0, "full_name": 1, "username": 1, "email": 1},
        )
        item["publisher"] = {
            "full_name": (publisher or {}).get("full_name"),
            "username": (publisher or {}).get("username"),
            "email": (publisher or {}).get("email"),
        }
        items.append(item)

    return items


@admin_router.patch("/campaigns/{campaign_id}/home-visibility")
async def set_campaign_home_visibility(campaign_id: str, payload: CampaignHomeVisibilityUpdate):
    if not ObjectId.is_valid(campaign_id):
        raise HTTPException(status_code=400, detail="Invalid campaign id")

    updated = await db.ad_campaigns.find_one_and_update(
        {"_id": ObjectId(campaign_id)},
        {
            "$set": {
                "home_enabled": bool(payload.enabled),
                "home_banner_enabled": bool(payload.enabled),
                "updated_at": datetime.utcnow(),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"success": True, "campaign": _serialize_campaign(updated)}


@admin_router.patch("/campaigns/{campaign_id}/placements")
async def set_campaign_placement_visibility(campaign_id: str, payload: CampaignPlacementVisibilityUpdate):
    if not ObjectId.is_valid(campaign_id):
        raise HTTPException(status_code=400, detail="Invalid campaign id")

    update_fields = {}
    if payload.home_banner_enabled is not None:
        update_fields["home_banner_enabled"] = bool(payload.home_banner_enabled)
        update_fields["home_enabled"] = bool(payload.home_banner_enabled)
    if payload.discover_banner_enabled is not None:
        update_fields["discover_banner_enabled"] = bool(payload.discover_banner_enabled)
    if payload.claims_banner_enabled is not None:
        update_fields["claims_banner_enabled"] = bool(payload.claims_banner_enabled)
    if payload.feed_inline_enabled is not None:
        update_fields["feed_inline_enabled"] = bool(payload.feed_inline_enabled)

    if not update_fields:
        raise HTTPException(status_code=400, detail="No placement fields provided")

    update_fields["updated_at"] = datetime.utcnow()

    updated = await db.ad_campaigns.find_one_and_update(
        {"_id": ObjectId(campaign_id)},
        {"$set": update_fields},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"success": True, "campaign": _serialize_campaign(updated)}


@user_router.put("/business-profile")
async def upsert_business_profile(
    payload: BusinessProfileUpdate,
    authorization: str = Header(...),
):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    await _require_approved_publisher(user_id)

    now = datetime.utcnow()
    await db.publisher_business_profiles.update_one(
        {"publisher_id": user_id},
        {
            "$set": {
                "business_name": payload.business_name.strip(),
                "about": (payload.about or "").strip() or None,
                "whatsapp": (payload.whatsapp or "").strip() or None,
                "website": (payload.website or "").strip() or None,
                "address": (payload.address or "").strip() or None,
                "headquarters": (payload.headquarters or "").strip() or None,
                "updated_at": now,
            },
            "$setOnInsert": {"publisher_id": user_id, "created_at": now},
        },
        upsert=True,
    )

    return {"success": True}


@user_router.get("/business-profile")
async def get_my_business_profile(authorization: str = Header(...)):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    app = await _require_approved_publisher(user_id)
    user = await db.users.find_one({"firebase_uid": user_id}, {"_id": 0, "full_name": 1, "image_name": 1, "website": 1, "mobile": 1})
    profile = await db.publisher_business_profiles.find_one({"publisher_id": user_id}, {"_id": 0})

    fallback = app.get("application", {})
    if not profile:
        profile = {
            "publisher_id": user_id,
            "business_name": fallback.get("business_name"),
            "about": None,
            "whatsapp": user.get("mobile") if user else None,
            "website": user.get("website") if user else None,
            "address": None,
            "headquarters": None,
        }

    profile["owner_name"] = (user or {}).get("full_name")
    profile["owner_image"] = (user or {}).get("image_name")
    return profile


@user_router.post("/campaigns")
async def create_campaign(
    payload: CampaignCreate,
    authorization: str = Header(...),
):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    await _require_approved_publisher(user_id)

    now = datetime.utcnow()
    budget_mode = payload.budget_mode
    duration_days = 0
    standard_daily_price = 55.0
    full_daily_price = 75.0
    selected_daily_price = standard_daily_price if payload.ad_type == "standard" else full_daily_price

    if budget_mode == "custom":
        duration_days = max(1, int((payload.custom_budget or 0) // selected_daily_price))
        if duration_days == 1 and (payload.custom_budget or 0) > selected_daily_price:
            duration_days = 2
    else:
        duration_days = int(payload.package_duration_days or 0)

    doc = {
        "publisher_id": user_id,
        "ad_name": payload.ad_name.strip(),
        "business_name": payload.business_name.strip(),
        "description": (payload.description or "").strip() or None,
        "logo_url": (payload.logo_url or "").strip() or None,
        "photo_preview_url": (payload.photo_preview_url or "").strip() or None,
        "video_preview_url": (payload.video_preview_url or "").strip() or None,
        "video_duration_seconds": payload.video_duration_seconds,
        "website_url": (payload.website_url or "").strip() or None,
        "target_audience": payload.target_audience.strip(),
        "target_region": [r.strip() for r in payload.target_region if r and r.strip()],
        "budget_mode": budget_mode,
        "ad_type": payload.ad_type,
        "custom_budget": payload.custom_budget if budget_mode == "custom" else None,
        "daily_price": selected_daily_price if budget_mode == "custom" else None,
        "package_name": payload.package_name if budget_mode == "package" else None,
        "package_duration_days": payload.package_duration_days if budget_mode == "package" else None,
        "package_total_budget": payload.package_total_budget if budget_mode == "package" else None,
        "started_at": now,
        "ends_at": now + timedelta(days=duration_days),
        "status": "active",
        "home_enabled": False,
        "home_banner_enabled": False,
        "discover_banner_enabled": False,
        "claims_banner_enabled": False,
        "feed_inline_enabled": False,
        "metrics": {"views": 0, "detail_clicks": 0},
        "created_at": now,
        "updated_at": now,
    }

    result = await db.ad_campaigns.insert_one(doc)
    doc["_id"] = result.inserted_id
    return {"success": True, "campaign": _serialize_campaign(doc)}


@user_router.get("/campaigns/mine")
async def list_my_campaigns(authorization: str = Header(...)):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    await _require_approved_publisher(user_id)

    items = []
    cursor = db.ad_campaigns.find({"publisher_id": user_id}).sort("created_at", -1)
    async for doc in cursor:
        items.append(_serialize_campaign(doc))
    return items


@user_router.get("/campaigns/{campaign_id}")
async def get_campaign(campaign_id: str, authorization: str = Header(...)):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    if not ObjectId.is_valid(campaign_id):
        raise HTTPException(status_code=400, detail="Invalid campaign id")

    doc = await db.ad_campaigns.find_one({"_id": ObjectId(campaign_id), "publisher_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return _serialize_campaign(doc)


@user_router.get("/dashboard/summary")
async def my_dashboard_summary(authorization: str = Header(...)):
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    await _require_approved_publisher(user_id)

    campaigns = []
    total_views = 0
    total_clicks = 0
    active_campaigns = 0

    cursor = db.ad_campaigns.find({"publisher_id": user_id}).sort("created_at", -1)
    async for doc in cursor:
        item = _serialize_campaign(doc)
        campaigns.append(item)
        views = item.get("metrics", {}).get("views", 0)
        clicks = item.get("metrics", {}).get("detail_clicks", 0)
        total_views += views
        total_clicks += clicks
        if item.get("status") == "active" and item.get("days_left", 0) > 0:
            active_campaigns += 1

    ctr = (total_clicks / total_views * 100.0) if total_views > 0 else 0.0
    return {
        "total_campaigns": len(campaigns),
        "active_campaigns": active_campaigns,
        "total_views": total_views,
        "total_detail_clicks": total_clicks,
        "ctr": round(ctr, 2),
        "campaigns": campaigns,
    }


@user_router.post("/campaigns/{campaign_id}/track-view")
async def track_campaign_view(campaign_id: str):
    if not ObjectId.is_valid(campaign_id):
        raise HTTPException(status_code=400, detail="Invalid campaign id")

    result = await db.ad_campaigns.find_one_and_update(
        {"_id": ObjectId(campaign_id)},
        {"$inc": {"metrics.views": 1}, "$set": {"updated_at": datetime.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"success": True, "views": result.get("metrics", {}).get("views", 0)}


@user_router.post("/campaigns/{campaign_id}/track-detail-click")
async def track_campaign_detail_click(campaign_id: str):
    if not ObjectId.is_valid(campaign_id):
        raise HTTPException(status_code=400, detail="Invalid campaign id")

    result = await db.ad_campaigns.find_one_and_update(
        {"_id": ObjectId(campaign_id)},
        {"$inc": {"metrics.detail_clicks": 1}, "$set": {"updated_at": datetime.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(status_code=404, detail="Campaign not found")

    return {"success": True, "detail_clicks": result.get("metrics", {}).get("detail_clicks", 0)}


@user_router.get("/publisher-profile/{publisher_id}")
async def get_publisher_profile(publisher_id: str, authorization: Optional[str] = Header(None)):
    requester_uid = None
    requester_role = None
    if authorization:
        try:
            token = _extract_bearer_token(authorization)
            decoded = verify_firebase_token(token)
            requester_uid = decoded.get("uid")
            if requester_uid:
                requester = await db.users.find_one({"firebase_uid": requester_uid}, {"_id": 0, "role": 1})
                requester_role = (requester or {}).get("role")
        except Exception:
            requester_uid = None
            requester_role = None

    can_view_insights = requester_uid == publisher_id or str(requester_role or "").lower() == "admin"

    user = await db.users.find_one(
        {"firebase_uid": publisher_id},
        {"_id": 0, "firebase_uid": 1, "full_name": 1, "image_name": 1, "website": 1, "mobile": 1},
    )
    if not user:
        raise HTTPException(status_code=404, detail="Publisher not found")

    approved = await db.publisher_applications.find_one({"user_id": publisher_id, "status": "approved"})
    if not approved:
        raise HTTPException(status_code=404, detail="Publisher profile not available")

    profile = await db.publisher_business_profiles.find_one({"publisher_id": publisher_id}, {"_id": 0})
    if not profile:
        app_data = approved.get("application", {})
        profile = {
            "publisher_id": publisher_id,
            "business_name": app_data.get("business_name"),
            "about": None,
            "whatsapp": user.get("mobile"),
            "website": user.get("website"),
            "address": None,
            "headquarters": None,
        }

    campaigns = []
    total_views = 0
    total_detail_clicks = 0
    cursor = db.ad_campaigns.find({"publisher_id": publisher_id, "status": "active"}).sort("created_at", -1)
    async for doc in cursor:
        serialized = _serialize_campaign(doc)
        if serialized.get("days_left", 0) > 0:
            if not can_view_insights:
                serialized["metrics"] = {"views": 0, "detail_clicks": 0}
            campaigns.append(serialized)
            metrics = serialized.get("metrics", {})
            total_views += int(metrics.get("views", 0) or 0)
            total_detail_clicks += int(metrics.get("detail_clicks", 0) or 0)

    followers_count = await db.follows.count_documents({"following_id": publisher_id, "status": "following"})
    connections_count = await db.follows.count_documents({"follower_id": publisher_id, "status": "following"})
    engagement_rate = round((total_detail_clicks / total_views) * 100, 2) if total_views > 0 else 0.0

    latest_logo_url = None
    for campaign in campaigns:
        if campaign.get("logo_url"):
            latest_logo_url = campaign.get("logo_url")
            break

    profile["logo_url"] = latest_logo_url or profile.get("logo_url")
    profile["headquarters"] = profile.get("headquarters")

    return {
        "publisher": {
            "firebase_uid": user.get("firebase_uid"),
            "full_name": user.get("full_name"),
            "image_name": user.get("image_name"),
        },
        "business_profile": profile,
        "followers_count": followers_count,
        "connections_count": connections_count,
        "can_view_insights": can_view_insights,
        "insights": {
            "total_views": total_views if can_view_insights else 0,
            "total_detail_clicks": total_detail_clicks if can_view_insights else 0,
            "engagement_rate": engagement_rate if can_view_insights else 0.0,
            "active_campaigns": len(campaigns),
        },
        "active_campaigns": campaigns,
    }


@user_router.get("/public/home-ads")
async def get_public_home_ads(limit: int = Query(default=10, ge=1, le=50)):
    now = datetime.utcnow()
    query = {
        "status": "active",
        "$or": [{"home_banner_enabled": True}, {"home_enabled": True}],
        "ends_at": {"$gt": now},
    }
    ads = []
    cursor = db.ad_campaigns.find(query).sort("created_at", -1)
    async for doc in cursor:
        serialized = _serialize_campaign(doc)
        if serialized.get("days_left", 0) > 0:
            ads.append(serialized)

    random.shuffle(ads)
    return {"items": ads[:limit]}


@user_router.get("/public/placement-ads")
async def get_public_placement_ads(
    placement: Literal["home_banner", "discover_banner", "claims_banner", "feed_inline", "daily_claim"] = Query(...),
    limit: int = Query(default=10, ge=1, le=50),
):
    now = datetime.utcnow()
    placement_field_map = {
        "home_banner": "home_banner_enabled",
        "discover_banner": "discover_banner_enabled",
        "claims_banner": "claims_banner_enabled",
        "feed_inline": "feed_inline_enabled",
        "daily_claim": "claims_banner_enabled",
    }
    field = placement_field_map[placement]

    query = {
        "status": "active",
        field: True,
        "ends_at": {"$gt": now},
    }

    # Keep legacy compatibility for old campaigns that only have home_enabled.
    if placement == "home_banner":
        query["$or"] = [{"home_banner_enabled": True}, {"home_enabled": True}]
        query.pop("home_banner_enabled", None)

    ads = []
    cursor = db.ad_campaigns.find(query).sort("created_at", -1)
    async for doc in cursor:
        serialized = _serialize_campaign(doc)
        if serialized.get("days_left", 0) > 0:
            ads.append(serialized)

    random.shuffle(ads)
    return {"items": ads[:limit]}


class PaymentIntentCreate(BaseModel):
    """Create Razorpay payment order for ad campaign"""
    ad_name: str
    business_name: str
    total_amount: int  # in paise (multiply by 100 for conversion from rupees)
    duration_days: int
    budget_mode: Literal["custom", "package"]


@user_router.post("/payment/create-order")
async def create_payment_order(
    payload: PaymentIntentCreate,
    authorization: str = Header(...),
):
    """Create a Razorpay order for ad campaign payment"""
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    await _require_approved_publisher(user_id)

    try:
        if payload.total_amount <= 0:
            raise HTTPException(status_code=400, detail="Amount must be greater than 0")
        if payload.duration_days <= 0:
            raise HTTPException(status_code=400, detail="Duration must be at least 1 day")

        user = await db.users.find_one({"firebase_uid": user_id}, {"email": 1, "mobile": 1, "full_name": 1})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Razorpay receipt must be short (<= 40 chars) and unique per order.
        receipt = f"ad-{user_id[-8:]}-{uuid4().hex[:10]}"
        if len(receipt) > 40:
            receipt = receipt[:40]

        # Create Razorpay order
        order_data = {
            "amount": payload.total_amount,  # amount in smallest currency unit (paise for INR)
            "currency": "INR",
            "receipt": receipt,
            "notes": {
                "publisher_id": user_id,
                "ad_name": payload.ad_name,
                "business_name": payload.business_name,
                "duration_days": str(payload.duration_days),
                "budget_mode": payload.budget_mode,
            }
        }

        order = razorpay_client.order.create(data=order_data)

        # Store order in database for tracking
        payment_doc = {
            "publisher_id": user_id,
            "razorpay_order_id": order["id"],
            "amount": payload.total_amount,
            "status": "pending",
            "ad_name": payload.ad_name,
            "business_name": payload.business_name,
            "duration_days": payload.duration_days,
            "budget_mode": payload.budget_mode,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        
        result = await db.ad_payments.insert_one(payment_doc)

        return {
            "success": True,
            "order_id": order["id"],
            "amount": order["amount"],
            "currency": order["currency"],
            "razorpay_key": RAZORPAY_KEY_ID,
            "user_email": user.get("email"),
            "user_phone": user.get("mobile"),
            "user_name": user.get("full_name"),
        }

    except HTTPException:
        raise
    except Exception as e:
        detail = str(e)
        try:
            if hasattr(e, "args") and e.args:
                first = e.args[0]
                if isinstance(first, dict):
                    detail = first.get("error", {}).get("description") or detail
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=detail)


@user_router.post("/payment/verify")
async def verify_payment(
    payload: dict,
    authorization: str = Header(...),
):
    """Verify Razorpay payment and create campaign"""
    token = _extract_bearer_token(authorization)
    decoded = verify_firebase_token(token)
    user_id = decoded.get("uid")

    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")

    try:
        razorpay_order_id = payload.get("razorpay_order_id")
        razorpay_payment_id = payload.get("razorpay_payment_id")
        razorpay_signature = payload.get("razorpay_signature")

        # Verify signature
        payment_verification = razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": razorpay_order_id,
            "razorpay_payment_id": razorpay_payment_id,
            "razorpay_signature": razorpay_signature,
        })

        # Update payment status
        now = datetime.utcnow()
        await db.ad_payments.update_one(
            {"razorpay_order_id": razorpay_order_id},
            {
                "$set": {
                    "razorpay_payment_id": razorpay_payment_id,
                    "status": "completed",
                    "updated_at": now,
                }
            }
        )

        # 📢 Send notification about successful ad payment
        try:
            notification_doc = {
                "user_id": user_id,
                "action": "ad_payment_verified",
                "description": "Your ad campaign payment verified! Your campaign is now live",
                "timestamp": now,
                "read": False,
                "created_at": now,
            }
            await db.notifications.insert_one(notification_doc)
        except Exception:
            pass  # Don't fail if notification fails

        return {
            "success": True,
            "message": "Payment verified successfully",
            "payment_id": razorpay_payment_id,
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Payment verification failed: {str(e)}")
