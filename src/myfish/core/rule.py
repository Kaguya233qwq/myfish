import re
from typing import Callable, Union, Tuple, Any
from .event import MessageEvent


class Rule:
    """
    规则类
    """

    def __init__(self, checker: Callable[[Any], bool]):
        self.checker = checker

    def __call__(self, event: MessageEvent) -> bool:
        return self.checker(event)

    def __and__(self, other: "Rule") -> "Rule":
        return Rule(lambda event: self(event) and other(event))

    def __or__(self, other: "Rule") -> "Rule":
        return Rule(lambda event: self(event) or other(event))


def _get_text(event: MessageEvent) -> str:
    """内部辅助函数：从 Event 中提取字符串，请根据你的真实 Event 结构修改"""
    return event.plain_text


def is_startswith(msg: Union[str, Tuple[str, ...]]) -> Rule:
    """匹配消息开头"""
    return Rule(lambda event: _get_text(event).startswith(msg))


def is_fullmatch(msg: Union[str, Tuple[str, ...]]) -> Rule:
    """完全匹配消息"""
    if isinstance(msg, str):
        msg = (msg,)
    return Rule(lambda event: _get_text(event) in msg)


def is_keywords(keyword: Union[str, Tuple[str, ...]]) -> Rule:
    """包含关键字"""
    if isinstance(keyword, str):
        keyword = (keyword,)
    return Rule(lambda event: any(kw in _get_text(event) for kw in keyword))


def is_regex(pattern: str, flags: int = 0) -> Rule:
    """正则匹配消息"""
    compiled = re.compile(pattern, flags)
    return Rule(lambda event: bool(compiled.search(_get_text(event))))
