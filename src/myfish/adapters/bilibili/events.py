from dataclasses import dataclass, field
from typing import ClassVar

from pydantic import JsonValue, ValidationError

from myfish.core.event import BaseEvent
from myfish.core.logger import logger

from .model import (
    LikedTarget,
    LikeNotification,
    LikeUpdate,
    LiveDanmaku,
    LiveGift,
    LiveGuardBuy,
    LiveInteraction,
    LiveSuperChat,
    MentionNotification,
    MentionTarget,
    NotificationUser,
    ReplyNotification,
    ReplyTarget,
)


@dataclass
class BilibiliNotificationEvent(BaseEvent):
    event_type: ClassVar[str] = "bilibili.notification"

    account_id: str
    notification_id: str
    timestamp_ms: int
    raw_payload: dict[str, JsonValue] = field(repr=False)


@dataclass
class BilibiliCommentEvent(BilibiliNotificationEvent):
    event_type: ClassVar[str] = "bilibili.comment"

    user: NotificationUser
    target: ReplyTarget


@dataclass
class BilibiliReplyEvent(BilibiliCommentEvent):
    event_type: ClassVar[str] = "bilibili.reply"


@dataclass
class BilibiliMentionEvent(BilibiliNotificationEvent):
    event_type: ClassVar[str] = "bilibili.mention"

    user: NotificationUser
    target: MentionTarget


@dataclass
class BilibiliLikedEvent(BilibiliNotificationEvent):
    event_type: ClassVar[str] = "bilibili.liked"

    users: list[NotificationUser]
    target: LikedTarget
    count: int
    new_count: int | None
    reported_users: list[NotificationUser]

    @property
    def user(self) -> NotificationUser | None:
        return self.users[0] if len(self.users) == 1 else None


@dataclass
class BilibiliLiveEvent(BaseEvent):
    event_type: ClassVar[str] = "bilibili.live"

    account_id: str
    room_id: int
    command: str
    timestamp_ms: int
    raw_payload: dict[str, JsonValue] = field(repr=False)


@dataclass
class BilibiliDanmakuEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.danmaku"

    danmaku: LiveDanmaku


@dataclass
class BilibiliGiftEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.gift"

    gift: LiveGift


@dataclass
class BilibiliSuperChatEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.super_chat"

    super_chat: LiveSuperChat


@dataclass
class BilibiliSuperChatDeleteEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.super_chat_delete"

    ids: list[int]


@dataclass
class BilibiliGuardBuyEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.guard_buy"

    purchase: LiveGuardBuy


@dataclass
class BilibiliLiveInteractionEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.interaction"

    interaction: LiveInteraction


@dataclass
class BilibiliLiveStatusEvent(BilibiliLiveEvent):
    event_type: ClassVar[str] = "bilibili.live.status"

    live: bool


def notification_event(
    account_id: str,
    notification: ReplyNotification
    | MentionNotification
    | LikeNotification
    | LikeUpdate,
) -> BilibiliNotificationEvent:
    update = notification if isinstance(notification, LikeUpdate) else None
    if isinstance(notification, LikeUpdate):
        notification = notification.notification
    common = (
        account_id,
        str(notification.id),
        notification.timestamp * 1000,
        notification.model_dump(mode="json"),
    )
    try:
        if isinstance(notification, LikeNotification):
            return BilibiliLikedEvent(
                *common,
                users=update.users if update is not None else notification.users,
                target=LikedTarget.model_validate(notification.item),
                count=notification.counts,
                new_count=update.new_count
                if update is not None
                else notification.counts,
                reported_users=update.reported_users
                if update is not None
                else notification.users,
            )
        user = NotificationUser.model_validate(notification.user)
        if isinstance(notification, MentionNotification):
            return BilibiliMentionEvent(
                *common,
                user=user,
                target=MentionTarget.model_validate(notification.item),
            )
        target = ReplyTarget.model_validate(notification.item)
        if target.type == "reply":
            return BilibiliReplyEvent(*common, user=user, target=target)
        if target.type == "video" and target.business_id == 1:
            return BilibiliCommentEvent(*common, user=user, target=target)
    except ValidationError:
        pass
    logger.debug(
        "[Bilibili] 无法解析通知: fields={}, item_fields={}",
        sorted(common[3]),
        sorted(notification.item or {}),
    )
    return BilibiliNotificationEvent(*common)
