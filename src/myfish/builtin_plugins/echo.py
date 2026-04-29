from myfish.core.event import MessageEvent
from myfish.core.plugin import Plugin, PluginMetadata

# 示例插件：回声插件
plugin = Plugin(
    metadata=PluginMetadata(
        id="echo",
        name="回声插件",
        description="一个简单的回声插件，用于演示自动回复功能",
        version="1.0.0",
        author="Kaguya233qwq",
    )
)


# 编写业务逻辑并注册handler
@plugin.on_fullmatch(("/echo",))
async def handle_hello(event: MessageEvent):
    """处理打招呼"""
    content = event.plain_text.strip()
    reply_message = f"Echo: {content}"
    await event.reply(reply_message)
