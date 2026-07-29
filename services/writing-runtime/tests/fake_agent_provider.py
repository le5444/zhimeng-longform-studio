from __future__ import annotations

import asyncio
import hashlib
import json
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse


app = FastAPI()
last_request_messages: list[dict[str, object]] = []
last_request_tool_names: list[str] = []


def _messages_hash(messages: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        messages,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_call_payload(
    call_id: str,
    tool_name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(
                                    arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def _non_workflow_tool_messages(
    messages: list[object],
) -> list[dict[str, object]]:
    return [
        message
        for message in messages
        if (
            isinstance(message, dict)
            and message.get("role") == "tool"
            and not str(message.get("tool_call_id", "")).startswith("nlcl_call_")
        )
    ]


def _creation_handoff_tool_result(
    messages: list[object],
) -> dict[str, object]:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        output = payload.get("output") if isinstance(payload, dict) else None
        if isinstance(output, dict) and "creation_handoff" in output:
            return output
    raise RuntimeError("managed Workflow handoff result was not exposed to Provider")


def _writer_task_dispatch(messages: list[object]) -> dict[str, object]:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema") == "zhimeng.writer-task-dispatch.v1"
        ):
            return payload
    raise RuntimeError("Runtime Writer task dispatch was not exposed to Provider")


def _creation_instruction_dispatch(messages: list[object]) -> dict[str, object]:
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        try:
            payload = json.loads(str(message.get("content", "{}")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema") == "zhimeng.creation-instruction-dispatch.v1"
        ):
            return payload
    raise RuntimeError("Runtime creation instruction was not exposed to Provider")


def _candidate_submission_arguments(
    messages: list[object],
    *,
    title: str,
    summary: str,
    body: str,
) -> dict[str, object]:
    writer_dispatch = _writer_task_dispatch(messages)
    instruction_dispatch = _creation_instruction_dispatch(messages)
    required_kind = writer_dispatch.get("required_candidate_kind")
    if required_kind not in {"repair_plan", "change_proposal"}:
        raise RuntimeError("Runtime Writer candidate kind is invalid")
    if writer_dispatch.get("runtime_adds_writer_task_reference") is not True:
        raise RuntimeError("Runtime Writer reference injection was not declared")
    if instruction_dispatch.get("runtime_adds_materialization_reference") is not True:
        raise RuntimeError("Runtime instruction reference injection was not declared")
    return {
        "kind": required_kind,
        "content": {
            "title": title,
            "summary": summary,
            "body": body,
            "format": "markdown",
        },
    }



def _required_candidate_kind(messages: list[object]) -> str | None:
    try:
        kind = _writer_task_dispatch(messages).get("required_candidate_kind")
    except RuntimeError:
        return None
    if isinstance(kind, str) and kind:
        return kind
    return None


DISTILLATION_TOOL_NAMES = (
    "distillation_anchor_search",
    "distillation_anchor_read",
    "distillation_output_submit",
)


def _advertised_tool_names(body: dict[str, object]) -> tuple[str, ...]:
    raw_tools = body.get("tools", [])
    if not isinstance(raw_tools, list):
        return ()
    names: list[str] = []
    for tool in raw_tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.append(str(function["name"]))
    return tuple(names)


def _is_distillation_tool_request(body: dict[str, object]) -> bool:
    names = _advertised_tool_names(body)
    return len(names) == len(DISTILLATION_TOOL_NAMES) and set(names) == set(
        DISTILLATION_TOOL_NAMES
    )


def _distillation_tool_messages(messages: list[object]) -> list[dict[str, object]]:
    return [
        message
        for message in messages
        if (
            isinstance(message, dict)
            and message.get("role") == "tool"
            and str(message.get("name", "")) in DISTILLATION_TOOL_NAMES
        )
    ]


def _tool_message_content(message: dict[str, object]) -> dict[str, object]:
    try:
        payload = json.loads(str(message.get("content", "{}")))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Runtime distillation tool result was not JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime distillation tool result was not an object")
    return payload


def _anchor_id_from_search(message: dict[str, object]) -> str:
    payload = _tool_message_content(message)
    matches = payload.get("matches")
    if not isinstance(matches, list) or not matches:
        raise RuntimeError("Runtime distillation search returned no anchors")
    first = matches[0]
    if not isinstance(first, dict):
        raise RuntimeError("Runtime distillation search match was invalid")
    anchor = first.get("anchor")
    if not isinstance(anchor, dict) or not isinstance(anchor.get("anchor_id"), str):
        raise RuntimeError("Runtime distillation search match lacked anchor_id")
    return str(anchor["anchor_id"])


def _anchor_ids_from_read(message: dict[str, object]) -> list[str]:
    payload = _tool_message_content(message)
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise RuntimeError("Runtime distillation read returned no anchors")
    anchor_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        anchor = item.get("anchor")
        if isinstance(anchor, dict) and isinstance(anchor.get("anchor_id"), str):
            anchor_ids.append(str(anchor["anchor_id"]))
    if not anchor_ids:
        raise RuntimeError("Runtime distillation read lacked anchor ids")
    return sorted(set(anchor_ids))


def _distillation_submission(anchor_ids: list[str]) -> dict[str, object]:
    work = {
        "title": "选择、代价与阶段兑现的长篇推进机制",
        "summary": "从林川雨夜选择中抽取可迁移的选择代价机制，供作者复核后再进入正式知识。",
        "reader_promise": "读者能看到人物为了行动空间承担明确代价，并让下一章压力回流。",
        "taxonomy": {
            "genre": ["悬疑", "长篇推进"],
            "scene_function": ["阶段兑现", "压力递进"],
            "emotion": ["压迫感", "克制紧张"],
            "craft": ["选择代价闭环", "证据反压"],
        },
        "type_payload": {
            "premise_pattern": "人物掌握线索前必须先承担暴露风险。",
            "core_reader_promise": "每次线索兑现都会制造更高一级的新义务。",
            "opening_contract": "先给危险提示，再迫使人物用行动验证。",
            "progression_map": ["发现异常", "留下证据", "承担暴露风险"],
            "recurring_scene_loops": ["线索出现 -> 选择代价 -> 新压力回流"],
            "transfer_limits": ["不得复用来源人物名称", "不得复制来源场景顺序"],
        },
    }
    voice = {
        "title": "贴近人物压力的自然叙事声音规则",
        "summary": "保留贴近行动人物判断的有限视角，用动作、物件和关系压力承接情绪。",
        "reader_promise": "读者能自然读懂人物正在做什么、为什么犹豫以及选择代价。",
        "taxonomy": {
            "platform": ["番茄"],
            "audience": ["男频"],
            "genre": ["悬疑"],
            "subgenre": ["都市疑案"],
            "style": ["现场压力", "自然网文"],
            "pov": ["贴近主角的有限视角"],
            "content_boundary": ["只迁移抽象叙事行为"],
        },
        "type_payload": {
            "pov_distance": "贴近当前行动人物判断，不替人物总结主题。",
            "sentence_rhythm": "动作处短句落点，关系变化用中句承接。",
            "diction_pool": ["具体动作", "现场物件", "关系站位"],
            "dialogue_behavior": "每句对话携带目的、关系或信息差。",
            "description_behavior": "描写必须改变人物判断或行动空间。",
            "emotion_handling": "先写身体反应、动作选择和关系后果。",
            "human_web_prose": "句子语义完整，连续阅读不出现报告腔。",
            "sentence_variety": "短中长句按压力变化交替。",
            "scene_anchor_rules": ["每两三段出现一次改变判断的现场锚点。"],
            "dialogue_pressure_rules": ["对话至少推进目的、关系或信息差之一。"],
            "anti_ai_failure_signatures": ["抽象心理替代动作", "角色同声解释设定"],
            "readability_acceptance_tests": ["连续阅读时能分清行动者与代价承担者。"],
            "do_not_copy": ["来源作者标志句", "来源人物口头禅"],
        },
        "nlcl_route_hints": {
            "nlcl_layers": ["distillation_index", "style", "deai"],
            "feedback_default_layer": "L5_DEAI_STYLE",
        },
    }
    validator = {
        "title": "人物压力与来源污染的正文拒收闸门",
        "summary": "检查正文候选是否把人物压力落到动作和代价，并阻断来源名称、设定壳或场景顺序污染。",
        "reader_promise": "只有动作、关系和代价可追溯且没有来源污染的候选才能进入审阅。",
        "taxonomy": {
            "platform": ["番茄"],
            "audience": ["男频"],
            "genre": ["悬疑"],
            "subgenre": ["都市疑案"],
            "problem_symptom": ["抽象总结", "来源污染"],
            "content_boundary": ["正文候选拒收闸门"],
        },
        "type_payload": {
            "validator_name": "character_pressure_and_source_contamination_gate",
            "applies_to": ["完整章节正文候选", "针对性正文改写候选"],
            "layer_id": "L6_REVIEW_ACCEPTANCE",
            "checks": ["人物选择是否产生可见代价。", "正文是否避开来源名称和标志句。"],
            "reject_conditions": ["抽象心理总结替代行动。", "出现来源专名或完整场景顺序。"],
            "rewrite_routes": ["L4_DRAFT_EXECUTION", "L5_DEAI_STYLE"],
            "execution_rules": [
                {
                    "rule_id": "forbid_source_terms",
                    "kind": "forbid_terms",
                    "terms": ["林川", "旧档案馆"],
                    "case_sensitive": False,
                    "failure_signature": "validator_source_term_leak",
                    "message": "正文候选包含正式校验器禁止的来源词项。",
                }
            ],
        },
        "nlcl_route_hints": {
            "nlcl_layers": ["distillation_index", "review"],
            "feedback_default_layer": "L6_REVIEW_ACCEPTANCE",
        },
    }
    return {
        "schema": "zhimeng.distillation-output-submit.v1",
        "cards": [
            {"card_type": "work", "card": work, "anchor_ids": anchor_ids},
            {"card_type": "voice", "card": voice, "anchor_ids": anchor_ids},
            {"card_type": "validator", "card": validator, "anchor_ids": anchor_ids},
        ],
    }


def _next_distillation_tool_call(messages: list[object]) -> tuple[str, str, dict[str, object]]:
    tool_messages = _distillation_tool_messages(messages)
    if not tool_messages:
        return (
            "call_distillation_search_0001",
            "distillation_anchor_search",
            {
                "schema": "zhimeng.distillation-anchor-search-request.v1",
                "query": "林川",
                "levels": ["small"],
                "limit": 5,
            },
        )
    if len(tool_messages) == 1:
        anchor_id = _anchor_id_from_search(tool_messages[-1])
        return (
            "call_distillation_read_0002",
            "distillation_anchor_read",
            {
                "schema": "zhimeng.distillation-anchor-read-request.v1",
                "anchor_ids": [anchor_id],
            },
        )
    anchor_ids = _anchor_ids_from_read(tool_messages[-1])
    return (
        "call_distillation_submit_0003",
        "distillation_output_submit",
        _distillation_submission(anchor_ids),
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ready"}


@app.get("/last-request")
async def last_request() -> dict[str, object]:
    return {
        "messages": last_request_messages,
        "tool_names": last_request_tool_names,
        "provider_messages_hash": _messages_hash(last_request_messages),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> StreamingResponse:
    global last_request_messages
    global last_request_tool_names
    body = await request.json()
    messages = body.get("messages", []) if isinstance(body, dict) else []
    last_request_messages = [
        dict(message)
        for message in messages
        if isinstance(message, dict)
    ]
    last_request_tool_names = (
        list(_advertised_tool_names(body))
        if isinstance(body, dict)
        else []
    )
    last_user_content = next(
        (
            str(message["content"])
            for message in reversed(messages)
            if (
                isinstance(message, dict)
                and message.get("role") == "user"
                and isinstance(message.get("content"), str)
            )
        ),
        "",
    )
    mobile = "移动端验证" in last_user_content
    tool_mode = "工具链验证" in last_user_content
    knowledge_mode = "使用本批已正式化知识" in last_user_content
    skill_mode = "受管 Skill 恢复验收" in last_user_content
    change_proposal_mode = _required_candidate_kind(messages) == "change_proposal"

    async def stream():
        if isinstance(body, dict) and _is_distillation_tool_request(body):
            call_id, tool_name, arguments = _next_distillation_tool_call(messages)
            payload = _tool_call_payload(call_id, tool_name, arguments)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

        if skill_mode:
            result = _creation_handoff_tool_result(messages)
            candidate = _candidate_submission_arguments(
                messages,
                title="受管 Creation Suite 审计候选",
                summary="三步受管 Workflow 已完成，结果仅供作者审查。",
                body=(
                    "受管 Creation Suite 审计已完成。"
                    f"执行状态：{result.get('execution_status', 'unknown')}；"
                    f"领域状态：{result.get('domain_status', 'unknown')}。"
                    "正式项目未写回。"
                ),
            )
            payload = _tool_call_payload(
                "call_browser_candidate_skill_0001",
                "candidate_output_submit",
                candidate,
            )
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

        if tool_mode or knowledge_mode or change_proposal_mode:
            if knowledge_mode:
                mode_name = "knowledge"
                candidate_title = "本批知识正文候选"
                candidate_line = "他必须在天亮前作出选择，并承担由此而来的代价。"
                propose_summary = "使用已正式化知识增加一条带有可见选择和代价的正文候选。"
                propose_rationale = "让人物选择产生可见代价，并保留作者逐项审阅边界。"
                submit_summary = "正式知识路由、项目读取和 ChangeSet 提案已完成，候选等待作者审阅。"
            elif tool_mode:
                mode_name = "browser"
                candidate_title = "工具链验证候选"
                candidate_line = "工具链验证句。"
                propose_summary = "保留原文并增加一条可审阅的测试句。"
                propose_rationale = "验证 Agent 到 ChangeSet 的浏览器全链。"
                submit_summary = "项目读取和 ChangeSet 提案已完成，候选等待作者审阅。"
            else:
                # Managed O2 author_review path: require same-Run ChangeSet
                # without depending on "工具链验证" objective keywords.
                mode_name = "changeset"
                candidate_title = "正文候选改动"
                candidate_line = "候选正文句，只供作者审阅。"
                propose_summary = "读取项目正文并生成可审阅的正文候选改动。"
                propose_rationale = "验证 O2 写前链到 ChangeSet Diff 的作者可见路径。"
                submit_summary = "写前链、项目读取和 ChangeSet 提案已完成，候选等待作者审阅。"
            tool_messages = _non_workflow_tool_messages(messages)
            if not tool_messages:
                tool_call = (
                    f"call_{mode_name}_list_0001",
                    "project_list_files",
                    {"query": "CH001_正文.md", "limit": 10},
                )
            else:
                previous = json.loads(str(tool_messages[-1].get("content", "{}")))
                if len(tool_messages) == 1:
                    tool_call = (
                        f"call_{mode_name}_read_0002",
                        "project_read_file",
                        {
                            "file_id": previous["output"]["files"][0]["file_id"],
                            "start_line": 1,
                            "max_lines": 200,
                        },
                    )
                elif len(tool_messages) == 2:
                    original = str(previous["output"]["content"])
                    tool_call = (
                        f"call_{mode_name}_propose_0003",
                        "change_set_propose",
                        {
                            "title": candidate_title,
                            "summary": propose_summary,
                            "changes": [
                                {
                                    "operation": "update",
                                    "file_id": previous["output"]["file_id"],
                                    "after_content": f"{original.rstrip()}\n{candidate_line}\n",
                                    "rationale": propose_rationale,
                                }
                            ],
                        },
                    )
                else:
                    tool_call = None
            if tool_call is not None:
                call_id, tool_name, arguments = tool_call
                payload = _tool_call_payload(call_id, tool_name, arguments)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return

            candidate = _candidate_submission_arguments(
                messages,
                title=candidate_title,
                summary=submit_summary,
                body="候选改动已生成，请审阅。",
            )
            payload = _tool_call_payload(
                f"call_{mode_name}_candidate_tool_0004",
                "candidate_output_submit",
                candidate,
            )
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

        first_text = (
            "## 移动建议\n\n移动端首段已到达。"
            if mobile
            else "## 修改建议\n\n第一段先收紧。"
        )
        second_text = (
            "移动端完整结果已保存。\n\n```text\n移动安全代码块\n```"
            if mobile
            else (
                "第二段保留黄铜钥匙。\n\n"
                "<script>window.__agentXss = true</script>\n\n"
                "```text\n安全代码块\n```"
            )
        )
        ordinary_tool_messages = _non_workflow_tool_messages(messages)
        if not ordinary_tool_messages:
            await asyncio.sleep(0.05)
            first_payload = {
                "choices": [
                    {
                        "delta": {"content": first_text},
                        "finish_reason": None,
                    }
                ]
            }
            yield f"data: {json.dumps(first_payload, ensure_ascii=False)}\n\n"
            await asyncio.sleep(1.0)
            tool_payload = _tool_call_payload(
                "call_browser_stream_probe_0001",
                "project_list_files",
                {"query": "CH001_正文.md", "limit": 1},
            )
            yield f"data: {json.dumps(tool_payload, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

        candidate = _candidate_submission_arguments(
            messages,
            title="移动端写作建议" if mobile else "开场修改建议",
            summary="候选已绑定受管 Workflow，只供作者审查。",
            body=f"{first_text}\n\n{second_text}",
        )
        payload = _tool_call_payload(
            "call_browser_candidate_stream_0002",
            "candidate_output_submit",
            candidate,
        )
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("PLAYWRIGHT_PROVIDER_PORT", "18790")),
        log_level="warning",
    )
