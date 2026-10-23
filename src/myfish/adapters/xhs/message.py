from typing import ClassVar, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

from myfish.core.message import Image, MessageChain, MessageSegment, Text, Video

from .model import MessageAck, PushMessage
from .notes import NoteRef, NoteUser, SharedNote


def _varint(value: int) -> bytes:
    if not 0 <= value < 1 << 64:
        raise ValueError("Protobuf integers must fit unsigned 64-bit values")
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(number: int, value: str | bytes | int) -> bytes:
    if isinstance(value, int):
        return _varint(number << 3) + _varint(value)
    raw = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(raw)) + raw


def encode_text(
    sender: str, receiver: str, text: str, message_id: str, timestamp_ms: int
) -> bytes:
    if not text.strip():
        raise ValueError("Message text must not be empty")
    if not all(
        identifier and identifier.isascii() and identifier.isalnum()
        for identifier in (sender, receiver)
    ):
        raise ValueError("Sender and receiver must be ASCII alphanumeric identifiers")
    if not message_id or len(message_id) > 128:
        raise ValueError("Message id must contain 1–128 characters")
    body = b"".join(
        _field(number, value)
        for number, value in (
            (1, message_id),
            (2, timestamp_ms),
            (3, ""),
            (4, sender),
            (5, receiver),
            (6, text),
            (7, 1),
            (8, ""),
            (9, 0),
            (11, ""),
            (12, 1),
        )
    )
    return _field(1, 1) + _field(9, body)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    result = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError("Truncated protobuf integer")
        value = data[offset]
        offset += 1
        if shift == 63 and value > 1:
            raise ValueError("Protobuf integer overflow")
        result |= (value & 127) << shift
        if value < 128:
            return result, offset
    raise ValueError("Invalid protobuf integer")


def _fields(data: bytes) -> dict[int, bytes | int]:
    if len(data) > 4 * 1024 * 1024:
        raise ValueError("IM payload exceeds size limit")
    fields: dict[int, bytes | int] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            value, offset = _read_varint(data, offset)
            fields[number] = value
            continue
        if wire == 2:
            size, offset = _read_varint(data, offset)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
        else:
            raise ValueError("Unsupported protobuf wire type")
        end = offset + size
        if end > len(data):
            raise ValueError("Truncated protobuf field")
        fields[number] = data[offset:end]
        offset = end
    return fields


def _text(fields: dict[int, bytes | int], number: int) -> str:
    value = fields.get(number, b"")
    if not isinstance(value, bytes):
        raise TypeError("Expected protobuf string")
    return value.decode()


def _number(fields: dict[int, bytes | int], number: int) -> int:
    value = fields.get(number, 0)
    if not isinstance(value, int):
        raise TypeError("Expected protobuf integer")
    return value


def decode_message(data: bytes) -> PushMessage | MessageAck | None:
    outer = _fields(data)
    for branch in (4, 5):
        payload = outer.get(branch)
        if payload is None:
            continue
        if not isinstance(payload, bytes):
            raise TypeError("Expected nested IM message")
        fields = _fields(payload)
        if branch == 4:
            return PushMessage(
                client_id=_text(fields, 1),
                id=_text(fields, 2),
                timestamp_ms=_number(fields, 3),
                payload=_text(fields, 5),
            )
        return MessageAck(
            client_id=_text(fields, 1),
            id=_text(fields, 2),
            timestamp_ms=_number(fields, 3),
            code=_number(fields, 5),
            store_id=_number(fields, 7),
        )
    return None


class MediaSize(BaseModel):
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)


class MediaSource(BaseModel):
    url: str


class ImagePayload(BaseModel):
    link: str = ""
    size: MediaSize = Field(default_factory=MediaSize)
    sources: dict[str, MediaSource] = Field(
        default_factory=dict, validation_alias="imageDataMap"
    )
    file_id: str = Field(default="", validation_alias="imageFileId")
    original: bool = Field(default=False, validation_alias="isSendOriginal")
    md5: str = ""


class VideoData(BaseModel):
    file_id: str = Field(default="", validation_alias="videoFileId")
    video_id: str = Field(default="", validation_alias="videoId")


class VideoPayload(ImagePayload):
    duration_ms: int = Field(ge=0, validation_alias="duration")
    video_link: str = Field(default="", validation_alias="videoLink")
    original_video_link: str = Field(default="", validation_alias="originalVideoLink")
    video_data: VideoData = Field(
        default_factory=VideoData, validation_alias="videoData"
    )


class StickerPayload(BaseModel):
    image: str
    emoji_key: str = Field(default="", validation_alias="emojiKey")
    subtype: str = Field(default="", validation_alias="subType")
    firework_id: str = Field(default="", validation_alias="fireworkId")
    interact_anim_type: str = Field(default="", validation_alias="interactAnimType")
    operation_info: str = Field(default="", validation_alias="operationInfo")
    play_mode: int = Field(default=0, validation_alias="playMode")


class Sticker(Image):
    type: str = "sticker"
    desc: str = "大表情"
    emoji_key: str = ""
    subtype: str = ""
    firework_id: str = ""
    interact_anim_type: str = ""
    operation_info: str = ""
    play_mode: int = 0


Payload = TypeVar("Payload", bound=BaseModel)


def _parse_content(payload: dict[str, JsonValue], model: type[Payload]) -> Payload:
    content = payload.get("content")
    if not isinstance(content, str):
        raise TypeError("Media message content must be a JSON string")
    try:
        return model.model_validate_json(content)
    except ValidationError:
        raise ValueError("Invalid media message payload") from None


def _media_url(*candidates: str) -> str:
    for candidate in candidates:
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if parsed.scheme in {"https", "http"} and parsed.hostname:
            return candidate
    raise ValueError("Media message has no HTTP resource")


def _image_url(payload: ImagePayload, *scenes: str) -> str:
    return _media_url(
        *(payload.sources[scene].url for scene in scenes if scene in payload.sources),
        payload.link,
    )


class NoteCard(MessageSegment):
    type: str = "note_card"
    desc: str = "小红书笔记卡片"
    ref: NoteRef
    title: str
    cover: str = ""
    author: NoteUser
    note_type: str = ""


class XhsPayloadNode(BaseModel):
    _registry: ClassVar[dict[int, type["XhsPayloadNode"]]] = {}
    content_type: ClassVar[int] = 0

    def __init_subclass__(cls) -> None:
        super().__init_subclass__()
        if cls.content_type:
            cls._registry[cls.content_type] = cls

    @classmethod
    def decode(cls, payload: dict[str, JsonValue]) -> list[MessageSegment]:
        raise NotImplementedError


class XhsTextNode(XhsPayloadNode):
    content_type = 1

    @classmethod
    def decode(cls, payload: dict[str, JsonValue]) -> list[MessageSegment]:
        content = payload.get("content")
        if not isinstance(content, str):
            raise TypeError("Text message content must be a string")
        return [Text(text=content)]


class XhsImageNode(XhsPayloadNode):
    content_type = 2

    @classmethod
    def decode(cls, payload: dict[str, JsonValue]) -> list[MessageSegment]:
        image = _parse_content(payload, ImagePayload)
        return [
            Image(
                image_url=_image_url(image, "IM_ORG", "IM_DTL", "IM_PRV"),
                width=image.size.width,
                height=image.size.height,
                extra={
                    "file_id": image.file_id,
                    "original": image.original,
                    "md5": image.md5,
                },
            )
        ]


class XhsVideoNode(XhsPayloadNode):
    content_type = 11

    @classmethod
    def decode(cls, payload: dict[str, JsonValue]) -> list[MessageSegment]:
        video = _parse_content(payload, VideoPayload)
        return [
            Video(
                video_url=_media_url(video.video_link, video.original_video_link),
                thumb_url=_image_url(video, "IM_PRV", "IM_DTL", "IM_ORG"),
                width=video.size.width,
                height=video.size.height,
                duration_ms=video.duration_ms,
                extra={
                    "file_id": video.video_data.file_id,
                    "video_id": video.video_data.video_id,
                },
            )
        ]


class XhsStickerNode(XhsPayloadNode):
    content_type = 13

    @classmethod
    def decode(cls, payload: dict[str, JsonValue]) -> list[MessageSegment]:
        sticker = _parse_content(payload, StickerPayload)
        return [
            Sticker(
                image_url=_media_url(sticker.image),
                emoji_key=sticker.emoji_key,
                subtype=sticker.subtype,
                firework_id=sticker.firework_id,
                interact_anim_type=sticker.interact_anim_type,
                operation_info=sticker.operation_info,
                play_mode=sticker.play_mode,
            )
        ]


class XhsNoteNode(XhsPayloadNode):
    content_type = 3

    @classmethod
    def decode(cls, payload: dict[str, JsonValue]) -> list[MessageSegment]:
        card = SharedNote.from_payload(payload)
        if card is None:
            return []
        try:
            ref = card.ref
        except ValueError:
            raise ValueError("Invalid shared note reference") from None
        return [
            NoteCard(
                ref=ref,
                title=card.title,
                cover=card.cover or card.image,
                author=card.user,
                note_type=card.note_type,
            )
        ]


def decode_content(content: str) -> MessageChain:
    try:
        payload = TypeAdapter(dict[str, JsonValue]).validate_json(content)
    except ValidationError:
        raise ValueError("Invalid XHS message content") from None
    content_type = payload.get("content_type")
    if not isinstance(content_type, int) or isinstance(content_type, bool):
        raise TypeError("Message content type must be an integer")
    node = XhsPayloadNode._registry.get(content_type)
    return MessageChain(node.decode(payload)) if node is not None else MessageChain()
