
from fastapi import APIRouter, UploadFile, File, Header, HTTPException, Form
from fastapi.responses import StreamingResponse
from app.database import db
from app.jwt_auth import verify_access_token, extract_token_from_header
from app.r2 import s3, BUCKET_NAME, PUBLIC_BASE
from datetime import datetime
from bson import ObjectId
from io import BytesIO
from typing import Optional

from app.services.media_processing import process_uploaded_image_bytes
from app.models.post import normalize_post_ratio

router = APIRouter(prefix="/posts", tags=["Posts"])


def _prompt_ordered_images(prompt_doc: dict) -> list[str]:
    images: list[str] = []

    def add_url(url: object):
        if not isinstance(url, str):
            return
        value = url.strip()
        if not value:
            return
        if value not in images:
            images.append(value)

    # Main sample image first.
    add_url(prompt_doc.get("image_url"))

    sample_urls = prompt_doc.get("sample_image_urls") or []
    if isinstance(sample_urls, list):
        for url in sample_urls:
            add_url(url)

    return images


async def enrich_prompt_post_images(posts: list[dict]) -> list[dict]:
    prompt_object_ids = []

    for post in posts:
        if not post.get("is_prompt_post"):
            continue
        raw_prompt_id = post.get("prompt_id")
        if not raw_prompt_id:
            continue
        prompt_id_str = str(raw_prompt_id)
        if not ObjectId.is_valid(prompt_id_str):
            continue
        prompt_oid = ObjectId(prompt_id_str)
        prompt_object_ids.append(prompt_oid)

    if not prompt_object_ids:
        return posts

    unique_prompt_ids = list({oid for oid in prompt_object_ids})
    prompts = await db.ai_creator_prompts.find(
        {"_id": {"$in": unique_prompt_ids}},
        {"sample_image_urls": 1, "image_url": 1},
    ).to_list(None)

    images_by_prompt_id = {str(doc.get("_id")): _prompt_ordered_images(doc) for doc in prompts}

    for post in posts:
        if not post.get("is_prompt_post"):
            continue
        raw_prompt_id = post.get("prompt_id")
        if not raw_prompt_id:
            continue
        ordered_images = images_by_prompt_id.get(str(raw_prompt_id), [])
        if ordered_images:
            post["image_url"] = ordered_images[0]
            post["prompt_sample_images"] = ordered_images

    return posts


async def create_post_notification(
    to_user_id: str,
    from_user: dict,
    action: str,
    description: str,
    post: dict,
):
    if to_user_id == str(from_user.get("_id")):
        return

    notif_doc = {
        "user_id": to_user_id,
        "from_user_id": str(from_user.get("_id")),
        "from_user_name": from_user.get("full_name", "User"),
        "from_user_username": from_user.get("username"),
        "from_user_image": from_user.get("image_name"),
        "from_user_gender": from_user.get("gender"),
        "action": action,
        "description": description,
        "post_id": str(post.get("_id")),
        "post_owner_id": post.get("user_id"),
        "post_media": post.get("image_url") or post.get("video_url"),
        "timestamp": datetime.utcnow(),
    }

    await db.notifications.insert_one(notif_doc)



def serialize_post(post: dict) -> dict:
    post["_id"] = str(post.get("_id"))

    if isinstance(post.get("prompt_id"), ObjectId):
        post["prompt_id"] = str(post.get("prompt_id"))

    # Ensure arrays exist
    post["likes"] = post.get("likes", [])
    post["views"] = post.get("views", [])
    
    # Process comments - ensure all fields are present and ObjectId converted to string
    comments = []
    for c in post.get("comments", []):
        comment_id = c.get("comment_id")
        if isinstance(comment_id, ObjectId):
            comment_id = str(comment_id)
        comments.append({
            "comment_id": comment_id,
            "user_id": c.get("user_id"),
            "username": c.get("username"),
            "user_image": c.get("user_image"),
            "text": c.get("text"),
            "created_at": c.get("created_at"),
        })
    post["comments"] = comments

    # 🔥 SAFE MEDIA NORMALIZATION
    if "video_url" in post and post.get("video_url"):
        post["type"] = "video"
    elif "image_url" in post and post.get("image_url"):
        post["type"] = "image"
    else:
        post["type"] = post.get("type", "text") or "text"

    if isinstance(post.get("prompt_sample_images"), list):
        post["prompt_sample_images"] = [
            url for url in post.get("prompt_sample_images", [])
            if isinstance(url, str) and url.strip()
        ]

    return post



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


# Video/image post creation

@router.post("/create")
async def create_post(
    video: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    ratio: str = Form("1:1"),
    caption: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    authorization: str = Header(...)
):
    # 🔐 Verify user
    user_id = get_user_id_from_header(authorization)
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    normalized_ratio = normalize_post_ratio(ratio)

    # Help trace bad request source
    print("[create_post] user_id=%s ratio=%s normalized_ratio=%s caption_len=%d tags=%r has_image=%s has_video=%s" % (
        user_id,
        ratio,
        normalized_ratio,
        len(caption or ""),
        tags,
        bool(image),
        bool(video),
    ))

    # For text-only posts, we allow no image/video and require caption text.
    if not image and not video:
        if not caption or not caption.strip():
            raise HTTPException(status_code=400, detail="Must provide an image, video, or caption")
        file_url = None
        file_type = "text"
    else:
        file_url = None
        file_type = None

    # ==========================
    # 🖼 IMAGE UPLOAD
    # ==========================
    if image:
        if not image.content_type or not image.content_type.startswith("image"):
            raise HTTPException(status_code=400, detail="Invalid image file")

        if not image.filename:
            raise HTTPException(status_code=400, detail="Invalid image filename")

        file_bytes = await image.read()
        if len(file_bytes) > 12 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image is too large. Please upload a file smaller than 12MB")

        processed_bytes, content_type, ext = process_uploaded_image_bytes(
            file_bytes,
            image.filename,
            content_type=image.content_type,
            max_width=1600,
            max_height=1600,
            quality=85,
            target_size_kb=900,
        )

        file_key = f"posts/{user_id}/{int(datetime.utcnow().timestamp())}.{ext}"

        s3.upload_fileobj(
            BytesIO(processed_bytes),
            BUCKET_NAME,
            file_key,
            ExtraArgs={
                "ContentType": content_type,
                "ACL": "public-read"
            }
        )

        file_url = f"{PUBLIC_BASE}/{file_key}"
        file_type = "image"

    # ==========================
    # 🎥 VIDEO UPLOAD
    # ==========================
    elif video:
        if not video.content_type or not video.content_type.startswith("video"):
            raise HTTPException(status_code=400, detail="Invalid video file")

        if not video.filename:
            raise HTTPException(status_code=400, detail="Invalid video filename")

        video_bytes = await video.read()
        if len(video_bytes) > 20 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Video is too large. Please upload a file smaller than 20MB")

        ext = video.filename.rsplit(".", 1)[-1].lower() if "." in video.filename else "mp4"
        file_key = f"posts/{user_id}/{int(datetime.utcnow().timestamp())}.{ext}"

        content_type = f"video/{ext}" if ext in ["mp4", "webm", "ogg"] else "video/mp4"

        s3.upload_fileobj(
            BytesIO(video_bytes),
            BUCKET_NAME,
            file_key,
            ExtraArgs={
                "ContentType": content_type,
                "ACL": "public-read"
            }
        )

        file_url = f"{PUBLIC_BASE}/{file_key}"
        file_type = "video"

    # ==========================
    # 🏷 TAGS
    # ==========================
    tag_list = [t.strip() for t in tags.split(",")] if tags else []

    # ==========================
    # 📝 SAVE POST
    # ==========================
    now = datetime.utcnow()
    post_doc = {
        "user_id": user_id,
        "ratio": normalized_ratio,
        "caption": caption,
        "tags": tag_list,
        "likes": [],
        "comments": [],
        "views": [],
        "created_at": now,
        "updated_at": now,
        "type": file_type
    }

    if file_type == "image":
        post_doc["image_url"] = file_url
    else:
        post_doc["video_url"] = file_url

    result = await db.posts.insert_one(post_doc)

    return {
        "success": True,
        "post_id": str(result.inserted_id),
        "url": file_url,
        "type": file_type,
        "ratio": normalized_ratio,
        "created_at": now,
    }


@router.get("/feed")
async def get_feed(
    authorization: str = Header(...),
    page: int = 1,
    limit: int = 5
):
    """
    🎯 INFINITE SCROLL: Get paginated feed posts (Instagram-like)
    
    Query Parameters:
    - page: Page number (default 1, starts from 1)
    - limit: Posts per page (default 5, Instagram-like loading)
    
    Returns:
    - posts: Array of posts
    - pagination: {total, page, limit, hasMore}
    """
    # ✅ Validate pagination parameters
    page = max(1, page)  # Ensure page >= 1
    limit = max(1, min(limit, 50))  # Ensure 1 <= limit <= 50 (prevent abuse)
    skip = (page - 1) * limit

    user_id = get_user_id_from_header(authorization)

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    following = await db.follows.distinct(
        "following_id",
        {"follower_id": user_id, "status": "following"}
    )

    public_users = await db.users.distinct(
        "firebase_uid",
        {"$or": [{"account_type": "public"}, {"account_type": {"$exists": False}}]}
    )

    # ✅ Build feed query filter
    feed_filter = {
        "$or": [
            {"user_id": user_id},
            {"user_id": {"$in": following}},
            {"user_id": {"$in": public_users}},
            {"is_prompt_post": True}
        ]
    }

    # ✅ Get total count for pagination metadata
    total_posts = await db.posts.count_documents(feed_filter)

    # ✅ Fetch paginated posts (skip + limit for database-level pagination)
    posts = await db.posts.find(feed_filter).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    posts = await enrich_prompt_post_images(posts)

    # ✅ Attach user profiles to each post
    user_ids = list(set([post["user_id"] for post in posts if "user_id" in post]))
    user_profiles = {}
    if user_ids:
        user_profiles = {u["firebase_uid"]: u for u in await db.users.find({"firebase_uid": {"$in": user_ids}}).to_list(len(user_ids))}

    def build_user_profile(u):
        if not u:
            return None
        return {
            "firebase_uid": u.get("firebase_uid"),
            "username": u.get("username"),
            "full_name": u.get("full_name"),
            "image_name": u.get("image_name"),
            "gender": u.get("gender"),
            "isVerified": u.get("isVerified", False)
        }

    # ✅ Build response with pagination metadata
    result = []
    for post in posts:
        p = serialize_post(post)
        u = user_profiles.get(post["user_id"])
        p["user_profile"] = build_user_profile(u)
        result.append(p)

    # ✅ Calculate if there are more posts to load
    has_more = (skip + limit) < total_posts

    return {
        "posts": result,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total_posts,
            "hasMore": has_more,
            "totalPages": (total_posts + limit - 1) // limit
        }
    }


@router.get("/explore")
async def get_explore_posts(
    authorization: str = Header(...),
    page: int = 1,
    limit: int = 12,
):
    page = max(1, page)
    limit = max(1, min(limit, 50))
    skip = (page - 1) * limit

    user_id = get_user_id_from_header(authorization)

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    following = await db.follows.distinct(
        "following_id",
        {"follower_id": user_id, "status": "following"}
    )

    public_users = await db.users.distinct(
        "firebase_uid",
        {"$or": [{"account_type": "public"}, {"account_type": {"$exists": False}}]}
    )

    feed_filter = {
        "$or": [
            {"user_id": user_id},
            {"user_id": {"$in": following}},
            {"user_id": {"$in": public_users}},
            {"is_prompt_post": True},
        ]
    }

    total_posts = await db.posts.count_documents(feed_filter)

    posts = await db.posts.find(feed_filter).sort("created_at", -1).skip(skip).limit(limit).to_list(limit)
    posts = await enrich_prompt_post_images(posts)

    user_ids = list(set([post["user_id"] for post in posts if "user_id" in post]))
    user_profiles = {
        u["firebase_uid"]: u
        for u in await db.users.find({"firebase_uid": {"$in": user_ids}}).to_list(len(user_ids))
    }

    def build_user_profile(u):
        if not u:
            return None
        return {
            "firebase_uid": u.get("firebase_uid"),
            "username": u.get("username"),
            "full_name": u.get("full_name"),
            "image_name": u.get("image_name"),
            "gender": u.get("gender"),
            "isVerified": u.get("isVerified", False),
        }

    result = []
    for post in posts:
        p = serialize_post(post)
        u = user_profiles.get(post.get("user_id"))
        p["user_profile"] = build_user_profile(u)
        result.append(p)

    has_more = (skip + len(posts)) < total_posts

    return {
        "posts": result,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total_posts,
            "hasMore": has_more,
            "totalPages": (total_posts + limit - 1) // limit,
        },
    }



@router.delete("/delete/{post_id}")
async def delete_post(
    post_id: str,
    reason: Optional[str] = None,
    authorization: str = Header(...)
):
    # 🔐 Verify user
    user_id = get_user_id_from_header(authorization)

    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # 🚫 Only owner can delete
    if post["user_id"] != user_id:
        raise HTTPException(
            status_code=403,
            detail="You can delete only your own posts"
        )

    if post.get("is_prompt_post"):
        raw_prompt_id = str(post.get("prompt_id") or "")
        if not ObjectId.is_valid(raw_prompt_id):
            raise HTTPException(status_code=400, detail="Prompt post is missing a valid prompt ID")

        prompt = await db.ai_creator_prompts.find_one({"_id": ObjectId(raw_prompt_id), "user_id": user_id})
        if not prompt:
            raise HTTPException(status_code=404, detail="Prompt not found")

        prompt_status = str(prompt.get("status") or "").lower()
        if prompt_status == "delete_requested":
            return {
                "success": True,
                "requires_approval": True,
                "message": "Delete request already pending admin approval",
            }

        if prompt_status != "approved":
            raise HTTPException(status_code=400, detail="Only approved prompt posts can request deletion")

        delete_reason = (reason or "Creator requested to delete this prompt post").strip()
        now = datetime.utcnow()

        await db.ai_creator_prompts.update_one(
            {"_id": ObjectId(raw_prompt_id)},
            {
                "$set": {
                    "status": "delete_requested",
                    "delete_requested_at": now,
                    "delete_request_reason": delete_reason,
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
            "prompt_id": raw_prompt_id,
            "timestamp": now,
        })

        user = await db.users.find_one({"firebase_uid": user_id}, {"_id": 0, "full_name": 1, "username": 1})
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
                "message": f"Delete request for prompt {prompt.get('unit_id') or raw_prompt_id} by {creator_name}",
                "description": delete_reason,
                "prompt_id": raw_prompt_id,
                "submitted_by_user_id": user_id,
                "submitted_by_name": creator_name,
                "timestamp": now,
            })

        return {
            "success": True,
            "requires_approval": True,
            "message": "Delete request submitted for admin approval",
        }

    await db.posts.delete_one({"_id": ObjectId(post_id)})

    return {
        "success": True,
        "message": "Post deleted successfully"
    }



@router.get("/user/{user_id}")
async def get_user_posts(
    user_id: str,
    authorization: str = Header(...)
):
    viewer_id = get_user_id_from_header(authorization)

    # user_id can be ObjectId or username
    if ObjectId.is_valid(user_id):
        target = await db.users.find_one({"_id": ObjectId(user_id)})
    else:
        target = await db.users.find_one({"username": user_id})
    
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    
    target_id = str(target["_id"])

    # 🔒 Private account check
    allow_prompt_only = False
    if target["account_type"] == "private" and viewer_id != target_id:
        follow = await db.follows.find_one({
            "follower_id": viewer_id,
            "following_id": target_id,
            "status": "following"
        })
        if not follow:
            allow_prompt_only = True

    query = {"user_id": target_id}
    if allow_prompt_only:
        query["is_prompt_post"] = True

    posts = await db.posts.find(query).sort("created_at", -1).to_list(None)
    posts = await enrich_prompt_post_images(posts)

    return [serialize_post(post) for post in posts]


@router.post("/like/{post_id}")
async def like_post(post_id: str, authorization: str = Header(...)):
    user_id = get_user_id_from_header(authorization)

    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # 🔄 UNLIKE
    if user_id in post.get("likes", []):
        await db.posts.update_one(
            {"_id": ObjectId(post_id)},
            {"$pull": {"likes": user_id}}
        )

        # Sync with prompt post if needed
        if post.get("is_prompt_post") and post.get("prompt_id"):
            await db.ai_creator_prompts.update_one(
                {"_id": ObjectId(post.get("prompt_id"))},
                {"$pull": {"likes": user_id}}
            )

        return {"liked": False}

    # ❤️ LIKE
    await db.posts.update_one(
        {"_id": ObjectId(post_id)},
        {"$addToSet": {"likes": user_id}}
    )

    # Sync with prompt post if needed
    if post.get("is_prompt_post") and post.get("prompt_id"):
        await db.ai_creator_prompts.update_one(
            {"_id": ObjectId(post.get("prompt_id"))},
            {"$addToSet": {"likes": user_id}}
        )

    # 🔔 SEND NOTIFICATION (using your top-level function)
    await create_post_notification(
        to_user_id=post.get("user_id"),
        from_user=user,
        action="post_like",
        description=f"{user.get('full_name', 'User')} liked your post",
        post=post,
    )

    return {"liked": True}

# ============================================================
# 💬 GET COMMENTS
# ============================================================

@router.get("/comments/{post_id}")
async def get_comments(post_id: str):
    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Process comments - ensure all fields are present and ObjectId converted to string
    comments = []
    for c in post.get("comments", []):
        comment_id = c.get("comment_id")
        if isinstance(comment_id, ObjectId):
            comment_id = str(comment_id)
        comments.append({
            "comment_id": comment_id,
            "user_id": c.get("user_id"),
            "username": c.get("username"),
            "user_image": c.get("user_image"),
            "text": c.get("text"),
            "created_at": c.get("created_at"),
        })

    return {
        "total": len(comments),
        "comments": comments
    }


# ============================================================
# 💬 ADD COMMENT
# ============================================================

@router.post("/comment/{post_id}")
async def add_comment(
    post_id: str,
    text: str = Form(...),
    authorization: str = Header(...)
):
    # 🔐 Get user from token
    user_id = get_user_id_from_header(authorization)

    user = await db.users.find_one({"firebase_uid": user_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # 💬 Create comment object
    comment = {
        "comment_id": ObjectId(),
        "user_id": user_id,
        "username": user.get("username", "User"),
        "user_image": user.get("image_name", ""),
        "text": text,
        "created_at": datetime.utcnow()
    }

    # 📌 Save comment to post
    await db.posts.update_one(
        {"_id": ObjectId(post_id)},
        {"$push": {"comments": comment}}
    )

    # 🔔 Send notification (if not self comment)
    if post.get("user_id") != user_id:
        await create_post_notification(
            to_user_id=post.get("user_id"),
            from_user=user,
            action="post_comment",
            description=f"{user.get('full_name', 'User')} commented: {text}",
            post=post,
        )

    # Convert ObjectId before returning
    comment["comment_id"] = str(comment["comment_id"])

    return {
        "success": True,
        "comment": comment
    }


    
@router.get("/likes/{post_id}")
async def get_post_likes(
    post_id: str,
    authorization: str = Header(...)
):
    viewer_id = get_user_id_from_header(authorization)

    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    target = await db.users.find_one({"firebase_uid": post["user_id"]})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.get("account_type") == "private" and viewer_id != post["user_id"]:
        follow = await db.follows.find_one({
            "follower_id": viewer_id,
            "following_id": post["user_id"],
            "status": "following"
        })
        if not follow:
            raise HTTPException(status_code=403, detail="Private account")

    likes = post.get("likes", [])
    users = await db.users.find(
        {"firebase_uid": {"$in": likes}},
        {"_id": 0, "firebase_uid": 1, "username": 1, "public_id": 1, "full_name": 1, "image_name": 1, "gender": 1}
    ).to_list(None)

    return {
        "total": len(likes),
        "likes": users
    }


@router.get("/views/{post_id}")
async def get_post_views(
    post_id: str,
    authorization: str = Header(...)
):
    user_id = get_user_id_from_header(authorization)

    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # 🔒 Only owner can see viewers
    if post["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not allowed")

    views = post.get("views", [])

    users = await db.users.find(
        {"firebase_uid": {"$in": views}},
        {"_id": 0, "firebase_uid": 1, "username": 1, "full_name": 1, "image_name": 1, "gender": 1}
    ).to_list(None)

    return {
        "total": len(views),
        "views": users
    }


@router.post("/view/{post_id}")
async def add_post_view(
    post_id: str,
    authorization: str = Header(...)
):
    viewer_id = get_user_id_from_header(authorization)

    post = await db.posts.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    if post["user_id"] == viewer_id:
        return {"success": True, "total": len(post.get("views", []))}

    target = await db.users.find_one({"firebase_uid": post["user_id"]})
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.get("account_type") == "private":
        follow = await db.follows.find_one({
            "follower_id": viewer_id,
            "following_id": post["user_id"],
            "status": "following"
        })
        if not follow:
            raise HTTPException(status_code=403, detail="Private account")

    await db.posts.update_one(
        {"_id": ObjectId(post_id)},
        {"$addToSet": {"views": viewer_id}}
    )

    updated = await db.posts.find_one({"_id": ObjectId(post_id)})
    return {"success": True, "total": len(updated.get("views", []))}



# Renamed to media-proxy to support both images and videos
@router.get("/media-proxy")
async def get_media_proxy(
    url: str,
    authorization: str = Header(...)
):
    get_user_id_from_header(authorization)

    if not url or not url.startswith(f"{PUBLIC_BASE}/"):
        raise HTTPException(status_code=400, detail="Invalid media url")

    key = url.split(f"{PUBLIC_BASE}/", 1)[1]
    if not key:
        raise HTTPException(status_code=400, detail="Invalid media url")

    try:
        obj = s3.get_object(Bucket=BUCKET_NAME, Key=key)
    except Exception:
        raise HTTPException(status_code=404, detail="Media not found")

    content_type = obj.get("ContentType") or "application/octet-stream"
    return StreamingResponse(obj["Body"], media_type=content_type)
