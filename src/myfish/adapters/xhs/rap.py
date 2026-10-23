from __future__ import annotations

import base64
import secrets
import struct
import zlib
from dataclasses import dataclass, field

from .utils import rotate32

_MASK = 0xFFFFFFFF
_ALPHABET = b"0123456789abcdefghijklmnopqrstuvwxyz"
_SBOX = bytes.fromhex(
    "7a0158e0504e02791d4b53da6b48d452ed7712211415ec1018e5b9f10c08fc7d"
    "f9cdb5c8e637268756bab82badf068f78b8dd35e364d2e923182f229703d2dd7"
    "b640b243448078d20d494a09636c073a9ed506c6e162f4342459a9572a003e17"
    "2c0a1a42fa93bedcf5b36a13e803c797bb737686e3467247d0054c387c1f81ab"
    "7551ebf33274118f84899c71227e9dcf3f9169653c6d96a2989933399acac39f"
    "a0bce4a3a4547fa7a8046f5dacb727afb02841aeb46e0b1bdf8e30b1fe906160"
    "c0cb5c0eef1683ea20e9c955c44585cc1eaa678a7b35d619d8d9c2db94dd1cde"
    "a6fff8bf5b5a0fe7c1bdd166c525ee8ce25f88a13ba5f6ce952f6423fbfd4f9b"
)
_PRIMES = (0x9E3779B1, 0x85EBCA77, 0xC2B2AE3D, 0x27D4EB2F, 0x165667B1)
_FIELD_SIZES = {
    **dict.fromkeys((*range(0x41B, 0x432), 0x448, *range(0x47F, 0x485)), 1),
    **dict.fromkeys(
        (0x3EB, 0x433, 0x434, 0x43A, 0x440, 0x442, 0x449, 0x444, 0x446, 0x445, 0x44C), 4
    ),
    **dict.fromkeys((0x435, 0x43B, 0x447), 8),
}
_BLOB_FIELDS = {0x3EA, 0x436, 0x43C, 0x443}


def creator_fingerprint(
    *, loaded_at_ms: int, viewport_width: int, viewport_height: int
) -> bytes:
    if not 0 <= loaded_at_ms < 1 << 64:
        raise ValueError("RAP page load time must fit an unsigned 64-bit integer")
    if any(not 0 < value <= _MASK for value in (viewport_width, viewport_height)):
        raise ValueError("RAP viewport dimensions must fit positive 32-bit integers")
    flags = (
        *range(0x41B, 0x42A),
        0x42E,
        *range(0x42A, 0x42E),
        0x44C,
        *range(0x42F, 0x432),
    )
    fields: list[tuple[int, int | bytes]] = [
        (0x3EA, bytes(16)),
        (0x3EB, 0),
        *((tag, 0) for tag in flags),
        (0x433, 0),
        (0x434, 0),
        (0x435, 0),
        (0x436, b""),
        (0x43A, 0),
        (0x43B, 0),
        (0x43C, b""),
        (0x440, 0),
        (0x442, 0),
        (0x449, 0),
        (0x444, viewport_width),
        (0x446, viewport_height),
        (0x447, loaded_at_ms),
        (0x445, 0),
        (0x448, 0),
        (0x443, bytes.fromhex("001effff")),
        *((tag, 0) for tag in range(0x47F, 0x485)),
    ]
    output = bytearray()
    for tag, value in fields:
        output.extend(tag.to_bytes(2, "big"))
        if isinstance(value, bytes):
            output.extend(len(value).to_bytes(4, "big"))
            output.extend(value)
        else:
            output.extend(value.to_bytes(_FIELD_SIZES[tag], "big"))
    return bytes(output)


def xxhash32(data: bytes, seed: int = 0) -> int:
    p1, p2, p3, p4, p5 = _PRIMES
    size = len(data)
    offset = 0
    if size >= 16:
        lanes = [
            (seed + p1 + p2) & _MASK,
            (seed + p2) & _MASK,
            seed & _MASK,
            (seed - p1) & _MASK,
        ]
        while offset <= size - 16:
            for index, word in enumerate(struct.unpack_from("<4I", data, offset)):
                lanes[index] = (rotate32(lanes[index] + word * p2, 13) * p1) & _MASK
            offset += 16
        result = (
            sum(
                rotate32(value, shift)
                for value, shift in zip(lanes, (1, 7, 12, 18), strict=True)
            )
            & _MASK
        )
    else:
        result = (seed + p5) & _MASK
    result = (result + size) & _MASK
    while offset <= size - 4:
        word = struct.unpack_from("<I", data, offset)[0]
        result = (rotate32(result + word * p3, 17) * p4) & _MASK
        offset += 4
    for value in data[offset:]:
        result = (rotate32(result + value * p5, 11) * p1) & _MASK
    result = ((result ^ (result >> 15)) * p2) & _MASK
    result = ((result ^ (result >> 13)) * p3) & _MASK
    return (result ^ (result >> 16)) & _MASK


def _double(value: int) -> int:
    return ((value << 1) ^ (0x11B if value & 0x80 else 0)) & 255


def _round_keys() -> tuple[bytes, ...]:
    expanded = bytearray(b"kqI1DTcwKX90ZtAy")
    constant = 1
    for index in range(4, 44):
        word = expanded[-4:]
        if index % 4 == 0:
            word = bytearray(_SBOX[value] for value in word[1:] + word[:1])
            word[0] ^= constant
            constant = _double(constant)
        expanded.extend(
            expanded[4 * (index - 4) + offset] ^ value
            for offset, value in enumerate(word)
        )
    return tuple(bytes(expanded[offset : offset + 16]) for offset in range(0, 176, 16))


_ROUND_KEYS = _round_keys()


def encrypt_blocks(data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("RAP cipher input must align to 16 bytes")
    output = bytearray()
    for offset in range(0, len(data), 16):
        state = [
            value ^ key
            for value, key in zip(
                data[offset : offset + 16], _ROUND_KEYS[0], strict=True
            )
        ]
        for round_index in range(1, 11):
            substituted = [_SBOX[value] for value in state]
            state = [
                substituted[4 * ((column + row) % 4) + row]
                for column in range(4)
                for row in range(4)
            ]
            if round_index != 10:
                for column in range(0, 16, 4):
                    a, b, c, d = state[column : column + 4]
                    total = a ^ b ^ c ^ d
                    state[column : column + 4] = [
                        a ^ total ^ _double(a ^ b),
                        b ^ total ^ _double(b ^ c),
                        c ^ total ^ _double(c ^ d),
                        d ^ total ^ _double(d ^ a),
                    ]
            state = [
                value ^ key
                for value, key in zip(state, _ROUND_KEYS[round_index], strict=True)
            ]
        output.extend(state)
    return bytes(output)


def _random_ascii(size: int) -> bytes:
    return bytes(secrets.choice(_ALPHABET) for _ in range(size))


@dataclass(frozen=True, slots=True)
class RapContext:
    timestamp_ms: int
    fingerprint_id: bytes = field(default_factory=lambda: _random_ascii(16))
    mask: int = field(default_factory=lambda: secrets.choice(_ALPHABET))
    xor_key: bytes = field(default_factory=lambda: _random_ascii(16), repr=False)
    nonce: bytes = field(
        default_factory=lambda: _random_ascii(4 + secrets.randbelow(3))
    )
    padding: bytes = field(default_factory=lambda: _random_ascii(16), repr=False)
    mtime: int = 0
    encryption_time_ms: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.timestamp_ms < 1 << 64:
            raise ValueError("RAP timestamp must fit an unsigned 64-bit integer")
        if any(
            not 0 <= value <= _MASK for value in (self.mtime, self.encryption_time_ms)
        ):
            raise ValueError("RAP counters must fit unsigned 32-bit integers")
        if len(self.nonce) not in (4, 5, 6) or self.mask not in _ALPHABET:
            raise ValueError("Invalid RAP nonce or mask")
        if any(
            len(value) != 16
            for value in (self.fingerprint_id, self.xor_key, self.padding)
        ):
            raise ValueError("RAP keys, identifiers and padding require 16 bytes")
        if any(
            value not in _ALPHABET
            for part in (self.fingerprint_id, self.xor_key, self.nonce, self.padding)
            for value in part
        ):
            raise ValueError(
                "RAP random fields must contain lowercase alphanumeric ASCII"
            )


def build_payload(
    url: str, body: bytes, fingerprint: bytes, context: RapContext
) -> bytes:
    if not url.startswith("https://"):
        raise ValueError("Creator RAP requires an absolute HTTPS URL")
    if not 0 < len(fingerprint) <= 4096:
        raise ValueError("Invalid RAP fingerprint size")
    values = bytearray(fingerprint)
    offset = 0
    seen: set[int] = set()
    while offset < len(values):
        if offset + 2 > len(values):
            raise ValueError("Truncated RAP fingerprint tag")
        tag = int.from_bytes(values[offset : offset + 2], "big")
        offset += 2
        if tag in seen:
            raise ValueError("Duplicate RAP fingerprint tag")
        seen.add(tag)
        if tag in _BLOB_FIELDS:
            if offset + 4 > len(values):
                raise ValueError("Truncated RAP fingerprint length")
            size = int.from_bytes(values[offset : offset + 4], "big")
            offset += 4
        else:
            size = _FIELD_SIZES.get(tag, -1)
        if size < 0 or offset + size > len(values):
            raise ValueError("Unsupported or truncated RAP fingerprint field")
        if tag == 0x3EA:
            if size != 16:
                raise ValueError("RAP fingerprint identifier requires 16 bytes")
            values[offset : offset + size] = context.fingerprint_id
        elif tag == 0x3EB:
            values[offset : offset + size] = struct.pack(
                ">I", xxhash32(url.encode("utf-8") + body)
            )
        offset += size
    if not {0x3EA, 0x3EB} <= seen:
        raise ValueError("RAP fingerprint identifier and request hash are required")
    header = struct.pack(
        ">HQHI", 0x3E8, context.timestamp_ms, 0x3E9, xxhash32(bytes((context.mask,)))
    )
    return header + bytes(value ^ context.mask for value in values)


def _gzip(payload: bytes, mtime: int) -> bytes:
    compressor = zlib.compressobj(level=9, wbits=-15)
    compressed = compressor.compress(payload) + compressor.flush()
    return (
        b"\x1f\x8b\x08\x00"
        + struct.pack("<I", mtime)
        + b"\x00\x03"
        + compressed
        + struct.pack("<II", zlib.crc32(payload), len(payload))
    )


def sign_rap(url: str, body: bytes, fingerprint: bytes, context: RapContext) -> str:
    payload = build_payload(url, body, fingerprint, context)
    compressed = _gzip(payload, context.mtime)
    padded = compressed + context.padding[: 16 - len(compressed) % 16]
    encrypted = encrypt_blocks(
        bytes(value ^ context.xor_key[index % 16] for index, value in enumerate(padded))
    )
    key = encrypt_blocks(context.xor_key)
    tail = (
        context.nonce
        + key
        + struct.pack(">I", 16)
        + encrypted
        + struct.pack(">I", len(compressed))
    )
    header = struct.pack(
        ">4B8I",
        7,
        0x24,
        1,
        len(context.nonce),
        1,
        20,
        len(encrypted) + 4,
        xxhash32(tail),
        0x283D,
        context.encryption_time_ms,
        0,
        0,
    )
    return base64.b64encode(header + tail).decode("ascii")
