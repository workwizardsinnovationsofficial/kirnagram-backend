import os
from typing import Optional

import boto3
from dotenv import load_dotenv
from botocore.config import Config

load_dotenv()

BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
PUBLIC_BASE = os.getenv("R2_PUBLIC_BASE") or os.getenv("R2_ENDPOINT")

# Cloudflare R2 requires AWS S3 V4 signing and path-style addressing
_config = Config(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
)

s3 = boto3.client(
    "s3",
    endpoint_url=os.getenv("R2_ENDPOINT"),
    aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
    aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
    region_name="auto",
    config=_config,
)


def _should_presign(base_url: Optional[str]) -> bool:
    if not base_url:
        return True
    base = base_url.rstrip("/")
    endpoint = (os.getenv("R2_ENDPOINT") or "").rstrip("/")
    if endpoint and base == endpoint:
        return True
    if "r2.cloudflarestorage.com" in base:
        return True
    return False


def get_public_url(key: str) -> str:
    if not PUBLIC_BASE:
        return ""
    base = PUBLIC_BASE.rstrip("/")
    return f"{base}/{key}"


def get_display_url(key: str, expires_in: int = 3600) -> str:
    public_url = get_public_url(key)
    if public_url and not _should_presign(public_url):
        return public_url
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )
