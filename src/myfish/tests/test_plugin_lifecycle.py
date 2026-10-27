import asyncio
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from myfish.core.engine import Engine
from myfish.core.plugin import Plugin, PluginMetadata


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def _run_configured_plugin(self, enabled):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = f"lifecycle_test_{uuid.uuid4().hex}"
            plugin_dir = root / package
            plugin_dir.mkdir()
            marker = root / "steps.txt"
            source = (
                "from pathlib import Path\n"
                "from myfish.core.plugin import Plugin, PluginMetadata\n"
                f"marker = Path({str(marker)!r})\n"
                "marker.write_text('import\\n', encoding='utf-8')\n"
                "plugin = Plugin(PluginMetadata(id='sample', name='Sample'))\n"
                "@plugin.on_startup()\n"
                "async def start(engine):\n"
                "    with marker.open('a', encoding='utf-8') as stream: stream.write('start\\n')\n"
                "@plugin.on_shutdown()\n"
                "async def stop(engine):\n"
                "    with marker.open('a', encoding='utf-8') as stream: stream.write('stop\\n')\n"
            )
            (plugin_dir / "sample").mkdir()
            (plugin_dir / "sample" / "__init__.py").write_text(source, encoding="utf-8")
            engine = Engine(plugin_configs={"sample": {"enabled": enabled}})
            engine.plugin_manager.builtin_plugins_dir = plugin_dir
            engine.plugin_manager.user_plugins_dir = root / "external"
            with patch.object(sys, "path", list(sys.path)):
                try:
                    await engine.run()
                finally:
                    for name in list(sys.modules):
                        if name == package or name.startswith(package + "."):
                            sys.modules.pop(name, None)
            if enabled:
                self.assertEqual(
                    marker.read_text(encoding="utf-8").splitlines(),
                    ["import", "start", "stop"],
                )
                self.assertEqual(set(engine.plugin_manager.plugins), {"Sample"})
            else:
                self.assertFalse(marker.exists())
                self.assertEqual(engine.plugin_manager.plugins, {})
            self.assertFalse(engine._service_tasks)

    async def test_disabled_plugin_is_not_imported_or_started(self):
        await self._run_configured_plugin(False)

    async def test_enabled_plugin_runs_startup_and_shutdown(self):
        await self._run_configured_plugin(True)

    async def test_service_keeps_engine_alive_without_adapters_and_stops_cleanly(self):
        engine = Engine()
        engine.load_all_plugins = Mock()
        plugin = Plugin(PluginMetadata(id="service", name="service"))
        engine.plugin_manager.plugins["service"] = plugin
        started = asyncio.Event()
        shutdown = asyncio.Event()
        steps = []

        @plugin.on_startup()
        async def start(context):
            async def serve():
                started.set()
                await shutdown.wait()

            context.add_service_task(asyncio.create_task(serve()))
            steps.append("started")

        @plugin.on_shutdown()
        async def stop(context):
            steps.append("stopped")
            shutdown.set()

        running = asyncio.create_task(engine.run())
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(running.done())
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(steps, ["started", "stopped"])
        self.assertFalse(engine._service_tasks)

    async def test_failed_startup_runs_partial_plugin_cleanup_in_reverse_order(self):
        engine = Engine()
        engine.load_all_plugins = Mock()
        calls = []
        for name in ("a", "b"):
            plugin = Plugin(PluginMetadata(id=name, name=name))
            engine.plugin_manager.plugins[name] = plugin

            async def start(context, name=name):
                calls.append("start:" + name)
                if name == "b":
                    raise ValueError("startup failed")

            async def stop(context, name=name):
                calls.append("stop:" + name)

            plugin.on_startup()(start)
            plugin.on_shutdown()(stop)
        with self.assertRaises(ValueError):
            await engine.run()
        self.assertEqual(calls, ["start:a", "start:b", "stop:b", "stop:a"])
