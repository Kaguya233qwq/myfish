import os
from pathlib import Path
from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # pyright: ignore[reportMissingImports]

class CustomWheelTagHook(BuildHookInterface):
    def initialize(self, version, build_data):
        target_plat = os.getenv("MYFISH_TARGET_PLATFORM")
        if target_plat:
            build_data["tag"] = f"py3-none-{target_plat}"
            
        libs_dir = Path("src/myfish/adapters/fish/libs")
        
        if libs_dir.exists():
            for lib_file in libs_dir.glob("*"):
                if lib_file.suffix in [".so", ".dll"]:
                    rel_path = str(lib_file)
                    target_path = f"myfish/adapters/fish/libs/{lib_file.name}"
                    build_data["force_include"][rel_path] = target_path
                    
                    print(f"[Hatch Hook] Include lib: {rel_path} -> {target_path}")