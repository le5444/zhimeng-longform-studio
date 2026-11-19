"""A per-provider-turn MCP catalog: the schema, version and dispatch stay together."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from jsonschema.validators import validator_for

from zhimeng_runtime.agent_provider import ProviderToolDefinition
from zhimeng_runtime.secret_mask import mask_secrets


def tool_alias(connection_id: str, raw_name: str) -> str:
    """Opaque stable identity; display names, discovery order and slug collisions do not matter."""
    digest = hashlib.sha256(json.dumps([connection_id, raw_name], ensure_ascii=False).encode()).hexdigest()[:32]
    readable = re.sub(r"[^a-zA-Z0-9_-]", "_", raw_name).strip("_")[:25] or "tool"
    return f"mcp__{readable}_{digest}"


@dataclass(frozen=True)
class McpBoundTool:
    source: Any
    definition: ProviderToolDefinition


class McpToolAdapter:
    def __init__(self, manager: Any):
        self.manager = manager
        self._bound: ContextVar[dict[str, McpBoundTool]] = ContextVar(f"mcp_catalog_{id(self)}", default={})
        self._read_only: ContextVar[bool] = ContextVar(f"mcp_read_only_{id(self)}", default=False)

    @staticmethod
    def definition(source: Any) -> ProviderToolDefinition:
        schema = deepcopy(source.input_schema)
        # Function arguments are objects. An omitted root type in an otherwise
        # valid schema needs that fact for the existing provider-neutral contract.
        schema.setdefault("type", "object")
        if schema["type"] != "object":
            raise ValueError("MCP 工具的 inputSchema 根类型需要是 object")
        validator_for(schema).check_schema(schema)
        description = str(source.description or source.raw_name).strip() or source.raw_name
        label = getattr(source, "title", None) or source.raw_name
        prefix = f"[{source.server_name[:80]}] {label[:160]}\n"
        description = prefix + description
        if len(description) > 4096:
            suffix = "\n（完整说明见设置中的工具详情。）"
            description = description[:4096 - len(suffix)] + suffix
        version_material = {"schema": schema, "description": source.description, "read_only": source.read_only,
                            "server_name": source.server_name, "title": getattr(source, "title", None)}
        version = str(int(hashlib.sha256(json.dumps(version_material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:40], 16))
        return ProviderToolDefinition(name=tool_alias(source.connection_id, source.raw_name), version=version,
                                      description=description, parameters=schema)

    def details(self, connection_id: str) -> list[dict[str, Any]]:
        result = []
        for source in self.manager.catalog():
            if source.connection_id != connection_id:
                continue
            issue = None
            try:
                self.definition(source)
            except Exception as exc:
                issue = self.redact(source.connection_id, str(exc))
            result.append({"alias": tool_alias(source.connection_id, source.raw_name),
                           "raw_name": self.redact(source.connection_id, source.raw_name),
                           "title": self.redact_value(source.connection_id, getattr(source, "title", None)),
                           "description": self.redact(source.connection_id, source.description or ""),
                           "input_schema": self.redact_value(source.connection_id, source.input_schema),
                           "read_only": source.read_only, "annotations": self.redact_value(source.connection_id, source.annotations),
                           "schema_error": issue})
        return result

    def bind(self, *, read_only: bool) -> tuple[ProviderToolDefinition, ...]:
        catalog: dict[str, McpBoundTool] = {}
        for source in sorted(self.manager.catalog(), key=lambda item: (item.connection_id, item.raw_name)):
            if read_only and source.read_only is not True:
                continue
            try:
                definition = self.definition(source)
                safe_description = self.redact(source.connection_id, definition.description)
                if len(safe_description) > 4096:
                    suffix = "\n（完整说明见设置中的工具详情。）"
                    safe_description = safe_description[:4096 - len(suffix)] + suffix
                definition = definition.model_copy(update={
                    "description": safe_description,
                    "parameters": self.redact_value(source.connection_id, definition.parameters),
                })
            except Exception:
                # Invalid schemas remain visible, with the reason, in tool details.
                continue
            catalog[definition.name] = McpBoundTool(source=source, definition=definition)
        self._bound.set(catalog)
        self._read_only.set(read_only)
        return tuple(item.definition for item in catalog.values())

    def lookup(self, name: str) -> McpBoundTool | None:
        return self._bound.get().get(name)

    def versions(self) -> dict[str, str]:
        return {name: item.definition.version for name, item in self._bound.get().items()}

    def result_metadata(self, name: str) -> dict[str, Any]:
        bound = self.lookup(name)
        if bound is None:
            raise ValueError("这个 MCP 工具已不在本轮工具目录中")
        source = bound.source
        return {"schema": "zhimeng.tool-result.mcp.v1", "source_mcp": True,
                "connection_id": source.connection_id, "read_only": source.read_only is True,
                "server": self.redact(source.connection_id, source.server_name),
                "tool": self.redact(source.connection_id, source.raw_name),
                "title": self.redact_value(source.connection_id, getattr(source, "title", None))}

    def redact(self, connection_id: str, value: str) -> str:
        return mask_secrets(self.manager.redact(connection_id, value))[0]

    def redact_value(self, connection_id: str, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(connection_id, value)
        if isinstance(value, dict):
            return {self.redact(connection_id, str(key)): self.redact_value(connection_id, item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [self.redact_value(connection_id, item) for item in value]
        return value

    async def execute(self, name: str, arguments: dict[str, Any], cancel_event: asyncio.Event) -> dict[str, Any]:
        bound = self.lookup(name)
        if bound is None:
            raise ValueError("这个 MCP 工具已不在本轮工具目录中")
        source = bound.source
        if self._read_only.get() and source.read_only is not True:
            raise ValueError("当前是只读模式，这个 MCP 工具未声明只读")
        validator = validator_for(source.input_schema)(source.input_schema)
        try:
            validator.validate(arguments)
        except Exception as exc:
            # A validation message can quote input values, including credentials.
            raise ValueError(self.redact(source.connection_id, str(exc))) from None
        if cancel_event.is_set():
            raise asyncio.CancelledError
        call = asyncio.create_task(self.manager.call(source.connection_id, source.generation, source.raw_name, arguments))
        cancelled = asyncio.create_task(cancel_event.wait())
        try:
            completed, _ = await asyncio.wait({call, cancelled}, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in completed and cancel_event.is_set():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                raise asyncio.CancelledError
            raw = await call
        except asyncio.CancelledError:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            raise
        except Exception as exc:
            # In particular, an interrupted write has an unknown outcome. This
            # layer sends exactly once; reconnect never replays calls.
            raise ValueError(self.redact(source.connection_id, str(exc)) or "MCP 调用中断，外部操作的结果尚未确认") from None
        finally:
            cancelled.cancel()
            await asyncio.gather(cancelled, return_exceptions=True)
        raw = self.redact_value(source.connection_id, raw)
        output = {**raw, **self.result_metadata(name)}
        if any(block.get("type") != "text" for block in raw.get("content", []) if isinstance(block, dict)):
            output["content_note"] = "非文本内容已保留为原始结构化结果；当前尚未作为图片或音频输入传给模型。"
        return output
