import asyncio
import inspect
from typing import Any, Callable, get_origin, get_args

from .logger import logger
from .event import MessageEvent
from .loader import PluginManager
from .adapter import BaseAdapter
from .bot import Bot


class Engine:
    """全局核心引擎：管理多端适配器、插件生态和事件循环"""

    def __init__(self):
        self.adapters: list[BaseAdapter] = []
        self._background_tasks: set[asyncio.Task] = set()
        self.plugin_manager = PluginManager()

    def add_adapter(self, adapter: BaseAdapter):
        """挂载适配器，并为其动态生成专属的 Bot 上下文"""
        self.adapters.append(adapter)

        bot = Bot(adapter=adapter)
        adapter.set_callback(self._create_dispatch_callback(bot))

    def _create_dispatch_callback(self, bot: Bot) -> Callable:
        """为每个适配器创建一个闭包回调函数，捕获专属的 Bot 上下文"""

        async def _callback(event: MessageEvent):
            await self._dispatch_event(bot, event)

        return _callback

    def load_all_plugins(self):
        self.plugin_manager.load_all_plugins()

    async def _dispatch_event(self, bot: Bot[BaseAdapter[Any]], event: MessageEvent):
        """事件分发器
        将接收到的事件分发给所有注册的插件处理函数。
        """

        async def _do_reply(msg):
            try:
                await bot.send_msg(
                    target_id=event.sender_id, message=msg, cid=event.cid
                )
            except Exception as e:
                logger.error(f"发送回复消息失败: {e}")

        event._callback_func = _do_reply

        async def _safe_execute(plugin_name: str, handler_func: Callable):
            try:
                sig = inspect.signature(handler_func)
                kwargs = {}

                for name, param in sig.parameters.items():
                    annotation = param.annotation

                    if name == "event" or (
                        inspect.isclass(annotation)
                        and issubclass(annotation, MessageEvent)
                    ):
                        kwargs[name] = event
                        continue

                    origin = get_origin(annotation) or annotation
                    if inspect.isclass(origin) and origin.__name__ == "Bot":
                        expected_args = get_args(annotation)
                        if expected_args:
                            expected_adapter_type = expected_args[0]
                            if not isinstance(bot.adapter, expected_adapter_type):
                                logger.trace(
                                    f"DI skipped: [{plugin_name}] expect {expected_adapter_type.__name__}"
                                )
                                return

                        kwargs[name] = bot

                await handler_func(**kwargs)
            except Exception as exc:
                logger.exception(f"❌ 插件 [{plugin_name}] 执行崩溃: {exc}")

        adapter_id = bot.adapter.meta_data.id
        for plugin_name, plugin in self.plugin_manager.plugins.items():
            supported = plugin.metadata.support_adapters
            if supported is not None and adapter_id not in supported:
                continue

            for handler_data in plugin.message_handlers:
                func = handler_data["func"]
                rule = handler_data["rule"]
                if rule is not None and not rule(event):
                    continue

                task = asyncio.create_task(_safe_execute(plugin_name, func))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    async def run(self):
        """多端并发启动"""
        self.load_all_plugins()
        tasks = [asyncio.create_task(adapter.run()) for adapter in self.adapters]
        await asyncio.gather(*tasks)
