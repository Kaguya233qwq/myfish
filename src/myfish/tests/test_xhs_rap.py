import base64
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx

from myfish.adapters.xhs.api import XhsAPI
from myfish.adapters.xhs.creator import CreatorAPI
from myfish.adapters.xhs.ds import CreatorDS
from myfish.adapters.xhs.rap import (
    RapContext,
    build_payload,
    creator_fingerprint,
    encrypt_blocks,
    sign_rap,
    xxhash32,
)
from myfish.adapters.xhs.utils import compact_json

_FIXTURE = json.loads((Path(__file__).parent / "fixtures/xhs_rap.json").read_text())
_COLD = json.loads((Path(__file__).parent / "fixtures/xhs_rap_cold.json").read_text())


def context(values: dict) -> RapContext:
    fields = dict(values)
    for key in ("fingerprint_id", "xor_key", "nonce", "padding"):
        fields[key] = fields[key].encode("ascii")
    return RapContext(**fields)


class RapTests(unittest.TestCase):
    def test_generated_fingerprints_match_rebuilt_reference_layout(self):
        parameters = context(_COLD["context"])
        for case in _COLD["cases"]:
            with self.subTest(width=case["width"], height=case["height"]):
                fingerprint = creator_fingerprint(
                    loaded_at_ms=case["loaded_at_ms"],
                    viewport_width=case["width"],
                    viewport_height=case["height"],
                )
                self.assertEqual(fingerprint.hex(), case["fingerprint"])
                self.assertEqual(len(fingerprint), 230)
                self.assertNotIn(b"KeyP", fingerprint)
                self.assertIn(bytes.fromhex("043600000000"), fingerprint)
                self.assertIn(bytes.fromhex("043c00000000"), fingerprint)
                self.assertEqual(
                    sign_rap(
                        _COLD["url"], _COLD["body"].encode(), fingerprint, parameters
                    ),
                    case["expected"],
                )

    def test_generated_fingerprint_rejects_invalid_dimensions_and_times(self):
        values = {"loaded_at_ms": 1000, "viewport_width": 1920, "viewport_height": 1080}
        for invalid in (
            {"loaded_at_ms": -1},
            {"loaded_at_ms": 1 << 64},
            {"viewport_width": 0},
            {"viewport_height": -1},
            {"viewport_width": 1 << 32},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                creator_fingerprint(**(values | invalid))

    def test_hashes_match_reference_across_word_and_lane_boundaries(self):
        self.assertEqual(xxhash32(b""), 0x02CC5D05)
        for case in _FIXTURE["hashes"]:
            with self.subTest(size=len(case["input"]) // 2, seed=case["seed"]):
                self.assertEqual(
                    xxhash32(bytes.fromhex(case["input"]), case["seed"]),
                    case["expected"],
                )

    def test_custom_cipher_matches_independent_reference(self):
        data = bytearray()
        expected = bytearray()
        for case in _FIXTURE["cipher"]:
            data.extend(bytes.fromhex(case["input"]))
            expected.extend(bytes.fromhex(case["expected"]))
        self.assertEqual(encrypt_blocks(bytes(data)), bytes(expected))
        with self.assertRaises(ValueError):
            encrypt_blocks(bytes(17))

    def test_payloads_and_complete_envelopes_match_reference_zlib_branch(self):
        for case in _FIXTURE["cases"]:
            with self.subTest(
                nonce=case["context"]["nonce"],
                fingerprint=case["fingerprint"],
                body=case["body"],
            ):
                parameters = context(case["context"])
                fingerprint = bytes.fromhex(
                    _FIXTURE["fingerprints"][case["fingerprint"]]
                )
                body = case["body"].encode("utf-8")
                self.assertEqual(
                    build_payload(case["url"], body, fingerprint, parameters).hex(),
                    case["plaintext"],
                )
                self.assertEqual(
                    sign_rap(case["url"], body, fingerprint, parameters),
                    case["expected"],
                )

    def test_request_binding_uses_exact_url_and_body_bytes(self):
        case = _FIXTURE["cases"][0]
        parameters = context(case["context"])
        fingerprint = bytes.fromhex(_FIXTURE["fingerprints"][0])
        inputs = [
            (case["url"], b"{}"),
            (case["url"].replace("%3D", "="), b"{}"),
            (case["url"], b"{ }"),
            (case["url"], compact_json({"title": "中文😀"}).encode()),
        ]
        signatures = [
            sign_rap(url, body, fingerprint, parameters) for url, body in inputs
        ]
        self.assertEqual(len(set(signatures)), len(inputs))
        with self.assertRaises(ValueError):
            sign_rap(
                "//edith.xiaohongshu.com/web_api/sns/v2/note",
                b"{}",
                fingerprint,
                parameters,
            )

    def test_blob_contents_cannot_be_mistaken_for_hash_tags(self):
        case = _FIXTURE["cases"][0]
        parameters = context(case["context"])
        blob = bytes.fromhex("03eb0102030403ea")
        fingerprint = (
            bytes.fromhex(_FIXTURE["fingerprints"][1])
            + bytes.fromhex("043600000008")
            + blob
        )
        payload = build_payload(case["url"], b"{}", fingerprint, parameters)
        unmasked = bytes(value ^ parameters.mask for value in payload[16:])
        self.assertTrue(unmasked.endswith(blob))
        self.assertEqual(fingerprint[-len(blob) :], blob)

    def test_malformed_or_ambiguous_fingerprints_are_rejected(self):
        case = _FIXTURE["cases"][0]
        parameters = context(case["context"])
        valid = bytes.fromhex(_FIXTURE["fingerprints"][1])
        for fingerprint in (
            b"",
            bytes(4097),
            valid[:-1],
            valid + b"\x04",
            valid + b"\xff\xff\x00",
            valid + bytes.fromhex("03eb00000000"),
            valid + bytes.fromhex("04360000"),
            valid + bytes.fromhex("043600001000"),
            valid[22:],
        ):
            with (
                self.subTest(fingerprint=fingerprint[:24]),
                self.assertRaises(ValueError),
            ):
                build_payload(case["url"], b"{}", fingerprint, parameters)

    def test_invalid_context_fields_are_rejected(self):
        parameters = context(_FIXTURE["cases"][0]["context"])
        for values in (
            {"timestamp_ms": -1},
            {"timestamp_ms": 1 << 64},
            {"mtime": 1 << 32},
            {"encryption_time_ms": -1},
            {"nonce": b"abc"},
            {"nonce": b"abcdefg"},
            {"mask": 255},
            {"xor_key": bytes(16)},
            {"padding": b"a" * 15},
            {"fingerprint_id": b"not-ascii-\xff" * 2},
        ):
            with self.subTest(fields=values), self.assertRaises(ValueError):
                replace(parameters, **values)


class CreatorRapTests(unittest.IsolatedAsyncioTestCase):
    async def test_rap_binds_the_same_bytes_as_the_http_request(self):
        case = _FIXTURE["cases"][0]
        parameters = context(case["context"])
        fingerprint = bytes.fromhex(_FIXTURE["fingerprints"][0])
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"success": True, "code": 0, "data": {}})

        ds_fixture = json.loads(
            (Path(__file__).parent / "fixtures/xhs_creator.json").read_text()
        )
        ds = CreatorDS.from_script(
            f"function getdss(){{return '{ds_fixture['anchor']}';}}var p='{ds_fixture['program']}';"
        )
        async with (
            XhsAPI({"a1": "a" * 52, "web_session": "test"}) as account,
            CreatorAPI(account, transport=httpx.MockTransport(handle)) as creator,
        ):
            creator.ds = ds
            creator._ds_loaded_at = creator.session.clock()
            with patch(
                "myfish.adapters.xhs.creator.RapContext", return_value=parameters
            ):
                await creator._request(
                    "POST",
                    "/web_api/sns/v2/note?test=abc%3D",
                    origin="https://edith.xiaohongshu.com",
                    payload={"title": "中文😀"},
                    rap_fingerprint=fingerprint,
                )
                await creator._request("GET", "/api/galaxy/user/info")
        request = requests[0]
        self.assertEqual(request.content, compact_json({"title": "中文😀"}).encode())
        self.assertEqual(
            request.headers["x-rap-param"],
            sign_rap(str(request.url), request.content, fingerprint, parameters),
        )
        self.assertTrue(
            base64.b64decode(request.headers["x-rap-param"]).startswith(b"\x07\x24\x01")
        )
        self.assertNotIn("x-rap-param", requests[1].headers)
