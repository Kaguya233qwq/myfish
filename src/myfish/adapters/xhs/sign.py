import base64
import hashlib
import hmac
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from string import ascii_letters, digits
from typing import Literal

from cryptography.hazmat.decrepit.ciphers.algorithms import ARC4, TripleDES
from cryptography.hazmat.primitives import padding as symmetric_padding
from cryptography.hazmat.primitives.asymmetric import padding as asymmetric_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pydantic import JsonValue

from .utils import MNS_ALPHABET, compact_json, encode_base64, rotate32, variant_crc32

_MASK = 0xFFFFFFFF
_KEYSTREAM = bytes.fromhex(
    "71a302257793271ddd273bcee3e4b98d9d7935e1da33f5765e2ea8afb6dc77a51a499d"
    "23b67c20660025860cbf13d4540d92497f58686c574e508f46e1956344f39139bf4faf22"
    "a3eef120b79258145b2feb5193b6478669961298e79bedca646e1a693a926154a5a7a1b"
    "d1cf0dedb742f917a747a1e388b234f2277516db7116035439730fa61e9822a0eca7bff72d8"
)
LEGACY_ENVIRONMENT = bytes.fromhex("f9416767c9b583635e0744fa8415")
MnsTier = Literal["0101", "0201", "0301"]
_CREATOR_KEYSTREAM = base64.b64decode(
    "r1crlcplstnsdrtdLpfLZTKZzGYzmcxmM5nM5nM5nM5nMxkMBgMBAAAAAACAQCCQSCQSicTicTgcDgcDAYBAoFAoFIrFYjEYDAaDwWAwmEwmk8lkslmsVqvV6vX6/X4/n08nk0mk0ul0Op1OJ5NJpNLp9Ho9Ho9HI5FIpNJpNBqNRiMRCIRCIZDIZDKZTKbT6XS63e53Ox2OR6NRKBSKxQ=="
)
_BASE58_ALPHABET = "NOPQRStuvwxWXYZabcyz012DEFTKLMdefghijkl4563GHIJBC7mnop89+/"


@dataclass(frozen=True, slots=True)
class SigningContext:
    timestamp_ms: int
    loaded_at_ms: int
    sequence: int
    environment_size: int
    seed: int
    environment: bytes = LEGACY_ENVIRONMENT

    def __post_init__(self) -> None:
        if not all(
            0 <= value <= _MASK
            for value in (self.sequence, self.environment_size, self.seed)
        ):
            raise ValueError("Signature counters must fit unsigned 32-bit integers")
        if not all(
            0 <= value < 1 << 64 for value in (self.timestamp_ms, self.loaded_at_ms)
        ):
            raise ValueError("Signature timestamps must fit unsigned 64-bit integers")
        if len(self.environment) != 14:
            raise ValueError("Signature environment must contain 14 bytes")


def device_digest(data: bytes) -> bytes:
    size = len(data)
    if size % 8:
        raise ValueError("Device digest input must align to eight bytes")
    a, b, c, d = (
        1831565813 ^ size,
        461845907 ^ (size << 8),
        2246822507 ^ (size << 16),
        3266489909 ^ (size << 24),
    )
    for left, right in struct.iter_unpack("<II", data):
        a = rotate32((a + left) ^ c, 7)
        b = rotate32((b ^ left) + d, 11)
        c = rotate32((c + right) ^ a, 13)
        d = rotate32((d ^ right) + b, 17)
    a ^= size
    b ^= a
    c = (c + b) & _MASK
    d ^= c
    a, b, c, d = rotate32(a, 9), rotate32(b, 13), rotate32(c, 17), rotate32(d, 19)
    a = (a + c) & _MASK
    b ^= d
    c = (c + a) & _MASK
    d ^= b
    return struct.pack("<IIII", a, b, c, d)


def pack_payload(
    path: str,
    body: str,
    a1: str,
    context: SigningContext,
    *,
    app_id: str = "xhs-pc-web",
    device_tag: str = "a3",
    ds_digest: bytes | None = None,
) -> bytes:
    full = (path + body).encode("utf-8")
    digest = hashlib.md5(full).digest()
    key = context.seed & 0xFF
    account = a1.encode("ascii")
    if app_id == "xhs-pc-web":
        account = account[:52].ljust(52, b"\0")
    app = app_id.encode("ascii")
    tag = device_tag.encode("ascii")
    if any(len(value) > 255 for value in (account, app, tag)):
        raise ValueError("MNS string fields must fit one-byte lengths")
    timestamp = struct.pack("<Q", context.timestamp_ms)
    url_digest = hashlib.md5(path.encode("utf-8")).digest()
    if device_tag == "nop":
        signature = url_digest
    elif ds_digest is not None:
        signature = ds_digest
    elif app_id == "ugc":
        raise ValueError("Creator signing requires a DS digest")
    else:
        signature = device_digest(timestamp + url_digest)
    if len(signature) != 16:
        raise ValueError("DS digest must contain exactly 16 bytes")
    return b"".join(
        (
            bytes.fromhex("79686029"),
            struct.pack("<I", context.seed),
            timestamp,
            struct.pack(
                "<QIII",
                context.loaded_at_ms,
                context.sequence,
                context.environment_size,
                len(full),
            ),
            bytes(value ^ key for value in digest[:8]),
            bytes((len(account),)),
            account,
            bytes((len(app),)),
            app,
            bytes((1, key ^ 115)),
            context.environment,
            bytes((len(tag),)),
            tag,
            b"\x10",
            bytes(value ^ key for value in signature),
        )
    )


def _encrypt_security_payload(payload: bytes) -> bytes:
    padded = payload + bytes(-len(payload) % 4)
    words = [*struct.unpack(f"<{len(padded) // 4}I", padded), len(payload)]
    key = struct.unpack("<4I", b"e6483ca2a1eed5e3")
    total = 0
    previous = words[-1]
    for _ in range(6 + 52 // len(words)):
        total = (total + 1013904243) & _MASK
        selector = (total >> 2) & 3
        for index in range(len(words)):
            following = words[(index + 1) % len(words)]
            mixed = (
                ((previous >> 5) ^ (following << 2))
                + ((following >> 3) ^ (previous << 4))
            ) ^ ((total ^ following) + (key[(index & 3) ^ selector] ^ previous))
            words[index] = (words[index] + mixed) & _MASK
            previous = words[index]
    return struct.pack(f"<{len(words)}I", *words)


def sign_mns(
    path: str,
    body: str,
    a1: str,
    context: SigningContext,
    *,
    tier: MnsTier = "0301",
    app_id: str = "xhs-pc-web",
    device_tag: str = "a3",
    ds_digest: bytes | None = None,
) -> str:
    packed = pack_payload(
        path,
        body,
        a1,
        context,
        app_id=app_id,
        device_tag=device_tag,
        ds_digest=ds_digest,
    )
    if tier == "0101":
        if len(packed) > len(_CREATOR_KEYSTREAM):
            raise ValueError("MNS payload exceeds the 0101 keystream")
        encrypted = bytes(
            value ^ _CREATOR_KEYSTREAM[index] for index, value in enumerate(packed)
        )
        number = int.from_bytes(encrypted, "big")
        digits: list[str] = []
        while number:
            number, digit = divmod(number, 58)
            digits.append(_BASE58_ALPHABET[digit])
        zeros = len(encrypted) - len(encrypted.lstrip(b"\0"))
        return "mns0101_" + "1" * zeros + "".join(reversed(digits))
    if tier == "0201":
        encrypted = _encrypt_security_payload(packed)
        return "mns0201_" + encode_base64(encrypted, alphabet=MNS_ALPHABET)
    encrypted = bytes(
        value ^ key for value, key in zip(packed, _KEYSTREAM, strict=True)
    )
    return "mns0301_" + encode_base64(encrypted, alphabet=MNS_ALPHABET)


_B1_KEYS = (
    "x33",
    "x34",
    "x35",
    "x36",
    "x37",
    "x38",
    "x39",
    "x42",
    "x43",
    "x44",
    "x45",
    "x46",
    "x48",
    "x49",
    "x50",
    "x51",
    "x52",
    "x82",
)
_B1_KEY = b"xhswebmplfbt"
_DES_KEY = b"zbp30y86"
_URI_SAFE = frozenset(ascii_letters + digits + "-_.!~*'()")


def encode_b1(fields: Mapping[str, JsonValue], *, current: bool = False) -> str:
    keys = (*_B1_KEYS, "x84") if current else _B1_KEYS
    if missing := set(keys) - fields.keys():
        raise ValueError(
            f"Fingerprint fields missing for b1: {', '.join(sorted(missing))}"
        )
    text = compact_json({key: fields[key] for key in keys})
    # Repeating the protocol key preserves RC4's schedule at a supported key size.
    encryptor = Cipher(ARC4(_B1_KEY * 2), mode=None).encryptor()
    if current:
        units = struct.unpack(
            f"<{len(text.encode('utf-16-le')) // 2}H", text.encode("utf-16-le")
        )
        stream = encryptor.update(bytes(len(units))) + encryptor.finalize()
        encrypted = struct.pack(
            f"<{len(units)}H",
            *(value ^ key for value, key in zip(units, stream, strict=True)),
        )
        binary = encrypted.decode("utf-16-le", errors="replace").encode("utf-8")
    else:
        encrypted = encryptor.update(text.encode("utf-8")) + encryptor.finalize()
        start = next(
            (
                index
                for index, value in enumerate(encrypted)
                if chr(value) not in _URI_SAFE
            ),
            len(encrypted),
        )
        binary = encrypted[start:].decode("latin1").encode("utf-8")
    return encode_base64(binary)


def encode_profile(fields: Mapping[str, JsonValue]) -> str:
    encoded = base64.b64encode(compact_json(dict(fields)).encode("utf-8"))
    padded = encoded + b"\0" * (-len(encoded) % 8)
    # Equal EDE keys reproduce the protocol's single DES operation.
    encryptor = Cipher(TripleDES(_DES_KEY * 3), modes.ECB()).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()


def sign_common(
    a1: str,
    b1: str,
    *,
    version: str = "4.3.5",
    web_build: str = "4.86.0",
    sequence: int = 0,
    ds_anchor: str | None = None,
    platform: str = "Windows",
    app_id: str = "xhs-pc-web",
) -> str:
    values: dict[str, JsonValue] = {
        "s0": 5,
        "s1": "",
        "x0": "1",
        "x1": version,
        "x2": platform,
        "x3": app_id,
        "x4": web_build,
        "x5": a1,
        "x6": "",
        "x7": "",
        "x8": b1,
        "x9": variant_crc32(b1.encode("ascii")),
        "x10": sequence,
        "x11": "normal",
    }
    if ds_anchor is not None:
        values["x12"] = ds_anchor
    return encode_base64(compact_json(values).encode("utf-8"))


def sign_xys(
    path: str,
    body: str,
    a1: str,
    context: SigningContext,
    *,
    tier: Literal["0201", "0301"] = "0301",
) -> str:
    envelope = {
        "x0": "4.3.5",
        "x1": "xhs-pc-web",
        "x2": "Windows",
        "x3": sign_mns(path, body, a1, context, tier=tier),
        "x4": "object",
    }
    return "XYS_" + encode_base64(compact_json(envelope).encode("utf-8"))


def sign_xyw(path: str, body: str, a1: str, timestamp_ms: int) -> str:
    digest = hashlib.md5(f"url={path}{body}".encode()).hexdigest()
    flags = "0|0|0|1|0|0|1|0|0|0|1|0|0|0|0|1|0|0|1"
    plaintext = f"x1={digest};x2={flags};x3={a1};x4={timestamp_ms};".encode()
    algorithm = algorithms.AES(b"7cc4adla5ay0701v")
    padder = symmetric_padding.PKCS7(algorithm.block_size).padder()
    padded = padder.update(base64.b64encode(plaintext)) + padder.finalize()
    encryptor = Cipher(algorithm, modes.CBC(b"4uzjr7mbsibcaldp")).encryptor()
    payload = encryptor.update(padded) + encryptor.finalize()
    envelope = {
        "signSvn": "56",
        "signType": "x2",
        "appId": "xhs-pc-web",
        "signVersion": "1",
        "payload": payload.hex(),
    }
    return "XYW_" + base64.b64encode(compact_json(envelope).encode()).decode("ascii")


def sign_current(
    path: str,
    body: str,
    a1: str,
    context: SigningContext,
    secret: bytes,
    nonce: int,
    *,
    has_body: bool,
    platform: str = "Windows",
    tier: Literal["0201", "0301"] = "0301",
) -> str:
    if len(secret) < 32:
        raise ValueError("SSK must contain at least 32 bytes")
    if not 0 <= nonce <= 0xFFFFFFFF:
        raise ValueError("SSK nonce must fit an unsigned 32-bit integer")
    digest = hashlib.md5((path + body).encode()).digest()
    prefix = struct.pack("<I", nonce)
    wrapped = prefix + hashlib.sha1(secret + prefix).digest() + secret[32:]
    app = "xhs-pc-web"
    envelope = {
        "x0": "4.4.3",
        "x1": app,
        "x2": platform,
        "x3": sign_mns(path, body, a1, context, tier=tier),
        "x4": "object" if has_body else "",
        "x5": digest.hex(),
        "x6": {app: base64.b64encode(hashlib.sha1(digest + wrapped).digest()).decode()},
        "x7": {app: base64.b64encode(wrapped).decode()},
    }
    return "XYS_" + encode_base64(compact_json(envelope).encode())


def creator_headers(
    path: str,
    body: str,
    fields: Mapping[str, JsonValue],
    context: SigningContext,
    *,
    a1: str,
    b1: str,
    ds_anchor: str,
    ds_digest: bytes | None,
    tier: Literal["0101", "0201"] = "0101",
    has_body: bool = False,
) -> dict[str, str]:
    platform = browser_platform(fields.get("x19"))
    envelope = {
        "x0": "4.3.6",
        "x1": "ugc",
        "x2": platform,
        "x3": sign_mns(
            path,
            body,
            a1,
            context,
            tier=tier,
            app_id="ugc",
            device_tag="nop" if tier == "0201" else "a1",
            ds_digest=ds_digest,
        ),
        "x4": "object" if has_body else "",
    }
    return {
        "x-s": "XYS_" + encode_base64(compact_json(envelope).encode()),
        "x-t": str(context.timestamp_ms),
        "x-s-common": sign_common(
            a1,
            b1,
            version="4.3.6",
            web_build="1.26.0",
            ds_anchor=ds_anchor,
            platform=platform,
            app_id="ugc",
        ),
    }


def current_headers(
    path: str,
    body: str,
    fields: Mapping[str, JsonValue],
    context: SigningContext,
    *,
    a1: str,
    web_build: str,
    secret: bytes,
    nonce: int,
    ds_anchor: str,
    has_body: bool,
    tier: Literal["0201", "0301"] = "0301",
) -> dict[str, str]:
    platform = browser_platform(fields.get("x19"))
    return {
        "x-s": sign_current(
            path,
            body,
            a1,
            context,
            secret,
            nonce,
            has_body=has_body,
            platform=platform,
            tier=tier,
        ),
        "x-t": str(context.timestamp_ms),
        "x-s-common": sign_common(
            a1,
            encode_b1(fields, current=True),
            version="4.4.3",
            web_build=web_build,
            sequence=context.sequence,
            ds_anchor=ds_anchor,
            platform=platform,
        ),
    }


def browser_platform(navigator_platform: JsonValue) -> str:
    if isinstance(navigator_platform, str) and navigator_platform.startswith("Linux"):
        return "Linux"
    if navigator_platform == "MacIntel":
        return "Mac OS"
    return "Windows"


def encrypt_message(payload: bytes) -> str:
    modulus = int(
        "d321555d67813eace010dc27e72ab14876a9b671c7d58d3c9c2064cd60f7e9f7"
        "9ad3799657b35a1b7654d82725408a71549d5ade11e74bbf1ec39b549ed32116a"
        "ffd4f6b03f2c9c44d91f84157b159a8a225150916e2716cc82dc8fd62385e5a"
        "01c83b784c139462a1dd45d47d96ebb4f5068c42b3de8590123a03565a9e5aed",
        16,
    )
    key = rsa.RSAPublicNumbers(65537, modulus).public_key()
    size = key.key_size // 8 - 11
    return b"".join(
        key.encrypt(payload[index : index + size], asymmetric_padding.PKCS1v15())
        for index in range(0, len(payload), size)
    ).decode("latin1")


def upload_authorization(
    path: str, host: str, size: int, starts_at: int, expires_at: int
) -> str:
    if size <= 0 or not 0 <= starts_at < expires_at:
        raise ValueError("Invalid upload size or signature lifetime")
    if not path.startswith("/spectrum/") or any(char in path + host for char in "\r\n"):
        raise ValueError("Invalid upload path or host")
    period = f"{starts_at};{expires_at}"
    key = hmac.new(b"null", period.encode(), hashlib.sha1).hexdigest().encode()
    canonical = f"put\n{path}\n\ncontent-length={size}&host={host}\n"
    digest = hashlib.sha1(canonical.encode()).hexdigest()
    signature = hmac.new(
        key, f"sha1\n{period}\n{digest}\n".encode(), hashlib.sha1
    ).hexdigest()
    return (
        f"q-sign-algorithm=sha1&q-ak=null&q-sign-time={period}&q-key-time={period}"
        f"&q-header-list=content-length;host&q-url-param-list=&q-signature={signature}"
    )
