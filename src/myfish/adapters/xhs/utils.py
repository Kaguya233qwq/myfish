import base64
import binascii
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from string import ascii_lowercase
from typing import TypeAlias

from pydantic import JsonValue

from myfish.core.storage import StorageManager

STANDARD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
XS_ALPHABET = "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"
MNS_ALPHABET = "MfgqrsbcyzPQRStuvC7mn501HIJBo2DEFTKdeNOwxWXYZap89+/A4UVLhijkl63G"
QueryScalar: TypeAlias = str | int | bool | None
QueryValue: TypeAlias = QueryScalar | tuple[QueryScalar, ...] | list[QueryScalar]


def rotate32(value: int, count: int) -> int:
    value &= 0xFFFFFFFF
    count %= 32
    return ((value << count) | (value >> (32 - count))) & 0xFFFFFFFF


def json_shape(value: JsonValue, depth: int = 0) -> JsonValue:
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            key[:80]: json_shape(item, depth + 1)
            for key, item in list(value.items())[:32]
        }
    if isinstance(value, list):
        return [json_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def get_auth_file() -> Path:
    return StorageManager.get_adapter_dir("xhs") / "auth.json"


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def encode_base64(value: bytes, *, alphabet: str = XS_ALPHABET) -> str:
    return (
        base64.b64encode(value)
        .decode("ascii")
        .translate(str.maketrans(STANDARD_ALPHABET, alphabet))
    )


def decode_base64(value: str, *, alphabet: str = XS_ALPHABET) -> bytes:
    return base64.b64decode(
        value.translate(str.maketrans(alphabet, STANDARD_ALPHABET)), validate=True
    )


def variant_crc32(value: bytes) -> int:
    result = binascii.crc32(value) ^ 0xEDB88320
    return result - 0x100000000 if result & 0x80000000 else result


def build_url(url: str, params: Mapping[str, QueryValue] | None = None) -> str:
    if not params:
        return url
    parts = []
    for key, value in params.items():
        if isinstance(value, list | tuple):
            text = ",".join(str(item) for item in value)
        else:
            text = "" if value is None else str(value)
        parts.append(f"{key}={text.replace('=', '%3D')}")
    separator = "?" if "?" not in url else "" if url.endswith(("?", "&")) else "&"
    return url + separator + "&".join(parts)


IDENTITY_ALPHABET = ascii_lowercase + "1234567890"


def generate_a1(timestamp_ms: int, random_part: str) -> str:
    if (
        timestamp_ms < 0
        or len(random_part) != 30
        or set(random_part) - set(IDENTITY_ALPHABET)
    ):
        raise ValueError(
            "a1 requires a nonnegative timestamp and 30 lowercase alphanumeric characters"
        )
    prefix = f"{timestamp_ms:x}{random_part}50000"
    checksum = binascii.crc32(prefix.encode("ascii"))
    return f"{prefix}{checksum}"[:52]


def generate_web_id(a1: str) -> str:
    return hashlib.md5(a1.encode("ascii")).hexdigest()


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def write_private_json(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".xhs-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
