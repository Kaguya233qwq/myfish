import os
from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # pyright: ignore[reportMissingImports, reportUnknownVariableType]

class CustomWheelTagHook(BuildHookInterface):
    def initialize(self, version, build_data):
        target_plat = os.getenv("MYFISH_TARGET_PLATFORM")
        if target_plat:
            build_data["tag"] = f"py3-none-{target_plat}"