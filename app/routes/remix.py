from fastapi import APIRouter, UploadFile, File, Form, Header, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
from datetime import datetime
from io import BytesIO
import base64
import traceback
import requests
from urllib.parse import urlparse
from PIL import Image
from bson import ObjectId
from openai import OpenAI
import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from app.jwt_auth import verify_access_token, extract_token_from_header, get_user_id_from_authorization_header
from app.firebase import verify_firebase_token
from app.database import db
from app.r2 import s3, BUCKET_NAME, PUBLIC_BASE
from app.config import (
    GEMINI_API_KEY,
    GEMINI_IMAGE_MODEL,
    GEMINI_FALLBACK_MODE,
    OPENAI_API_KEY,
    OPENAI_IMAGE_MODEL,
)
from app.credits import ensure_wallet, record_transaction

router = APIRouter(prefix="/remix", tags=["Remix"])


def normalize_variable_key(value: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9_]", "", re.sub(r"\s+", "_", str(value or "").strip())).lower()

# ==========================================================
# MY REMIX HISTORY
# ==========================================================
@router.get("/my-remixes")
async def get_my_remixes(authorization: str = Header(...)):
    user_id = get_user_id(authorization)

    remixes = await db.ai_creator_remixes.find(
        {"user_id": user_id}
    ).sort("created_at", -1).to_list(length=None)

    result = []

    for remix in remixes:
        result.append({
            "id": str(remix["_id"]),
            "image_url": remix.get("output_image"),
            "prompt_id": remix.get("prompt_id"),
            "ratio": remix.get("ratio"),
            "payout_per_remix": int(remix.get("payout_per_remix", 1) or 1),
            "review_rating": remix.get("review_rating"),
            "review_comment": remix.get("review_comment"),
            "review_improvement": remix.get("review_improvement"),
            "review_submitted_at": remix.get("review_submitted_at"),
            "created_at": remix.get("created_at")
        })

    return {
        "total": len(result),
        "remixes": result
    }

# ============================================================
# AUTH
# ============================================================
def get_user_id(authorization: str) -> str:
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header")
    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)
    return decoded["uid"]


def add_watermark_pil(image: Image.Image, logo_path: str = None, text: str = "KIRANAGRAM") -> Image.Image:
    from PIL import ImageDraw, ImageFont

    width, height = image.size
    is_landscape = width > height
    
    # Keep landscape watermark smaller while preserving visibility.
    if is_landscape:
        # 16:9 landscape
        font_size = max(16, int(min(width, height) * 0.028))
    else:
        # 9:16 portrait
        font_size = max(24, int(min(width, height) * 0.04))

    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except:
        font = ImageFont.load_default()

    # Create transparent layer for watermark
    txt_layer = Image.new("RGBA", image.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(txt_layer)

    # Measure text
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Slightly tighter corner placement for smaller landscape text.
    if is_landscape:
        padding = max(12, int(min(width, height) * 0.018))
    else:
        padding = max(20, int(min(width, height) * 0.025))

    # Bottom right position
    x = width - text_width - padding
    y = height - text_height - padding

    # Strong shadow + visible text
    shadow_offset = 2
    shadow_opacity = 200  # Much darker (was 60-70)
    text_opacity = 235    # Much brighter (was 65-75)
    
    # Draw dark shadow for contrast
    draw.text(
        (x + shadow_offset, y + shadow_offset),
        text,
        font=font,
        fill=(0, 0, 0, shadow_opacity)
    )

    # Main watermark text - bright white
    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 255, 255, text_opacity)
    )

    return Image.alpha_composite(image, txt_layer)

def crop_to_ratio(image: Image.Image, ratio: str) -> Image.Image:
    width, height = image.size

    if ratio == "16:9":
        target_ratio = 16 / 9
    elif ratio == "9:16":
        target_ratio = 9 / 16
    else:
        return image

    current_ratio = width / height

    if current_ratio > target_ratio:
        # Crop width
        new_width = int(height * target_ratio)
        offset = (width - new_width) // 2
        return image.crop((offset, 0, offset + new_width, height))
    else:
        # Crop height
        new_height = int(width / target_ratio)
        offset = (height - new_height) // 2
        return image.crop((0, offset, width, offset + new_height))

# Resize image to match the requested aspect ratio
    # REMOVED resize_to_ratio
# ============================================================
def convert_to_png(upload: UploadFile) -> bytes:
    raw = upload.file.read()
    try:
        img = Image.open(BytesIO(raw)).convert("RGBA")
        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return out.read()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file")


def _load_pil_image(upload: UploadFile) -> Image.Image:
    raw = upload.file.read()
    try:
        return Image.open(BytesIO(raw)).convert("RGBA")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid image file")


def _load_pil_from_base64(image_base64: str) -> Image.Image:
    if not image_base64:
        raise HTTPException(status_code=400, detail="Missing base64 image data")

    prefix = "base64,"
    if prefix in image_base64:
        image_base64 = image_base64.split(prefix, 1)[1]

    try:
        raw = base64.b64decode(image_base64)
        return Image.open(BytesIO(raw)).convert("RGBA")
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")


def _pil_to_png_bytes(image: Image.Image) -> bytes:
    out = BytesIO()
    image.save(out, format="PNG")
    out.seek(0)
    return out.read()


def fetch_image_base64(url: str) -> str:
    try:
        res = requests.get(url, timeout=60)
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to fetch source image")

    if res.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch source image")

    return base64.b64encode(res.content).decode("utf-8")


# ============================================================
# PROMPT
# ============================================================
def build_prompt(style: str, description: str, ratio: str) -> str:
    """Build fallback prompt from style and description fields."""
    parts = []
    if style:
        parts.append(f"Style: {style}")
    if description:
        parts.append(f"Description: {description}")
    return " ".join(parts) or "Create a stylized image"


def render_prompt_template(template: str, values: dict) -> str:
    import re

    normalized_values = {}
    for key, value in (values or {}).items():
        normalized_key = normalize_variable_key(key)
        if normalized_key:
            normalized_values[normalized_key] = str(value or "")

    def resolve_value(raw_key: str) -> str:
        direct = values.get(raw_key, "") if isinstance(values, dict) else ""
        if str(direct or "").strip():
            return str(direct).strip()

        normalized_key = normalize_variable_key(raw_key)
        if not normalized_key:
            return ""

        normalized_direct = values.get(normalized_key, "") if isinstance(values, dict) else ""
        if str(normalized_direct or "").strip():
            return str(normalized_direct).strip()

        return str(normalized_values.get(normalized_key, "") or "").strip()

    def replacer(match):
        key = (match.group(1) or "").strip()
        return resolve_value(key)

    # Support legacy {{var}} and current {var} tokens.
    rendered = re.sub(r"{{\s*([^{}]+?)\s*}}", replacer, template or "")
    rendered = re.sub(r"\{\s*([^{}]+?)\s*\}", replacer, rendered)
    return " ".join(rendered.split()).strip()


def build_identity_preserving_prompt(user_prompt: str, ratio: str) -> str:
    safety_prefix = (
        "Use the uploaded image as the primary identity reference. "
        "Preserve face identity, facial structure, skin tone, eye shape, hairstyle direction, and natural expression. "
        "Do not swap gender, do not change age drastically, and do not deform facial features. "
        "Keep realistic facial proportions and clean eyes, nose, lips, jawline, and ears. "
        "Maintain original pose and camera angle as much as possible while applying style. "
        f"Target output aspect ratio: {ratio}."
    )
    return f"{safety_prefix}\n\nStyle instructions: {user_prompt}".strip()


def build_variable_lock_instructions(values: dict) -> str:
    if not isinstance(values, dict):
        return ""

    pairs = []
    color_pairs = []

    for raw_key, raw_value in values.items():
        key = normalize_variable_key(raw_key)
        value = str(raw_value or "").strip()
        if not key or not value:
            continue

        pairs.append((key, value))
        if any(token in key for token in ("color", "colour", "colur")):
            color_pairs.append((key, value))

    if not pairs:
        return ""

    lines = [
        "Variable locks (high priority): Apply these values exactly as provided.",
        "Do not keep placeholders. Do not substitute with defaults.",
    ]

    for key, value in pairs:
        lines.append(f"- {key}: {value}")

    if color_pairs:
        lines.append(
            "Color lock: If a variable specifies a color for an item (e.g., shirt), keep that item in the exact requested color and do not recolor it due to global style grading."
        )

    return "\n".join(lines)


# ============================================================
# DOWNLOAD REMIX
# ============================================================
@router.get("/download/{remix_id}")
async def download_remix(remix_id: str, authorization: str = Header(...)):
    user_id = get_user_id(authorization)

    remix = await db.ai_creator_remixes.find_one({
        "_id": ObjectId(remix_id),
        "user_id": user_id
    })

    if not remix:
        raise HTTPException(status_code=404, detail="Remix not found")

    image_url = remix.get("output_image")

    # 🔥 Count download
    await db.ai_creator_remixes.update_one(
        {"_id": ObjectId(remix_id)},
        {"$inc": {"download_count": 1}}
    )

    # 🔥 Properly fetch full image content
    response = requests.get(image_url)

    if response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to fetch image")

    return StreamingResponse(
        BytesIO(response.content),
        media_type="image/png",
        headers={
            "Content-Disposition": "attachment; filename=kirnagram-remix.png"
        }
    )


@router.post("/{remix_id}/review")
async def submit_remix_review(
    remix_id: str,
    rating: str = Form(...),
    comment: Optional[str] = Form(None),
    improvement: Optional[str] = Form(None),
    authorization: str = Header(...)
):
    user_id = get_user_id(authorization)

    if not ObjectId.is_valid(remix_id):
        raise HTTPException(status_code=400, detail="Invalid remix id")

    normalized_rating = str(rating or "").strip().lower()
    if normalized_rating not in {"good", "bad"}:
        raise HTTPException(status_code=400, detail="Rating must be 'good' or 'bad'")

    remix_object_id = ObjectId(remix_id)
    remix = await db.ai_creator_remixes.find_one({"_id": remix_object_id, "user_id": user_id})
    if not remix:
        raise HTTPException(status_code=404, detail="Remix not found")

    review_doc = {
        "review_rating": normalized_rating,
        "review_comment": str(comment or "").strip() or None,
        "review_improvement": str(improvement or "").strip() or None,
        "review_submitted_at": datetime.utcnow(),
    }

    await db.ai_creator_remixes.update_one(
        {"_id": remix_object_id, "user_id": user_id},
        {"$set": review_doc}
    )

    return {
        "success": True,
        **review_doc,
        "id": remix_id,
    }


def _public_url_to_r2_key(url: Optional[str]) -> Optional[str]:
    if not url:
        return None

    parsed = urlparse(url)
    path = (parsed.path or "").lstrip("/")
    public_base_path = urlparse(PUBLIC_BASE).path.lstrip("/")

    if public_base_path and path.startswith(f"{public_base_path}/"):
        return path[len(public_base_path) + 1:]

    return path or None


@router.delete("/{remix_id}")
async def delete_remix(remix_id: str, authorization: str = Header(...)):
    user_id = get_user_id(authorization)

    if not ObjectId.is_valid(remix_id):
        raise HTTPException(status_code=400, detail="Invalid remix id")

    remix_object_id = ObjectId(remix_id)
    remix = await db.ai_creator_remixes.find_one({
        "_id": remix_object_id,
        "user_id": user_id
    })

    if not remix:
        raise HTTPException(status_code=404, detail="Remix not found")

    prompt_id = remix.get("prompt_id")

    # Best-effort object cleanup in R2.
    for image_field in ("source_image", "output_image"):
        key = _public_url_to_r2_key(remix.get(image_field))
        if not key:
            continue
        try:
            s3.delete_object(Bucket=BUCKET_NAME, Key=key)
        except Exception as cleanup_error:
            print(f"R2 delete failed for {key}: {cleanup_error}")

    await db.ai_creator_remixes.delete_one({"_id": remix_object_id, "user_id": user_id})

    # Keep prompt remix counters in sync even with legacy prompt_id formats.
    prompt_matchers = [{"remixes": remix_id}]
    prompt_id_str = ""

    if isinstance(prompt_id, ObjectId):
        prompt_matchers.append({"_id": prompt_id})
        prompt_id_str = str(prompt_id)
    elif isinstance(prompt_id, str) and prompt_id:
        prompt_id_str = prompt_id
        prompt_matchers.append({"unit_id": prompt_id})
        if ObjectId.is_valid(prompt_id):
            prompt_matchers.append({"_id": ObjectId(prompt_id)})

    prompt_doc = await db.ai_creator_prompts.find_one({"$or": prompt_matchers})
    if prompt_doc:
        prompt_oid = prompt_doc.get("_id")
        prompt_unit_id = prompt_doc.get("unit_id")

        await db.ai_creator_prompts.update_one(
            {"_id": prompt_oid},
            {"$pull": {"remixes": remix_id}}
        )

        prompt_ids_for_count = {str(prompt_oid)}
        if prompt_id_str:
            prompt_ids_for_count.add(prompt_id_str)
        if isinstance(prompt_unit_id, str) and prompt_unit_id:
            prompt_ids_for_count.add(prompt_unit_id)

        total_remix_count = await db.ai_creator_remixes.count_documents(
            {"prompt_id": {"$in": list(prompt_ids_for_count)}}
        )

        await db.ai_creator_prompts.update_one(
            {"_id": prompt_oid},
            {"$set": {"remix_count": max(0, int(total_remix_count))}}
        )

    return {"success": True, "message": "Remix deleted"}


# ============================================================
# GEMINI IMAGE GENERATION (SAFE)
# ============================================================
def generate_with_gemini(prompt: str, image_url: str) -> bytes:
    if not GEMINI_API_KEY:
        raise HTTPException(status_code=500, detail="Gemini API key not configured")

    genai.configure(api_key=GEMINI_API_KEY)
    model_name = (GEMINI_IMAGE_MODEL or "").strip()
    if not model_name:
        raise HTTPException(status_code=500, detail="Gemini model not configured")
    resolved_model = model_name if model_name.startswith("models/") else f"models/{model_name}"
    model = genai.GenerativeModel(resolved_model)

    image_base64 = fetch_image_base64(image_url)

    try:
        response = model.generate_content(
            [
                {
                    "role": "user",
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": image_base64
                            }
                        }
                    ]
                }
            ]
        )
    except ResourceExhausted:
        # 🔥 IMPORTANT: do NOT crash backend
        raise HTTPException(
            status_code=429,
            detail="AI quota exceeded. Please try again later."
        )
    except Exception as e:
        print("GEMINI ERROR:", str(e))
        print(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"AI generation failed: {str(e)}"
        )

    for candidate in response.candidates or []:
        for part in candidate.content.parts or []:
            if hasattr(part, "inline_data") and part.inline_data:
                data = part.inline_data.data
                return base64.b64decode(data) if isinstance(data, str) else data

    raise HTTPException(status_code=500, detail="AI did not return image")


def _map_ratio_to_size(ratio: str) -> str:
    ratio = (ratio or "").strip()

    if ratio == "1:1":
        return "1024x1024"
    elif ratio == "16:9":
        return "1536x1024"
    elif ratio == "9:16":
        return "1024x1536"

    return "1024x1024"



def _resolve_openai_edit_model() -> str:
    configured = (OPENAI_IMAGE_MODEL or "").strip()
    return configured or "gpt-image-1"


def generate_with_openai(prompt: str, ratio: str, source_png: bytes, quality: str = "medium") -> bytes:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OpenAI API key not configured")

    size = _map_ratio_to_size(ratio)
    model = _resolve_openai_edit_model()

    # Clamp quality to OpenAI-supported values.
    requested_quality = (quality or "").strip().lower()
    output_quality = requested_quality if requested_quality in {"low", "medium", "high", "auto"} else "medium"

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        source_file = BytesIO(source_png)
        source_file.name = "source.png"

        result = client.images.edit(
            model=model,
            image=[source_file],
            prompt=prompt,
            size=size,
            quality=output_quality,
            input_fidelity="high",
        )

        b64 = result.data[0].b64_json
        return base64.b64decode(b64)

    except Exception as e:
        print("OPENAI ERROR:", str(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"OpenAI failed: {str(e)}")
# ============================================================
# API: GENERATE REMIX
# ============================================================
# SIMPLIFIED: No complex prompt manipulation
# Just: image + user's raw prompt → OpenAI/Gemini → watermark → store


@router.post("/test-gemini")
async def test_gemini(
    prompt_text: str = Form(...),
    image: UploadFile = File(...),
    authorization: str = Header(...)
):
    # Debug-only endpoint to verify Gemini output.
    user_id = get_user_id(authorization)
    print("GEMINI_TEST_REQUEST:", {
        "user_id": user_id,
        "image_filename": getattr(image, "filename", None),
        "image_content_type": getattr(image, "content_type", None),
    })

    source_png = convert_to_png(image)
    ts = int(datetime.utcnow().timestamp())

    source_key = f"remix/test/{user_id}/{ts}.png"
    s3.upload_fileobj(
        BytesIO(source_png),
        BUCKET_NAME,
        source_key,
        ExtraArgs={"ContentType": "image/png"}
    )
    source_url = f"{PUBLIC_BASE}/{source_key}"

    output_bytes = generate_with_gemini(prompt_text, source_url)
    return StreamingResponse(BytesIO(output_bytes), media_type="image/png")


@router.post("/generate")
async def generate_remix(
    prompt_id: str = Form(...),
    ratio: str = Form("1:1"),
    quality: Optional[str] = Form(None),
    model: Optional[str] = Form(None),
    prompt_text: Optional[str] = Form(None),
    variable_values_json: Optional[str] = Form(None),
    image: UploadFile = File(...),
    authorization: str = Header(...)
):
    try:
        user_id = get_user_id(authorization)

        print("REMIX_REQUEST:", {
            "user_id": user_id,
            "prompt_id": prompt_id,
            "ratio": ratio,
            "requested_model": (model or "").lower(),
            "image_filename": getattr(image, "filename", None),
            "image_content_type": getattr(image, "content_type", None),
        })

        if not ObjectId.is_valid(prompt_id):
            raise HTTPException(status_code=400, detail="Invalid prompt id")

        # 🔎 Prompt validation
        prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(prompt_id)})
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")

        prompt_status = str(prompt.get("status") or "").lower()
        if prompt.get("is_deleted") or prompt_status not in {"approved", "delete_requested"}:
            raise HTTPException(status_code=404, detail="Prompt not found or not approved")

        prompt_ai_model = (prompt.get("ai_model") or "").lower()
        requested_model = (model or "").lower()
        if prompt_ai_model == "gemini":
            resolved_model = "gemini"
        elif prompt_ai_model == "chatgpt":
            resolved_model = "chatgpt"
        elif prompt_ai_model == "both":
            resolved_model = requested_model if requested_model in {"chatgpt", "gemini"} else "chatgpt"
        else:
            resolved_model = requested_model if requested_model in {"chatgpt", "gemini"} else "chatgpt"

        # 💳 Resolve requested quality and per-prompt burn credits
        quality_aliases = {
            "gemini": {"low": "fast", "medium": "standard", "high": "ultra"},
            "chatgpt": {"fast": "low", "standard": "medium", "ultra": "high"},
        }
        normalized_quality = (quality or "").strip().lower()
        mapped_quality = quality_aliases.get(resolved_model, {}).get(normalized_quality, normalized_quality)
        burn_cost = int(prompt.get("burn_credits", 3) or 3)

        # 💳 Credits check
        wallet = await ensure_wallet(user_id)
        current_balance = int(wallet.get("balance", 0) or 0)
        print("REMIX_WALLET:", {"user_id": user_id, "balance": current_balance, "burn_cost": burn_cost})
        if current_balance < burn_cost:
            raise HTTPException(status_code=400, detail=f"Not enough credits. Required: {burn_cost}")

        # 🖼 Convert & upload source image
        source_png = convert_to_png(image)
        ts = int(datetime.utcnow().timestamp())

        source_key = f"remix/source/{user_id}/{ts}.png"
        s3.upload_fileobj(
            BytesIO(source_png),
            BUCKET_NAME,
            source_key,
            ExtraArgs={"ContentType": "image/png"}
        )
        source_url = f"{PUBLIC_BASE}/{source_key}"

        canonical_prompt_id = str(prompt.get("_id"))

        # 🎨 Resolve prompt text from template + selected variables (authoritative on backend)
        raw_prompt = (prompt_text or "").strip()
        prompt_template = (prompt.get("prompt_template") or "").strip()
        prompt_variables = prompt.get("prompt_variables") or []

        variable_values = {}
        if variable_values_json:
            try:
                loaded = __import__("json").loads(variable_values_json)
                if isinstance(loaded, dict):
                    variable_values = {str(k): str(v or "") for k, v in loaded.items()}
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid variable_values_json")

        normalized_variable_values = {}
        for key, value in variable_values.items():
            normalized_key = normalize_variable_key(key)
            if normalized_key:
                normalized_variable_values[normalized_key] = str(value or "")

        if prompt_template:
            for item in prompt_variables:
                key = str(item.get("key") or "").strip()
                normalized_key = normalize_variable_key(key)
                if not normalized_key:
                    continue

                selected_value = str(variable_values.get(key, "") or "").strip()
                if not selected_value:
                    selected_value = str(normalized_variable_values.get(normalized_key, "") or "").strip()

                if not selected_value:
                    default_value = str(item.get("default_value") or "").strip()
                    if default_value:
                        selected_value = default_value
                        normalized_variable_values[normalized_key] = default_value

                if item.get("required") and not selected_value:
                    raise HTTPException(status_code=400, detail=f"Missing required variable: {key}")

            resolved_prompt = render_prompt_template(prompt_template, normalized_variable_values)
            if not resolved_prompt:
                raise HTTPException(status_code=400, detail="Rendered prompt is empty")
        elif raw_prompt:
            resolved_prompt = raw_prompt
        else:
            resolved_prompt = build_prompt(
                prompt.get("style_name", ""),
                prompt.get("prompt_description", ""),
                ratio
            )

        variable_lock_instructions = build_variable_lock_instructions(normalized_variable_values)
        final_generation_prompt = build_identity_preserving_prompt(resolved_prompt, ratio)
        if variable_lock_instructions:
            final_generation_prompt = f"{final_generation_prompt}\n\n{variable_lock_instructions}".strip()

        print("REMIX_MODEL:", {"prompt_ai_model": prompt_ai_model, "resolved_model": resolved_model})
        print("REMIX_PROMPT:", {"resolved_prompt": final_generation_prompt[:200]})

        # 🤖 AI Generation
        if resolved_model == "gemini":
            try:
                output_bytes = generate_with_gemini(final_generation_prompt, source_url)
            except HTTPException as exc:
                if exc.status_code == 429 and (GEMINI_FALLBACK_MODE or "").lower() == "openai":
                    print("OPENAI_PROMPT_USED:", final_generation_prompt)
                    print("OPENAI_RATIO_USED:", ratio)
                    output_bytes = generate_with_openai(
                        final_generation_prompt,
                        ratio,
                        source_png,
                        mapped_quality,
                    )
                else:
                    raise
            try:
                img = Image.open(BytesIO(output_bytes)).convert("RGBA")
                img = crop_to_ratio(img, ratio)
                img = add_watermark_pil(img, logo_path="kirnagram-logo.png", text="KIRNAGRAM")
                out = BytesIO()
                img.save(out, format="PNG")
                out.seek(0)
                output_bytes = out.read()
            except Exception as e:
                print("WATERMARK ERROR (Gemini):", e)
                print(traceback.format_exc())
        else:
            print("OPENAI_PROMPT_USED:", final_generation_prompt)
            print("OPENAI_RATIO_USED:", ratio)
            output_bytes = generate_with_openai(
                final_generation_prompt,
                ratio,
                source_png,
                mapped_quality,
            )
            try:
                img = Image.open(BytesIO(output_bytes)).convert("RGBA")
                img = crop_to_ratio(img, ratio)
                img = add_watermark_pil(img, logo_path="kirnagram-logo.png", text="KIRNAGRAM")
                out = BytesIO()
                img.save(out, format="PNG")
                out.seek(0)
                output_bytes = out.read()
            except Exception as e:
                print("WATERMARK ERROR (OpenAI):", e)
                print(traceback.format_exc())

        output_key = f"remix/output/{user_id}/{ts}.png"
        s3.upload_fileobj(
            BytesIO(output_bytes),
            BUCKET_NAME,
            output_key,
            ExtraArgs={"ContentType": "image/png"}
        )
        output_url = f"{PUBLIC_BASE}/{output_key}"

        remix_result = await db.ai_creator_remixes.insert_one({
            "user_id": user_id,
            "prompt_id": canonical_prompt_id,
            "source_image": source_url,
            "output_image": output_url,
            "ratio": ratio,
            "model": resolved_model,
            "quality": mapped_quality,
            "credits_used": burn_cost,
            "payout_per_remix": int(prompt.get("payout_per_remix", 1) or 1),
            "review_rating": None,
            "review_comment": None,
            "review_improvement": None,
            "review_submitted_at": None,
            "created_at": datetime.utcnow()
        })

        await db.ai_creator_prompts.update_one(
            {"_id": ObjectId(prompt_id)},
            {"$push": {"remixes": str(remix_result.inserted_id)}}
        )

        prompt_owner_id = prompt.get("user_id")
        if user_id == prompt_owner_id:
            owner_remix_count = await db.ai_creator_remixes.count_documents({
                "prompt_id": canonical_prompt_id,
                "user_id": user_id
            })
            if owner_remix_count == 1:
                await db.ai_creator_prompts.update_one(
                    {"_id": ObjectId(prompt_id)},
                    {"$inc": {"remix_count": 1}}
                )
        else:
            await db.ai_creator_prompts.update_one(
                {"_id": ObjectId(prompt_id)},
                {"$inc": {"remix_count": 1}}
            )

        await db.credit_wallets.update_one(
            {"user_id": user_id},
            {"$inc": {"balance": -burn_cost}}
        )
        await record_transaction(
            user_id,
            -burn_cost,
            "burn",
            {"type": "remix", "model": resolved_model, "quality": mapped_quality}
        )

        # 💬 Send notification about credit burn for remix
        try:
            model_label = (resolved_model or "AI model").capitalize()
            quality_label = (mapped_quality or "").strip().capitalize()
            quality_suffix = f" ({quality_label})" if quality_label else ""
            notification_doc = {
                "user_id": user_id,
                "action": "credits_burned",
                "description": f"You spent {burn_cost} credits for remix with {model_label}{quality_suffix}",
                "timestamp": datetime.utcnow(),
                "read": False,
                "created_at": datetime.utcnow(),
            }
            await db.notifications.insert_one(notification_doc)
        except Exception:
            pass  # Don't fail if notification fails

        return {
            "success": True,
            "image_url": output_url,
            "remix_id": str(remix_result.inserted_id),
            "credits_used": burn_cost
        }
    except HTTPException:
        raise
    except Exception as e:
        print("REMIX GENERATE UNHANDLED ERROR:", str(e))
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Remix generation failed: {str(e)}")


@router.post("/gemini-edit")
async def gemini_image_edit(
    prompt_text: str = Form(...),
    image_base64: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    authorization: str = Header(...)
):
    user_id = get_user_id(authorization)
    print("GEMINI_EDIT_REQUEST:", {
        "user_id": user_id,
        "has_image_file": image is not None,
        "has_base64": bool(image_base64),
    })

    if image is None and not image_base64:
        raise HTTPException(status_code=400, detail="Provide image file or base64 image")

    if image is not None:
        source_png = convert_to_png(image)
    else:
        source_png = _pil_to_png_bytes(_load_pil_from_base64(image_base64 or ""))

    ts = int(datetime.utcnow().timestamp())
    source_key = f"remix/gemini-edit/{user_id}/{ts}.png"
    s3.upload_fileobj(
        BytesIO(source_png),
        BUCKET_NAME,
        source_key,
        ExtraArgs={"ContentType": "image/png"}
    )
    source_url = f"{PUBLIC_BASE}/{source_key}"

    output_bytes = generate_with_gemini(prompt_text, source_url)
    return StreamingResponse(BytesIO(output_bytes), media_type="image/png")




