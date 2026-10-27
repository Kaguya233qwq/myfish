"""插件依赖的静态发现、锁定与独立运行环境管理。"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path

from .config import BotConfig


class DependencyError(RuntimeError):
    pass


def runtime_paths(root: Path) -> tuple[Path, Path]:
    environment = root / ".myfish" / "venv"
    executable = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return environment, environment / executable


def plugin_entries(directory: Path, configs: dict, log=None):
    """通用插件入口规则"""
    if not directory.is_dir():
        return
    for path in sorted(directory.iterdir()):
        if path.name.startswith(("_", ".")) or path.name.endswith(".config.py"):
            continue
        package = path.is_dir() and (path / "__init__.py").is_file()
        module = path.is_file() and path.suffix == ".py"
        if not (package or module):
            continue
        name = path.name if package else path.stem
        if module and (directory / name / "__init__.py").is_file():
            if log is not None:
                log.warning(f"同名插件包优先，跳过单文件插件: {path.name}")
            continue
        if configs.get(name, {}).get("enabled", True) is not False:
            yield name, path
        elif log is not None:
            log.info(f"插件已禁用: {directory.name}.{name}")


class PluginDependencies:
    def __init__(self, config: BotConfig, root: Path | None = None):
        self.root = (root or Path.cwd()).resolve()
        self.config = config
        self.package = Path(__file__).resolve().parent.parent
        self.directory = self.root / ".myfish"
        self.environment, self.python = runtime_paths(self.root)
        self.lock = self.directory / "requirements.lock"
        self.state = self.directory / "dependencies.json"

    def inputs(self) -> tuple[str, list[Path], str]:
        builtin = self.package / "builtin_plugins"
        external = Path(
            os.getenv("MYFISH_PLUGINS_DIR", str(self.root / "plugins"))
        ).resolve()
        extras = (
            ["xhs"]
            if any(
                adapter.id == "xhs" and adapter.enabled
                for adapter in self.config.adapters
            )
            else []
        )
        files = []
        entries = []
        for directory in (builtin, external):
            for name, path in plugin_entries(directory, self.config.plugins):
                entries.append(f"builtin:{name}" if directory == builtin else str(path))
                if directory == builtin and name == "fishy":
                    extras.append("fishy")
                    continue
                requirement = (
                    path / "requirements.txt"
                    if path.is_dir()
                    else path.with_suffix(".requirements.txt")
                )
                if requirement.is_file():
                    files.append(requirement)
        suffix = f"[{','.join(extras)}]" if extras else ""
        source = self.package.parent.parent
        project = source / "pyproject.toml"
        metadata = (
            tomllib.loads(project.read_text(encoding="utf-8"))
            if project.is_file()
            else {}
        )
        if metadata.get("project", {}).get("name") == "myfish":
            requirement = f"-e {source.as_uri()}{suffix}"
            framework = project.read_text(encoding="utf-8")
        else:
            framework = version("myfish")
            requirement = f"myfish{suffix}=={framework}"
        # 包含本地 -r/-c 引用，避免嵌套依赖变动后继续使用过期锁。
        contents = {}

        def collect(path: Path):
            path = path.resolve()
            if str(path) in contents:
                return
            content = path.read_text(encoding="utf-8")
            contents[str(path)] = content
            for line in content.splitlines():
                tokens = shlex.split(line, comments=True)
                if not tokens:
                    continue
                value = None
                if (
                    tokens[0] in ("-r", "-c", "--requirement", "--constraint")
                    and len(tokens) > 1
                ):
                    value = tokens[1]
                elif tokens[0].startswith(("--requirement=", "--constraint=")):
                    value = tokens[0].split("=", 1)[1]
                elif tokens[0].startswith(("-r", "-c")) and len(tokens[0]) > 2:
                    value = tokens[0][2:]
                if value:
                    if "://" in value:
                        raise DependencyError(
                            "依赖文件的 -r/-c 引用须为本地文件，以便检测依赖变化"
                        )
                    collect(path.parent / value)

        for path in files:
            collect(path)
        payload = [
            requirement,
            framework,
            entries,
            contents,
            platform.platform(),
            list(sys.version_info[:3]),
        ]
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        return requirement, files, fingerprint

    def uv(self, *args: str):
        executable = shutil.which("uv")
        if not executable:
            raise DependencyError("未找到 uv，请安装 uv 后执行 myfish sync")
        try:
            subprocess.run([executable, *map(str, args)], cwd=self.root, check=True)
        except subprocess.CalledProcessError as exc:
            raise DependencyError(
                f"依赖操作失败（退出码 {exc.returncode}），请检查上方 uv 输出"
            ) from exc

    def snapshot(self):
        result = subprocess.run(
            [
                str(self.python),
                "-c",
                "import importlib.metadata as m,json; print(json.dumps(sorted((d.metadata['Name'].lower().replace('_','-'), d.version) for d in m.distributions())))",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    def sync(self, upgrade: bool = False):
        self.directory.mkdir(parents=True, exist_ok=True)
        guard = self.directory / "syncing"
        try:
            guard.mkdir()
        except FileExistsError as exc:
            raise DependencyError(
                "同步正在进行；若之前进程异常退出，请确认无同步进程后删除 .myfish/syncing"
            ) from exc
        try:
            self._sync(upgrade)
        finally:
            guard.rmdir()

    def _sync(self, upgrade: bool = False):
        requirement, files, fingerprint = self.inputs()
        self.directory.mkdir(parents=True, exist_ok=True)
        inputs = self.directory / "requirements.in"
        inputs.write_text(requirement + "\n", encoding="utf-8")
        candidate = self.directory / "requirements.pending.txt"
        if self.lock.exists():
            shutil.copyfile(self.lock, candidate)
        reuse = False
        if self.state.exists() and self.lock.exists() and not upgrade:
            try:
                previous = json.loads(self.state.read_text(encoding="utf-8"))
                reuse = (
                    previous["fingerprint"] == fingerprint
                    and previous["lock"]
                    == hashlib.sha256(self.lock.read_bytes()).hexdigest()
                )
            except (ValueError, KeyError):
                pass
        args = [
            "pip",
            "compile",
            str(inputs),
            *map(str, files),
            "--python",
            sys.executable,
            "-o",
            str(candidate),
        ]
        if upgrade:
            args.append("--upgrade")
        try:
            if not reuse:
                self.uv(*args)
            # 解析失败不影响已有环境；安装开始后必须使旧成功标记失效。
            self.state.unlink(missing_ok=True)
            if not self.python.exists():
                self.uv("venv", str(self.environment), "--python", sys.executable)
            self.uv("pip", "sync", str(candidate), "--python", str(self.python))
            candidate.replace(self.lock)
            state = {
                "fingerprint": fingerprint,
                "lock": hashlib.sha256(self.lock.read_bytes()).hexdigest(),
                "packages": self.snapshot(),
            }
            temporary = self.state.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.replace(self.state)
        finally:
            candidate.unlink(missing_ok=True)

    def check(self):
        message = "插件依赖未同步或已变化，请先执行 myfish sync"
        if (self.directory / "syncing").exists():
            raise DependencyError("依赖正在同步，请等待完成后启动")
        try:
            _, _, fingerprint = self.inputs()
            state = json.loads(self.state.read_text(encoding="utf-8"))
            valid = (
                state["fingerprint"] == fingerprint
                and state["lock"] == hashlib.sha256(self.lock.read_bytes()).hexdigest()
                and state["packages"] == self.snapshot()
            )
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise DependencyError(message) from exc
        if not valid:
            raise DependencyError(message)
