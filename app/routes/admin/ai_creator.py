from fastapi import APIRouter, HTTPException, Header, UploadFile, File, Form
from pydantic import BaseModel
from typing import Optional, List
import json
from app.database import db
from app.firebase import verify_firebase_token
from app.r2 import s3, BUCKET_NAME, PUBLIC_BASE
from bson import ObjectId
from datetime import datetime, timedelta
from pymongo import ReturnDocument

DEFAULT_PAYOUT_PER_REMIX = 1
DEFAULT_BURN_CREDITS_PER_REMIX = 3

# Admin router for dashboard actions
admin_router = APIRouter(prefix="/admin/ai-creator", tags=["AI Creator Admin"])
# User router for user actions
user_router = APIRouter(prefix="/ai-creator", tags=["AI Creator User"])


def _normalize_email(value: Optional[str]) -> str:
    return str(value or "").strip().lower()


def _normalize_mobile(value: Optional[str]) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


async def ensure_not_blocked_identifiers(email: Optional[str], mobile: Optional[str]) -> None:
    normalized_email = _normalize_email(email)
    normalized_mobile = _normalize_mobile(mobile)

    checks = []
    if normalized_email:
        checks.append({"kind": "email", "value": normalized_email})
    if normalized_mobile:
        checks.append({"kind": "mobile", "value": normalized_mobile})

    if not checks:
        return

    blocked = await db.ai_creator_blocklist.find_one({"$or": checks})
    if blocked:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ai_creator_blocked_identifier",
                "message": "This email/mobile is permanently blocked for AI Creator.",
            },
        )


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


async def require_creator(user_id: str) -> None:
    user_doc = await db.users.find_one({"firebase_uid": user_id}, {"creator_blocked": 1})
    if user_doc and bool(user_doc.get("creator_blocked")):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ai_creator_blocked",
                "message": "Your AI Creator account is blocked.",
            },
        )

    creator_app = await db.ai_creator_applications.find_one({"user_id": user_id})
    if not creator_app:
        raise HTTPException(status_code=403, detail="AI Creator approval required")

    creator_app = await resolve_creator_application_status(creator_app)
    status = str(creator_app.get("status") or "").lower()

    if status == "approved":
        return

    if status == "suspended":
        suspended_until = creator_app.get("suspended_until")
        until_label = suspended_until.isoformat() if isinstance(suspended_until, datetime) else str(suspended_until or "")
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ai_creator_suspended",
                "message": "Your AI Creator account is suspended.",
                "suspended_until": until_label,
            },
        )

    if status == "blocked":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ai_creator_blocked",
                "message": "Your AI Creator account is blocked.",
            },
        )

    raise HTTPException(status_code=403, detail="AI Creator approval required")


async def resolve_creator_application_status(app_data: dict) -> dict:
    if not app_data:
        return app_data

    status = str(app_data.get("status") or "").lower()
    if status != "suspended":
        return app_data

    suspended_until = app_data.get("suspended_until")
    if not isinstance(suspended_until, datetime):
        return app_data

    if suspended_until > datetime.utcnow():
        return app_data

    await db.ai_creator_applications.update_one(
        {"_id": app_data.get("_id")},
        {
            "$set": {
                "status": "approved",
                "updated_at": datetime.utcnow(),
            },
            "$unset": {
                "suspended_at": "",
                "suspended_until": "",
                "suspension_days": "",
                "suspension_reason": "",
                "block_reason": "",
            },
        },
    )

    app_data["status"] = "approved"
    app_data.pop("suspended_at", None)
    app_data.pop("suspended_until", None)
    app_data.pop("suspension_days", None)
    app_data.pop("suspension_reason", None)
    app_data.pop("block_reason", None)
    return app_data


def serialize_prompt(prompt: dict) -> dict:
    # Ensure all arrays exist and have correct format
    prompt["_id"] = str(prompt.get("_id"))
    prompt["payout_per_remix"] = int(prompt.get("payout_per_remix", DEFAULT_PAYOUT_PER_REMIX) or DEFAULT_PAYOUT_PER_REMIX)
    prompt["burn_credits"] = int(prompt.get("burn_credits", DEFAULT_BURN_CREDITS_PER_REMIX) or DEFAULT_BURN_CREDITS_PER_REMIX)
    
    # Ensure arrays exist (empty if not present)
    prompt["likes"] = prompt.get("likes", [])
    prompt["views"] = prompt.get("views", [])
    prompt["remixes"] = prompt.get("remixes", [])
    
    # Process comments - ensure comment_id is string and all required fields present
    comments = []
    for c in prompt.get("comments", []):
        if isinstance(c.get("comment_id"), ObjectId):
            c["comment_id"] = str(c["comment_id"])
        comments.append({
            "comment_id": c.get("comment_id"),
            "user_id": c.get("user_id"),
            "username": c.get("username"),
            "user_image": c.get("user_image"),
            "text": c.get("text"),
            "created_at": c.get("created_at"),
        })
    prompt["comments"] = comments

    prompt["prompt_template"] = prompt.get("prompt_template", "")
    prompt["prompt_variables"] = prompt.get("prompt_variables", [])
    prompt["aspect_ratio"] = prompt.get("aspect_ratio", "9:16")
    prompt["require_reference_image"] = bool(prompt.get("require_reference_image", False))
    prompt["sample_image_urls"] = prompt.get("sample_image_urls", [])
    prompt["reference_correct_image_urls"] = prompt.get("reference_correct_image_urls", [])
    prompt["reference_wrong_image_urls"] = prompt.get("reference_wrong_image_urls", [])
    
    return prompt


def _parse_bool_form(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_prompt_variables(raw_json: str) -> List[dict]:
    if not raw_json or not raw_json.strip():
        return []
    try:
        data = json.loads(raw_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid prompt_variables_json")

    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="prompt_variables_json must be an array")

    normalized = []
    for item in data:
        if not isinstance(item, dict):
            continue

        key = str(item.get("key") or "").strip()
        if not key:
            continue

        input_type = str(item.get("input_type") or "text").strip().lower()
        if input_type not in {"text", "dropdown"}:
            input_type = "text"

        options = item.get("options")
        if not isinstance(options, list):
            options = []
        option_values = [str(opt).strip() for opt in options if str(opt).strip()]

        normalized.append({
            "key": key,
            "label": str(item.get("label") or key).strip(),
            "input_type": input_type,
            "options": option_values,
            "placeholder": str(item.get("placeholder") or "").strip(),
            "default_value": str(item.get("default_value") or "").strip(),
            "required": bool(item.get("required", False)),
        })

    return normalized


async def get_creator_contact(user_id: str) -> dict:
    app = await db.ai_creator_applications.find_one({"user_id": user_id})
    if not app:
        return {}
    return {
        "email": app.get("email"),
        "mobile": app.get("mobile"),
        "full_name": app.get("full_name"),
        "dob": app.get("dob"),
    }


async def next_prompt_unit_id() -> str:
    counter = await db.counters.find_one_and_update(
        {"_id": "ai_creator_prompt_unit"},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return f"KRGM{counter.get('seq', 1)}"


class SuspendCreatorPayload(BaseModel):
    days: int
    reason: Optional[str] = None


class BlockCreatorPayload(BaseModel):
    reason: Optional[str] = None

# --- ADMIN ENDPOINTS ---
@admin_router.get("/applications")
async def get_all_applications():
    cursor = db.ai_creator_applications.find()
    apps = []
    async for app in cursor:
        app = await resolve_creator_application_status(app)
        app["_id"] = str(app["_id"])
        apps.append(app)
    return apps

@admin_router.post("/applications/{id}/approve")
async def approve_application(id: str):
    app_data = await db.ai_creator_applications.find_one({"_id": ObjectId(id)})
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found.")
    await db.ai_creator_applications.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {"status": "approved", "updated_at": datetime.utcnow()},
            "$unset": {
                "suspended_at": "",
                "suspended_until": "",
                "suspension_days": "",
                "suspension_reason": "",
                "block_reason": "",
            },
        }
    )
    # Update users collection with social media URLs and website info
    social_fields = ["instagram", "youtube", "facebook", "x", "linkedin", "whatsapp", "website", "website_name"]
    update_data = {field: app_data.get(field, None) for field in social_fields}
    await db.users.update_one(
        {"firebase_uid": app_data["user_id"]},
        {"$set": update_data}
    )
    await db.notifications.insert_one({
        "user_id": app_data["user_id"],
        "user_name": "Kirnagram",
        "action": "ai_creator_application_approved",
        "type": "ai_creator_status",
        "status": "approved",
        "message": "Your AI Creator application was approved!",
        "description": "Your AI Creator application was approved!",
        "timestamp": datetime.utcnow(),
    })
    return {"success": True, "id": id, "status": "approved"}

@admin_router.post("/applications/{id}/reject")
async def reject_application(id: str, reason: Optional[str] = None):
    app_data = await db.ai_creator_applications.find_one({"_id": ObjectId(id)})
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found.")
    await db.ai_creator_applications.delete_one({"_id": ObjectId(id)})
    await db.notifications.insert_one({
        "user_id": app_data["user_id"],
        "user_name": "Kirnagram",
        "action": "ai_creator_application_rejected",
        "type": "ai_creator_status",
        "status": "rejected",
        "message": f"Your AI Creator application was rejected. Reason: {reason or 'Not specified.'}",
        "description": f"Your AI Creator application was rejected. Reason: {reason or 'Not specified.'}",
        "timestamp": datetime.utcnow(),
    })
    return {"success": True, "id": id, "status": "rejected"}


@admin_router.post("/applications/{id}/suspend")
async def suspend_creator_application(id: str, payload: SuspendCreatorPayload):
    if payload.days <= 0:
        raise HTTPException(status_code=400, detail="days must be greater than 0")

    app_data = await db.ai_creator_applications.find_one({"_id": ObjectId(id)})
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found.")

    now = datetime.utcnow()
    suspended_until = now + timedelta(days=int(payload.days))
    reason = (payload.reason or "Illegal activity").strip()

    await db.ai_creator_applications.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "status": "suspended",
                "suspended_at": now,
                "suspended_until": suspended_until,
                "suspension_days": int(payload.days),
                "suspension_reason": reason,
                "updated_at": now,
            },
            "$unset": {"block_reason": ""},
        },
    )

    await db.notifications.insert_one({
        "user_id": app_data["user_id"],
        "user_name": "Kirnagram",
        "action": "ai_creator_account_suspended",
        "type": "ai_creator_status",
        "status": "suspended",
        "message": f"Your AI Creator account has been suspended for {int(payload.days)} day(s).",
        "description": f"Your AI Creator account has been suspended for {int(payload.days)} day(s).",
        "timestamp": now,
    })

    return {
        "success": True,
        "id": id,
        "status": "suspended",
        "days": int(payload.days),
        "suspended_until": suspended_until.isoformat(),
    }


@admin_router.post("/applications/{id}/block")
async def block_creator_application(id: str, payload: Optional[BlockCreatorPayload] = None):
    app_data = await db.ai_creator_applications.find_one({"_id": ObjectId(id)})
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found.")

    now = datetime.utcnow()
    reason = (payload.reason if payload else None) or "Illegal activity"
    user_doc = await db.users.find_one({"firebase_uid": app_data.get("user_id")}, {"email": 1, "mobile": 1})
    blocked_email = _normalize_email((user_doc or {}).get("email") or app_data.get("email"))
    blocked_mobile = _normalize_mobile((user_doc or {}).get("mobile") or app_data.get("mobile"))

    await db.ai_creator_applications.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "status": "blocked",
                "block_reason": str(reason).strip(),
                "updated_at": now,
            },
            "$unset": {
                "suspended_at": "",
                "suspended_until": "",
                "suspension_days": "",
                "suspension_reason": "",
            },
        },
    )

    await db.notifications.insert_one({
        "user_id": app_data["user_id"],
        "user_name": "Kirnagram",
        "action": "ai_creator_account_blocked",
        "type": "ai_creator_status",
        "status": "blocked",
        "message": "Your AI Creator account has been blocked.",
        "description": "Your AI Creator account has been blocked.",
        "timestamp": now,
    })

    blocklist_entries = []
    if blocked_email:
        blocklist_entries.append({"kind": "email", "value": blocked_email})
    if blocked_mobile:
        blocklist_entries.append({"kind": "mobile", "value": blocked_mobile})

    for entry in blocklist_entries:
        await db.ai_creator_blocklist.update_one(
            entry,
            {
                "$set": {
                    "reason": str(reason).strip(),
                    "blocked_at": now,
                    "source_user_id": app_data.get("user_id"),
                    "active": True,
                }
            },
            upsert=True,
        )

    await db.users.update_one(
        {"firebase_uid": app_data.get("user_id")},
        {"$set": {"creator_blocked": True, "creator_blocked_at": now, "creator_block_reason": str(reason).strip()}},
    )

    return {"success": True, "id": id, "status": "blocked"}


@admin_router.post("/applications/{id}/revoke")
async def revoke_creator_restriction(id: str):
    app_data = await db.ai_creator_applications.find_one({"_id": ObjectId(id)})
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found.")

    if str(app_data.get("status") or "").lower() == "blocked":
        raise HTTPException(status_code=400, detail="Blocked creator accounts are permanent and cannot be revoked.")

    now = datetime.utcnow()
    await db.ai_creator_applications.update_one(
        {"_id": ObjectId(id)},
        {
            "$set": {
                "status": "approved",
                "updated_at": now,
            },
            "$unset": {
                "suspended_at": "",
                "suspended_until": "",
                "suspension_days": "",
                "suspension_reason": "",
                "block_reason": "",
            },
        },
    )

    await db.notifications.insert_one({
        "user_id": app_data["user_id"],
        "user_name": "Kirnagram",
        "action": "ai_creator_account_revoked",
        "type": "ai_creator_status",
        "status": "approved",
        "message": "Your AI Creator account suspension has been revoked.",
        "description": "Your AI Creator account suspension has been revoked.",
        "timestamp": now,
    })

    return {"success": True, "id": id, "status": "approved"}


class AICreatorApplication(BaseModel):
    user_id: str
    full_name: str
    email: str
    mobile: str
    dob: str
    instagram: Optional[str] = None
    youtube: Optional[str] = None
    facebook: Optional[str] = None
    x: Optional[str] = None
    linkedin: Optional[str] = None
    whatsapp: Optional[str] = None
    website: Optional[str] = None
    website_name: Optional[str] = None
    status: str = "pending"  # pending, approved, rejected
    reason: Optional[str] = None


class PromptCreateResponse(BaseModel):
    success: bool
    prompt_id: str
    status: str


class PromptPayoutUpdate(BaseModel):
    payout_per_remix: int


class PromptBurnCreditsUpdate(BaseModel):
    burn_credits: int


class CreatorProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    dob: Optional[str] = None
    website: Optional[str] = None
    website_name: Optional[str] = None
    instagram: Optional[str] = None
    youtube: Optional[str] = None
    facebook: Optional[str] = None
    x: Optional[str] = None
    linkedin: Optional[str] = None
    whatsapp: Optional[str] = None


def _normalize_dob_string(value: Optional[str]) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None

    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return raw


# --- PROMPT USER ENDPOINTS ---
@user_router.post("/prompts", response_model=PromptCreateResponse)
async def create_prompt(
    style_name: str = Form(...),
    prompt_description: str = Form(""),
    ai_model: str = Form(...),
    prompt_category: str = Form(...),
    tags: str = Form(""),
    prompt_template: str = Form(""),
    prompt_variables_json: str = Form("[]"),
    aspect_ratio: str = Form("9:16"),
    require_reference_image: str = Form("false"),
    image: UploadFile = File(...),
    sample_images: Optional[List[UploadFile]] = File(None),
    reference_correct_images: Optional[List[UploadFile]] = File(None),
    reference_wrong_images: Optional[List[UploadFile]] = File(None),
    authorization: str = Header(...)
):
    user_id = get_user_id_from_header(authorization)
    await require_creator(user_id)

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    normalized_category = prompt_category.strip()
    if not normalized_category:
        raise HTTPException(status_code=400, detail="prompt_category is required")

    normalized_ai_model = (ai_model or "").strip().lower()
    if normalized_ai_model not in {"chatgpt", "gemini"}:
        raise HTTPException(status_code=400, detail="ai_model must be chatgpt or gemini")

    ext = image.filename.split(".")[-1]
    file_key = f"ai-prompts/{user_id}/{int(datetime.utcnow().timestamp())}.{ext}"
    s3.upload_fileobj(
        image.file,
        BUCKET_NAME,
        file_key,
        ExtraArgs={"ContentType": image.content_type}
    )
    image_url = f"{PUBLIC_BASE}/{file_key}"

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    prompt_variables = _parse_prompt_variables(prompt_variables_json)
    normalized_aspect_ratio = aspect_ratio if aspect_ratio in {"9:16", "16:9", "1:1"} else "9:16"
    require_reference = _parse_bool_form(require_reference_image, default=False)

    def upload_extra_images(files: Optional[List[UploadFile]], folder: str, max_count: int = 3) -> List[str]:
        urls: List[str] = []
        if not files:
            return urls

        for idx, upload in enumerate(files[:max_count]):
            if not upload or not upload.filename:
                continue
            ext = upload.filename.split(".")[-1]
            extra_key = f"ai-prompts/{user_id}/{folder}/{int(datetime.utcnow().timestamp())}-{idx}.{ext}"
            s3.upload_fileobj(
                upload.file,
                BUCKET_NAME,
                extra_key,
                ExtraArgs={"ContentType": upload.content_type}
            )
            urls.append(f"{PUBLIC_BASE}/{extra_key}")

        return urls

    sample_image_urls = upload_extra_images(sample_images, "samples")
    reference_correct_urls = upload_extra_images(reference_correct_images, "reference-correct")
    reference_wrong_urls = upload_extra_images(reference_wrong_images, "reference-wrong")

    normalized_prompt_description = (prompt_description or "").strip() or prompt_template.strip()

    prompt_doc = {
        "user_id": user_id,
        "style_name": style_name,
        "prompt_description": normalized_prompt_description,
        "prompt_template": prompt_template.strip(),
        "prompt_variables": prompt_variables,
        "ai_model": normalized_ai_model,
        "prompt_category": normalized_category,
        "aspect_ratio": normalized_aspect_ratio,
        "require_reference_image": require_reference,
        "tags": tag_list,
        "image_url": image_url,
        "sample_image_urls": sample_image_urls,
        "reference_correct_image_urls": reference_correct_urls,
        "reference_wrong_image_urls": reference_wrong_urls,
        "status": "pending",
        "reason": None,
        "unit_id": None,
        "likes": [],
        "views": [],
        "remixes": [],
        "comments": [],
        "user_snapshot": {
            "firebase_uid": user.get("firebase_uid"),
            "username": user.get("username"),
            "full_name": user.get("full_name"),
            "image_name": user.get("image_name"),
        },
        "payout_per_remix": DEFAULT_PAYOUT_PER_REMIX,
        "burn_credits": DEFAULT_BURN_CREDITS_PER_REMIX,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }

    result = await db.ai_creator_prompts.insert_one(prompt_doc)

    # Notification to creator
    await db.notifications.insert_one({
        "user_id": user_id,
        "type": "ai_creator_prompt",
        "status": "pending",
        "message": "Your prompt was submitted for review.",
        "prompt_id": str(result.inserted_id),
        "timestamp": datetime.utcnow(),
    })

    # Notifications to all admins for review
    admins = await db.users.find({"role": "admin"}).to_list(None)
    for admin in admins:
        await db.notifications.insert_one({
            "user_id": admin.get("firebase_uid"),
            "type": "ai_creator_prompt_admin",
            "status": "new_submission",
            "message": f"New AI prompt submitted: {style_name} by {user.get('full_name') or user.get('username')}",
            "prompt_id": str(result.inserted_id),
            "submitted_by_user_id": user_id,
            "submitted_by_name": user.get("full_name") or user.get("username"),
            "timestamp": datetime.utcnow(),
        })

    return {
        "success": True,
        "prompt_id": str(result.inserted_id),
        "status": "pending",
    }


@user_router.get("/prompts/me")
async def get_my_prompts(
    status: str = "all",
    authorization: str = Header(...)
):
    user_id = get_user_id_from_header(authorization)
    await require_creator(user_id)

    query = {"user_id": user_id, "is_deleted": {"$ne": True}}
    if status != "all":
        normalized_status = (status or "").strip().lower()
        if normalized_status in {"modify", "modified"}:
            query["status"] = {"$in": ["modify", "modified"]}
        else:
            query["status"] = normalized_status

    prompts = await db.ai_creator_prompts.find(query).sort("created_at", -1).to_list(None)
    serialized_prompts = [serialize_prompt(p) for p in prompts]

    # Build remix payout map once so each prompt can show historical earnings.
    remix_object_ids = []
    for prompt in serialized_prompts:
        for remix_id in prompt.get("remixes", []):
            if isinstance(remix_id, ObjectId):
                remix_object_ids.append(remix_id)
            elif isinstance(remix_id, str) and ObjectId.is_valid(remix_id):
                remix_object_ids.append(ObjectId(remix_id))

    remix_docs = []
    if remix_object_ids:
        remix_docs = await db.ai_creator_remixes.find(
            {"_id": {"$in": remix_object_ids}},
            {"payout_per_remix": 1}
        ).to_list(None)

    remix_payout_map = {
        str(doc.get("_id")): int(doc.get("payout_per_remix", DEFAULT_PAYOUT_PER_REMIX) or DEFAULT_PAYOUT_PER_REMIX)
        for doc in remix_docs
    }

    for prompt in serialized_prompts:
        historical_earnings = 0
        historical_remix_count = 0
        for remix_id in prompt.get("remixes", []):
            remix_id_str = str(remix_id)
            if remix_id_str in remix_payout_map:
                historical_earnings += remix_payout_map[remix_id_str]
                historical_remix_count += 1

        prompt["historical_earnings"] = int(historical_earnings)
        prompt["historical_remix_count"] = int(historical_remix_count)

    return serialized_prompts


@user_router.get("/prompts/approved")
async def get_public_prompts(limit: int = 50, skip: int = 0):
    prompts = await db.ai_creator_prompts.find({"status": "approved", "is_deleted": {"$ne": True}})\
        .sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)

    result = []
    for p in prompts:
        user = await db.users.find_one({"firebase_uid": p.get("user_id")}, {"_id": 0})
        result.append({
            "_id": str(p.get("_id")),
            "unit_id": p.get("unit_id"),
            "style_name": p.get("style_name"),
            "prompt_category": p.get("prompt_category"),
            "tags": p.get("tags", []),
            "image_url": p.get("image_url"),
            "ai_model": p.get("ai_model"),
            "likes_count": len(p.get("likes", [])),
            "views_count": len(p.get("views", [])),
            "remixes_count": len(p.get("remixes", [])),
            "comments_count": len(p.get("comments", [])),
            "user": {
                "firebase_uid": user.get("firebase_uid") if user else None,
                "username": user.get("username") if user else None,
                "full_name": user.get("full_name") if user else None,
                "image_name": user.get("image_name") if user else None,
                "gender": user.get("gender") if user else None,
            }
        })
    return result


@user_router.get("/prompts/{prompt_id}")
async def get_prompt_detail(prompt_id: str, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if prompt.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Prompt not found")

    if prompt.get("status") != "approved" and prompt.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not allowed")

    return serialize_prompt(prompt)


@user_router.get("/prompts/{prompt_id}/remix-reviews")
async def get_prompt_remix_reviews(prompt_id: str, limit: int = 100, skip: int = 0):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if prompt.get("is_deleted"):
        raise HTTPException(status_code=404, detail="Prompt not found")

    status = str(prompt.get("status") or "").lower()
    if status not in {"approved", "delete_requested"}:
        raise HTTPException(status_code=404, detail="Prompt not found or not approved")

    remixes = await db.ai_creator_remixes.find(
        {"prompt_id": prompt_id}
    ).sort("created_at", -1).skip(skip).limit(limit).to_list(length=limit)

    user_ids = [r.get("user_id") for r in remixes if r.get("user_id")]
    users = {}
    if user_ids:
        user_docs = await db.users.find(
            {"firebase_uid": {"$in": user_ids}},
            {"firebase_uid": 1, "username": 1, "full_name": 1}
        ).to_list(None)
        users = {u.get("firebase_uid"): u for u in user_docs}

    result = []
    for remix in remixes:
        user = users.get(remix.get("user_id"), {})
        result.append({
            "id": str(remix.get("_id")),
            "remix_user_id": remix.get("user_id"),
            "remix_username": user.get("username"),
            "remix_user_full_name": user.get("full_name"),
            "prompt_id": str(prompt.get("_id")),
            "prompt_title": prompt.get("style_name"),
            "review_rating": remix.get("review_rating"),
            "review_comment": remix.get("review_comment"),
            "review_improvement": remix.get("review_improvement"),
            "review_submitted_at": remix.get("review_submitted_at"),
            "remix_created_at": remix.get("created_at"),
            "output_image": remix.get("output_image"),
            "reviewed": bool(remix.get("review_rating") or remix.get("review_comment") or remix.get("review_improvement")),
        })

    return {
        "total": len(result),
        "reviews": result,
    }


@admin_router.get("/remix-reviews")
async def get_all_remix_reviews(
    limit: int = 100,
    skip: int = 0,
    rating: Optional[str] = None,
):
    query = {
        "$or": [
            {"review_rating": {"$exists": True, "$ne": None}},
            {"review_comment": {"$exists": True, "$ne": None}},
            {"review_improvement": {"$exists": True, "$ne": None}},
        ]
    }

    normalized_rating = str(rating or "").strip().lower()
    if normalized_rating in {"good", "bad"}:
        query["review_rating"] = normalized_rating

    remixes = await db.ai_creator_remixes.find(query).sort("review_submitted_at", -1).skip(skip).limit(limit).to_list(length=limit)

    prompt_ids = [r.get("prompt_id") for r in remixes if r.get("prompt_id")]
    user_ids = [r.get("user_id") for r in remixes if r.get("user_id")]

    prompts = {}
    if prompt_ids:
        prompt_docs = await db.ai_creator_prompts.find(
            {"_id": {"$in": [ObjectId(pid) for pid in prompt_ids if ObjectId.is_valid(pid)]}},
            {"style_name": 1}
        ).to_list(None)
        prompts = {str(p.get("_id")): p for p in prompt_docs}

    users = {}
    if user_ids:
        user_docs = await db.users.find(
            {"firebase_uid": {"$in": user_ids}},
            {"firebase_uid": 1, "username": 1, "full_name": 1}
        ).to_list(None)
        users = {u.get("firebase_uid"): u for u in user_docs}

    result = []
    for remix in remixes:
        prompt = prompts.get(remix.get("prompt_id")) or {}
        user = users.get(remix.get("user_id")) or {}
        result.append({
            "id": str(remix.get("_id")),
            "prompt_id": remix.get("prompt_id"),
            "prompt_title": prompt.get("style_name"),
            "remix_user_id": remix.get("user_id"),
            "remix_username": user.get("username"),
            "remix_user_full_name": user.get("full_name"),
            "image_url": remix.get("output_image"),
            "rating": remix.get("review_rating"),
            "comment": remix.get("review_comment"),
            "improvement": remix.get("review_improvement"),
            "reviewed_at": remix.get("review_submitted_at"),
            "remix_created_at": remix.get("created_at"),
        })

    return {
        "total": len(result),
        "reviews": result,
    }


@user_router.post("/prompts/{prompt_id}/view")
async def add_prompt_view(prompt_id: str, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$addToSet": {"views": user_id}}
    )

    return {"success": True}


@user_router.post("/prompts/{prompt_id}/like")
async def toggle_prompt_like(prompt_id: str, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if user_id in prompt.get("likes", []):
        await db.ai_creator_prompts.update_one(
            {"_id": ObjectId(prompt_id)},
            {"$pull": {"likes": user_id}}
        )
        return {"liked": False}

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$addToSet": {"likes": user_id}}
    )
    return {"liked": True}


@user_router.post("/prompts/{prompt_id}/comment")
async def add_prompt_comment(
    prompt_id: str,
    text: str = Form(...),
    authorization: str = Header(...)
):
    user_id = get_user_id_from_header(authorization)
    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    comment = {
        "comment_id": ObjectId(),
        "user_id": user_id,
        "username": user.get("username"),
        "user_image": user.get("image_name"),
        "text": text,
        "created_at": datetime.utcnow(),
    }

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$push": {"comments": comment}}
    )

    return {"success": True}


@user_router.post("/prompts/{prompt_id}/remix")
async def add_prompt_remix(prompt_id: str, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$addToSet": {"remixes": user_id}}
    )

    return {"success": True}


@user_router.post("/prompts/{prompt_id}/request-delete")
async def request_prompt_delete(prompt_id: str, reason: Optional[str] = None, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)
    await require_creator(user_id)

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id), "user_id": user_id})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if prompt.get("is_deleted"):
        raise HTTPException(status_code=400, detail="Prompt already deleted")

    status = str(prompt.get("status") or "").lower()
    if status == "delete_requested":
        return {"success": True, "status": "delete_requested", "message": "Delete request already pending"}

    if status != "approved":
        raise HTTPException(status_code=400, detail="Only approved prompts can request deletion")

    request_reason = (reason or "Creator requested to delete this prompt post").strip()
    now = datetime.utcnow()

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {
            "$set": {
                "status": "delete_requested",
                "delete_requested_at": now,
                "delete_request_reason": request_reason,
                "updated_at": now,
            }
        },
    )

    await db.notifications.insert_one({
        "user_id": user_id,
        "type": "ai_creator_prompt_delete",
        "status": "delete_requested",
        "message": "Your prompt deletion request was sent to admin for approval.",
        "description": "Your prompt deletion request was sent to admin for approval.",
        "prompt_id": str(prompt.get("_id")),
        "timestamp": now,
    })

    user = await db.users.find_one({"firebase_uid": user_id}, {"_id": 0, "username": 1, "full_name": 1})
    creator_name = (user or {}).get("full_name") or (user or {}).get("username") or "Creator"
    admins = await db.users.find({"role": "admin"}, {"_id": 0, "firebase_uid": 1}).to_list(None)
    for admin in admins:
        admin_id = admin.get("firebase_uid")
        if not admin_id:
            continue
        await db.notifications.insert_one({
            "user_id": admin_id,
            "type": "ai_creator_prompt_admin",
            "status": "delete_requested",
            "message": f"Delete request for prompt {prompt.get('unit_id') or str(prompt.get('_id'))} by {creator_name}",
            "description": request_reason,
            "prompt_id": str(prompt.get("_id")),
            "submitted_by_user_id": user_id,
            "submitted_by_name": creator_name,
            "timestamp": now,
        })

    return {"success": True, "status": "delete_requested", "message": "Delete request submitted for admin approval"}


# --- USER ENDPOINTS ---
@user_router.post("/apply")
async def apply_ai_creator(application: AICreatorApplication):
    await ensure_not_blocked_identifiers(application.email, application.mobile)

    existing = await db.ai_creator_applications.find_one({"user_id": application.user_id})
    if existing:
        if str(existing.get("status") or "").lower() == "blocked":
            raise HTTPException(status_code=403, detail="This account is permanently blocked for AI Creator.")
        raise HTTPException(status_code=400, detail="Application already exists.")

    user_doc = await db.users.find_one({"firebase_uid": application.user_id}, {"creator_blocked": 1})
    if user_doc and bool(user_doc.get("creator_blocked")):
        raise HTTPException(status_code=403, detail="This account is permanently blocked for AI Creator.")

    await db.ai_creator_applications.insert_one(application.dict())
    return {"message": "Application submitted."}


@user_router.get("/application/{user_id}")
async def get_application(user_id: str):
    app_data = await db.ai_creator_applications.find_one({"user_id": user_id})
    if not app_data:
        raise HTTPException(status_code=404, detail="Application not found.")
    app_data = await resolve_creator_application_status(app_data)
    app_data["_id"] = str(app_data["_id"])
    return app_data


@user_router.put("/profile")
async def update_creator_profile(payload: CreatorProfileUpdate, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    user_doc = await db.users.find_one({"firebase_uid": user_id})
    if not user_doc:
        raise HTTPException(status_code=404, detail="User not found")

    creator_app = await db.ai_creator_applications.find_one({"user_id": user_id})

    update_fields = {}
    for key, value in payload.dict(exclude_unset=True).items():
        if isinstance(value, str):
            cleaned = value.strip()
            update_fields[key] = cleaned
        else:
            update_fields[key] = value

    if "dob" in update_fields:
        update_fields["dob"] = _normalize_dob_string(update_fields.get("dob"))

    if not creator_app:
        # If no AI Creator application exists, create one from the user's profile and the creator-specific fields.
        application_data = {
            "user_id": user_id,
            "full_name": user_doc.get("full_name", "") or "",
            "email": user_doc.get("email", "") or "",
            "mobile": user_doc.get("mobile", "") or "",
            "dob": user_doc.get("dob", "") or "",
            "status": "pending",
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        application_data.update(update_fields)

        if not application_data["full_name"] or not application_data["email"] or not application_data["mobile"] or not application_data["dob"]:
            raise HTTPException(
                status_code=400,
                detail="Complete your personal profile (name, email, mobile, dob) before starting AI Creator application."
            )

        await db.ai_creator_applications.insert_one(application_data)

        user_updates = {k: v for k, v in update_fields.items() if k in {
            "full_name",
            "dob",
            "website",
            "website_name",
            "instagram",
            "youtube",
            "facebook",
            "x",
            "linkedin",
            "whatsapp",
        }}
        if user_updates:
            await db.users.update_one(
                {"firebase_uid": user_id},
                {"$set": user_updates},
            )

        return {
            "success": True,
            "message": "AI Creator application created and profile updated",
            "created": True,
            "updated_fields": list(update_fields.keys()),
        }

    if not update_fields:
        return {"success": True, "message": "No changes", "updated_fields": []}

    update_fields["updated_at"] = datetime.utcnow()

    await db.ai_creator_applications.update_one(
        {"user_id": user_id},
        {"$set": update_fields},
    )

    user_updates = {k: v for k, v in update_fields.items() if k in {
        "full_name",
        "dob",
        "website",
        "website_name",
        "instagram",
        "youtube",
        "facebook",
        "x",
        "linkedin",
        "whatsapp",
    }}
    if user_updates:
        await db.users.update_one(
            {"firebase_uid": user_id},
            {"$set": user_updates},
        )

    return {
        "success": True,
        "message": "Creator profile updated",
        "updated_fields": [k for k in update_fields.keys() if k != "updated_at"],
    }


# --- ADMIN PROMPT ENDPOINTS ---
@admin_router.get("/prompts")
async def get_all_prompts(status: str = "pending"):
    normalized_status = (status or "pending").strip().lower()
    if normalized_status == "all":
        query = {}
    elif normalized_status == "pending":
        query = {"status": {"$in": ["pending", "delete_requested"]}}
    elif normalized_status in {"modify", "modified"}:
        query = {"status": {"$in": ["modify", "modified"]}}
    else:
        query = {"status": normalized_status}
    prompts = await db.ai_creator_prompts.find(query).sort("created_at", -1).to_list(None)
    result = []
    for p in prompts:
        user = await db.users.find_one({"firebase_uid": p.get("user_id")}, {"_id": 0})
        contact = await get_creator_contact(p.get("user_id"))
        result.append({
            **serialize_prompt(p),
            "user": user,
            "creator_contact": contact,
        })
    return result


@admin_router.get("/prompts/{prompt_id}")
async def get_prompt_admin_detail(prompt_id: str):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    user = await db.users.find_one({"firebase_uid": prompt.get("user_id")}, {"_id": 0})
    contact = await get_creator_contact(prompt.get("user_id"))
    return {
        **serialize_prompt(prompt),
        "user": user,
        "creator_contact": contact,
        "likes_count": len(prompt.get("likes", [])),
        "views_count": len(prompt.get("views", [])),
        "remixes_count": len(prompt.get("remixes", [])),
        "comments_count": len(prompt.get("comments", [])),
    }


@admin_router.post("/prompts/{prompt_id}/approve")
async def approve_prompt(prompt_id: str):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    unit_id = prompt.get("unit_id") or await next_prompt_unit_id()

    post_id = prompt.get("post_id")
    if not post_id:
        post_ratio = prompt.get("aspect_ratio") if prompt.get("aspect_ratio") in {"9:16", "16:9", "1:1", "4:5"} else "4:5"
        post_doc = {
            "user_id": prompt.get("user_id"),
            "image_url": prompt.get("image_url"),
            "ratio": post_ratio,
            "caption": prompt.get("style_name"),
            "tags": prompt.get("tags", []),
            "prompt_category": prompt.get("prompt_category"),
            "likes": [],
            "comments": [],
            "views": [],
            "is_prompt_post": True,
            "prompt_id": str(prompt.get("_id")),
            "prompt_unit_id": unit_id,
            "prompt_style_name": prompt.get("style_name"),
            "prompt_badge": unit_id,
            "created_at": datetime.utcnow(),
        }
        post_result = await db.posts.insert_one(post_doc)
        post_id = str(post_result.inserted_id)

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$set": {
            "status": "approved",
            "unit_id": unit_id,
            "post_id": post_id,
            "reason": None,
            "payout_per_remix": int(prompt.get("payout_per_remix", DEFAULT_PAYOUT_PER_REMIX) or DEFAULT_PAYOUT_PER_REMIX),
            "burn_credits": int(prompt.get("burn_credits", DEFAULT_BURN_CREDITS_PER_REMIX) or DEFAULT_BURN_CREDITS_PER_REMIX),
            "approved_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }}
    )

    await db.notifications.insert_one({
        "user_id": prompt.get("user_id"),
        "user_name": "Kirnagram",
        "action": "ai_creator_prompt_approved",
        "type": "ai_creator_prompt",
        "status": "approved",
        "message": f"Your prompt {unit_id} was approved.",
        "description": f"Your prompt {unit_id} was approved.",
        "prompt_id": str(prompt.get("_id")),
        "timestamp": datetime.utcnow(),
    })

    return {"success": True, "status": "approved", "unit_id": unit_id}


@admin_router.post("/prompts/{prompt_id}/reject")
async def reject_prompt(prompt_id: str, reason: Optional[str] = None):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$set": {
            "status": "rejected",
            "reason": reason or "Not specified",
            "updated_at": datetime.utcnow(),
        }}
    )

    await db.notifications.insert_one({
        "user_id": prompt.get("user_id"),
        "user_name": "Kirnagram",
        "action": "ai_creator_prompt_rejected",
        "type": "ai_creator_prompt",
        "status": "rejected",
        "message": f"Your prompt was rejected. Reason: {reason or 'Not specified.'}",
        "description": f"Your prompt was rejected. Reason: {reason or 'Not specified.'}",
        "prompt_id": str(prompt.get("_id")),
        "timestamp": datetime.utcnow(),
    })

    return {"success": True, "status": "rejected"}


@admin_router.post("/prompts/{prompt_id}/modify")
async def modify_prompt(prompt_id: str, reason: Optional[str] = None):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$set": {
            "status": "modified",
            "reason": reason or "Please update and resubmit",
            "updated_at": datetime.utcnow(),
        }}
    )

    await db.notifications.insert_one({
        "user_id": prompt.get("user_id"),
        "user_name": "Kirnagram",
        "action": "ai_creator_prompt_modified",
        "type": "ai_creator_prompt",
        "status": "modified",
        "message": f"Your prompt requires changes. Reason: {reason or 'Please update and resubmit'}",
        "description": f"Your prompt requires changes. Reason: {reason or 'Please update and resubmit'}",
        "prompt_id": str(prompt.get("_id")),
        "timestamp": datetime.utcnow(),
    })

    return {"success": True, "status": "modified"}


@admin_router.patch("/prompts/{prompt_id}/payout")
async def update_prompt_payout(prompt_id: str, payload: PromptPayoutUpdate):
    if payload.payout_per_remix < 0:
        raise HTTPException(status_code=400, detail="payout_per_remix must be 0 or greater")

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    previous_payout = int(prompt.get("payout_per_remix", DEFAULT_PAYOUT_PER_REMIX) or DEFAULT_PAYOUT_PER_REMIX)
    new_payout = int(payload.payout_per_remix)

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$set": {"payout_per_remix": new_payout, "updated_at": datetime.utcnow()}},
    )

    if previous_payout != new_payout:
        await db.notifications.insert_one({
            "user_id": prompt.get("user_id"),
            "type": "ai_creator_prompt_payout",
            "status": "updated",
            "message": f"Your prompt payout changed from Rs {previous_payout} to Rs {new_payout} per remix.",
            "prompt_id": str(prompt.get("_id")),
            "timestamp": datetime.utcnow(),
        })

    return {
        "success": True,
        "prompt_id": prompt_id,
        "previous_payout_per_remix": previous_payout,
        "payout_per_remix": new_payout,
    }


@admin_router.patch("/prompts/{prompt_id}/burn-credits")
async def update_prompt_burn_credits(prompt_id: str, payload: PromptBurnCreditsUpdate):
    if payload.burn_credits < 0:
        raise HTTPException(status_code=400, detail="burn_credits must be 0 or greater")

    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    previous_burn_credits = int(prompt.get("burn_credits", DEFAULT_BURN_CREDITS_PER_REMIX) or DEFAULT_BURN_CREDITS_PER_REMIX)
    new_burn_credits = int(payload.burn_credits)

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {"$set": {"burn_credits": new_burn_credits, "updated_at": datetime.utcnow()}},
    )

    if previous_burn_credits != new_burn_credits:
        await db.notifications.insert_one({
            "user_id": prompt.get("user_id"),
            "type": "ai_creator_prompt_burn_credits",
            "status": "updated",
            "message": f"Your prompt burn credits changed from {previous_burn_credits} to {new_burn_credits}.",
            "prompt_id": str(prompt.get("_id")),
            "timestamp": datetime.utcnow(),
        })

    return {
        "success": True,
        "prompt_id": prompt_id,
        "previous_burn_credits": previous_burn_credits,
        "burn_credits": new_burn_credits,
    }


@admin_router.post("/prompts/{prompt_id}/delete/approve")
async def approve_prompt_delete(prompt_id: str):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if str(prompt.get("status") or "").lower() != "delete_requested":
        raise HTTPException(status_code=400, detail="Prompt has no pending delete request")

    now = datetime.utcnow()
    post_id = prompt.get("post_id")

    if post_id and ObjectId.is_valid(str(post_id)):
        await db.posts.delete_one({"_id": ObjectId(str(post_id))})
    else:
        await db.posts.delete_many({"prompt_id": str(prompt.get("_id")), "is_prompt_post": True})

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {
            "$set": {
                "status": "deleted",
                "is_deleted": True,
                "deleted_at": now,
                "delete_request_status": "approved",
                "updated_at": now,
            },
            "$unset": {
                "post_id": "",
            },
        },
    )

    await db.notifications.insert_one({
        "user_id": prompt.get("user_id"),
        "user_name": "Kirnagram",
        "action": "ai_creator_prompt_delete_approved",
        "type": "ai_creator_prompt_delete",
        "status": "approved",
        "message": "Your prompt deletion request was approved and the prompt post was removed.",
        "description": "Your prompt deletion request was approved and the prompt post was removed.",
        "prompt_id": str(prompt.get("_id")),
        "timestamp": now,
    })

    return {"success": True, "status": "deleted"}


@admin_router.post("/prompts/{prompt_id}/delete/reject")
async def reject_prompt_delete(prompt_id: str, reason: Optional[str] = None):
    prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")

    if str(prompt.get("status") or "").lower() != "delete_requested":
        raise HTTPException(status_code=400, detail="Prompt has no pending delete request")

    reject_reason = (reason or "Deletion rejected. Please contact customer care.").strip()
    now = datetime.utcnow()

    await db.ai_creator_prompts.update_one(
        {"_id": ObjectId(prompt_id)},
        {
            "$set": {
                "status": "approved",
                "delete_request_status": "rejected",
                "delete_request_reject_reason": reject_reason,
                "updated_at": now,
            },
            "$unset": {
                "delete_requested_at": "",
                "delete_request_reason": "",
            },
        },
    )

    await db.notifications.insert_one({
        "user_id": prompt.get("user_id"),
        "user_name": "Kirnagram",
        "action": "ai_creator_prompt_delete_rejected",
        "type": "ai_creator_prompt_delete",
        "status": "rejected",
        "message": "Your prompt deletion request was rejected. Please contact customer care.",
        "description": f"Your prompt deletion request was rejected. Reason: {reject_reason}",
        "prompt_id": str(prompt.get("_id")),
        "timestamp": now,
    })

    return {"success": True, "status": "approved"}
