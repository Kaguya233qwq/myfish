import json
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)


class NoteRef(BaseModel):
    note_id: str
    xsec_token: str = Field(min_length=1, repr=False)
    xsec_source: Literal["pc_feed", "pc_search", "pc_user", "app_share"] = "pc_feed"

    @field_validator("note_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value or not value.isascii() or not value.isalnum():
            raise ValueError("Note id must be an ASCII alphanumeric identifier")
        return value

    @classmethod
    def from_url(cls, url: str) -> "NoteRef":
        parsed = urlsplit(url)
        if parsed.scheme == "xhsdiscover" and parsed.hostname == "item":
            parts = ["explore", parsed.path.strip("/")]
        elif parsed.scheme == "https" and parsed.hostname in {
            "www.xiaohongshu.com",
            "xiaohongshu.com",
        }:
            parts = parsed.path.strip("/").split("/")
        else:
            raise ValueError("Expected a Xiaohongshu note URL")
        if not (
            (len(parts) == 2 and parts[0] == "explore")
            or (len(parts) == 3 and parts[:2] == ["discovery", "item"])
        ):
            raise ValueError("Expected /explore/<note_id> note path")
        query = parse_qs(parsed.query)
        return cls.model_validate(
            {
                "note_id": parts[-1],
                "xsec_token": query.get("xsec_token", [""])[0],
                "xsec_source": query.get("xsec_source", ["pc_feed"])[0],
            }
        )


class NoteUser(BaseModel):
    user_id: str = Field(validation_alias=AliasChoices("user_id", "userid", "id"))
    nickname: str = Field(
        default="", validation_alias=AliasChoices("nickname", "nick_name")
    )
    avatar: str = Field(
        default="", validation_alias=AliasChoices("avatar", "image", "images")
    )


class NoteImage(BaseModel):
    url: str = ""
    url_default: str = ""
    width: int = 0
    height: int = 0


class NoteInteraction(BaseModel):
    liked: bool | None = None
    collected: bool | None = None
    liked_count: str | int = "0"
    collected_count: str | int = "0"
    comment_count: str | int = "0"
    share_count: str | int = "0"


class NoteCard(BaseModel):
    note_id: str = ""
    type: str
    title: str = Field(
        default="", validation_alias=AliasChoices("title", "display_title")
    )
    desc: str = ""
    user: NoteUser
    interact_info: NoteInteraction = Field(default_factory=NoteInteraction)
    cover: NoteImage | None = None
    image_list: list[NoteImage] = Field(default_factory=list)
    video: JsonValue = None
    time: int | None = None
    last_update_time: int | None = None


class UserNote(NoteCard):
    note_id: str = Field(min_length=1)
    xsec_token: str = Field(default="", repr=False)

    @property
    def ref(self) -> NoteRef:
        return NoteRef(
            note_id=self.note_id,
            xsec_token=self.xsec_token,
            xsec_source="pc_user",
        )


class UserNotePage(BaseModel):
    notes: list[UserNote] = Field(default_factory=list)
    cursor: str = ""
    has_more: bool


class FeedItem(BaseModel):
    id: str
    model_type: str
    ignore: bool = False
    xsec_token: str = Field(default="", repr=False)
    note_card: NoteCard | None = None

    @property
    def ref(self) -> NoteRef:
        if self.model_type != "note" or self.note_card is None:
            raise ValueError("Feed item is not a note")
        return NoteRef(note_id=self.id, xsec_token=self.xsec_token)


class FeedPage(BaseModel):
    cursor_score: str
    items: list[FeedItem] = Field(default_factory=list)


class Note(BaseModel):
    ref: NoteRef
    card: NoteCard


class CommentTarget(BaseModel):
    id: str
    user_info: NoteUser


class Comment(BaseModel):
    id: str
    note_id: str
    content: str = Field(repr=False)
    user_info: NoteUser
    create_time: int
    like_count: str | int = "0"
    liked: bool = False
    invalid: bool = False
    status: int = 0
    target_comment: CommentTarget | None = None
    sub_comments: list["Comment"] = Field(default_factory=list)
    sub_comment_count: str | int = "0"
    sub_comment_has_more: bool = False
    sub_comment_cursor: str = ""


class CommentPage(BaseModel):
    comments: list[Comment] = Field(default_factory=list)
    cursor: str = ""
    has_more: bool


class CommentCreated(BaseModel):
    comment: Comment


class NoteAction(BaseModel):
    note_id: str
    action: Literal["like", "collect"]
    enabled: bool


class SharedNote(BaseModel):
    id: str
    type: Literal["note"]
    title: str
    link: str = Field(repr=False)
    image: str = ""
    cover: str = ""
    desc: str = ""
    user: NoteUser
    note_type: str = Field(
        default="", validation_alias=AliasChoices("noteType", "note_type")
    )

    @property
    def ref(self) -> NoteRef:
        ref = NoteRef.from_url(self.link)
        if ref.note_id != self.id:
            raise ValueError("Shared note id does not match its link")
        return ref

    @classmethod
    def from_payload(cls, envelope: dict[str, JsonValue]) -> "SharedNote | None":
        if envelope.get("content_type") != 3:
            return None
        nested = envelope.get("content")
        if not isinstance(nested, str):
            raise TypeError("Shared card content must be a JSON string")
        try:
            payload = json.loads(nested)
            if not isinstance(payload, dict) or payload.get("type") != "note":
                return None
            return cls.model_validate(payload)
        except (ValueError, ValidationError):
            raise ValueError("Invalid shared note card") from None
