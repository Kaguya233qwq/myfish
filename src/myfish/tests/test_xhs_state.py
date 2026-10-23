import json
import stat
import tempfile
import unittest
from pathlib import Path

import httpx

from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.device import generate_telemetry
from myfish.adapters.xhs.utils import decode_base64


class ClientStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_preserves_identity_counters_secret_and_cookie_scopes(self):
        requests = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "code": 0,
                    "data": {"guest": False, "user_id": "self"},
                },
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            async with XhsAPI({"a1": "a" * 52}) as original:
                fields = dict(original.fingerprint.fields)
                fields.update(
                    x83="", x84=generate_telemetry(original.session.loaded_at_ms)
                )
                original.set_profile_fields(fields)
                original.session.dsl = "1234"
                original.session.tiga_timestamp = 2345
                original.session.sequence = 7
                original.session_secret = bytes(range(48))
                original.use_current_signing = True
                original._client.cookies.set(
                    "scoped", "private", domain="as.xiaohongshu.com", path="/api"
                )
                original.save_state(path)
                loaded_at = original.session.loaded_at_ms
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            async with XhsAPI.from_state(
                path, transport=httpx.MockTransport(handle)
            ) as restored:
                self.assertEqual(restored.session.loaded_at_ms, loaded_at)
                self.assertEqual(restored.session.sequence, 7)
                self.assertEqual(restored.session_secret, bytes(range(48)))
                self.assertEqual(restored.fingerprint.fields, fields)
                self.assertEqual(restored.session.ds_anchor(), "2345;1234")
                self.assertNotIn(
                    "scoped",
                    restored.cookies_for_url("https://edith.xiaohongshu.com/api/test"),
                )
                self.assertIn(
                    "scoped",
                    restored.cookies_for_url("https://as.xiaohongshu.com/api/test"),
                )
                await restored.get_self_info()
                common = json.loads(decode_base64(requests[0].headers["x-s-common"]))
                self.assertEqual(common["x10"], 8)
                self.assertEqual(common["x12"], "2345;1234")
                restored.save_state(path)
            async with XhsAPI.from_state(path) as second:
                self.assertEqual(second.session.sequence, 8)
                self.assertEqual(second.session.loaded_at_ms, loaded_at)

    async def test_uninitialized_or_invalid_state_is_rejected_without_secret_details(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            async with XhsAPI({"a1": "a" * 52}) as api:
                with self.assertRaises(ValueError):
                    api.save_state(path)
            self.assertFalse(path.exists())
            path.write_text('{"version":2,"secret":"sensitive"}')
            with self.assertRaisesRegex(ValueError, "^Invalid XHS client state$"):
                XhsAPI.from_state(path)
