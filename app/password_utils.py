import re

from fastapi import HTTPException
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a password."""

    print("Password repr:", repr(password))
    print("Type:", type(password))
    print("Length:", len(password))
    print("Bytes:", len(password.encode("utf-8")))

    if not validate_password(password):
        raise HTTPException(
            status_code=400,
            detail=(
                "Password must be 8-72 characters long and contain at least "
                "one uppercase letter, one lowercase letter, and one number."
            ),
        )

    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def validate_password(password: str) -> bool:
    """
    Rules:
    - 8-72 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one number
    - Special characters optional
    """

    if not password:
        return False

    # bcrypt limit
    if len(password.encode("utf-8")) > 72:
        return False

    if len(password) < 8:
        return False

    if not re.search(r"[A-Z]", password):
        return False

    if not re.search(r"[a-z]", password):
        return False

    if not re.search(r"\d", password):
        return False

    return True