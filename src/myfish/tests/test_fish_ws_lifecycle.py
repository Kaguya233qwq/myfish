"""使用本地 WebSocket 服务验证连接生命周期，不访问闲鱼。"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import websockets
from loguru import logger
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from myfish.adapters.fish import FishAdapter
from myfish.adapters.fish.api import FishAPI, FishAPIError


class WebSocketLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def run_server_scenario(self, mode):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        adapter.api.device_id = "test-device"
        adapter.api.cookies_dict = {}
        adapter.api.get_access_token.return_value = "test-access-token"
        token_calls = 0

        async def token():
            nonlocal token_calls
            token_calls += 1
            if token_calls == 2:
                if mode == "rejected":
                    raise FishAPIError("token rejected")
                if mode == "timeout":
                    raise TimeoutError("token request timed out")
            return "test-access-token"

        adapter.api.get_access_token.side_effect = token
        adapter._get_headers = dict
        connections = 0
        registrations = 0
        refreshes = 0
        connected = 0
        reconnected = asyncio.Event()
        refreshed = asyncio.Event()
        messages = []

        def capture(message):
            nonlocal refreshes, connected
            text = message.record["message"]
            messages.append(text)
            if "WebSocket 连接成功" in text:
                connected += 1
                if connected == 2:
                    reconnected.set()
            if "登录凭证刷新成功" in text:
                refreshes += 1
                if refreshes == 3:
                    refreshed.set()

        async def handler(ws):
            nonlocal connections, registrations
            connections += 1
            connection = connections
            local_registrations = 0
            try:
                async for raw in ws:
                    request = json.loads(raw)
                    path = request.get("lwp")
                    if path == "/reg":
                        registrations += 1
                        local_registrations += 1
                    if path:
                        await ws.send(
                            json.dumps({"code": 200, "headers": request["headers"]})
                        )
                    if (
                        path == "/reg"
                        and local_registrations > 1
                        and mode == "duplicate_registration"
                    ):
                        await asyncio.sleep(0.01)
                        ws.transport.abort()
                        return
                    if path == "/r/SyncStatus/ackDiff" and connection == 1:
                        if mode == "normal_close":
                            await ws.close(1001, "server restart")
                        elif mode == "server_close":
                            await ws.close(4001, "token expired test-access-token")
                        elif mode == "abrupt_close":
                            await asyncio.sleep(0.01)
                            ws.transport.abort()
                            return
            except websockets.ConnectionClosed:
                pass

        sink = logger.add(capture)
        real_connect = websockets.connect

        def connect(*args, **kwargs):
            return real_connect(*args, **kwargs, proxy=None)

        try:
            async with serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                adapter.base_url = f"ws://127.0.0.1:{port}"
                with (
                    patch("myfish.adapters.fish._TOKEN_REFRESH_INTERVAL", 0.02),
                    patch("myfish.adapters.fish._WS_REQUEST_TIMEOUT", 0.5),
                    patch("myfish.adapters.fish._RECONNECT_INTERVAL", 0.01),
                    patch(
                        "myfish.adapters.fish.websockets.connect", side_effect=connect
                    ),
                ):
                    task = asyncio.create_task(adapter._connect_ws())
                    try:
                        await asyncio.wait_for(
                            refreshed.wait()
                            if mode in {"success", "duplicate_registration"}
                            else reconnected.wait(),
                            5,
                        )
                    finally:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
        finally:
            logger.remove(sink)
        self.assertIsNone(adapter._active_ws)
        self.assertEqual(adapter._pending_requests, {})
        self.assertEqual(adapter._ws_token, "")
        self.assertNotIn("test-access-token", "\n".join(messages))
        return connections, registrations, "\n".join(messages)

    async def test_successful_refreshes_keep_the_same_connection(self):
        connections, registrations, messages = await self.run_server_scenario("success")
        self.assertEqual(connections, 1)
        self.assertEqual(registrations, 1)
        self.assertNotIn("WebSocket断开", messages)

    async def test_token_refresh_does_not_repeat_reg_on_server_that_aborts_after_ack(
        self,
    ):
        connections, registrations, messages = await self.run_server_scenario(
            "duplicate_registration"
        )
        self.assertEqual(connections, 1)
        self.assertEqual(registrations, 1)
        self.assertNotIn("WebSocket断开", messages)

    async def test_token_rejection_reconnects_without_registering_again(self):
        connections, _, messages = await self.run_server_scenario("rejected")
        self.assertEqual(connections, 2)
        self.assertIn("stage=fetch_token error=FishAPIError", messages)
        self.assertIn("reconnect_reason=auth_refresh_failure", messages)

    async def test_token_request_timeout_reconnects(self):
        connections, _, messages = await self.run_server_scenario("timeout")
        self.assertEqual(connections, 2)
        self.assertIn("stage=fetch_token error=TimeoutError", messages)
        self.assertIn("reconnect_reason=auth_refresh_failure", messages)

    async def test_server_close_records_code_stage_and_safe_reason(self):
        connections, _, messages = await self.run_server_scenario("server_close")
        self.assertEqual(connections, 2)
        self.assertIn("received_code=4001", messages)
        self.assertIn("sent_code=4001", messages)
        self.assertIn("reason=token expired [redacted]", messages)
        self.assertIn("refresh_stage=waiting", messages)

    async def test_abrupt_close_is_local_1006_without_received_close_frame(self):
        connections, _, messages = await self.run_server_scenario("abrupt_close")
        self.assertEqual(connections, 2)
        self.assertIn("close_code=1006 received_code=None sent_code=None", messages)

    async def test_normal_server_close_is_logged_and_reconnected(self):
        connections, _, messages = await self.run_server_scenario("normal_close")
        self.assertEqual(connections, 2)
        self.assertIn("received_code=1001", messages)
        self.assertIn("reason=server restart", messages)
        self.assertIn("reconnect_reason=connection_closed", messages)

    async def test_heartbeat_failure_closes_current_connection(self):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        ws = AsyncMock()
        adapter._active_ws = ws
        ws.send.side_effect = OSError("connection lost")
        await adapter._heart_beat_loop()
        ws.close.assert_awaited_once()
        self.assertEqual(adapter._ws_reconnect_reason, "heartbeat_failure")

    async def test_old_heartbeat_failure_does_not_close_replacement_connection(self):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        old, replacement = AsyncMock(), AsyncMock()
        adapter._active_ws = old

        async def replace_and_fail(raw):
            adapter._active_ws = replacement
            raise OSError("old connection lost")

        old.send.side_effect = replace_and_fail
        await adapter._heart_beat_loop()
        old.close.assert_not_awaited()
        replacement.close.assert_not_awaited()

    async def test_unexpected_refresh_error_does_not_silently_stop_refreshing(self):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        adapter._active_ws = AsyncMock()
        adapter.api.get_access_token.side_effect = ValueError("invalid response")
        with patch("myfish.adapters.fish.asyncio.sleep", AsyncMock()):
            await adapter._keep_token_alive_loop()
        adapter._active_ws.close.assert_awaited_once()
        self.assertEqual(adapter._ws_reconnect_reason, "auth_refresh_failure")

    def test_close_reason_redacts_credentials_and_line_breaks(self):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        adapter.api.cookies_dict = {"cookie": "private-cookie"}
        adapter._ws_token = "private-token"
        self.assertEqual(
            adapter._safe_close_reason("private-cookie\r\nprivate-token"),
            "[redacted]  [redacted]",
        )

    def test_ping_timeout_records_sent_close_code_and_reason(self):
        adapter = FishAdapter(AsyncMock(spec=FishAPI))
        adapter.api.cookies_dict = {}
        failure = ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))
        with patch("myfish.adapters.fish.logger") as log:
            adapter._log_ws_disconnect(None, 0, failure)
        template, *values = log.warning.call_args.args
        message = template.format(*values)
        self.assertIn("sent_code=1011", message)
        self.assertIn("reason=keepalive ping timeout", message)


if __name__ == "__main__":
    unittest.main()
