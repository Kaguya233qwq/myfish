from __future__ import annotations

import re
from enum import IntEnum
from pathlib import Path
from typing import Literal, TypeAlias

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
)

from .utils import compact_json

MediaSource: TypeAlias = Path | bytes


class Visibility(IntEnum):
    PUBLIC = 0
    PRIVATE = 1


def media_id(value: str) -> str:
    leaf = value.removeprefix("spectrum/")
    if re.fullmatch(r"[A-Za-z0-9_-]+", leaf) is None:
        raise ValueError("Invalid spectrum file identifier")
    return "spectrum/" + leaf


class UploadPermit(BaseModel):
    file_ids: list[str] = Field(alias="fileIds", min_length=1)
    upload_addr: str = Field(alias="uploadAddr", min_length=1)
    token: SecretStr
    expire_time: int = Field(alias="expireTime", gt=0)
    signed_at_ms: int = Field(default=0, exclude=True)

    @property
    def expires_at(self) -> int:
        return (
            self.expire_time // 1000
            if self.expire_time >= 1_000_000_000_000
            else self.expire_time
        )


class UploadPermits(BaseModel):
    permits: list[UploadPermit] = Field(alias="uploadTempPermits", min_length=1)


class ImageAsset(BaseModel):
    file_id: str
    size: int = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]

    _normalize_id = field_validator("file_id")(media_id)


class VideoAsset(BaseModel):
    file_id: str
    size: int = Field(gt=0)
    video_id: str = Field(min_length=1)

    _normalize_id = field_validator("file_id")(media_id)


class VideoTrack(BaseModel):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_ms: int = Field(gt=0, serialization_alias="duration")
    frame_rate: float = Field(gt=0, allow_inf_nan=False)
    format: str = Field(min_length=1)
    bitrate: int | None = Field(default=None, gt=0)
    rotation: int = 0
    colour_primaries: str | None = None
    matrix_coefficients: str | None = None
    transfer_characteristics: str | None = None


class AudioTrack(BaseModel):
    duration_ms: int = Field(gt=0, serialization_alias="duration")
    format: str = Field(min_length=1)
    channels: int = Field(gt=0)
    sampling_rate: int = Field(gt=0)
    bitrate: int | None = Field(default=None, gt=0)


class VideoMetadata(BaseModel):
    video: VideoTrack
    audio: AudioTrack | None = None


class PostContent(BaseModel):
    title: str = ""
    description: str = ""
    visibility: Visibility = Visibility.PRIVATE
    scheduled_at_ms: int | None = Field(default=None, gt=0)


class ImagePost(PostContent):
    images: list[ImageAsset] = Field(min_length=1)


class VideoPost(PostContent):
    video: VideoAsset
    cover: ImageAsset
    metadata: VideoMetadata


class TranscodeStatus(BaseModel):
    status: int | str | None = None
    has_first_frame: bool = Field(
        default=False, validation_alias=AliasChoices("hasFirstFrame", "has_first_frame")
    )
    first_frame_id: str = Field(
        default="",
        validation_alias=AliasChoices("firstFrameFileId", "first_frame_file_id"),
    )

    @property
    def ready(self) -> bool:
        return (
            self.has_first_frame
            or bool(self.first_frame_id)
            or self.status in (2, "success", "SUCCESS")
        )


class PublishResult(BaseModel):
    accepted: Literal[True] = True
    note_id: str | None = None
    raw: dict[str, JsonValue] = Field(repr=False)


class PublishUncertainError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "Publication outcome is unknown; check the creator note list before retrying"
        )


def post_payload(post: ImagePost | VideoPost) -> dict[str, JsonValue]:
    binds: dict[str, JsonValue] = {
        "version": 1,
        "noteId": 0,
        "bizType": 0,
        "noteOrderBind": {},
        "notePostTiming": {},
        "noteCollectionBind": {"id": ""},
        "noteSketchCollectionBind": {"id": ""},
        "noteCopyBind": {"copyable": isinstance(post, VideoPost)},
        "interactionPermissionBind": {"commentPermission": 0},
        "optionRelationList": [],
    }
    if post.scheduled_at_ms is not None:
        binds = {
            "version": 1,
            "noteId": 0,
            "bizType": 13,
            "noteOrderBind": {},
            "notePostTiming": {"postTime": str(post.scheduled_at_ms)},
            "noteCollectionBind": {"id": ""},
        }
    common: dict[str, JsonValue] = {
        "type": "video" if isinstance(post, VideoPost) else "normal",
        "title": post.title,
        "note_id": "",
        "desc": post.description,
        "source": compact_json(
            {
                "type": "web",
                "ids": "",
                "extraInfo": compact_json({"subType": "official", "systemId": "web"}),
            }
        ),
        "business_binds": compact_json(binds),
        "ats": [],
        "hash_tag": [],
        "privacy_info": {"op_type": 1, "type": post.visibility.value, "user_ids": []},
        "goods_info": {},
        "biz_relations": [],
        "capa_trace_info": {
            "contextJson": compact_json(
                {
                    "recommend_title": {
                        "recommend_title_id": "",
                        "is_use": 3,
                        "used_index": -1,
                    },
                    "recommendTitle": [],
                    "recommend_topics": {"used": []},
                }
            )
        },
    }
    if isinstance(post, ImagePost):
        images: list[JsonValue] = [
            {
                "file_id": image.file_id,
                "width": image.width,
                "height": image.height,
                "metadata": {"source": -1},
                "extra_info_json": compact_json(
                    {
                        "mimeType": image.mime_type,
                        "image_metadata": {
                            "bg_color": "",
                            "origin_size": image.size / 1024,
                        },
                    }
                ),
            }
            for image in post.images
        ]
        return {"common": common, "image_info": {"images": images}, "video_info": None}
    video = post.metadata.video.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    audio = (
        post.metadata.audio.model_dump(mode="json", by_alias=True, exclude_none=True)
        if post.metadata.audio is not None
        else {}
    )
    metadata: dict[str, JsonValue] = {"video": video, "audio": audio}
    seconds = post.metadata.video.duration_ms / 1000
    duration = int(seconds) if seconds.is_integer() else seconds
    video_info: dict[str, JsonValue] = {
        "fileid": post.video.file_id,
        "file_id": post.video.file_id,
        "format_width": post.metadata.video.width,
        "format_height": post.metadata.video.height,
        "video_preview_type": "",
        "composite_metadata": metadata,
        "timelines": [],
        "cover": {
            "fileid": post.cover.file_id,
            "file_id": post.cover.file_id,
            "height": post.cover.height,
            "width": post.cover.width,
            "frame": {"ts": 0, "is_user_select": True, "is_upload": True},
            "stickers": {"version": 2, "neptune": []},
            "fonts": [],
            "extra_info_json": compact_json(
                {"cover_recommend": {"isUseRecommend": False, "isAiCover": False}}
            ),
        },
        "chapters": [],
        "summary": "",
        "chapter_sync_text": False,
        "segments": {
            "count": 1,
            "need_slice": False,
            "items": [
                {
                    "mute": 0,
                    "speed": 1,
                    "start": 0,
                    "duration": duration,
                    "transcoded": 0,
                    "media_source": 1,
                    "original_metadata": metadata,
                }
            ],
        },
        "entrance": "web",
        "pk_cover_biz_relations": [],
    }
    return {"common": common, "image_info": None, "video_info": video_info}
