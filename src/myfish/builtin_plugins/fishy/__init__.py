"""渔渔商品管理插件。"""

from __future__ import annotations

import asyncio
import socket
from contextlib import contextmanager
from typing import TYPE_CHECKING

from loguru import logger

from myfish.adapters.fish import FishAdapter, FishBot
from myfish.adapters.fish.events import FishTradeEvent
from myfish.core.bot import Bot
from myfish.core.config import get_config
from myfish.core.plugin import Plugin, PluginMetadata
from myfish.core.storage import StorageManager

from .settings import Settings

if TYPE_CHECKING:
    import uvicorn

    from myfish.core.engine import Engine

    from .service import Service

plugin = Plugin(
    PluginMetadata(
        id="fishy",
        name="渔渔",
        version="1.4.0",
        description="虚拟与实物商品、手动选类目、订单跟踪及虚拟自动交付",
        support_adapters=["fish"],
    )
)
_runtime: Runtime | None = None


class Runtime:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.tasks: list[asyncio.Task[None]] = []
        self.server: uvicorn.Server | None = None
        self.listener: socket.socket | None = None
        self.service: Service | None = None

    async def start(self):
        try:
            import uvicorn

            from .web import create_app
        except ImportError as exc:
            raise RuntimeError(
                "商品管理插件缺少 Web 依赖，请安装插件目录下的 requirements.txt，或在配置中禁用插件"
            ) from exc
        from .service import Service
        from .store import Store

        directory = self.settings.data_dir or StorageManager.get_plugin_dir(
            "fishy"
        )
        store = Store(directory)
        await store.initialize()
        if not (await store.rows("SELECT COUNT(*) AS n FROM users"))[0]["n"]:
            store.setup_token()
            logger.info(
                "请打开商品管理后台初始化管理员；初始化令牌保存在 {}",
                directory / "setup.token",
            )
        bots: list[FishBot] = [
            Bot(adapter)
            for adapter in self.engine.adapters
            if isinstance(adapter, FishAdapter)
        ]
        self.service = Service(
            store,
            bots,
            emit=self.engine.dispatch_event,
            legacy_order_dir=StorageManager.get_root_dir() / "adapters" / "fish",
        )
        app = create_app(self.service, self.settings)

        class EmbeddedServer(uvicorn.Server):
            @contextmanager
            def capture_signals(self):
                # 退出信号由引擎处理。
                yield

        self.server = EmbeddedServer(
            uvicorn.Config(
                app,
                host=self.settings.host,
                port=self.settings.port,
                log_level="warning",
                access_log=False,
                proxy_headers=False,
            )
        )
        # 预绑定端口，在启动钩子中报告冲突。
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((self.settings.host, self.settings.port))
        self.listener.listen(128)
        self.listener.setblocking(False)
        task = asyncio.create_task(self.server.serve(sockets=[self.listener]))
        self.tasks.append(task)
        for _ in range(100):
            if self.server.started:
                break
            if task.done():
                await task
                raise RuntimeError("Web 服务提前退出")
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("Web 服务启动超时")
        self.tasks.extend(
            [
                asyncio.create_task(self.service.worker()),
                asyncio.create_task(self.sync_loop()),
                asyncio.create_task(self.listing_sync_loop()),
            ]
        )
        if hasattr(self.engine, "add_service_task"):
            self.engine.add_service_task(task)
        logger.success(
            "商品管理后台已启动：http://{}:{}",
            self.settings.host,
            self.settings.port,
        )

    async def sync_loop(self):
        service = self.service
        if service is None:
            return
        while True:
            for bot in service.bots:
                if bot.bot_id and bot.adapter._active_ws:
                    try:
                        await service.sync(bot.bot_id)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("商品订单同步未完成: {}", type(exc).__name__)
            await asyncio.sleep(self.settings.poll_seconds)

    async def listing_sync_loop(self):
        service = self.service
        if service is None:
            return
        cursors: dict[str, int] = {}
        while True:
            for bot in service.bots:
                if not bot.bot_id or not bot.adapter._active_ws:
                    continue
                try:
                    cursors[bot.bot_id] = await service.sync_listings(
                        bot.bot_id,
                        after_id=cursors.get(bot.bot_id, 0),
                        limit=self.settings.listing_sync_batch,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "商品状态自动同步未完成，保留原状态: {}", type(exc).__name__
                    )
            await asyncio.sleep(self.settings.listing_sync_seconds)

    async def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.tasks:
            for task in self.tasks[1:]:
                task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self.tasks, return_exceptions=True), timeout=10
                )
            except TimeoutError:
                for task in self.tasks:
                    task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.listener:
            self.listener.close()
        self.tasks.clear()


@plugin.on_startup()
async def start(engine):
    global _runtime
    settings = Settings.model_validate(get_config().plugins.get("fishy", {}))
    if settings.enabled:
        _runtime = Runtime(settings, engine)
        await _runtime.start()


@plugin.on_shutdown()
async def stop(engine):
    global _runtime
    if _runtime:
        await _runtime.stop()
        _runtime = None


@plugin.on_event(FishTradeEvent)
async def trade_event(event: FishTradeEvent, bot: FishBot):
    if _runtime and _runtime.service and event.order_id:
        await _runtime.service.sync(
            bot.bot_id, order_id=event.order_id, chat_id=event.cid
        )
