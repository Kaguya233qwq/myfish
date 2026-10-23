import base64
import hashlib
import json
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes

from myfish.adapters.xhs.auth import decode_legacy_websectiga
from myfish.adapters.xhs.device import (
    DeviceFingerprint,
    DeviceProfile,
    generate_telemetry,
)
from myfish.adapters.xhs.sign import SigningContext, current_headers, encode_b1
from myfish.adapters.xhs.utils import MNS_ALPHABET, decode_base64

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/xhs_signing.json").read_text(encoding="utf-8")
)


class FingerprintTests(unittest.TestCase):
    def test_device_profile_is_consistent_and_reproducible(self):
        profile = DeviceProfile(
            screen_width=1280,
            screen_height=720,
            available_width=1280,
            available_height=680,
            memory_gb=4,
            cpu_cores=4,
        )
        first = DeviceFingerprint.create(
            {"a1": "a" * 52}, profile=profile, timestamp_ms=1234
        )
        second = DeviceFingerprint.create(
            {"a1": "a" * 52}, profile=profile, timestamp_ms=1234
        )
        self.assertEqual(first.fields, second.fields)
        self.assertEqual(first.fields["x9"], "1280;720")
        self.assertEqual(first.fields["x10"], "1280;680")
        self.assertEqual(first.fields["x5"], "4")
        self.assertIn(first.fields["x22"], first.fields["x56"])
        self.assertNotEqual(
            first.fields["x53"],
            DeviceFingerprint.create({"a1": "b" * 52}, timestamp_ms=1234).fields["x53"],
        )
        with self.assertRaises(ValueError):
            DeviceProfile(available_width=3000)

    def test_environment_profiles_keep_platform_fields_consistent(self):
        cases = (
            ("Linux", "x86_64", "Linux", "Linux x86_64"),
            ("Linux", "aarch64", "Linux", "Linux aarch64"),
            ("Windows", "AMD64", "Windows", "Win32"),
            ("Windows", "ARM64", "Windows", "Win32"),
            ("Darwin", "arm64", "Mac OS", "MacIntel"),
            ("Darwin", "x86_64", "Mac OS", "MacIntel"),
        )
        for system, machine, expected_platform, navigator in cases:
            with (
                self.subTest(system=system, machine=machine),
                patch(
                    "myfish.adapters.xhs.device.platform.system",
                    return_value=system,
                ),
                patch(
                    "myfish.adapters.xhs.device.platform.machine",
                    return_value=machine,
                ),
                patch("myfish.adapters.xhs.device.cpu_cores", return_value=12),
                patch("myfish.adapters.xhs.device.browser_memory", return_value=8),
                patch(
                    "myfish.adapters.xhs.device.screen_size",
                    return_value=(2560, 1440),
                ),
                patch(
                    "myfish.adapters.xhs.device.local_timezone",
                    return_value=("Asia/Shanghai", -480),
                ),
            ):
                profile = DeviceProfile.from_environment()
                self.assertEqual(profile.platform, expected_platform)
                self.assertEqual(profile.navigator_platform, navigator)
                self.assertEqual(profile.cpu_cores, 12)
                self.assertEqual(profile.memory_gb, 8)
                state = DeviceFingerprint.create({"a1": "a" * 52}, profile=profile)
                self.assertEqual(state.fields["x19"], navigator)
                self.assertEqual(state.fields["x9"], "2560;1440")
                self.assertEqual(state.fields["x11"], "-480")

    def test_linux_signatures_use_matching_platform(self):
        state = DeviceFingerprint.create({"a1": "a" * 52})
        state.fields["x19"] = "Linux x86_64"
        state.fields["x84"] = generate_telemetry(1234)
        headers = current_headers(
            "/test",
            "",
            state.fields,
            SigningContext(1234, 1200, 1, 1352, 42),
            a1="a" * 52,
            web_build="6.47.2",
            secret=bytes(range(48)),
            nonce=42,
            ds_anchor="1234;1200",
            has_body=False,
        )
        envelope = json.loads(decode_base64(headers["x-s"].removeprefix("XYS_")))
        common = json.loads(decode_base64(headers["x-s-common"]))
        self.assertEqual(envelope["x2"], "Linux")
        self.assertEqual(common["x2"], "Linux")
        self.assertIn("to:1234", state.fields["x84"])
        with self.assertRaises(ValueError):
            generate_telemetry(-1)

    def test_x83_changes_profile_payload_but_not_b1_or_read_request_signature(self):
        fields = DeviceFingerprint.create({"a1": "a" * 52}, timestamp_ms=1234).fields
        fields.update(x83="", x84=generate_telemetry(1200))
        reference = dict(fields, x83="synthetic-reference")
        self.assertNotEqual(
            DeviceFingerprint(fields).profile_data(),
            DeviceFingerprint(reference).profile_data(),
        )
        for current in (False, True):
            self.assertEqual(
                encode_b1(fields, current=current),
                encode_b1(reference, current=current),
            )
        context = SigningContext(1234, 1200, 1, 1352, 42)

        def headers(profile):
            return current_headers(
                "/api/sns/web/v2/user/me",
                "",
                profile,
                context,
                a1="a" * 52,
                web_build="6.47.2",
                secret=bytes(range(48)),
                nonce=42,
                ds_anchor="1234;1200",
                has_body=False,
            )

        self.assertEqual(headers(fields), headers(reference))

    def test_zero_padding_does_not_append_an_aligned_block(self):
        state = DeviceFingerprint.create({"a1": "a" * 52})
        state.fields = {"x": "aaaa"}
        serialized = base64.b64encode(b'{"x":"aaaa"}')
        self.assertEqual(len(serialized) % 8, 0)
        self.assertEqual(len(bytes.fromhex(state.profile_data())), len(serialized))

    def test_current_signature_binds_body_and_negotiated_secret(self):
        state = DeviceFingerprint.create({"a1": "a" * 52})
        state.fields["x84"] = "telemetry"
        secret = bytes(range(48))
        path = "/api/sec/v1/shield/webprofile"
        body = '{"platform":"Windows"}'
        context = SigningContext(
            1792590000123,
            1792589999000,
            1,
            1358,
            0x12345678,
            bytes.fromhex("f9416767c9b581635e0744fa8415"),
        )
        headers = current_headers(
            path,
            body,
            state.fields,
            context,
            a1="a" * 52,
            web_build="6.47.2",
            secret=secret,
            nonce=42,
            ds_anchor="1234567890124;1234567890123",
            has_body=True,
        )
        envelope = json.loads(decode_base64(headers["x-s"].removeprefix("XYS_")))
        digest = hashlib.md5((path + body).encode()).digest()
        wrapped = base64.b64decode(envelope["x7"]["xhs-pc-web"])
        self.assertEqual(envelope["x5"], digest.hex())
        self.assertEqual(wrapped[4:24], hashlib.sha1(secret + wrapped[:4]).digest())
        self.assertEqual(wrapped[24:], secret[32:])
        self.assertEqual(
            base64.b64decode(envelope["x6"]["xhs-pc-web"]),
            hashlib.sha1(digest + wrapped).digest(),
        )
        cipher = decode_base64(
            envelope["x3"].removeprefix("mns0301_"), alphabet=MNS_ALPHABET
        )
        packed = bytes(
            value ^ key
            for value, key in zip(
                cipher, bytes.fromhex(FIXTURES["keystream"]), strict=True
            )
        )
        self.assertEqual(struct.unpack_from("<I", packed, 28)[0], 1358)
        self.assertEqual(
            struct.unpack_from("<I", packed, 32)[0], len((path + body).encode())
        )
        common = json.loads(decode_base64(headers["x-s-common"]))
        self.assertEqual(common["x12"], "1234567890124;1234567890123")
        self.assertEqual(common["x10"], 1)

    def test_profile_preserves_device_identity_across_cookie_updates(self):
        state = DeviceFingerprint.create({"a1": "a" * 52})
        identity = {key: state.fields[key] for key in ("x53", "x54", "x56")}
        cookies = {"a1": "a" * 52, "web_session": "authenticated", "gid": "profile"}
        state.update(cookies)
        self.assertEqual(identity, {key: state.fields[key] for key in identity})
        self.assertNotIn("web_session", state.fields["x57"])
        self.assertIn("gid=profile", state.fields["x57"])
        encrypted = bytes.fromhex(state.profile_data())
        decryptor = Cipher(TripleDES(b"zbp30y86" * 3), modes.ECB()).decryptor()
        encoded = decryptor.update(encrypted) + decryptor.finalize()
        decoded = json.loads(base64.b64decode(encoded.rstrip(b"\0")))
        self.assertEqual(decoded, state.fields)

    def test_legacy_script_layout_and_unknown_format(self):
        table = [0] * 264
        table[93] = 802
        instructions = [[0] * 5 for _ in range(803)]
        for index in range(64):
            instructions[675 + index * 2][2] = 200 + index
            table[200 + index] = ord("a")
        packed = "".join(chr(value + 1) for row in instructions for value in row)
        script = json.dumps(
            {"b": base64.b64encode(packed.encode()).decode(), "d": table}
        )
        self.assertEqual(decode_legacy_websectiga(script), "a" * 64)
        with self.assertRaises(ValueError):
            decode_legacy_websectiga("unsupported program")
