import json
import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    field_validator,
)

from .notes import NoteUser
from .sign import SigningContext
from .utils import now_ms


@dataclass(slots=True)
class SigningSession:
    clock: Callable[[], int] = field(default=now_ms, repr=False)
    random_bits: Callable[[int], int] = field(default=secrets.randbits, repr=False)
    random_below: Callable[[int], int] = field(default=secrets.randbelow, repr=False)
    loaded_at_ms: int = field(init=False)
    sequence: int = 0
    dsl: str | None = None
    tiga_timestamp: int | None = None
    tab_device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    rwp_fingerprint: str = ""
    _legacy_sequence: int = field(init=False, repr=False)
    _legacy_environment: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.loaded_at_ms = self.clock()
        if not self.rwp_fingerprint:
            self.rwp_fingerprint = str(self.loaded_at_ms)
        self._legacy_sequence = 15 + self.random_below(3)
        self._legacy_environment = 1000 + self.random_below(1001)

    def legacy_context(self) -> SigningContext:
        self._legacy_sequence += self.random_below(2)
        self._legacy_environment += 1 + self.random_below(10)
        return SigningContext(
            timestamp_ms=self.clock(),
            loaded_at_ms=self.loaded_at_ms,
            sequence=self._legacy_sequence,
            environment_size=self._legacy_environment,
            seed=self.random_bits(32),
        )

    def current_context(self, path: str, *, security: bool = False) -> SigningContext:
        stages = {
            "/api/sns/web/v1/login/qrcode/create": 1350,
            "/api/qrcode/userinfo": 1351,
            "/api/sns/web/v1/login/qrcode/status": 1351,
            "/api/sec/v1/shield/webprofile": 1358,
        }
        self.sequence += 1
        return SigningContext(
            timestamp_ms=self.clock(),
            loaded_at_ms=self.loaded_at_ms,
            sequence=self.sequence,
            environment_size=1315
            if security
            else stages.get(path.split("?", 1)[0], 1352),
            seed=self.random_bits(32),
            environment=bytes.fromhex(
                "f8536767c9b581635e0744fa8415"
                if security
                else "f9416767c9b581635e0744fa8415"
            ),
        )

    def ds_anchor(self) -> str:
        if self.dsl is None or self.tiga_timestamp is None:
            raise ValueError("DS and websectiga initialization are required")
        return f"{self.tiga_timestamp};{self.dsl}"


class CookieState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(min_length=1)
    value: str = Field(repr=False)
    domain: str
    path: str
    secure: bool
    expires: int | None
    domain_specified: bool
    domain_initial_dot: bool
    path_specified: bool


class ClientState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    cookies: list[CookieState] = Field(repr=False)
    fingerprint: dict[str, JsonValue] = Field(repr=False)
    secret: str = Field(pattern=r"^(?:[0-9a-f]{2}){32,}$", repr=False)
    loaded_at_ms: int = Field(ge=0)
    sequence: int = Field(ge=0, lt=0xFFFFFFFF)
    dsl: str = Field(pattern=r"^\d+$")
    tiga_timestamp: int = Field(ge=0)
    tab_device_id: str = Field(default_factory=lambda: str(uuid.uuid4()), min_length=1)
    rwp_fingerprint: str = ""


class ScriptData(BaseModel):
    data: str = Field(min_length=1)
    secPoisonId: str = Field(min_length=1)


class ScriptResponse(BaseModel):
    data: ScriptData


class QrcodeData(BaseModel):
    qr_id: str = Field(min_length=1)
    code: str = Field(min_length=1)
    url: str = Field(min_length=1)


class QrcodeResponse(BaseModel):
    data: QrcodeData


class ActivateData(BaseModel):
    session: str = ""
    ssk: str = ""


class ActivateResponse(BaseModel):
    data: ActivateData


class PollData(BaseModel):
    codeStatus: Literal[0, 1, 2, 3]


class PollResponse(BaseModel):
    data: PollData


class LoginInfo(BaseModel):
    session: str = ""
    ssk: str = Field(default="", repr=False)


class LoginData(BaseModel):
    code_status: Literal[2]
    login_info: LoginInfo


class LoginResponse(BaseModel):
    data: LoginData


class VerificationInitData(BaseModel):
    rid: str = Field(min_length=1, repr=False)


class VerificationInitResponse(BaseModel):
    data: VerificationInitData


class VerificationStatusData(BaseModel):
    status: Literal[1, 2, 3, 4, 5, 6]


class VerificationStatusResponse(BaseModel):
    data: VerificationStatusData


class XhsResponse(BaseModel):
    success: bool
    code: int


class CreatorResponse(BaseModel):
    success: bool
    code: int | None = Field(
        default=None, validation_alias=AliasChoices("code", "result")
    )


class SmsReceiver(BaseModel):
    receiver: str = Field(min_length=1, repr=False)


class SmsCodeSent(BaseModel):
    rid: str = Field(min_length=1, repr=False)
    expire_seconds: int = Field(alias="expireSec", ge=0)


class Account(BaseModel):
    user_id: str
    guest: StrictBool
    nickname: str = ""
    red_id: str = ""


class CreatorAccount(BaseModel):
    user_id: str = Field(alias="userId", min_length=1)
    nickname: str = Field(default="", alias="userName")
    avatar: str = Field(default="", alias="userAvatar")
    images: str = ""
    imageb: str = ""
    desc: str = ""
    gender: int = 0


class ChatInfo(BaseModel):
    nickname: str = ""
    avatar: str = ""
    user_name: str = ""
    is_friend: bool = False
    is_official: bool = False
    is_mute: bool = False
    is_block: bool = False


class Chat(BaseModel):
    user_id: str
    chat_user_id: str
    start_store_id: int
    max_store_id: int
    last_msg_content: str = Field(repr=False)
    last_msg_time: int
    update_time: int
    chat_status: int
    info: ChatInfo


class ChatPage(BaseModel):
    page: int = Field(ge=0)
    total: int = Field(ge=0)
    next_ts: int = Field(ge=-1)
    chats: list[Chat]


class UnreadCounts(BaseModel):
    user_chat_unread_counts: dict[str, int]
    group_chat_unread_counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.user_chat_unread_counts.values()) + sum(
            self.group_chat_unread_counts.values()
        )


class Message(BaseModel):
    id: str
    uuid: str
    sender_id: str
    receiver_id: str
    store_id: int = Field(ge=0)
    created_at: int
    content: str = Field(repr=False)
    group_chat: bool
    revoked: bool
    domain: str = ""
    extra_status: int = 0
    range_blank_end_store_id: int = 0

    @property
    def text(self) -> str | None:
        try:
            payload = json.loads(self.content)
        except ValueError:
            return None
        if isinstance(payload, dict) and payload.get("content_type") == 1:
            content = payload.get("content")
            return content if isinstance(content, str) else None
        return None


class OperationResult(BaseModel):
    success: StrictBool
    code: int
    message: str = Field(default="", repr=False)


class MessagePage(BaseModel):
    out_message_list: list[Message] = Field(default_factory=list)
    result: OperationResult

    @property
    def next_cursor(self) -> int | None:
        cursors = [
            message.store_id
            for message in self.out_message_list
            if message.store_id > 0
        ]
        return min(cursors) if cursors else None


class SendResult(BaseModel):
    message_id: str
    chat_ack: str = Field(repr=False)


class SendReceipt(SendResult):
    client_id: str


class PushMessage(BaseModel):
    client_id: str
    id: str
    timestamp_ms: int
    payload: str = Field(repr=False)


class MessageAck(BaseModel):
    client_id: str
    id: str
    timestamp_ms: int
    code: int
    store_id: int


class PushToken(BaseModel):
    access_token: str = Field(
        min_length=1, repr=False, validation_alias=AliasChoices("a_lt", "aLt")
    )
    expires_in: int = Field(
        gt=0, validation_alias=AliasChoices("expired_time", "expiredTime")
    )


class XhsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    receive_offline: bool = False
    reconnects: int = Field(default=5, ge=0, le=10)


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    sender_id: str = Field(alias="sender", min_length=1)
    receiver_id: str = Field(alias="receiver", min_length=1)
    timestamp_ms: int = Field(alias="timestamp", ge=0)
    id: str = Field(alias="message_id", min_length=1)
    content: str = Field(repr=False)
    uuid: str = ""
    group_chat: bool = False
    revoked: bool = False

    @property
    def sender_name(self) -> str:
        try:
            payload = json.loads(self.content)
        except ValueError:
            return self.sender_id
        if isinstance(payload, dict):
            name = payload.get("nickname")
            if isinstance(name, str) and name:
                return name
        return self.sender_id


NotificationCategory = Literal["mentions", "likes", "connections"]


class NotificationNote(BaseModel):
    id: str
    type: str = ""
    content: str = ""
    image: str = ""
    link: str = ""
    xsec_token: str = Field(default="", repr=False)


class NotificationComment(BaseModel):
    id: str
    content: str = ""
    target_comment: "NotificationComment | None" = None


class Notification(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    type: str
    time: int = Field(ge=0)
    user: NoteUser = Field(validation_alias=AliasChoices("user_info", "user"))
    item_info: NotificationNote | None = None
    comment_info: NotificationComment | None = None


class NotificationPage(BaseModel):
    message_list: list[dict[str, JsonValue]] = Field(default_factory=list)
    has_more: bool = False
    cursor: str | int = Field(
        default="", validation_alias=AliasChoices("strCursor", "cursor")
    )


class NotificationPush(BaseModel):
    id: str = Field(alias="pushid", min_length=1)
    topic: str
    timestamp_ms: int = Field(alias="ts", ge=0)
    payload: dict[str, JsonValue] = Field(repr=False)

    @field_validator("payload", mode="before")
    @classmethod
    def decode_payload(cls, value: object) -> object:
        return json.loads(value) if isinstance(value, str) else value

    @property
    def categories(self) -> tuple[NotificationCategory, ...]:
        you = self.payload.get("you")
        if self.topic != "message_center" or not isinstance(you, dict):
            return ()
        categories: tuple[NotificationCategory, ...] = (
            "mentions",
            "likes",
            "connections",
        )
        result: list[NotificationCategory] = []
        for category in categories:
            count = you.get(category)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                result.append(category)
        return tuple(result)
