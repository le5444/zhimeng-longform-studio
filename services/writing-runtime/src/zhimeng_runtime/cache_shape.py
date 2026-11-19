"""每次调模型前给这次请求拍个「形状」指纹，和同一场对话上一次请求比，说得出提示缓存没命中是哪一段变了。

照 Reasonix（系统 / 工具 / 逐条累积的消息链哈希，5.6.8）和 Pi（``cache-stats.ts``）：只记事实，不改请求。
供应商的前缀缓存是按字节从头比的——工具定义在最前（OpenAI / Anthropic / xAI 都把工具排在消息前面），
然后是一条条消息。所以只要知道「上一次的哪一段原样留在这一次的开头」，就知道这次最多能命中多少：

- ``tools``：工具清单变了（换挡位、启停技能、技能目录变了），整段历史都不命中；
- ``first_message``：第一条（环境那段）变了，同上；
- ``tail``：只有上一次末尾那一两条没留下（每轮末尾那条「开着哪个文件」本来就不进历史），正常；
- ``diverged``：上一次从第 ``kept_messages + 1`` 条起没留下。看数字分两种：``kept_messages`` 只剩开头几条，是历史被改写了
  （比如历史窗口往后滑了一格），整段不命中；上一次丢的只是它那一轮自己的工具往来（织梦的历史只存每轮的回答文字，
  下一轮不带上一轮的工具调用和结果），开头大半照样命中；
- ``append_only``：上一次整个留在这一次开头，这次该几乎全命中——还没命中就是供应商那边的事（过期、换了机器）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# 上一次末尾这么多条没留下也算正常（每轮末尾那条「开着哪个文件 / 附上的文件」不进历史）。
TAIL_MESSAGES = 2
# 链上每一环只留这么多位十六进制，够区分，事件里也不占地方。
_DIGEST_CHARS = 16


@dataclass(frozen=True)
class RequestShape:
    tools: str
    chain: tuple[str, ...]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _tool_material(tool: Any) -> Any:
    dump = getattr(tool, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    return tool


def request_shape(messages: Sequence[Mapping[str, Any]], tools: Sequence[Any] = ()) -> RequestShape:
    """工具定义（按发出去的顺序）一个指纹；消息按顺序串成链，每一环 = 前一环 + 这一条的全部字段。"""

    tools_digest = hashlib.sha256(_canonical([_tool_material(tool) for tool in tools]).encode("utf-8")).hexdigest()[
        :_DIGEST_CHARS
    ]
    chain: list[str] = []
    previous = ""
    for message in messages:
        digest = hashlib.sha256((previous + _canonical(dict(message))).encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
        chain.append(digest)
        previous = digest
    return RequestShape(tools=tools_digest, chain=tuple(chain))


def compare_shapes(previous: RequestShape | None, current: RequestShape) -> dict[str, Any]:
    """这一次比上一次：上一次的开头原样留下了几条、从哪一条起变了、算哪一类原因。"""

    if previous is None:
        return {"reason": "first_request", "messages": len(current.chain)}
    kept = 0
    for before, now in zip(previous.chain, current.chain):
        if before != now:
            break
        kept += 1
    tools_changed = previous.tools != current.tools
    if tools_changed:
        reason = "tools"
    elif kept == len(previous.chain):
        reason = "append_only"
    elif kept == 0:
        reason = "first_message"
    elif len(previous.chain) - kept <= TAIL_MESSAGES:
        reason = "tail"
    else:
        reason = "diverged"
    return {
        "reason": reason,
        "tools_changed": tools_changed,
        "kept_messages": kept,
        "previous_messages": len(previous.chain),
        "messages": len(current.chain),
    }


__all__ = ["TAIL_MESSAGES", "RequestShape", "compare_shapes", "request_shape"]
