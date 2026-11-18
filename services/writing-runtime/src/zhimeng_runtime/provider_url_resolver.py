"""Provider 接口地址的统一解析入口。

作者会粘来各种形态的地址：`https://x.com`、`https://x.com/v1`、
`https://x.com/v1/chat/completions`，甚至直接给完整的模型列表地址。
这里把这些形态收敛成确定性的最终端点，供设置服务与 Agent 运行时共用。

分工说明：`provider_probe.py` 里另有一份更严格的 `normalize_base_url`
（https-only、拒绝 query/凭证），那是独立 CLI 能力探测工具的入口校验，
面向不信任的命令行输入；这里管的是作者界面配置与运行时的宽容解析。
两边用途不同，不要互相替换。
"""

from __future__ import annotations

CHAT_COMPLETIONS_PATH = "/chat/completions"
RESPONSES_PATH = "/responses"
MESSAGES_PATH = "/messages"
MODELS_PATH = "/models"

_PROTOCOL_PATHS = {
    "chat": CHAT_COMPLETIONS_PATH,
    "responses": RESPONSES_PATH,
    "anthropic_messages": MESSAGES_PATH,
}

_KNOWN_SUFFIXES = (CHAT_COMPLETIONS_PATH, RESPONSES_PATH, MESSAGES_PATH, MODELS_PATH)


def strip_known_suffixes(raw: str) -> str:
    """剥掉作者一起粘进来的已知端点后缀，返回干净的 base（可能为空）。"""
    value = (raw or "").strip().rstrip("/")
    for suffix in _KNOWN_SUFFIXES:
        if value.endswith(suffix):
            value = value[: -len(suffix)].rstrip("/")
    return value


def resolve_endpoint(
    base_url: str,
    protocol: str,
    *,
    explicit_url: str | None = None,
) -> str:
    """解析一次生成调用最终要打到的完整地址。

    * 显式地址（作者在模型级覆盖里直接给的完整端点）优先，原样使用。
    * 否则按协议在 base 后追加 `/chat/completions` 或 `/responses`；
      base 本身已以该端点结尾时不重复追加。
    """
    explicit = (explicit_url or "").strip()
    if explicit:
        return explicit
    path = _PROTOCOL_PATHS.get(protocol)
    if path is None:
        raise ValueError(f"unknown provider protocol: {protocol!r}")
    base = (base_url or "").strip().rstrip("/")
    if base.endswith(path):
        return base
    return f"{base}{path}"


def resolve_models_url(base_url: str, models_url: str | None = None) -> str:
    """模型列表地址：显式配置优先，否则 base + `/models`。"""
    explicit = (models_url or "").strip()
    if explicit:
        return explicit
    base = (base_url or "").strip().rstrip("/")
    if base.endswith(MODELS_PATH):
        return base
    return f"{base}{MODELS_PATH}"
