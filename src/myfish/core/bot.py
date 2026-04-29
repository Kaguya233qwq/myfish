import asyncio
import inspect
from typing import Callable, Generic

from .logger import logger

from .event import MessageEvent
from .loader import PluginManager

from .adapter import AdapterT
from .message import MessageSegment, MessageChain


class Bot(Generic[AdapterT]):
    """通用的 Bot 实例引擎"""

    def __init__(self, adapter: AdapterT):
        self.adapter: AdapterT = adapter

        self._background_tasks: set[asyncio.Task] = set()
        self.adapter.set_callback(self._dispatch_event)
        self.plugin_manager = PluginManager()

    def load_all_plugins(self):
        self.plugin_manager.load_all_plugins()

    async def send_msg(
        self, target_id: str, message: MessageSegment | MessageChain, cid: str = ""
    ):
        """调用抽象层的 send"""
        await self.adapter.send(target_id, message, cid)

    async def _dispatch_event(self, event: MessageEvent):
        """分发器：将数据封装成 Context，投递给所有命中的插件路由"""

        async def _do_reply(msg: MessageSegment | MessageChain):
            try:
                await self.send_msg(
                    target_id=event.sender_id, message=msg, cid=event.cid
                )
            except Exception as e:
                logger.error(f"发送回复消息失败: {e}")

        event._callback_func = _do_reply

        async def _safe_execute(
            plugin_name: str, handler_func: Callable, bot_instance, event: MessageEvent
        ):
            try:
                sig = inspect.signature(handler_func)
                kwargs = {}

                for name, param in sig.parameters.items():
                    if name == "event" or param.annotation == MessageEvent:
                        kwargs[name] = event
                    elif name == "bot" or "Bot" in str(param.annotation):
                        kwargs[name] = bot_instance

                await handler_func(**kwargs)

            except Exception as exc:
                logger.exception(
                    f"❌ 插件 [{plugin_name}] 在后台执行业务逻辑时崩溃: {exc}"
                )

        # 遍历所有已加载的插件
        for plugin_name, plugin in self.plugin_manager.plugins.items():
            for handler_data in plugin.message_handlers:
                func = handler_data["func"]
                rule = handler_data["rule"]

                try:
                    if rule is not None and not rule(event):
                        continue
                except Exception as e:
                    logger.warning(
                        f"警告：插件 [{plugin_name}] 注册handler失败，已跳过: {e}"
                    )
                    continue
                task = asyncio.create_task(
                    _safe_execute(plugin_name, func, self, event)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def run(self):
        """启动 Bot"""
        self.load_all_plugins()
        logger.success(
            f"Bot 启动完毕，共加载 {len(self.plugin_manager.plugins)} 个插件"
        )
        logger.info("正在启动通信适配器...")
        await self.adapter.run()
