from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, List, Optional, Tuple, Union

from .rule import Rule, is_fullmatch, is_keywords, is_regex, is_startswith


@dataclass
class PluginMetadata:
    id: str
    name: str
    description: str
    version: str = "1.0.0"
    author: str = "Unknown"


class Plugin:
    def __init__(self, metadata: PluginMetadata):
        self.metadata = metadata
        self.message_handlers: List[dict] = []
        self.event_handlers: dict[str, list[dict]] = defaultdict(list)

    def on_message(self, rule: Optional[Union[Rule, Callable]] = None):
        """注册一个消息处理器
        rule参数可以是一个Rule对象或者一个可调用的函数
        """

        def decorator(func: Callable[..., Coroutine[Any, Any, None]]):
            final_rule = (
                Rule(rule) if callable(rule) and not isinstance(rule, Rule) else rule
            )
            self.message_handlers.append({"func": func, "rule": final_rule})
            return func

        return decorator

    def on_event(self, event_type: str, rule: Optional[Union[Rule, Callable]] = None):
        """注册一个特定类型的事件处理器
        rule参数可以是一个Rule对象或者一个可调用的函数
        """

        def decorator(func: Callable[..., Coroutine[Any, Any, None]]):
            final_rule = (
                Rule(rule) if callable(rule) and not isinstance(rule, Rule) else rule
            )

            # 将处理函数注册到指定的 event_type 槽位中
            self.event_handlers[event_type].append({"func": func, "rule": final_rule})

            return func

        return decorator

    def on_startswith(
        self,
        msg: Union[str, Tuple[str, ...]],
        rule: Optional[Union[Rule, Callable]] = None,
    ):
        """注册一个匹配消息开头的处理器"""
        match_rule = is_startswith(msg)
        final_rule = (
            Rule(rule) if callable(rule) and not isinstance(rule, Rule) else rule
        )
        combined_rule = (match_rule & final_rule) if final_rule else match_rule
        return self.on_message(rule=combined_rule)

    def on_fullmatch(
        self,
        msg: Union[str, Tuple[str, ...]],
        rule: Optional[Union[Rule, Callable]] = None,
    ):
        """注册一个完全匹配消息的处理器"""
        match_rule = is_fullmatch(msg)
        final_rule = (
            Rule(rule) if callable(rule) and not isinstance(rule, Rule) else rule
        )
        combined_rule = (match_rule & final_rule) if final_rule else match_rule
        return self.on_message(rule=combined_rule)

    def on_keywords(
        self,
        keywords: Union[str, Tuple[str, ...]],
        rule: Optional[Union[Rule, Callable]] = None,
    ):
        """注册一个包含关键字的处理器"""
        match_rule = is_keywords(keywords)
        final_rule = (
            Rule(rule) if callable(rule) and not isinstance(rule, Rule) else rule
        )
        combined_rule = (match_rule & final_rule) if final_rule else match_rule
        return self.on_message(rule=combined_rule)

    def on_regex(
        self, pattern: str, flags: int = 0, rule: Optional[Union[Rule, Callable]] = None
    ):
        """注册一个正则匹配的处理器"""
        match_rule = is_regex(pattern, flags)
        final_rule = (
            Rule(rule) if callable(rule) and not isinstance(rule, Rule) else rule
        )
        combined_rule = (match_rule & final_rule) if final_rule else match_rule
        return self.on_message(rule=combined_rule)
