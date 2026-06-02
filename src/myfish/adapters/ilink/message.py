from typing import Any, ClassVar, Type, TYPE_CHECKING
from pydantic import BaseModel, Field

from myfish.core.logger import logger
from myfish.core.message import (
    Audio,
    File,
    Image,
    MessageChain,
    MessageSegment,
    Text,
    Video,
)

if TYPE_CHECKING:
    from .api import ILinkAPI


class ILinkPayloadNode(BaseModel):
    """
    iLink 协议节点基类
    """

    _registry: ClassVar[dict[int, Type["ILinkPayloadNode"]]] = {}
    item_type: ClassVar[int] = 0

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.item_type != 0:
            cls._registry[cls.item_type] = cls

    @classmethod
    def decode(cls, item_data: dict[str, Any]) -> list[MessageSegment]:
        """将 iLink 的 item 字典反序列化为标准 MessageSegment 对象 (接收时为同步)"""
        raise NotImplementedError

    @classmethod
    async def encode(cls, msg: Any, api: "ILinkAPI", target_id: str) -> dict[str, Any]:
        """
        将 MessageSegment 序列化为 iLink item 格式的字典 (发送时强制异步)
        :param api: 透传的胖 API 实例，用于处理 CDN 上传等网络操作
        :param target_id: 接收方 ID，供申请 CDN 凭证时使用
        """
        raise NotImplementedError


class ILinkTextNode(ILinkPayloadNode):
    item_type = 1

    @classmethod
    def decode(cls, item_data: dict) -> list[MessageSegment]:
        text_val = item_data.get("text_item", {}).get("text", "")
        return [Text(text=text_val)] if text_val else []

    @classmethod
    async def encode(cls, msg: Text, api: "ILinkAPI", target_id: str) -> dict:
        return {"type": 1, "text_item": {"text": msg.text}}


class ILinkImageNode(ILinkPayloadNode):
    item_type = 2

    @classmethod
    def decode(cls, item_data: dict) -> list[MessageSegment]:
        pic_info = item_data.get("image_item", {})
        media_info = pic_info.get("media", {})

        url = (
            media_info.get("full_url", "")
            or pic_info.get("url", "")
            or pic_info.get("cdn_url", "")
        )

        aes_key = media_info.get("aes_key", "") or pic_info.get("aes_key", "")

        if not url:
            return []

        return [Image(image_url=url, extra={"aes_key": aes_key})]

    @classmethod
    async def encode(cls, msg: Image, api: "ILinkAPI", target_id: str) -> dict:
        media_info = await api.upload_media(
            msg.image_url, to_user_id=target_id, media_type=2
        )
        return {
            "type": 2,
            "image_item": {
                "cdn_url": media_info["cdn_url"],
                "aes_key": media_info["aes_key"],
                "cdn_param": media_info.get("cdn_param", ""),
            },
        }


class ILinkAudioNode(ILinkPayloadNode):
    item_type = 3

    @classmethod
    def decode(cls, item_data: dict) -> list[MessageSegment]:
        audio_info = item_data.get("audio_item", {})
        aes_key = audio_info.get("aes_key", "")
        return [
            Audio(
                audio_url=audio_info.get("url", "") or audio_info.get("cdn_url", ""),
                duration_ms=audio_info.get("duration", 0),
                extra={"aes_key": aes_key},
            )
        ]

    @classmethod
    async def encode(cls, msg: Audio, api: "ILinkAPI", target_id: str) -> dict:
        media_info = await api.upload_media(
            msg.audio_url, to_user_id=target_id, media_type=3
        )
        return {
            "type": 3,
            "audio_item": {
                "cdn_url": media_info["cdn_url"],
                "aes_key": media_info["aes_key"],
                "cdn_param": media_info.get("cdn_param", ""),
                "duration": msg.duration_ms,
            },
        }


class ILinkMessage(BaseModel):
    """iLink 单条消息的root模型"""

    from_user_id: str = Field(default="")
    to_user_id: str = Field(default="")
    message_type: int = Field(default=0)
    message_state: int = Field(default=0)
    context_token: str = Field(default="")
    item_list: list[dict[str, Any]] = Field(default_factory=list)
    create_time_ms: int = Field(default=0)

    def to_message_chain(self) -> MessageChain:
        chain = MessageChain()
        for item in self.item_list:
            item_type = item.get("type", 0)
            node_class = ILinkPayloadNode._registry.get(item_type)
            if node_class:
                try:
                    chain.extend(node_class.decode(item))
                except Exception as e:
                    logger.error(f"解析 iLink 消息节点失败 (type={item_type}): {e}")
        return chain


class ILinkVideoNode(ILinkPayloadNode):
    item_type = 5

    @classmethod
    def decode(cls, item_data: dict) -> list[MessageSegment]:
        """接收视频时的反序列化"""
        video_info = item_data.get("video_item", {})

        media_info = video_info.get("media", {})
        thumb_media_info = video_info.get("thumb_media", {})

        video_url = (
            media_info.get("full_url", "")
            or video_info.get("url", "")
            or video_info.get("cdn_url", "")
        )
        video_aes_key = media_info.get("aes_key", "") or video_info.get("aes_key", "")

        thumb_url = (
            thumb_media_info.get("full_url", "")
            or video_info.get("thumb_url", "")
            or video_info.get("thumb_cdn_url", "")
        )
        thumb_aes_key = thumb_media_info.get("aes_key", "") or video_info.get(
            "thumb_aes_key", ""
        )

        if not video_url:
            return []

        return [
            Video(
                video_url=video_url,
                thumb_url=thumb_url,
                extra={
                    "aes_key": video_aes_key,
                    "thumb_aes_key": thumb_aes_key,
                    "duration": video_info.get("play_length", 0),
                    "file_size": video_info.get("video_size", 0),
                },
            )
        ]

    @classmethod
    async def encode(cls, msg: Video, api: "ILinkAPI", target_id: str) -> dict:
        """发送视频时的序列化与异步上传"""
        media_info = await api.upload_media(
            msg.video_url, to_user_id=target_id, media_type=5
        )

        video_item = {
            "cdn_url": media_info["cdn_url"],
            "aes_key": media_info["aes_key"],
            "cdn_param": media_info.get("cdn_param", ""),
            "duration": msg.extra.get("duration", 0) if msg.extra else 0,
        }

        if msg.thumb_url:
            try:
                thumb_info = await api.upload_media(
                    msg.thumb_url, to_user_id=target_id, media_type=2
                )
                video_item["thumb_cdn_url"] = thumb_info["cdn_url"]
                video_item["thumb_aes_key"] = thumb_info["aes_key"]
            except Exception as e:
                logger.warning(f"[ILink] 视频封面上传失败，将发送无封面视频: {e}")

        return {"type": 5, "video_item": video_item}


class ILinkFileNode(ILinkPayloadNode):
    item_type = 4

    @classmethod
    def decode(cls, item_data: dict) -> list[MessageSegment]:
        file_info = item_data.get("file_item", {})

        media_info = file_info.get("media", {})

        url = (
            media_info.get("full_url", "")
            or file_info.get("url", "")
            or file_info.get("cdn_url", "")
        )
        aes_key = media_info.get("aes_key", "") or file_info.get("aes_key", "")

        file_name = file_info.get("file_name", "未知文件")
        file_size = int(file_info.get("len", 0) or file_info.get("file_size", 0))

        if not url:
            return []

        # 在 File 结构中，我们将下载 URL 存入 file_id 字段
        return [
            File(
                file_id=url,
                file_name=file_name,
                file_size=file_size,
                extra={"aes_key": aes_key, "md5": file_info.get("md5", "")},
            )
        ]

    @classmethod
    async def encode(cls, msg: File, api: "ILinkAPI", target_id: str) -> dict:
        media_info = await api.upload_media(
            msg.file_id, to_user_id=target_id, media_type=4
        )

        return {
            "type": 4,
            "file_item": {
                "file_name": msg.file_name,
                "len": str(media_info.get("file_size", msg.file_size)),
                "md5": media_info.get("md5", ""),
                "cdn_url": media_info["cdn_url"],
                "aes_key": media_info["aes_key"],
                "cdn_param": media_info.get("cdn_param", ""),
            },
        }


class Typing(MessageSegment):
    """虚拟消息段：对方正在输入状态"""

    type: str = "typing"
    desc: str = "正在输入"
    status: int = 1


_ENCODE_MAP = {
    Text: ILinkTextNode,
    Image: ILinkImageNode,
    Audio: ILinkAudioNode,
    Video: ILinkVideoNode,
    File: ILinkFileNode,
    Typing: Typing,
}


async def auto_encode_ilink(
    message: MessageSegment | MessageChain, api: "ILinkAPI", target_id: str
) -> list[dict]:
    """
    发消息时的序列化路由
    """
    item_list = []

    if isinstance(message, MessageSegment):
        message = MessageChain([message])

    for seg in message:
        node_class = _ENCODE_MAP.get(type(seg))
        if node_class and hasattr(node_class, "encode"):
            item_data = await node_class.encode(seg, api, target_id)
            item_list.append(item_data)
        else:
            logger.warning(
                f"iLink 适配器暂不支持发送类型或未注册: {type(seg).__name__}，已跳过"
            )

    if not item_list:
        raise ValueError("消息链序列化后为空，或包含了完全不支持的类型")

    return item_list
