from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    secure_cookies: bool = False
    session_hours: int = Field(default=12, ge=1, le=72)
    public_origin: str = ""
    poll_seconds: int = Field(default=60, ge=10, le=3600)
    listing_sync_seconds: int = Field(default=300, ge=60, le=86400)
    listing_sync_batch: int = Field(default=20, ge=1, le=100)
    data_dir: Path | None = None
