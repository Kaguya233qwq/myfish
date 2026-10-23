import json
import unittest
from pathlib import Path

from myfish.adapters.xhs.model import SigningSession
from myfish.adapters.xhs.sign import (
    SigningContext,
    current_headers,
    encode_b1,
    encode_profile,
    pack_payload,
    sign_common,
    sign_mns,
    sign_xys,
    sign_xyw,
)
from myfish.adapters.xhs.utils import (
    MNS_ALPHABET,
    build_url,
    decode_base64,
    encode_base64,
    generate_a1,
    generate_web_id,
    variant_crc32,
)

FIXTURES = json.loads(
    (Path(__file__).parent / "fixtures/xhs_signing.json").read_text(encoding="utf-8")
)


class SigningMigrationTests(unittest.TestCase):
    def test_security_signatures_match_spider_xhs_pr192_vectors(self):
        for case in FIXTURES["security_mns"]:
            with self.subTest(context=case["context"], path=case["path"]):
                values = dict(case["context"])
                values["environment"] = bytes.fromhex(values["environment"])
                self.assertEqual(
                    sign_mns(
                        case["path"],
                        case["body"],
                        case["a1"],
                        SigningContext(**values),
                        tier="0201",
                    ),
                    case["signed"],
                )

    def test_device_identity_matches_reference(self):
        fixture = FIXTURES["identity"]
        self.assertEqual(
            generate_a1(fixture["timestamp_ms"], fixture["random_part"]), fixture["a1"]
        )
        self.assertEqual(generate_web_id(fixture["a1"]), fixture["web_id"])

    def test_session_counters_and_ds_are_isolated(self):
        first = SigningSession(
            clock=lambda: 1234, random_bits=lambda _: 1, random_below=lambda _: 0
        )
        second = SigningSession(
            clock=lambda: 1234, random_bits=lambda _: 1, random_below=lambda _: 0
        )
        self.assertEqual(first.current_context("/api/test").sequence, 1)
        self.assertEqual(first.current_context("/api/test").sequence, 2)
        self.assertEqual(second.current_context("/api/test").sequence, 1)
        first.dsl, first.tiga_timestamp = "server-anchor", 42
        self.assertEqual(first.ds_anchor(), "42;server-anchor")
        with self.assertRaises(ValueError):
            second.ds_anchor()

    def test_encoding_matches_reference_vectors(self):
        for case in FIXTURES["encoding"]:
            with self.subTest(hex=case["hex"]):
                data = bytes.fromhex(case["hex"])
                self.assertEqual(encode_base64(data), case["xs"])
                self.assertEqual(
                    encode_base64(data, alphabet=MNS_ALPHABET), case["mns"]
                )
                self.assertEqual(decode_base64(case["xs"]), data)
                self.assertEqual(variant_crc32(data), case["crc"])
        for case in FIXTURES["urls"]:
            self.assertEqual(build_url(case["url"], case["params"]), case["expected"])

    def test_mns_matches_reference_payloads_and_signatures(self):
        for case in FIXTURES["mns"]:
            with self.subTest(context=case["context"], path=case["path"]):
                values = dict(case["context"])
                values["environment"] = bytes.fromhex(values["environment"])
                context = SigningContext(**values)
                self.assertEqual(
                    pack_payload(case["path"], case["body"], case["a1"], context).hex(),
                    case["packed"],
                )
                self.assertEqual(
                    sign_mns(case["path"], case["body"], case["a1"], context),
                    case["signed"],
                )

    def test_legacy_envelopes_match_reference(self):
        context = SigningContext(1792590000123, 1792589999000, 7, 1358, 0x12345678)
        for case in FIXTURES["envelopes"]:
            self.assertEqual(
                sign_xys(case["path"], case["body"], "a" * 52, context), case["xys"]
            )
            self.assertEqual(
                sign_xyw(case["path"], case["body"], "a" * 52, context.timestamp_ms),
                case["xyw"],
            )
        b1 = encode_b1(FIXTURES["current"]["fields"])
        self.assertEqual(b1, FIXTURES["legacy_b1"])
        self.assertEqual(sign_common("a" * 52, b1), FIXTURES["legacy_common"])

    def test_profile_cipher_matches_pre_migration_vectors(self):
        for case in FIXTURES["profile_cipher"]:
            self.assertEqual(encode_profile(case["fields"]), case["encrypted"])

    def test_current_headers_match_successful_baseline(self):
        fixture = FIXTURES["current"]
        for case in fixture["cases"]:
            session = SigningSession(
                clock=lambda: 1792590000123,
                random_bits=lambda _: 0x12345678,
                random_below=lambda _: 0,
            )
            session.loaded_at_ms = fixture["load_timestamp"]
            session.dsl = fixture["dsl"]
            session.tiga_timestamp = fixture["tiga_timestamp"]
            context = session.current_context(case["path"])
            actual = current_headers(
                case["path"],
                case["body"],
                fixture["fields"],
                context,
                a1="a" * 52,
                web_build="6.47.2",
                secret=bytes(range(48)),
                nonce=(context.timestamp_ms + 0x12345678) & 0xFFFFFFFF,
                ds_anchor=session.ds_anchor(),
                has_body=case["include_body"],
            )
            self.assertEqual(actual, case["expected"])
