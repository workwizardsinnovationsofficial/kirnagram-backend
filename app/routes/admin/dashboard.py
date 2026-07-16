from fastapi import APIRouter, Header, HTTPException, Query
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from app.database import db
from app.firebase import verify_firebase_token
from firebase_admin import auth

router = APIRouter(prefix="/admin/dashboard", tags=["Admin Dashboard"])


def _start_of_day(dt: datetime) -> datetime:
    return datetime(dt.year, dt.month, dt.day)


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _resolve_range(
    range_key: str,
    start_date_value: Optional[str] = None,
    end_date_value: Optional[str] = None,
) -> Tuple[datetime, datetime, str]:
    now = datetime.utcnow()
    today_start = _start_of_day(now)

    if range_key == "custom":
        start_custom = _parse_iso_datetime(start_date_value)
        end_custom = _parse_iso_datetime(end_date_value)
        if start_custom and end_custom:
            if end_custom < start_custom:
                start_custom, end_custom = end_custom, start_custom
            granularity = "hour" if (end_custom - start_custom).days <= 2 else "day"
            return start_custom, end_custom, granularity
        return now - timedelta(days=30), now, "day"

    if range_key in {"live", "today"}:
        return today_start, now, "hour"
    if range_key == "yesterday":
        start = today_start - timedelta(days=1)
        return start, today_start, "hour"
    if range_key == "7days":
        return now - timedelta(days=7), now, "day"
    if range_key == "90days":
        return now - timedelta(days=90), now, "day"

    # default = last 30 days
    return now - timedelta(days=30), now, "day"


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def _tracked_duration_seconds(first_seen, last_seen, min_seconds: int = 60) -> int:
    if not isinstance(first_seen, datetime) or not isinstance(last_seen, datetime):
        return 0
    diff = max(0, int((last_seen - first_seen).total_seconds()))
    return diff if diff >= min_seconds else 0


async def verify_admin(authorization: str):
    if not authorization or " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)
    uid = decoded["uid"]
    user = await db.users.find_one({"firebase_uid": uid})

    if not user:
        raise HTTPException(status_code=403, detail="User not found")
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return user


async def verify_agent_or_admin(authorization: Optional[str]):
    # Dashboard is used by a local admin panel that may not forward auth headers.
    # If auth is absent, allow read-only analytics.
    if not authorization:
        return {"role": "admin", "permissions": {"prompts": True}}

    if " " not in authorization:
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.split(" ")[1]
    decoded = verify_firebase_token(token)
    uid = decoded["uid"]

    user = await db.users.find_one({"firebase_uid": uid})
    if not user:
        raise HTTPException(status_code=403, detail="User not found")

    return user


@router.post("/create-agent")
async def create_agent(data: dict, authorization: str = Header(...)):
    await verify_admin(authorization)

    email = data.get("email")
    password = data.get("password")
    permissions = data.get("permissions", {})

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="Agent already exists")

    firebase_user = auth.create_user(email=email, password=password)

    await db.users.insert_one(
        {
            "firebase_uid": firebase_user.uid,
            "email": email,
            "role": "agent",
            "permissions": permissions,
            "created_at": datetime.utcnow(),
        }
    )

    return {"message": "Agent created successfully"}


@router.get("/metrics")
async def get_dashboard_metrics(
    range_key: str = Query(default="30days", alias="range"),
    start_date_value: Optional[str] = Query(default=None, alias="start_date"),
    end_date_value: Optional[str] = Query(default=None, alias="end_date"),
    authorization: Optional[str] = Header(default=None),
):
    await verify_agent_or_admin(authorization)
    start_date, end_date, _ = _resolve_range(range_key, start_date_value, end_date_value)

    time_match = {"created_at": {"$gte": start_date, "$lt": end_date}}

    total_users = await db.users.count_documents(time_match)
    total_posts = await db.posts.count_documents(time_match)
    total_withdraw_requests = await db.withdraw_requests.count_documents(time_match)

    accepted_prompts = await db.ai_creator_prompts.count_documents(
        {**time_match, "status": "approved"}
    )
    rejected_prompts = await db.ai_creator_prompts.count_documents(
        {**time_match, "status": "rejected"}
    )
    edit_prompts = await db.ai_creator_prompts.count_documents(
        {**time_match, "status": "modify"}
    )

    likes_result = await db.posts.aggregate(
        [
            {"$match": time_match},
            {
                "$project": {
                    "likes_count": {
                        "$cond": [
                            {"$isArray": "$likes"},
                            {"$size": "$likes"},
                            0,
                        ]
                    }
                }
            },
            {"$group": {"_id": None, "total": {"$sum": "$likes_count"}}},
        ]
    ).to_list(1)
    total_likes = _safe_int(likes_result[0].get("total")) if likes_result else 0

    comments_result = await db.posts.aggregate(
        [
            {"$match": time_match},
            {
                "$project": {
                    "comments_count": {
                        "$cond": [
                            {"$isArray": "$comments"},
                            {"$size": "$comments"},
                            0,
                        ]
                    }
                }
            },
            {"$group": {"_id": None, "total": {"$sum": "$comments_count"}}},
        ]
    ).to_list(1)
    total_comments = _safe_int(comments_result[0].get("total")) if comments_result else 0

    shares_result = await db.posts.aggregate(
        [
            {"$match": time_match},
            {
                "$project": {
                    "shares_count": {
                        "$add": [
                            {
                                "$cond": [
                                    {"$isArray": "$shares"},
                                    {"$size": "$shares"},
                                    0,
                                ]
                            },
                            {"$ifNull": ["$share_count", 0]},
                        ]
                    }
                }
            },
            {"$group": {"_id": None, "total": {"$sum": "$shares_count"}}},
        ]
    ).to_list(1)
    total_shares = _safe_int(shares_result[0].get("total")) if shares_result else 0

    return {
        "range": range_key,
        "from": start_date,
        "to": end_date,
        "total_users": total_users,
        "total_posts": total_posts,
        "total_likes": total_likes,
        "total_comments": total_comments,
        "total_shares": total_shares,
        "withdraw_requests": total_withdraw_requests,
        "edit_prompts": edit_prompts,
        "accepted_prompts": accepted_prompts,
        "rejected_prompts": rejected_prompts,
    }


@router.get("/analytics")
async def get_analytics(
    range_key: str = Query(default="30days", alias="range"),
    start_date_value: Optional[str] = Query(default=None, alias="start_date"),
    end_date_value: Optional[str] = Query(default=None, alias="end_date"),
    authorization: Optional[str] = Header(default=None),
):
    user = await verify_agent_or_admin(authorization)

    if user.get("role") == "agent" and not user.get("permissions", {}).get("prompts", False):
        raise HTTPException(status_code=403, detail="You do not have permission to view analytics")

    start_date, end_date, granularity = _resolve_range(
        range_key,
        start_date_value,
        end_date_value,
    )

    data: List[Dict] = []
    cursor = start_date

    while cursor < end_date:
        if granularity == "hour":
            bucket_end = min(cursor + timedelta(hours=1), end_date)
            label = cursor.strftime("%H:00")
        else:
            bucket_end = min(cursor + timedelta(days=1), end_date)
            label = cursor.strftime("%d %b")

        bucket_match = {"created_at": {"$gte": cursor, "$lt": bucket_end}}

        likes_result = await db.posts.aggregate(
            [
                {"$match": bucket_match},
                {
                    "$project": {
                        "likes_count": {
                            "$cond": [{"$isArray": "$likes"}, {"$size": "$likes"}, 0]
                        }
                    }
                },
                {"$group": {"_id": None, "total": {"$sum": "$likes_count"}}},
            ]
        ).to_list(1)

        comments_result = await db.posts.aggregate(
            [
                {"$match": bucket_match},
                {
                    "$project": {
                        "comments_count": {
                            "$cond": [{"$isArray": "$comments"}, {"$size": "$comments"}, 0]
                        }
                    }
                },
                {"$group": {"_id": None, "total": {"$sum": "$comments_count"}}},
            ]
        ).to_list(1)

        shares_result = await db.posts.aggregate(
            [
                {"$match": bucket_match},
                {
                    "$project": {
                        "shares_count": {
                            "$add": [
                                {
                                    "$cond": [
                                        {"$isArray": "$shares"},
                                        {"$size": "$shares"},
                                        0,
                                    ]
                                },
                                {"$ifNull": ["$share_count", 0]},
                            ]
                        }
                    }
                },
                {"$group": {"_id": None, "total": {"$sum": "$shares_count"}}},
            ]
        ).to_list(1)

        posts_count = await db.posts.count_documents(bucket_match)
        accepted_prompts = await db.ai_creator_prompts.count_documents(
            {**bucket_match, "status": "approved"}
        )
        rejected_prompts = await db.ai_creator_prompts.count_documents(
            {**bucket_match, "status": "rejected"}
        )
        edit_prompts = await db.ai_creator_prompts.count_documents(
            {**bucket_match, "status": "modify"}
        )
        withdraw_requests = await db.withdraw_requests.count_documents(bucket_match)
        total_users = await db.users.count_documents(bucket_match)

        data.append(
            {
                "period": label,
                "likes": _safe_int(likes_result[0].get("total")) if likes_result else 0,
                "comments": _safe_int(comments_result[0].get("total")) if comments_result else 0,
                "shares": _safe_int(shares_result[0].get("total")) if shares_result else 0,
                "posts": posts_count,
                "editPrompts": edit_prompts,
                "acceptedPrompts": accepted_prompts,
                "rejectedPrompts": rejected_prompts,
                "withdrawRequests": withdraw_requests,
                "totalUsers": total_users,
            }
        )

        cursor = bucket_end

    return {
        "range": range_key,
        "from": start_date,
        "to": end_date,
        "granularity": granularity,
        "data": data,
    }


@router.get("/traffic")
async def get_traffic_data(
    range_key: str = Query(default="today", alias="range"),
    start_date_value: Optional[str] = Query(default=None, alias="start_date"),
    end_date_value: Optional[str] = Query(default=None, alias="end_date"),
    authorization: Optional[str] = Header(default=None),
):
    await verify_agent_or_admin(authorization)
    start_date, end_date, _ = _resolve_range(range_key, start_date_value, end_date_value)

    base_match = {"created_at": {"$gte": start_date, "$lt": end_date}}
    hourly_totals = {hour: 0 for hour in range(24)}

    async def _accumulate_hourly(collection_name: str):
        pipeline = [
            {"$match": base_match},
            {
                "$group": {
                    "_id": {"$hour": "$created_at"},
                    "count": {"$sum": 1},
                }
            },
        ]

        results = await getattr(db, collection_name).aggregate(pipeline).to_list(length=None)
        for row in results:
            hour = _safe_int(row.get("_id"))
            if 0 <= hour <= 23:
                hourly_totals[hour] += _safe_int(row.get("count"))

    await _accumulate_hourly("users")
    await _accumulate_hourly("posts")
    await _accumulate_hourly("ai_creator_remixes")

    traffic_data = [
        {"hour": f"{hour:02d}:00", "traffic": hourly_totals[hour]}
        for hour in range(24)
    ]

    return {
        "range": range_key,
        "from": start_date,
        "to": end_date,
        "data": traffic_data,
    }


@router.get("/users")
async def get_users_dashboard(
    range_key: str = Query(default="30days", alias="range"),
    start_date_value: Optional[str] = Query(default=None, alias="start_date"),
    end_date_value: Optional[str] = Query(default=None, alias="end_date"),
    limit: int = 500,
    authorization: Optional[str] = Header(default=None),
):
    await verify_agent_or_admin(authorization)
    start_date, end_date, _ = _resolve_range(range_key, start_date_value, end_date_value)

    user_query = {"created_at": {"$gte": start_date, "$lt": end_date}}
    users = await db.users.find(user_query).sort("created_at", -1).limit(limit).to_list(length=limit)

    normal_users = []
    ai_creator_users = []

    for user in users:
        uid = user.get("firebase_uid")
        if not uid:
            continue

        creator_app = await db.ai_creator_applications.find_one({"user_id": uid, "status": "approved"})
        is_creator = bool(creator_app)

        prompts_created_count = await db.ai_creator_prompts.count_documents({"user_id": uid})
        posts_count = await db.posts.count_documents(
            {"user_id": uid, "is_prompt_post": {"$ne": True}}
        )
        remixes_count = await db.ai_creator_remixes.count_documents({"user_id": uid})
        withdraw_count = await db.withdraw_requests.count_documents({"user_id": uid})

        followers = user.get("followers")
        following = user.get("following")

        followers_count = len(followers) if isinstance(followers, list) else await db.follows.count_documents(
            {"following_id": uid, "status": "following"}
        )
        following_count = len(following) if isinstance(following, list) else await db.follows.count_documents(
            {"follower_id": uid, "status": "following"}
        )

        wallet = await db.credit_wallets.find_one({"user_id": uid})
        wallet_balance = _safe_int((wallet or {}).get("balance"))

        creator_remaining_money = 0
        if is_creator:
            prompts = await db.ai_creator_prompts.find(
                {"user_id": uid},
                {"_id": 0, "payout_per_remix": 1, "remix_count": 1, "remixes": 1},
            ).to_list(length=None)

            total_earned = 0
            for prompt in prompts:
                payout = _safe_int(prompt.get("payout_per_remix") or 1)
                remix_count = _safe_int(prompt.get("remix_count"))
                if remix_count <= 0:
                    remixes_arr = prompt.get("remixes", [])
                    remix_count = len(remixes_arr) if isinstance(remixes_arr, list) else 0
                total_earned += payout * remix_count

            withdrawn_rows = await db.withdraw_requests.aggregate(
                [
                    {
                        "$match": {
                            "user_id": uid,
                            "status": {"$in": ["approved", "paid", "completed"]},
                        }
                    },
                    {"$group": {"_id": None, "sum": {"$sum": "$amount"}}},
                ]
            ).to_list(length=1)
            total_withdrawn = _safe_int(withdrawn_rows[0].get("sum")) if withdrawn_rows else 0
            creator_remaining_money = max(0, total_earned - total_withdrawn)

        base_row = {
            "user_id": uid,
            "name": user.get("full_name") or "",
            "username": user.get("username") or "",
            "email": user.get("email") or "",
            "mobile": user.get("mobile") or "",
            "gender": user.get("gender") or "",
            "location": user.get("location") or "",
            "wallet": wallet_balance,
            "posts_count": posts_count,
            "remixes_count": remixes_count,
            "created_at": user.get("created_at"),
            "followers_count": followers_count,
            "following_count": following_count,
            "account_type": user.get("account_type") or "public",
            "is_creator": is_creator,
        }

        if is_creator:
            ai_creator_users.append(
                {
                    **base_row,
                    "ai_creator_accepted_at": (creator_app or {}).get("updated_at")
                    or (creator_app or {}).get("approved_at")
                    or (creator_app or {}).get("created_at"),
                    "prompts_count": prompts_created_count,
                    "total_remaining_money": creator_remaining_money,
                    "withdraw_count": withdraw_count,
                }
            )
        else:
            normal_users.append(base_row)

    users_all = normal_users + ai_creator_users

    return {
        "range": range_key,
        "from": start_date,
        "to": end_date,
        "total_users": len(users_all),
        "normal_users_count": len(normal_users),
        "ai_creator_users_count": len(ai_creator_users),
        "users": users_all,
        "normal_users": normal_users,
        "ai_creator_users": ai_creator_users,
    }


@router.get("/traffic-users")
async def get_traffic_users(
    range_key: str = Query(default="30days", alias="range"),
    start_date_value: Optional[str] = Query(default=None, alias="start_date"),
    end_date_value: Optional[str] = Query(default=None, alias="end_date"),
    limit: int = 300,
    authorization: Optional[str] = Header(default=None),
):
    await verify_agent_or_admin(authorization)
    start_date, end_date, _ = _resolve_range(range_key, start_date_value, end_date_value)

    today_start = _start_of_day(datetime.utcnow())
    today_end = today_start + timedelta(days=1)
    range_start_day = _start_of_day(start_date)
    range_end_day = _start_of_day(end_date)

    today_rows = await db.user_daily_activity.find(
        {"date": today_start}
    ).sort("last_seen_at", -1).limit(limit).to_list(length=limit)

    today_users = []
    if today_rows:
        user_ids = [row.get("user_id") for row in today_rows if row.get("user_id")]
        creator_rows = await db.ai_creator_applications.find(
            {"user_id": {"$in": user_ids}, "status": "approved"},
            {"_id": 0, "user_id": 1},
        ).to_list(length=None)
        creator_ids = {row.get("user_id") for row in creator_rows if row.get("user_id")}

        for row in today_rows:
            first_seen = row.get("first_seen_at")
            last_seen = row.get("last_seen_at")
            duration_seconds = _tracked_duration_seconds(first_seen, last_seen)

            uid = row.get("user_id") or ""
            today_users.append(
                {
                    "user_id": uid,
                    "name": row.get("name") or "",
                    "username": row.get("username") or "",
                    "email": row.get("email") or "",
                    "mobile": row.get("mobile") or "",
                    "user_type": "ai_creator" if uid in creator_ids else "normal",
                    "first_seen_at": first_seen,
                    "last_seen_at": last_seen,
                    "time_used_seconds": duration_seconds,
                    "time_used_minutes": round(duration_seconds / 60, 2),
                    "hit_count": _safe_int(row.get("hit_count")),
                    "last_path": row.get("last_path") or "",
                }
            )

    if not today_users:
        fallback_users = await db.users.find(
            {
                "$or": [
                    {"created_at": {"$gte": today_start, "$lt": today_end}},
                    {"updated_at": {"$gte": today_start, "$lt": today_end}},
                ]
            }
        ).sort("updated_at", -1).limit(limit).to_list(length=limit)

        for user in fallback_users:
            uid = user.get("firebase_uid") or user.get("_id")
            if not uid:
                continue
            creator_app = await db.ai_creator_applications.find_one(
                {"user_id": uid, "status": "approved"}
            )
            created_at = user.get("created_at")
            updated_at = user.get("updated_at") or created_at
            today_users.append(
                {
                    "user_id": str(uid),
                    "name": user.get("full_name") or "",
                    "username": user.get("username") or "",
                    "email": user.get("email") or "",
                    "mobile": user.get("mobile") or "",
                    "user_type": "ai_creator" if creator_app else "normal",
                    "first_seen_at": created_at,
                    "last_seen_at": updated_at,
                    "time_used_seconds": 0,
                    "time_used_minutes": 0,
                    "hit_count": 0,
                    "last_path": "",
                }
            )

    history_docs = await db.user_daily_activity.find(
        {
            "date": {
                "$gte": range_start_day,
                "$lte": range_end_day,
            }
        },
        {
            "_id": 0,
            "date_key": 1,
            "hit_count": 1,
            "first_seen_at": 1,
            "last_seen_at": 1,
        },
    ).to_list(length=None)

    history_map: Dict[str, Dict[str, int]] = {}
    for doc in history_docs:
        date_key = doc.get("date_key") or ""
        if not date_key:
            continue

        first_seen = doc.get("first_seen_at")
        last_seen = doc.get("last_seen_at")
        seconds = _tracked_duration_seconds(first_seen, last_seen)

        if date_key not in history_map:
            history_map[date_key] = {
                "active_users": 0,
                "total_hits": 0,
                "total_seconds": 0,
            }

        history_map[date_key]["active_users"] += 1
        history_map[date_key]["total_hits"] += _safe_int(doc.get("hit_count"))
        history_map[date_key]["total_seconds"] += seconds

    if not history_map:
        fallback_users_history = await db.users.find(
            {
                "created_at": {
                    "$gte": range_start_day,
                    "$lt": range_end_day + timedelta(days=1),
                }
            },
            {"_id": 0, "created_at": 1},
        ).to_list(length=None)

        for user in fallback_users_history:
            created_at = user.get("created_at")
            if not created_at:
                continue
            date_key = created_at.strftime("%Y-%m-%d")
            if date_key not in history_map:
                history_map[date_key] = {
                    "active_users": 0,
                    "total_hits": 0,
                    "total_seconds": 0,
                }
            history_map[date_key]["active_users"] += 1

    daily_history = []
    for date_key in sorted(history_map.keys(), reverse=True)[:60]:
        row = history_map[date_key]
        daily_history.append(
            {
                "date": date_key,
                "active_users": row["active_users"],
                "total_hits": row["total_hits"],
                "total_time_minutes": round(row["total_seconds"] / 60, 2),
            }
        )

    return {
        "range": range_key,
        "from": start_date,
        "to": end_date,
        "today": today_start,
        "today_active_users": len(today_users),
        "today_users": today_users,
        "daily_history": daily_history,
    }
