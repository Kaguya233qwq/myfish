import hashlib
from collections.abc import Mapping
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_REFRESH_KEY = serialization.load_pem_public_key(b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLgd2OAkcGVtoE3ThUREbio0Eg
Uc/prcajMKXvkCKFCWhJYJcLkcM2DKKcSeFpD/j6Boy538YXnR6VhcuUJOhH2x71
nzPjfdTcqMz7djHum0qSZA0AyCBDABUqCrfNgCiJ00Ra7GmRj+YCK1NJEuewlb40
JNrRuoEUXpabUzGB8QIDAQAB
-----END PUBLIC KEY-----""")


def correspond_path(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        raise ValueError("Refresh timestamp must be positive")
    if not isinstance(_REFRESH_KEY, rsa.RSAPublicKey):
        raise TypeError("Invalid Cookie refresh key")
    return _REFRESH_KEY.encrypt(
        f"refresh_{timestamp_ms}".encode(),
        padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
        ),
    ).hex()


_MIXIN_ORDER = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


def sign_wbi(
    params: Mapping[str, str | int], img_key: str, sub_key: str, timestamp: int
) -> dict[str, str]:
    original = img_key + sub_key
    if len(img_key) != 32 or len(sub_key) != 32 or timestamp < 0:
        raise ValueError("Invalid WBI keys or timestamp")
    mixin = "".join(original[index] for index in _MIXIN_ORDER[:32])
    values = {
        key: "".join(char for char in str(value) if char not in "!'()*")
        for key, value in sorted({**params, "wts": timestamp}.items())
        if key != "w_rid"
    }
    values["w_rid"] = hashlib.md5((urlencode(values) + mixin).encode()).hexdigest()
    return values
