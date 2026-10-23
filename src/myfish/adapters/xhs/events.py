from dataclasses import dataclass
from typing import ClassVar

from pydantic import JsonValue

from myfish.core.event import BaseEvent

from .model import Notification, NotificationComment, NotificationNote
from .notes import NoteUser


@dataclass
class XhsNotificationEvent(BaseEvent):
    event_type: ClassVar[str] = "xhs.notification"
    account_id: str
    notification_id: str
    timestamp_ms: int
    user: NoteUser
    raw_payload: dict[str, JsonValue]


@dataclass
class XhsFollowEvent(XhsNotificationEvent):
    event_type: ClassVar[str] = "xhs.follow"


@dataclass
class XhsLikeEvent(XhsNotificationEvent):
    event_type: ClassVar[str] = "xhs.like"
    note: NotificationNote
    comment: NotificationComment | None = None


@dataclass
class XhsCommentEvent(XhsNotificationEvent):
    event_type: ClassVar[str] = "xhs.comment"
    note: NotificationNote
    comment: NotificationComment


def notification_event(
    account_id: str,
    notification: Notification,
    *,
    raw_payload: dict[str, JsonValue] | None = None,
) -> XhsNotificationEvent:
    common = (
        account_id,
        notification.id,
        notification.time * 1000,
        notification.user,
        raw_payload
        if raw_payload is not None
        else notification.model_dump(mode="json", by_alias=True),
    )
    if notification.type == "follow/you":
        return XhsFollowEvent(*common)
    note = notification.item_info
    if note is None or note.type != "note_info" or not note.id:
        raise ValueError("Notification note is missing or unsupported")
    if notification.type in {"liked/item", "liked/comment"}:
        return XhsLikeEvent(*common, note=note, comment=notification.comment_info)
    if (
        notification.type in {"comment/item", "comment/comment"}
        and notification.comment_info is not None
    ):
        return XhsCommentEvent(*common, note=note, comment=notification.comment_info)
    raise ValueError("Unsupported notification type or missing comment")
