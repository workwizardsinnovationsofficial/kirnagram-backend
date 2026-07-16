from pydantic import BaseModel
from typing import Optional

class GoogleUser(BaseModel):
    full_name: str
    image_name: Optional[str] = "default.png"
