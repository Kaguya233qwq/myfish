import asyncio
import ctypes
import json
from pathlib import Path
import platform
import random
import time

__all__ = [
    "generate_sign",
    "generate_uuid",
    "generate_device_id",
    "decrypt",
    "generate_mid",
]


def _load_engine():
    """
    根据当前操作系统和架构动态加载对应的核心加密算法库文件
    Windows -> sign_core_win.dll
    Linux (amd64) -> sign_core_linux.so
    Linux (arm64) -> sign_core_linux_arm64.so
    """
    base_dir = Path(__file__).resolve().parent / "libs"
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        lib_name = "sign_core_win.dll"

    elif system == "Linux":
        if "arm" in machine or "aarch64" in machine:
            lib_name = "sign_core_linux_arm64.so"
        else:
            lib_name = "sign_core_linux.so"

    elif system == "Darwin":
        raise OSError(
            "抱歉，Myfish的sign核心引擎暂不支持原生macOS系统。\n"
            "Mac用户请使用Docker运行，或在Linux服务器上部署。"
        )
    else:
        raise OSError(f"不支持的操作系统: {system}")

    lib_path = base_dir / lib_name

    if not lib_path.exists():
        raise FileNotFoundError(f"找不到sign核心文件: {lib_name}\n")

    return ctypes.cdll.LoadLibrary(str(lib_path))


engine = _load_engine()

engine.GenerateSign.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]
engine.GenerateSign.restype = ctypes.c_void_p

engine.GenerateMid.argtypes = []
engine.GenerateMid.restype = ctypes.c_void_p

engine.GenerateMidNoSleep.argtypes = []
engine.GenerateMidNoSleep.restype = ctypes.c_void_p

engine.GenerateUUID.argtypes = []
engine.GenerateUUID.restype = ctypes.c_void_p

engine.GenerateDeviceID.argtypes = [ctypes.c_char_p]
engine.GenerateDeviceID.restype = ctypes.c_void_p

engine.Decrypt.argtypes = [ctypes.c_char_p]
engine.Decrypt.restype = ctypes.c_void_p

engine.FreeString.argtypes = [ctypes.c_void_p]
engine.FreeString.restype = None


def _call_c_func(func, *args) -> str:
    c_args = [arg.encode("utf-8") if isinstance(arg, str) else arg for arg in args]
    c_ptr = func(*c_args)

    if not c_ptr:
        return ""

    result = ctypes.string_at(c_ptr).decode("utf-8")
    engine.FreeString(c_ptr)

    return result


def generate_sign(t: str, token: str, data: str) -> str:
    """生成 Sign"""
    return _call_c_func(engine.GenerateSign, t, token, data)


async def generate_mid() -> str:
    """生成MID"""
    # return await asyncio.to_thread(_call_c_func, engine.GenerateMid)
    await asyncio.sleep(random.uniform(0.035, 0.045))
    return _call_c_func(engine.GenerateMidNoSleep)


def generate_uuid() -> str:
    """生成 UUID"""
    return _call_c_func(engine.GenerateUUID)


def generate_device_id(user_id: str) -> str:
    """生成 DeviceID"""
    return _call_c_func(engine.GenerateDeviceID, user_id)


def decrypt(b64_str: str) -> dict:
    """解密消息体"""
    json_str = _call_c_func(engine.Decrypt, b64_str)
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {}


if __name__ == "__main__":
    print("测试sign生成..")
    sign = generate_sign("test_timestamp", "test_token", "test_data")
    print(f"Sign: {sign[:50]}...")

    print("测试uuid生成...")
    uuid_res = generate_uuid()
    print(f"UUID: {uuid_res}")

    print("测试mid生成...")
    start = time.time()
    mid_res = asyncio.run(generate_mid())
    print(f"MID: {mid_res} (costs: {time.time() - start:.3f}s)")
