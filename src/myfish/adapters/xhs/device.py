import hashlib
import os
import platform
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil
from pydantic import JsonValue
from tzlocal import get_localzone

from .sign import encode_profile
from .utils import now_ms


def browser_memory() -> int | float:
    try:
        total = psutil.virtual_memory().total
    except (OSError, RuntimeError):
        return 8
    if total <= 0:
        return 8
    lower = 1 << (total.bit_length() - 1)
    rounded = lower if total - lower <= lower * 2 - total else lower * 2
    gib = min(8.0, max(0.25, rounded / 1024**3))
    return int(gib) if gib >= 1 else gib


def local_timezone() -> tuple[str, int]:
    try:
        zone = get_localzone()
        offset = datetime.now(zone).utcoffset()
        if offset is not None:
            return zone.key, -int(offset.total_seconds() // 60)
    except (OSError, ValueError, KeyError, RuntimeError):
        pass
    return "UTC", 0


def screen_size(system: str) -> tuple[int, int]:
    if system == "Linux":
        for connector in sorted(Path("/sys/class/drm").glob("card*-*")):
            try:
                if (connector / "status").read_text().strip() != "connected":
                    continue
                mode = (connector / "modes").read_text().splitlines()[0]
                width, height = map(int, mode.split("x"))
                if width > 0 and height > 0:
                    return width, height
            except (OSError, ValueError, IndexError):
                continue
    elif system in {"Windows", "Darwin"}:
        try:
            import tkinter
        except ImportError:
            return 1920, 1080
        try:
            root = tkinter.Tk()
            try:
                root.withdraw()
                width, height = root.winfo_screenwidth(), root.winfo_screenheight()
                if width > 0 and height > 0:
                    return width, height
            finally:
                root.destroy()
        except tkinter.TclError:
            pass
    return 1920, 1080


def cpu_cores() -> int:
    return max(1, os.cpu_count() or 8)


LEGACY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36 Edg/142.0.0.0"
)


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    user_agent: str = LEGACY_USER_AGENT
    screen_width: int = 1920
    screen_height: int = 1080
    available_width: int = 1920
    available_height: int = 1040
    color_depth: int = 24
    memory_gb: int | float = 8
    cpu_cores: int = 8
    timezone: str = "Asia/Shanghai"
    timezone_offset: int = -480
    graphics_vendor: str = "Google Inc. (Intel)"
    graphics_renderer: str = "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)"

    @property
    def platform(self) -> str:
        if "Windows NT" in self.user_agent:
            return "Windows"
        if "Linux" in self.user_agent:
            return "Linux"
        if "Macintosh" in self.user_agent:
            return "Mac OS"
        raise ValueError("Unsupported browser platform")

    @classmethod
    def from_environment(cls) -> "DeviceProfile":
        system = platform.system()
        machine = platform.machine().lower()
        if system == "Darwin":
            browser_os = "Macintosh; Intel Mac OS X 10_15_7"
        elif system == "Linux":
            architecture = {
                "aarch64": "aarch64",
                "arm64": "aarch64",
                "armv7l": "armv7l",
                "i386": "i686",
                "i686": "i686",
            }.get(machine, "x86_64")
            browser_os = f"X11; Linux {architecture}"
        else:
            browser_os = "Windows NT 10.0; Win64; x64"
        width, height = screen_size(system)
        timezone, offset = local_timezone()
        return cls(
            user_agent=(
                f"Mozilla/5.0 ({browser_os}) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
            ),
            screen_width=width,
            screen_height=height,
            available_width=width,
            available_height=height,
            memory_gb=browser_memory(),
            cpu_cores=cpu_cores(),
            timezone=timezone,
            timezone_offset=offset,
            graphics_vendor="Google Inc. (Google)",
            graphics_renderer=(
                "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) "
                "(0x0000C0DE)), SwiftShader driver)"
            ),
        )

    @property
    def navigator_platform(self) -> str:
        if self.platform == "Windows":
            return "Win32"
        if self.platform == "Mac OS":
            return "MacIntel"
        return self.user_agent.partition("X11; ")[2].partition(")")[0] or "Linux x86_64"

    def __post_init__(self) -> None:
        if (
            not 0 < self.available_width <= self.screen_width
            or not 0 < self.available_height <= self.screen_height
        ):
            raise ValueError("Available screen dimensions must fit the screen")
        if min(self.color_depth, self.memory_gb, self.cpu_cores) <= 0:
            raise ValueError("Device capabilities must be positive")
        if self.platform not in {"Windows", "Linux", "Mac OS"}:
            raise ValueError("Unsupported browser platform")


def generate_fields(
    profile: DeviceProfile, a1: str, timestamp_ms: int
) -> dict[str, JsonValue]:
    identity = f"{a1}:{profile.graphics_vendor}:{profile.graphics_renderer}".encode()
    webgl = hashlib.md5(b"webgl:" + identity).hexdigest()
    canvas = hashlib.md5(b"canvas:" + identity).hexdigest()
    return {
        "x1": profile.user_agent,
        "x2": "false",
        "x3": "zh-CN",
        "x4": str(profile.color_depth),
        "x5": str(profile.memory_gb),
        "x6": "24",
        "x7": f"{profile.graphics_vendor},{profile.graphics_renderer}",
        "x8": str(profile.cpu_cores),
        "x9": f"{profile.screen_width};{profile.screen_height}",
        "x10": f"{profile.available_width};{profile.available_height}",
        "x11": str(profile.timezone_offset),
        "x12": profile.timezone,
        "x13": "true",
        "x14": "true",
        "x15": "true",
        "x16": "false",
        "x17": "false",
        "x18": "un",
        "x19": profile.navigator_platform,
        "x20": "",
        "x21": "PDF Viewer,Chrome PDF Viewer,Chromium PDF Viewer,Microsoft Edge PDF Viewer,WebKit built-in PDF",
        "x22": webgl,
        "x23": "false",
        "x24": "false",
        "x25": "false",
        "x26": "false",
        "x27": "false",
        "x28": "0,false,false",
        "x29": "4,7,8",
        "x30": "swf object not loaded",
        "x31": "124.04347527516074",
        "x33": "0",
        "x34": "0",
        "x35": "0",
        "x36": "2",
        "x37": "0|0|0|0|0|0|0|0|0|1|0|0|0|0|0|0|0|0|1|0|0|0|0|0",
        "x38": "0|0|1|0|1|0|0|0|0|0|1|0|1|0|1|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0|0",
        "x39": 0,
        "x40": "0",
        "x41": "0",
        "x42": "3.4.4",
        "x43": canvas,
        "x44": str(timestamp_ms),
        "x45": "__SEC_CAV__1-1-1-1-1|__SEC_WSA__|",
        "x46": "false",
        "x47": "1|0|0|0|0|0",
        "x48": "",
        "x49": "{list:[],type:}",
        "x50": "",
        "x51": "",
        "x52": "",
        "x53": hashlib.md5(b"device:" + identity).hexdigest(),
        "x54": "11311144241322244122",
        "x55": "380,380,360,400,380,400,420,380,400,400,360,360,440,420",
        "x56": f"{profile.graphics_vendor}|{profile.graphics_renderer}|{webgl}|35",
        "x57": "",
        "x58": "180",
        "x59": "2",
        "x60": "63",
        "x61": "1291",
        "x62": "2047",
        "x63": "0",
        "x64": "0",
        "x65": "0",
        "x66": {
            "referer": "",
            "location": "https://www.xiaohongshu.com/explore",
            "frame": 0,
        },
        "x67": "1|0",
        "x68": "0",
        "x69": "326|1292|30",
        "x70": ["location"],
        "x71": "true",
        "x72": "complete",
        "x73": "1191",
        "x74": "0|0|0",
        "x75": "Google Inc.",
        "x76": "true",
        "x77": "1|1|1|1|1|1|1|1|1|1",
        "x78": {
            "x": 0,
            "y": 2400,
            "left": 0,
            "right": 290.828125,
            "bottom": 2418,
            "height": 18,
            "top": 2400,
            "width": 290.828125,
            "font": "Arial, sans-serif",
        },
        "x79": "144|599565058866",
        "x80": "1|[object FileSystemDirectoryHandle]",
        "x82": "_0x17a2|_0x1954",
    }


@dataclass(slots=True)
class DeviceFingerprint:
    fields: dict[str, JsonValue]

    @classmethod
    def create(
        cls,
        cookies: dict[str, str],
        *,
        profile: DeviceProfile | None = None,
        timestamp_ms: int | None = None,
    ) -> "DeviceFingerprint":
        timestamp = now_ms() if timestamp_ms is None else timestamp_ms
        fingerprint = cls(
            generate_fields(profile or DeviceProfile(), cookies["a1"], timestamp)
        )
        fingerprint.update(cookies, timestamp)
        return fingerprint

    def update(self, cookies: dict[str, str], timestamp_ms: int | None = None) -> None:
        hidden = {"acw_tc", "web_session", "secure_session", "id_token"}
        self.fields["x44"] = str(now_ms() if timestamp_ms is None else timestamp_ms)
        self.fields["x57"] = "; ".join(
            f"{key}={value}" for key, value in cookies.items() if key not in hidden
        )
        self.fields["x66"] = {
            "referer": "",
            "location": "https://www.xiaohongshu.com/explore",
            "frame": 0,
        }

    def profile_data(self) -> str:
        return encode_profile(self.fields)


def generate_telemetry(loaded_at_ms: int) -> str:
    if loaded_at_ms < 0:
        raise ValueError("Page load time must be nonnegative")
    return (
        "{mt:{to:" + str(loaded_at_ms) + "},m:{},k:{},"
        "p:{ulr:0,rs:1,ps:1,f:1},st:{h:0,f:1,kr:0},"
        "ft:{ae:null,ak:null,cdr:null,bf:null,fi:null}}"
    )
