"""Recover model tool exchanges from durable receipts, without executing tools."""
from __future__ import annotations

import json
from collections.abc import Sequence

from zhimeng_runtime.agent_store import StoredToolCall
from zhimeng_runtime.workflow_runner import fold_skill_output_for_model, workflow_runtime_tool_call_id


def tool_result_message(call_id: str, call: StoredToolCall, *, replayed: bool = False) -> dict[str, object]:
    output = call.output if call.output is not None else {}
    if call.tool_name == "skill_execute" and isinstance(output, dict):
        output = fold_skill_output_for_model(output)
    payload = {
        "status": call.status,
        "output": output,
        "error": call.error.model_dump(mode="json", by_alias=True) if call.error is not None else None,
    }
    if replayed:
        payload["replayed"] = True
    return {"role": "tool", "tool_call_id": call_id,
            "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}


def repair_tool_pairs(
    run_id: str, messages: Sequence[dict[str, object]], calls: Sequence[StoredToolCall],
) -> list[dict[str, object]]:
    """Complete a saved request batch using receipts, preserving its original IDs/order."""
    stored = {call.tool_call_id: call for call in calls}
    repaired: list[dict[str, object]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        repaired.append(message)
        index += 1
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        results = {}
        while index < len(messages) and messages[index].get("role") == "tool":
            result = messages[index]
            results[result["tool_call_id"]] = result
            index += 1
        for requested in message["tool_calls"]:
            call_id = requested["id"]
            result = results.get(call_id)
            if result is None:
                call = stored.get(workflow_runtime_tool_call_id(run_id, call_id))
                if call is not None:
                    result = tool_result_message(call_id, call)
                else:
                    result = {"role": "tool", "tool_call_id": call_id, "content": json.dumps({
                        "status": "not_started", "output": {}, "error": {
                            "code": "run_interrupted_before_tool",
                            "message": "任务在此工具开始执行前已停止。",
                        }}, ensure_ascii=False)}
            repaired.append(result)
    return repaired


def recovered_tool_exchange(call: StoredToolCall) -> list[dict[str, object]]:
    """Older runs lack provider IDs. Use a paired durable identity for their receipts."""
    return [{"role": "assistant", "content": None, "tool_calls": [{
        "id": call.tool_call_id, "type": "function", "function": {
            "name": call.tool_name,
            "arguments": json.dumps(call.arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }}]}, tool_result_message(call.tool_call_id, call)]
