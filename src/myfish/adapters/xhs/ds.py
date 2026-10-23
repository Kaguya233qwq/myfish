from __future__ import annotations

import hashlib
import math
import re
import struct
from dataclasses import dataclass

from .utils import rotate32

_MASK = 0xFFFFFFFF
_CONSTANTS = tuple(int(abs(math.sin(index + 1)) * (1 << 32)) for index in range(64))
_SHIFTS = (
    (7, 12, 17, 22) * 4 + (5, 9, 14, 20) * 4 + (4, 11, 16, 23) * 4 + (6, 10, 15, 21) * 4
)
_CODE_HASH = "b14ae561e778849815d63293c53b2d575fe2416f18f52d8b65280b9bcc06ef38"


def _string_pool(program: bytes, offset: int, mask: int) -> list[str]:
    count = int.from_bytes(program[offset : offset + 2], "big")
    offset += 2
    if count != 67:
        raise ValueError("Unsupported Creator DS constants")

    def read() -> int:
        nonlocal offset
        value = program[offset]
        offset += 1
        if value < 128:
            return value
        size = 2 if value >> 6 == 2 else 3
        value &= 63
        for _ in range(size - 1):
            value = (value << 8) | program[offset]
            offset += 1
        return value

    values = ["".join(chr(read() ^ mask) for _ in range(read())) for _ in range(count)]
    if offset != len(program):
        raise ValueError("Unexpected Creator DS trailing data")
    return values


@dataclass(frozen=True, slots=True)
class CreatorDS:
    anchor: str
    initial: tuple[int, int, int, int]

    @classmethod
    def from_script(cls, script: str) -> CreatorDS:
        if len(script) > 1_000_000:
            raise ValueError("Creator DS script exceeds size limit")
        anchor = re.search(
            r"function\s+getdss\s*\(\s*\)\s*\{\s*return\s+['\"](\d+)['\"]", script
        )
        programs = re.findall(r"['\"]([0-9a-fA-F]{1000,})['\"]", script)
        if anchor is None or len(programs) != 1:
            raise ValueError("Unsupported Creator DS script")
        try:
            program = bytes.fromhex(programs[0])
            size = int.from_bytes(program[24:28], "big")
            if program[:8] != b"VTKBBQFM" or size % 2:
                raise ValueError("Invalid Creator DS header")
            end = 28 + size // 2
            if hashlib.sha256(program[28:end]).hexdigest() != _CODE_HASH:
                raise ValueError("Unsupported Creator DS algorithm revision")
            mask = sum(
                (value & 3) << (2 * index) for index, value in enumerate(program[12:16])
            )
            pool = _string_pool(program, end, mask)
            expected = ["", *(str(value) for value in _CONSTANTS if value >= 1 << 31)]
            if pool[:45] != expected or pool[45:49] != ["6", "9", "8", "7"]:
                raise ValueError("Unsupported Creator DS round constants")
            if pool[57:] != [
                "length",
                "concat",
                "function",
                "106",
                "_dsl",
                "_dsf",
                "_dsn",
                "a1",
                "undefined",
                "Object",
            ]:
                raise ValueError("Unsupported Creator DS exports")
            words = [pool[index] + pool[index + 1] for index in range(49, 57, 2)]
            if any(re.fullmatch(r"[0-9a-fA-F]{8}", word) is None for word in words):
                raise ValueError("Invalid Creator DS initial state")
            initial = tuple(int(word, 16) for word in words)
        except (IndexError, OverflowError) as exc:
            raise ValueError("Truncated Creator DS program") from exc
        return cls(anchor[1], (initial[0], initial[1], initial[2], initial[3]))

    def digest(self, data: bytes) -> bytes:
        if len(data) != 24:
            raise ValueError("Creator DS input must contain exactly 24 bytes")
        words = struct.unpack("<8I", data + bytes(8)) + (
            128,
            35,
            73,
            153,
            250,
            17,
            256,
            122,
        )
        a, b, c, d = self.initial
        for index in range(64):
            if index < 16:
                mixed, word = (b & d) | (~b & c), index
            elif index < 32:
                mixed, word = (b & c) | (d & ~c), (5 * index + 1) % 16
            elif index < 48:
                mixed, word = b ^ d ^ c, (3 * index + 5) % 16
            else:
                mixed, word = d ^ (b | ~c), 7 * index % 16
            following = (
                b
                + rotate32(a + mixed + words[word] + _CONSTANTS[index], _SHIFTS[index])
            ) & _MASK
            a, b, c, d = d, following, b, c
        a, b, c, d = (
            (value + initial) & _MASK
            for value, initial in zip((a, b, c, d), self.initial, strict=True)
        )
        if (self.initial[0] + self.initial[3]) & 7 == 0:
            d = (d + rotate32(self.initial[2], 2)) & _MASK
        mix = rotate32(_CONSTANTS[40], 1)
        a = (a + mix) & _MASK
        d = (d + rotate32(mix, 3)) & _MASK
        return struct.pack("<4I", a, d, c, b)
