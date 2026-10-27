from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import os
import sys
import types
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import ClassVar, Literal, TypeVar, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict

Kind = Literal["adapter", "plugin"]
Migration = Callable[[dict[str, object]], dict[str, object]]


class BaseConfig(BaseModel):
    """
    所有配置项的基类，需要显式继承
    """

    model_config = ConfigDict(hide_input_in_errors=True)
    comment: ClassVar[str | tuple[str, ...]] = ""


ConfigT = TypeVar("ConfigT", bound=BaseConfig)


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigSpec:
    kind: Kind
    name: str
    model: type[BaseConfig]
    default_enabled: bool = False
    migrate: Migration | None = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.name}"


class ConfigRegistry:
    _providers: ClassVar[dict[Path, list[ConfigSpec]]] = {}
    _loading: ClassVar[Path | None] = None
    _lock: ClassVar[RLock] = RLock()

    @classmethod
    def adapter(
        cls,
        name: str,
        *,
        default_enabled: bool = False,
        migrate: Migration | None = None,
    ) -> Callable[[type[ConfigT]], type[ConfigT]]:
        return cls._register("adapter", name, default_enabled, migrate)

    @classmethod
    def plugin(
        cls, name: str, *, migrate: Migration | None = None
    ) -> Callable[[type[ConfigT]], type[ConfigT]]:
        return cls._register("plugin", name, False, migrate)

    @classmethod
    def _register(
        cls, kind: Kind, name: str, enabled: bool, migrate: Migration | None
    ) -> Callable[[type[ConfigT]], type[ConfigT]]:
        def register(model: type[ConfigT]) -> type[ConfigT]:
            if not issubclass(model, BaseConfig):
                raise ConfigurationError("配置模型必须继承 BaseConfig")
            if not name or any(character in name for character in "/\\:"):
                raise ConfigurationError("配置 ID 不合法")
            source = cls._loading or Path(inspect.getfile(model)).resolve()
            with cls._lock:
                entries = cls._providers.setdefault(source, [])
                entry = ConfigSpec(kind, name, model, enabled, migrate)
                if entry not in entries:
                    entries.append(entry)
            return model

        return register

    @classmethod
    def discover(cls, plugins_dir: Path | None = None) -> list[ConfigSpec]:
        package = Path(__file__).resolve().parents[2]
        external = (
            plugins_dir or Path(os.getenv("MYFISH_PLUGINS_DIR", "plugins")).resolve()
        )
        providers: list[tuple[Path, str, Kind, str]] = []
        sources: tuple[tuple[Kind, Path, str], ...] = (
            ("adapter", package / "adapters", "myfish.adapters"),
            ("plugin", package / "builtin_plugins", "myfish.builtin_plugins"),
            ("plugin", external, ""),
        )
        for kind, directory, prefix in sources:
            if not directory.is_dir():
                continue
            for child in sorted(directory.iterdir()):
                if child.name.startswith(("_", ".")):
                    continue
                if child.is_dir() and (child / "__init__.py").is_file():
                    path = child / "config.py"
                    module = f"{prefix}.{child.name}.{path.stem}" if prefix else ""
                elif child.suffix == ".py" and not child.name.endswith(".config.py"):
                    if (directory / child.stem / "__init__.py").is_file():
                        continue
                    path = child.with_suffix(".config.py")
                    module = ""
                else:
                    continue
                if path.is_file() and cls._is_provider(path):
                    owner = child.stem if child.is_file() else child.name
                    providers.append((path.resolve(), module, kind, owner))
        with cls._lock:
            entries: dict[tuple[Kind, str], ConfigSpec] = {}
            for path, module, kind, owner in providers:
                if path not in cls._providers:
                    name = (
                        module
                        or "_myfish_config_"
                        + hashlib.sha256(str(path).encode()).hexdigest()[:20]
                    )
                    definition = importlib.util.spec_from_file_location(name, path)
                    if definition is None or definition.loader is None:
                        raise ConfigurationError(f"无法发现配置入口: {path}")
                    loaded = importlib.util.module_from_spec(definition)
                    previous = cls._loading
                    cls._loading = path
                    sys.modules[name] = loaded
                    try:
                        definition.loader.exec_module(loaded)
                    except Exception:  # noqa: BLE001
                        cls._providers.pop(path, None)
                        sys.modules.pop(name, None)
                        raise ConfigurationError(f"配置入口加载失败: {path}") from None
                    finally:
                        cls._loading = previous
                    cls._providers.setdefault(path, [])
                for spec in cls._providers[path]:
                    if spec.kind != kind or spec.name != owner:
                        raise ConfigurationError(
                            f"配置注册与组件入口不匹配: {spec.key}, {path}"
                        )
                    key = spec.kind, spec.name
                    if key in entries:
                        raise ConfigurationError(f"配置注册冲突: {spec.key}")
                    entries[key] = spec
            return list(entries.values())

    @staticmethod
    def _is_provider(path: Path) -> bool:
        text = path.read_text(encoding="utf-8")
        if "ConfigRegistry" not in text:
            return False
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            raise ConfigurationError(f"配置入口语法错误: {path}") from None
        names = {
            name.asname or name.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module in ("myfish.core.config", "myfish.core.config.registry")
            for name in node.names
            if name.name == "ConfigRegistry"
        }
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in names
            and node.func.attr in ("adapter", "plugin")
            for node in ast.walk(tree)
        )

    @classmethod
    def plugin_defaults(
        cls, specs: Sequence[ConfigSpec] | None = None
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for spec in specs if specs is not None else cls.discover():
            if spec.kind != "plugin":
                continue
            enabled = spec.model.model_fields.get("enabled")
            if enabled is None or enabled.is_required():
                raise ConfigurationError(f"插件配置缺少 enabled 默认值: {spec.name}")
            value = enabled.get_default(call_default_factory=True)
            if not isinstance(value, bool):
                raise ConfigurationError(
                    f"插件 enabled 默认值必须是布尔值: {spec.name}"
                )
            result[spec.name] = {"enabled": value}
        return result


def model_type(annotation: object) -> type[BaseConfig] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseConfig):
        return annotation
    if get_origin(annotation) in (types.UnionType, Union):
        models = [
            found
            for value in get_args(annotation)
            if (found := model_type(value)) is not None
        ]
        return models[0] if len(models) == 1 else None
    return None


def model_shape(
    model: type[BaseConfig], visited: frozenset[type[BaseConfig]] = frozenset()
) -> list[str]:
    if model in visited:
        return []
    visited = visited | {model}
    paths = []
    for name, definition in model.model_fields.items():
        key = definition.alias or name
        paths.append(key)
        child = model_type(definition.annotation)
        if child is not None:
            paths.extend(f"{key}.{path}" for path in model_shape(child, visited))
        elif get_origin(definition.annotation) is list:
            args = get_args(definition.annotation)
            child = model_type(args[0]) if args else None
            if child is not None:
                paths.extend(f"{key}[].{path}" for path in model_shape(child, visited))
    return paths


def block_comment(model: type[BaseConfig]) -> list[str]:
    values = (model.comment,) if isinstance(model.comment, str) else model.comment
    return [
        line.strip() for value in values for line in value.splitlines() if line.strip()
    ]
