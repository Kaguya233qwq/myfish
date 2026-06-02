from myfish.core.event import MessageEvent
from myfish.core.message import Image
from myfish.core.plugin import Plugin, PluginMetadata

# 示例插件：回声插件
plugin = Plugin(
    metadata=PluginMetadata(
        id="echo",
        name="回声插件",
        description="一个简单的回声插件，用于演示自动回复功能",
        version="1.0.1",
        author="Kaguya233qwq",
        support_adapters=None,  # 支持所有适配器
    )
)


def text_rule(event: MessageEvent) -> bool:
    """判断消息是否包含纯文本"""
    return bool(event.plain_text.strip())


def image_rule(event: MessageEvent) -> bool:
    """判断消息是否包含图片"""
    return event.get_segments(Image) != []


# 编写业务逻辑并注册handler
@plugin.on_message(text_rule)
async def handle_echo(event: MessageEvent):
    """当收到纯文本消息时，重复发送相同的文本作为回复"""

    assert event.messages[0].type == "text"
    content = event.plain_text.strip()
    reply_message = f"[echo] 你发送的消息是: {content}"
    await event.reply(reply_message)


@plugin.on_message(image_rule)
async def handle_image(event: MessageEvent):
    """当收到图片消息时，回复固定的文本"""
    images = event.get_segments(Image)
    if images:
        await event.reply(f"你发送了 {len(images)} 张图片！")
