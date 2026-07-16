from io import BytesIO
from typing import Optional, Tuple

from PIL import Image, UnidentifiedImageError


def process_uploaded_image_bytes(
    file_bytes: bytes,
    filename: str,
    content_type: Optional[str] = None,
    max_width: int = 1600,
    max_height: int = 1600,
    quality: int = 85,
    target_size_kb: int = 1200,
) -> Tuple[bytes, str, str]:
    """Resize and optimize an uploaded image before storing it."""
    if not file_bytes:
        raise ValueError("Empty image file")

    try:
        with Image.open(BytesIO(file_bytes)) as image:
            original_mode = image.mode
            if image.mode in {"RGBA", "LA", "P"}:
                image = image.convert("RGBA")
            else:
                image = image.convert("RGB")

            width, height = image.size
            if width > max_width or height > max_height:
                scale = min(max_width / width, max_height / height, 1.0)
                new_size = (max(1, int(width * scale)), max(1, int(height * scale)))
                image = image.resize(new_size, Image.LANCZOS)

            ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg")
            output_format = "JPEG"
            output_ext = "jpg"
            output_content_type = "image/jpeg"

            if ext == "png":
                output_format = "PNG"
                output_ext = "png"
                output_content_type = "image/png"
            elif ext == "webp":
                output_format = "WEBP"
                output_ext = "webp"
                output_content_type = "image/webp"

            if output_format == "JPEG":
                if image.mode in {"RGBA", "LA"}:
                    image = image.convert("RGB")
                elif image.mode == "P" and original_mode != "P":
                    image = image.convert("RGB")
            elif output_format == "PNG" and image.mode != "RGBA":
                image = image.convert("RGBA")
            elif output_format == "WEBP" and image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if image.mode == "LA" else "RGB")

            buffer = BytesIO()
            if output_format == "JPEG":
                image.save(buffer, format="JPEG", quality=quality, optimize=True)
            elif output_format == "WEBP":
                image.save(buffer, format="WEBP", quality=quality, optimize=True)
            else:
                image.save(buffer, format="PNG", optimize=True)

            data = buffer.getvalue()
            if len(data) > target_size_kb * 1024:
                for reduced_quality in (quality - 10, quality - 20, 70, 60, 50):
                    if reduced_quality <= 0:
                        break
                    buffer = BytesIO()
                    if output_format == "JPEG":
                        if image.mode in {"RGBA", "LA"}:
                            image = image.convert("RGB")
                        image.save(buffer, format="JPEG", quality=reduced_quality, optimize=True)
                    elif output_format == "WEBP":
                        image.save(buffer, format="WEBP", quality=reduced_quality, optimize=True)
                    else:
                        image.save(buffer, format="PNG", optimize=True)
                    data = buffer.getvalue()
                    if len(data) <= target_size_kb * 1024:
                        break

            return data, output_content_type, output_ext
    except UnidentifiedImageError as exc:
        raise ValueError("Invalid image file") from exc
