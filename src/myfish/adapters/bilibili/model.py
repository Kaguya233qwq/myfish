from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_validator,
    model_validator,
)


class Account(BaseModel):
    mid: int = Field(gt=0)
    uname: str
    is_login: bool = Field(alias="isLogin")


class Message(BaseModel):
    sender_uid: int = Field(gt=0)
    receiver_id: int = Field(gt=0)
    receiver_type: int
    msg_type: int
    content: str = Field(repr=False)
    msg_seqno: int = Field(ge=0)
    msg_key: int = Field(ge=0)
    timestamp: int = Field(ge=0)
    msg_status: int = 0


class Session(BaseModel):
    talker_id: int = Field(gt=0)
    session_type: int
    session_ts: int = Field(ge=0)
    max_seqno: int = Field(ge=0)
    system_msg_type: int = 0


class SessionPage(BaseModel):
    session_list: list[Session] | None = None
    has_more: int = Field(ge=0, le=1)


class MessagePage(BaseModel):
    messages: list[Message] | None = None
    has_more: int = Field(ge=0, le=1)
    min_seqno: int = Field(ge=0)
    max_seqno: int = Field(ge=0)


class SendReceipt(BaseModel):
    msg_key: int = Field(ge=0)


class LoginQRCode(BaseModel):
    url: str = Field(repr=False)
    qrcode_key: SecretStr


class LoginStatus(BaseModel):
    code: int
    refresh_token: SecretStr | None = None


class CookieInfo(BaseModel):
    refresh: bool
    timestamp: int = Field(gt=0)


class CookieRefresh(BaseModel):
    status: int
    refresh_token: SecretStr


class DeviceCookies(BaseModel):
    buvid3: str = Field(alias="b_3", min_length=1, repr=False)
    buvid4: str = Field(default="", alias="b_4", repr=False)


class NotificationCursor(BaseModel):
    is_end: bool
    id: int = Field(ge=0)
    time: int = Field(ge=0)


class ReplyNotification(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(gt=0)
    reply_time: int = Field(ge=0)
    user: dict[str, JsonValue] | None = Field(default=None, repr=False)
    item: dict[str, JsonValue] | None = Field(default=None, repr=False)

    @property
    def timestamp(self) -> int:
        return self.reply_time

    @property
    def key(self) -> str:
        return str(self.id)


class ReplyNotificationPage(BaseModel):
    cursor: NotificationCursor
    items: list[ReplyNotification] | None = None


class MentionNotification(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(gt=0)
    at_time: int = Field(ge=0)
    user: dict[str, JsonValue] | None = Field(default=None, repr=False)
    item: dict[str, JsonValue] | None = Field(default=None, repr=False)

    @property
    def timestamp(self) -> int:
        return self.at_time

    @property
    def key(self) -> str:
        return str(self.id)


class MentionNotificationPage(BaseModel):
    cursor: NotificationCursor
    items: list[MentionNotification] | None = None


class NotificationUser(BaseModel):
    model_config = ConfigDict(extra="allow")

    mid: int = Field(gt=0)
    nickname: str
    avatar: str = ""


NotificationCategory = Literal["reply", "like", "at"]


class LikeNotification(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: int = Field(gt=0)
    like_time: int = Field(ge=0)
    counts: int = Field(ge=0)
    users: list[NotificationUser]
    item: dict[str, JsonValue] = Field(repr=False)

    @property
    def timestamp(self) -> int:
        return self.like_time

    @property
    def key(self) -> str:
        users = ",".join(
            str(user.mid) for user in sorted(self.users, key=lambda user: user.mid)
        )
        return f"{self.id}:{self.like_time}:{self.counts}:{users}"


class LikeSection(BaseModel):
    items: list[LikeNotification] | None = None


class LikeUpdate(BaseModel):
    notification: LikeNotification
    users: list[NotificationUser]
    new_count: int | None = Field(ge=0)
    reported_users: list[NotificationUser]

    @property
    def id(self) -> int:
        return self.notification.id

    @property
    def key(self) -> str:
        return self.notification.key


class LikeHistory(LikeSection):
    cursor: NotificationCursor


class LikeNotificationPage(BaseModel):
    latest: LikeSection | None = None
    total: LikeHistory


class LikedTarget(BaseModel):
    item_id: int = Field(ge=0)
    pid: int = Field(default=0, ge=0)
    type: str
    business_id: int
    title: str = ""
    uri: str = ""


class ReplyTarget(BaseModel):
    subject_id: int = Field(ge=0)
    root_id: int = Field(default=0, ge=0)
    source_id: int = Field(default=0, ge=0)
    target_id: int = Field(default=0, ge=0)
    business_id: int
    type: str
    source_content: str = ""
    root_reply_content: str = ""
    target_reply_content: str = ""
    uri: str = ""


class MentionTarget(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    business_id: int
    subject_id: int = Field(default=0, ge=0)
    item_id: int = Field(default=0, ge=0)
    root_id: int = Field(default=0, ge=0)
    source_id: int = Field(default=0, ge=0)
    target_id: int = Field(default=0, ge=0)
    title: str = ""
    source_content: str = ""
    uri: str = ""


class ApiResponse(BaseModel):
    code: int
    data: JsonValue = None


class WbiImages(BaseModel):
    img_url: str
    sub_url: str


class WbiConfig(BaseModel):
    wbi_img: WbiImages


class User(BaseModel):
    mid: int = Field(gt=0)
    name: str = Field(validation_alias=AliasChoices("name", "uname"))
    face: str = ""


class UserCard(BaseModel):
    card: User


class FansPage(BaseModel):
    users: list[User] | None = Field(default=None, alias="list")
    total: int = Field(ge=0)


class FavoriteFolder(BaseModel):
    id: int = Field(gt=0)
    title: str
    media_count: int = Field(default=0, ge=0)


class FavoriteFolders(BaseModel):
    folders: list[FavoriteFolder] | None = Field(default=None, alias="list")


class FavoriteStatus(BaseModel):
    favoured: bool


class UserRelation(BaseModel):
    mid: int = Field(gt=0)
    attribute: int = Field(ge=0)
    mtime: int = Field(default=0, ge=0)

    @property
    def following(self) -> bool:
        return self.attribute in (2, 6)

    @property
    def mutual(self) -> bool:
        return self.attribute == 6

    @property
    def blocked(self) -> bool:
        return self.attribute == 128


class Video(BaseModel):
    bvid: str
    title: str


class FavoritePage(BaseModel):
    medias: list[Video] | None = None
    has_more: bool


class VideoList(BaseModel):
    vlist: list[Video]


class UpVideoPage(BaseModel):
    videos: VideoList = Field(alias="list")


class VideoPart(BaseModel):
    cid: int = Field(gt=0)
    page: int = Field(gt=0)
    part: str


class VideoDetails(BaseModel):
    aid: int | None = Field(default=None, gt=0)
    bvid: str
    title: str
    cid: int = Field(gt=0)
    pages: list[VideoPart]


class PlayPart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    url: str = Field(repr=False)
    size: int = Field(ge=0)
    length: int = Field(ge=0)
    backup_urls: list[str] = Field(
        default_factory=list, validation_alias="backup_url", repr=False
    )

    @field_validator("backup_urls", mode="before")
    @classmethod
    def empty_backups(cls, value: JsonValue) -> JsonValue:
        return [] if value is None else value


class DashSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    initialization: str = Field(
        validation_alias=AliasChoices("Initialization", "initialization")
    )
    index_range: str = Field(validation_alias=AliasChoices("indexRange", "index_range"))


class DashStream(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int = Field(gt=0)
    base_url: str = Field(
        min_length=1, validation_alias=AliasChoices("baseUrl", "base_url"), repr=False
    )
    backup_urls: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("backupUrl", "backup_url"),
        repr=False,
    )
    bandwidth: int = Field(ge=0)
    mime_type: str = Field(validation_alias=AliasChoices("mimeType", "mime_type"))
    codecs: str
    codecid: int = Field(default=0, ge=0)
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    frame_rate: str = Field(
        default="", validation_alias=AliasChoices("frameRate", "frame_rate")
    )
    segment_base: DashSegment | None = Field(
        default=None, validation_alias=AliasChoices("SegmentBase", "segment_base")
    )

    @field_validator("backup_urls", mode="before")
    @classmethod
    def empty_backups(cls, value: JsonValue) -> JsonValue:
        return [] if value is None else value

    @field_validator("segment_base", mode="before")
    @classmethod
    def empty_segment(cls, value: JsonValue) -> JsonValue:
        return None if value == "" else value


class DashStreams(BaseModel):
    duration: float = Field(ge=0, allow_inf_nan=False)
    video: list[DashStream] = Field(default_factory=list)
    audio: list[DashStream] = Field(default_factory=list)

    @field_validator("video", "audio", mode="before")
    @classmethod
    def empty_tracks(cls, value: JsonValue) -> JsonValue:
        return [] if value is None else value


class PlayUrls(BaseModel):
    quality: int
    durl: list[PlayPart] = Field(default_factory=list)
    dash: DashStreams | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timelength: int = Field(default=0, ge=0)
    accept_quality: list[int] = Field(default_factory=list)

    @field_validator("durl", mode="before")
    @classmethod
    def empty_parts(cls, value: JsonValue) -> JsonValue:
        return [] if value is None else value

    @model_validator(mode="after")
    def validate_streams(self) -> PlayUrls:
        if not self.durl and (
            self.dash is None or not (self.dash.video or self.dash.audio)
        ):
            raise ValueError("Playback response has no media streams")
        return self


class SubtitleTrack(BaseModel):
    id: int = Field(gt=0)
    lan: str
    lan_doc: str
    subtitle_url: str = Field(repr=False)
    ai_type: int = 0
    ai_status: int = 0

    @field_validator("subtitle_url")
    @classmethod
    def normalize_url(cls, value: str) -> str:
        url = "https:" + value if value.startswith("//") else value
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("Invalid subtitle URL")
        return parsed._replace(scheme="https").geturl()


class SubtitleList(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bvid: str
    cid: int = Field(gt=0)
    tracks: list[SubtitleTrack] = Field(
        default_factory=list, validation_alias=AliasPath("subtitle", "subtitles")
    )
    need_login: bool = Field(default=False, validation_alias="need_login_subtitle")

    @model_validator(mode="before")
    @classmethod
    def validate_metadata(cls, value: JsonValue) -> JsonValue:
        if isinstance(value, dict):
            subtitle = value.get("subtitle")
            if subtitle is not None and not isinstance(subtitle, dict):
                raise ValueError("Invalid subtitle metadata")
        return value

    @field_validator("tracks", mode="before")
    @classmethod
    def empty_tracks(cls, value: JsonValue) -> JsonValue:
        return [] if value is None else value


class SubtitleSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    start: float = Field(alias="from", ge=0, allow_inf_nan=False)
    end: float = Field(alias="to", ge=0, allow_inf_nan=False)
    content: str

    @model_validator(mode="after")
    def validate_times(self) -> SubtitleSegment:
        if self.end < self.start:
            raise ValueError("Subtitle segment ends before it starts")
        return self


class Subtitle(BaseModel):
    body: list[SubtitleSegment]

    @property
    def text(self) -> str:
        return "\n".join(segment.content for segment in self.body)


class CommentRef(BaseModel):
    oid: int = Field(gt=0)
    kind: int = Field(gt=0)


class CommentContent(BaseModel):
    message: str = Field(repr=False)


class Comment(BaseModel):
    rpid: int = Field(gt=0)
    oid: int = Field(gt=0)
    kind: int = Field(alias="type", gt=0)
    root: int = Field(default=0, ge=0)
    parent: int = Field(default=0, ge=0)
    ctime: int = Field(ge=0)
    like: int = Field(default=0, ge=0)
    member: User
    content: CommentContent
    replies: list[Comment] | None = None

    @property
    def ref(self) -> CommentRef:
        return CommentRef(oid=self.oid, kind=self.kind)


class CommentOffset(BaseModel):
    next_offset: str


class CommentCursor(BaseModel):
    is_end: bool
    pagination_reply: CommentOffset | None = None


class CommentPage(BaseModel):
    cursor: CommentCursor
    replies: list[Comment] | None = None
    hots: list[Comment] | None = None

    @property
    def next_offset(self) -> str | None:
        if self.cursor.is_end or self.cursor.pagination_reply is None:
            return None
        return self.cursor.pagination_reply.next_offset


class ReplyPagination(BaseModel):
    num: int = Field(gt=0)
    size: int = Field(gt=0)
    count: int = Field(ge=0)


class CommentReplies(BaseModel):
    page: ReplyPagination
    root: Comment | None = None
    replies: list[Comment] | None = None


class CommentReceipt(BaseModel):
    rpid: int = Field(gt=0)
    root: int = Field(ge=0)
    parent: int = Field(ge=0)


SearchKind = Literal[
    "video",
    "bili_user",
    "article",
    "live",
    "live_room",
    "live_user",
    "media_bangumi",
    "media_ft",
    "topic",
    "photo",
]
SearchOrder = Literal[
    "totalrank",
    "click",
    "pubdate",
    "dm",
    "stow",
    "scores",
    "attention",
    "0",
    "fans",
    "level",
    "online",
    "live_time",
]

_ORDERS: dict[str, tuple[str, ...]] = {
    "video": ("totalrank", "click", "pubdate", "dm", "stow", "scores"),
    "article": ("totalrank", "click", "pubdate", "dm", "stow", "scores", "attention"),
    "photo": ("totalrank", "click", "pubdate", "dm", "stow", "scores"),
    "bili_user": ("0", "fans", "level"),
    "live": ("online", "live_time"),
    "live_room": ("online", "live_time"),
}


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    keyword: str = Field(min_length=1)
    kind: SearchKind = "video"
    page: int = Field(default=1, ge=1)
    order: SearchOrder | None = None
    duration: Literal[0, 1, 2, 3, 4] = 0
    tids: int = Field(default=0, ge=0)
    user_type: Literal[0, 1, 2, 3] = 0
    order_sort: Literal[0, 1] = 0
    category_id: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_filters(self) -> SearchQuery:
        if self.order is not None and self.order not in _ORDERS.get(self.kind, ()):
            raise ValueError(f"Unsupported sort order for {self.kind}")
        if self.kind != "video" and (self.duration or self.tids):
            raise ValueError("Duration and region filters require video search")
        if self.kind != "bili_user" and (self.user_type or self.order_sort):
            raise ValueError("User filters require user search")
        if self.order_sort and self.order not in ("fans", "level"):
            raise ValueError("Sort direction requires fans or level order")
        if self.kind not in ("article", "photo") and self.category_id:
            raise ValueError("Category filter requires article or photo search")
        return self


class SearchItem(BaseModel):
    model_config = ConfigDict(extra="allow")

    @field_validator("title", "uname", check_fields=False)
    @classmethod
    def clean_highlights(cls, value: str) -> str:
        return unescape(re.sub(r"</?em(?:\s[^>]*)?>", "", value, flags=re.IGNORECASE))


class SearchVideo(SearchItem):
    type: Literal["video"]
    aid: int = Field(gt=0)
    bvid: str
    title: str
    mid: int = Field(gt=0)
    author: str
    description: str = ""
    duration: str = ""
    pic: str = ""


class SearchUser(SearchItem):
    type: Literal["bili_user"]
    mid: int = Field(gt=0)
    uname: str
    usign: str = ""
    upic: str = ""
    fans: int = Field(default=0, ge=0)


class SearchArticle(SearchItem):
    type: Literal["article"]
    id: int = Field(gt=0)
    title: str
    mid: int = Field(gt=0)


class SearchMedia(SearchItem):
    type: Literal["media_bangumi", "media_ft"]
    media_id: int = Field(gt=0)
    season_id: int = Field(gt=0)
    title: str


class SearchLiveRoom(SearchItem):
    type: Literal["live_room"]
    uid: int = Field(gt=0)
    roomid: int = Field(gt=0)
    uname: str
    title: str
    online: int = Field(default=0, ge=0)


class SearchLiveUser(SearchItem):
    type: Literal["live_user"]
    uid: int = Field(gt=0)
    roomid: int = Field(ge=0)
    uname: str


class SearchTopic(SearchItem):
    type: Literal["topic"]
    id: int = Field(gt=0)
    title: str


class SearchPhoto(SearchItem):
    type: Literal["photo"]
    id: int = Field(gt=0)
    title: str
    mid: int = Field(gt=0)
    uname: str


SearchResult = Annotated[
    SearchVideo
    | SearchUser
    | SearchArticle
    | SearchMedia
    | SearchLiveRoom
    | SearchLiveUser
    | SearchTopic
    | SearchPhoto,
    Field(discriminator="type"),
]


class SearchPagination(BaseModel):
    page: int = Field(ge=1)
    page_size: int = Field(
        default=20, ge=0, validation_alias=AliasChoices("pagesize", "page_size")
    )
    total: int = Field(ge=0, validation_alias="numResults")
    pages: int = Field(ge=0, validation_alias="numPages")


class SearchPage(SearchPagination):
    items: list[SearchResult] = Field(default_factory=list, validation_alias="result")

    @field_validator("items", mode="before")
    @classmethod
    def empty_results(cls, value: JsonValue) -> JsonValue:
        return [] if value is None else value


class LiveSearchResult(BaseModel):
    live_room: list[SearchLiveRoom] = Field(default_factory=list)
    live_user: list[SearchLiveUser] = Field(default_factory=list)


class LiveSearchPage(SearchPagination):
    items: LiveSearchResult = Field(validation_alias="result")


class LiveRoom(BaseModel):
    room_id: int = Field(gt=0)
    short_id: int = Field(default=0, ge=0)
    uid: int = Field(gt=0)
    live_status: int = Field(ge=0)


class LiveHost(BaseModel):
    host: str
    wss_port: int = Field(gt=0, le=65535)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value.endswith(".chat.bilibili.com") or any(
            character in value for character in "/:@?#"
        ):
            raise ValueError("Invalid live server host")
        return value


class LiveConnection(BaseModel):
    token: SecretStr = Field(min_length=1)
    host_list: list[LiveHost] = Field(min_length=1)


@dataclass(frozen=True)
class LivePacket:
    operation: int
    version: int
    body: bytes = field(repr=False)


class LiveDanmaku(BaseModel):
    uid: int = Field(ge=0, validation_alias=AliasPath("info", 2, 0))
    uname: str = Field(validation_alias=AliasPath("info", 2, 1))
    text: str = Field(validation_alias=AliasPath("info", 1))
    timestamp: int = Field(ge=0, validation_alias=AliasPath("info", 0, 4))


class LiveGift(BaseModel):
    uid: int = Field(ge=0)
    uname: str
    gift_id: int = Field(gt=0, validation_alias="giftId")
    gift_name: str = Field(validation_alias="giftName")
    num: int = Field(gt=0)
    timestamp: int = Field(default=0, ge=0)
    coin_type: str = ""
    total_coin: int = Field(default=0, ge=0)


class LiveInteraction(BaseModel):
    uid: int = Field(ge=0)
    uname: str
    msg_type: int = Field(ge=0)
    timestamp: int = Field(default=0, ge=0)
    roomid: int = Field(default=0, ge=0)

    @property
    def kind(self) -> str:
        return {1: "enter", 2: "follow", 3: "share"}.get(self.msg_type, "unknown")


class LiveSendReceipt(BaseModel):
    mode_info: dict[str, JsonValue]
