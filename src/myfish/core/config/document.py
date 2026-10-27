from __future__ import annotations

import copy
import enum
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, Union, get_args, get_origin, overload

import tomlkit
from pydantic import BaseModel, Field, SecretBytes, SecretStr
from tomlkit.container import Container
from tomlkit.items import (
    AoT,
    Comment,
    InlineTable,
    Item,
    Key,
    Null,
    Table,
    Trivia,
    Whitespace,
)
from tomlkit.toml_document import TOMLDocument

from .registry import BaseConfig, ConfigurationError, block_comment, model_type

_SEPARATOR = "#" * 21


class ConfigState(BaseConfig):
    version: Literal[1] = 1
    schemas: dict[str, list[str]] = Field(default_factory=dict)
    block_comments: list[list[str]] = Field(default_factory=list)
    inline_comments: dict[str, str] = Field(default_factory=dict)


def _value(value: object) -> object:
    if isinstance(value, enum.Enum):
        return _value(value.value)
    if isinstance(value, (SecretStr, SecretBytes)):
        secret = value.get_secret_value()
        return secret.decode() if isinstance(secret, bytes) else secret
    if isinstance(value, BaseModel):
        return _value(value.model_dump(mode="python", by_alias=True))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _value(item) for key, item in value.items() if item is not None
        }
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def _dictionary(annotation: object) -> bool:
    if get_origin(annotation) is dict:
        return True
    return get_origin(annotation) in (types.UnionType, Union) and any(
        _dictionary(value) for value in get_args(annotation)
    )


def as_table(value: object, path: str) -> Table:
    if isinstance(value, Table):
        return value
    if isinstance(value, InlineTable):
        result = tomlkit.table()
        result.update(value.unwrap())
        result.trivia.comment = value.trivia.comment
        return result
    raise ConfigurationError(f"配置块必须是表: {path}")


class ConfigDocument:
    def __init__(
        self,
        model: type[BaseConfig],
        original: bytes | None,
        previous: ConfigState,
        state: ConfigState,
    ) -> None:
        self.model = model
        self.source = (
            tomlkit.parse(original.decode("utf-8"))
            if original is not None
            else tomlkit.document()
        )
        self.state = state
        self.changes: list[str] = []
        self.notes: list[str] = []
        self._models: dict[int, type[BaseConfig]] = {}
        self._headings: dict[int, list[str]] = {}
        self._hidden_headings: set[int] = set()
        self._dictionary_comments: dict[tuple[int, str], list[Comment]] = {}
        self._previous = previous
        definition = model.model_fields.get("adapters")
        args = get_args(definition.annotation) if definition is not None else ()
        self._adapter_model = model_type(args[0]) if args else None

    def describe(
        self,
        table: Table,
        model: type[BaseConfig],
        fallback: str,
        *,
        config: Table | None = None,
    ) -> None:
        self._headings[id(table)] = block_comment(model) or [fallback]
        if config is not None:
            self._hidden_headings.add(id(config))

    def render(self) -> str:
        return tomlkit.dumps(self._annotate(self.source))

    def patch(self, table: Table, values: Mapping[str, object], path: str) -> None:
        original = table.unwrap()
        for key in list(table):
            if key not in values:
                del table[key]
                self.changes.append(f"迁移删除 {path}.{key}")
        for key, value in values.items():
            current = table.get(key)
            if isinstance(current, Table) and isinstance(value, dict):
                self.patch(current, value, f"{path}.{key}")
            elif key not in original or original[key] != value:
                table[key] = tomlkit.item(_value(value))
                self.changes.append(f"迁移 {path}.{key}")

    def merge(
        self,
        table: TOMLDocument | Table,
        model: type[BaseConfig],
        previous: set[str],
        path: str,
    ) -> None:
        self._models[id(table)] = model
        fields = {
            definition.alias or name: definition
            for name, definition in model.model_fields.items()
        }
        for key in sorted(
            key for key in previous if "." not in key and "[]" not in key
        ):
            if key not in fields and key in table:
                del table[key]
                self.changes.append(f"删除 {path}.{key}")
        for key, definition in fields.items():
            extra = definition.json_schema_extra
            renamed = (
                extra.get("x-myfish-renamed-from") if isinstance(extra, dict) else None
            )
            if isinstance(renamed, str) and renamed in table:
                if key in table:
                    raise ConfigurationError(
                        f"新旧配置字段同时存在: {path}.{key}, {renamed}"
                    )
                table[key] = table.pop(renamed)
                self.changes.append(f"迁移 {path}.{renamed} -> {key}")
            if key not in table and not definition.is_required():
                default = definition.get_default(call_default_factory=True)
                if default is not None:
                    table[key] = tomlkit.item(_value(default))
                    self.changes.append(f"新增 {path}.{key}")
            if key not in table:
                continue
            child = model_type(definition.annotation)
            if child is not None:
                value = as_table(table[key], f"{path}.{key}")
                if table[key] is not value:
                    table[key] = value
                old = {
                    name[len(key) + 1 :]
                    for name in previous
                    if name.startswith(key + ".")
                }
                self.merge(value, child, old, f"{path}.{key}")
            elif get_origin(definition.annotation) is list:
                args = get_args(definition.annotation)
                child = model_type(args[0]) if args else None
                array = table[key]
                if child is not None and isinstance(array, AoT):
                    old = {
                        name[len(key) + 3 :]
                        for name in previous
                        if name.startswith(key + "[].")
                    }
                    for index, item in enumerate(array):
                        self.merge(item, child, old, f"{path}.{key}[{index}]")
            elif _dictionary(definition.annotation) and not (
                model is self.model
                and key == "plugins"
                or model is self._adapter_model
                and key == "config"
            ):
                current = table[key]
                if isinstance(current, Table):
                    self._dictionary_comments[id(table), key] = self._collect_comments(
                        current
                    )
                    inline = tomlkit.inline_table()
                    inline.update(current.unwrap())
                    inline.trivia.comment = current.trivia.comment
                    inline.trivia.comment_ws = " " if current.trivia.comment else ""
                    table[key] = inline
        for key in table:
            if key not in fields:
                self.notes.append(f"保留未识别字段 {path}.{key}")

    def _clean_body(
        self, body: Sequence[tuple[Key | None, Item]]
    ) -> list[tuple[Key | None, Item]]:
        result: list[tuple[Key | None, Item]] = []
        index = 0
        while index < len(body):
            matched = None
            for group in self._previous.block_comments:
                items = body[index : index + len(group)]
                if (
                    group
                    and len(items) == len(group)
                    and all(
                        key is None
                        and isinstance(item, Comment)
                        and item.as_string().strip() == line
                        for (key, item), line in zip(items, group, strict=True)
                    )
                ):
                    matched = group
                    break
            if matched is not None:
                if (
                    matched[0] == _SEPARATOR
                    and result
                    and isinstance(result[-1][1], Whitespace)
                ):
                    key, whitespace = result.pop()
                    text = whitespace.as_string()
                    text = (
                        text[:-2] if text.endswith("\r\n") else text.removesuffix("\n")
                    )
                    if text:
                        result.append((key, tomlkit.ws(text)))
                index += len(matched)
            else:
                result.append(body[index])
                index += 1
        return result

    def _collect_comments(self, source: Table) -> list[Comment]:
        result: list[Comment] = []
        for _, item in self._clean_body(source.value.body):
            if isinstance(item, Comment):
                result.append(item)
            elif isinstance(item, Table):
                result.extend(self._collect_comments(item))
            elif isinstance(item, AoT):
                for entry in item:
                    result.extend(self._collect_comments(entry))
            elif not isinstance(item, (Whitespace, Null)) and item.trivia.comment:
                result.append(
                    tomlkit.comment(item.trivia.comment.removeprefix("#").strip())
                )
        return result

    def _record_comments(self, lines: list[str]) -> None:
        if lines and lines not in self.state.block_comments:
            self.state.block_comments.append(lines)

    def _field_comment(self, item: Item, description: str, path: str) -> None:
        previous = self._previous.inline_comments.get(path)
        current = item.trivia.comment.removeprefix("#").strip()
        if previous and (current == previous or current.startswith(previous + "；")):
            current = current[len(previous) :].removeprefix("；")
        description = " ".join(description.splitlines()).strip()
        if not previous and description and current == description:
            return
        if description:
            comment = description + ("；" + current if current else "")
            item.comment(comment)
            item.trivia.comment_ws = " "
            self.state.inline_comments[path] = description
        elif previous:
            item.trivia.comment = "# " + current if current else ""
            item.trivia.comment_ws = " " if current else ""

    @overload
    def _annotate(
        self, source: TOMLDocument, *, path: str = "root"
    ) -> TOMLDocument: ...

    @overload
    def _annotate(self, source: Table, *, path: str = "root") -> Table: ...

    def _annotate(
        self, source: TOMLDocument | Table, *, path: str = "root"
    ) -> TOMLDocument | Table:
        model = self._models.get(id(source))
        fields = (
            {
                definition.alias or name: definition
                for name, definition in model.model_fields.items()
            }
            if model is not None
            else {}
        )
        heading = self._headings.get(id(source))
        if (
            heading is None
            and model is not None
            and id(source) not in self._hidden_headings
        ):
            heading = block_comment(model)
        if path == "root" and not heading:
            heading = ["全局配置"]
        lines = [_SEPARATOR, *("# " + line for line in heading)] if heading else []
        self._record_comments(lines)
        if isinstance(source, TOMLDocument):
            result: TOMLDocument | Table = TOMLDocument(parsed=True)
        else:
            trivia = copy.copy(source.trivia)
            if source.is_super_table():
                trivia.comment = ""
                trivia.comment_ws = ""
            if lines:
                trivia.indent = "\n" + "\n".join(lines) + "\n"
            elif path == "root.plugins" and not trivia.comment:
                trivia.indent = ""
            result = Table(
                Container(parsed=True),
                trivia,
                source.is_aot_element(),
                True
                if path == "root.plugins" and not trivia.comment
                else source.is_super_table(),
                source.name,
                source.display_name,
            )
        append = result.raw_append if isinstance(result, Table) else result.append
        if isinstance(source, TOMLDocument):
            for line in lines:
                append(None, Comment(Trivia(comment=line)))
        for name, definition in fields.items():
            if definition.is_required() and name not in source:
                description = definition.description or ""
                required = (
                    f"必填字段 {name}" + (f"：{description}" if description else "")
                ).splitlines()
                comments = ["# " + line for line in required]
                self._record_comments(comments)
                for line in comments:
                    append(None, Comment(Trivia(comment=line)))
        body = source.body if isinstance(source, TOMLDocument) else source.value.body
        for key, item in self._clean_body(body):
            name = key.key if key is not None else ""
            if key is not None:
                for comment in self._dictionary_comments.get((id(source), name), []):
                    append(None, comment)
            if isinstance(item, Table):
                item = self._annotate(item, path=f"{path}.{name}")
            elif isinstance(item, AoT):
                entries: list[Table] = []
                for index, entry in enumerate(item):
                    rendered = self._annotate(entry, path=f"{path}.{name}[{index}]")
                    entries.append(rendered)
                item = AoT(entries, parsed=True)
            if key is not None and not isinstance(item, (Table, AoT)):
                definition = fields.get(name)
                if definition is not None:
                    self._field_comment(
                        item, definition.description or "", f"{path}.{name}"
                    )
            append(key, item)
        return result
