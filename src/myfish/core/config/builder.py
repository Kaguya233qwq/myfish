from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import tomlkit
from pydantic import ValidationError
from tomlkit.items import AoT, Table

from .document import ConfigDocument, ConfigState, as_table
from .registry import (
    BaseConfig,
    ConfigRegistry,
    ConfigSpec,
    ConfigurationError,
    model_shape,
    model_type,
)


@dataclass
class CompiledConfig:
    text: str
    original: bytes | None
    state: ConfigState
    changes: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    backup: Path | None = None

    @property
    def changed(self) -> bool:
        return self.original is None or self.text.encode("utf-8") != self.original


class ConfigBuilder:
    def __init__(
        self,
        model: type[BaseConfig],
        specs: Sequence[ConfigSpec] | None = None,
        *,
        plugins_dir: Path | None = None,
    ) -> None:
        self.model = model
        self.specs = (
            list(specs) if specs is not None else ConfigRegistry.discover(plugins_dir)
        )

    def validate(self, data: Mapping[str, object]) -> list[str]:
        checks: list[tuple[str, type[BaseConfig], object]] = [
            ("root", self.model, data)
        ]
        adapters = data.get("adapters", [])
        plugins = data.get("plugins", {})
        for spec in self.specs:
            if spec.kind == "adapter" and isinstance(adapters, list):
                for index, entry in enumerate(adapters):
                    if (
                        isinstance(entry, Mapping)
                        and entry.get("id") == spec.name
                        and entry.get("enabled", True) is True
                    ):
                        checks.append(
                            (
                                f"adapters[{index}].config",
                                spec.model,
                                entry.get("config", {}),
                            )
                        )
            elif spec.kind == "plugin" and isinstance(plugins, Mapping):
                settings = plugins.get(spec.name)
                if (
                    isinstance(settings, Mapping)
                    and settings.get("enabled", True) is True
                ):
                    checks.append((f"plugins.{spec.name}", spec.model, settings))
        issues = []
        for prefix, model, settings in checks:
            try:
                model.model_validate(settings, context={"config_specs": self.specs})
            except ValidationError as exc:
                for error in exc.errors(include_input=False, include_url=False):
                    path = ".".join(str(part) for part in error["loc"])
                    issues.append(f"{prefix}.{path}: {error['type']}")
        return issues

    def compile(self, path: Path) -> CompiledConfig:
        path = path.resolve()
        original = path.read_bytes() if path.exists() else None
        state_path = path.with_name(path.name + ".schema.json")
        try:
            state = (
                ConfigState.model_validate_json(state_path.read_bytes())
                if state_path.exists()
                else ConfigState()
            )
        except ValidationError:
            raise ConfigurationError(f"配置结构记录无效: {state_path}") from None
        schemas = {
            **state.schemas,
            "root": model_shape(self.model),
            **{spec.key: model_shape(spec.model) for spec in self.specs},
        }
        document = ConfigDocument(
            self.model, original, state, ConfigState(schemas=schemas)
        )
        source = document.source
        if "plugins" not in source:
            source["plugins"] = tomlkit.table()
            document.changes.append("新增 root.plugins")
        document.merge(source, self.model, set(state.schemas.get("root", [])), "root")
        adapters = source.get("adapters")
        if not isinstance(adapters, AoT):
            if adapters is not None and list(adapters):
                raise ConfigurationError("adapters 必须是配置表数组")
            adapters = tomlkit.aot()
            source["adapters"] = adapters
        plugins = as_table(source.get("plugins"), "plugins")
        if source.get("plugins") is not plugins:
            source["plugins"] = plugins
        for spec in self.specs:
            targets: list[tuple[Table, str]] = []
            if spec.kind == "adapter":
                if not any(entry.get("id") == spec.name for entry in adapters):
                    entry = tomlkit.table()
                    entry.update(
                        {
                            "id": spec.name,
                            "enabled": spec.default_enabled
                            if original is None
                            else False,
                        }
                    )
                    entry["config"] = tomlkit.table()
                    adapters.append(entry)
                    definition = self.model.model_fields["adapters"]
                    args = get_args(definition.annotation)
                    wrapper = model_type(args[0]) if args else None
                    if wrapper is not None:
                        document.merge(
                            entry,
                            wrapper,
                            set(),
                            f"adapters[{len(adapters) - 1}]",
                        )
                    document.changes.append(f"新增适配器 {spec.name}")
                for index, entry in enumerate(adapters):
                    if entry.get("id") == spec.name:
                        value = as_table(
                            entry.get("config"), f"adapters[{index}].config"
                        )
                        if entry.get("config") is not value:
                            entry["config"] = value
                        document.describe(
                            entry, spec.model, f"{spec.name} 适配器配置", config=value
                        )
                        targets.append((value, f"adapters[{index}].config"))
            else:
                if spec.name not in plugins:
                    plugins[spec.name] = tomlkit.table()
                    document.changes.append(f"新增插件 {spec.name}")
                value = as_table(plugins[spec.name], f"plugins.{spec.name}")
                if plugins[spec.name] is not value:
                    plugins[spec.name] = value
                document.describe(value, spec.model, f"{spec.name} 插件配置")
                targets.append((value, f"plugins.{spec.name}"))
            for table, prefix in targets:
                if spec.migrate is not None:
                    try:
                        migrated = spec.migrate(dict(table.unwrap()))
                        if not isinstance(migrated, dict) or any(
                            not isinstance(key, str) for key in migrated
                        ):
                            raise TypeError(
                                "Migration must return a string-keyed mapping"
                            )
                        document.patch(table, migrated, prefix)
                    except Exception:  # noqa: BLE001
                        raise ConfigurationError(f"配置迁移失败: {spec.key}") from None
                document.merge(
                    table,
                    spec.model,
                    set(state.schemas.get(spec.key, [])),
                    prefix,
                )
        compiled = CompiledConfig(
            document.render(),
            original,
            document.state,
            changes=document.changes,
            notes=document.notes,
        )
        compiled.issues = self.validate(tomlkit.parse(compiled.text).unwrap())
        if compiled.changed and not compiled.changes:
            compiled.changes.append("更新配置说明")
        return compiled

    @staticmethod
    def _write(path: Path, data: bytes, mode: int = 0o600) -> None:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def sync(self, path: Path) -> CompiledConfig:
        path = path.resolve()
        compiled = self.compile(path)
        if compiled.issues:
            raise ConfigurationError("配置校验失败:\n" + "\n".join(compiled.issues))
        current = path.read_bytes() if path.exists() else None
        if current != compiled.original:
            raise ConfigurationError("配置在构建期间发生变化，请重试")
        path.parent.mkdir(parents=True, exist_ok=True)
        if compiled.changed:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            if compiled.original is not None:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
                compiled.backup = path.with_name(path.name + ".bak." + stamp)
                with compiled.backup.open("xb") as backup:
                    os.chmod(compiled.backup, 0o600)
                    backup.write(compiled.original)
                state_path = path.with_name(path.name + ".schema.json")
                if state_path.exists():
                    self._write(
                        compiled.backup.with_name(
                            compiled.backup.name + ".schema.json"
                        ),
                        state_path.read_bytes(),
                    )
            self._write(path, compiled.text.encode("utf-8"), mode)
        self._write(
            path.with_name(path.name + ".schema.json"),
            compiled.state.model_dump_json(indent=2).encode(),
        )
        return compiled
