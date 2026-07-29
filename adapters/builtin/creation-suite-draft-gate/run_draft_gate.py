#!/usr/bin/env python3
"""Run the narrow, deterministic post-draft cleanliness gate.

The adapter consumes candidate file bytes supplied in the request.  It never
opens a project path, invokes a provider, or writes anywhere except the
current Runtime staging directory.  The gate detects delivery-contract
contamination; it does not score prose quality or authorize a formal draft.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any


SCHEMA = "zhimeng.creation-draft-gate-result.v1"
REQUEST_SCHEMA = "zhimeng.creation-draft-gate-request.v1"
MANIFEST_SCHEMA = "zhimeng.candidate-draft-manifest.v1"
MAX_FILES = 128
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE = 160


# These rules are intentionally limited to obvious transport/delivery leaks.
# They are not style, quality, AI-smell, or reader-evaluation heuristics.
BLOCKER_RULES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    (
        "code_or_json_body",
        "draft_body_contract_violation",
        "正文包含代码块、JSON 或数组形态输出。",
        re.compile(r"^\s*(?:```|\{|\[)|```\s*$", re.MULTILINE),
    ),
    (
        "system_field_or_metadata",
        "draft_metadata_leak",
        "正文包含系统字段、作战包字段或生产元数据。",
        re.compile(
            r"\b(?:assistant_message|draft_chapter_body|brief_draft|chapter_id|"
            r"source_title|approved_at|operation_package_id|draft_version|"
            r"ContextPack|OperationPackage|SceneContract|WriterContextPack|"
            r"mandatory_usage_closure|human_friction_surface_pass)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "auxiliary_metadata_leak",
        "draft_metadata_leak",
        "正文包含自检面板、差异说明、审稿摘要或修改说明。",
        re.compile(
            r"(?:SelfCheckPanel|RevisionDiff|WriterAuxiliaryReview|draftReview|"
            r"draftDiff|ArchivistAudit|旧稿\s*/\s*新稿|新稿\s*/\s*旧稿|"
            r"自检面板|自检结果|修改说明|审稿摘要|评改建议|修改建议|"
            r"以下是.{0,20}(自检|修改|审稿|差异)|根据你的要求)",
            re.IGNORECASE,
        ),
    ),
    (
        "role_marker_leak",
        "draft_role_marker_leak",
        "正文包含聊天角色标记。",
        re.compile(
            r"^\s*(?:系统|AI|助手|模型|用户|USER|ASSISTANT|SYSTEM)\s*[:：]",
            re.MULTILINE | re.IGNORECASE,
        ),
    ),
    (
        "model_error_leak",
        "draft_model_error_leak",
        "正文包含模型错误、流式中断或无法完成提示。",
        re.compile(
            r"(?:This operation was aborted|Model returned empty|STREAM_CANCELLED|"
            r"模型输出中途|模型这次没有返回|模型服务异常|模型服务超时|"
            r"无法完成|不能执行|稍后重试)",
            re.IGNORECASE,
        ),
    ),
    (
        "prose_preface_or_commentary",
        "draft_delivery_commentary_leak",
        "正文包含说明性前缀、评改口吻或交付提示。",
        re.compile(
            r"^\s*(?:(?:以下是|下面是).{0,24}(完整正文|章节正文|重写正文|正文)|"
            r"好的[，,。]?\s*我来|已根据|说明[:：]|备注[:：]|注[:：]|"
            r"修改意见|修改建议|评改|点评|建议如下|总体来看)",
            re.MULTILINE,
        ),
    ),
)


PATH_PATTERN = re.compile(
    r"^(?![A-Za-z]:)(?!/)(?!.*\\)(?!.*(?:^|/)\.\.(?:/|$))"
    r"(?!.*(?:^|/)\.(?:/|$))(?!.*(?:^|/)\.git(?:/|$)).+$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CANDIDATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
SAFE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clip_evidence(value: str) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= MAX_EVIDENCE:
        return compact
    return compact[:MAX_EVIDENCE] + "...[clipped]"


def _strip_front_matter(text: str) -> tuple[str, int]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if normalized.startswith("---\n"):
        end = normalized.find("\n---\n", 4)
        if end >= 0:
            body_start = end + len("\n---\n")
            return normalized[body_start:], body_start
    return normalized, 0


def _point(text: str, offset: int) -> dict[str, int]:
    bounded = max(0, min(offset, len(text)))
    line = text.count("\n", 0, bounded) + 1
    previous_newline = text.rfind("\n", 0, bounded)
    column = bounded - previous_newline
    return {"line": line, "column": column, "offset": bounded}


def _location(text: str, start: int, end: int) -> dict[str, dict[str, int]]:
    return {"start": _point(text, start), "end": _point(text, max(start, end))}


def _finding(
    *,
    code: str,
    signature: str,
    message: str,
    body: str,
    start: int,
    end: int,
    evidence: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "failure_signature": signature,
        "severity": "blocked",
        "message": message,
        "evidence": _clip_evidence(evidence),
        "location": _location(body, start, end),
    }


def _validate_text(text: str) -> list[dict[str, Any]]:
    body, _ = _strip_front_matter(text)
    body = body.strip()
    findings: list[dict[str, Any]] = []
    if not body:
        findings.append(
            _finding(
                code="empty_body",
                signature="draft_empty_body",
                message="正文为空。",
                body=body,
                start=0,
                end=0,
                evidence="",
            )
        )
        return findings

    for code, signature, message, pattern in BLOCKER_RULES:
        match = pattern.search(body)
        if match is None:
            continue
        findings.append(
            _finding(
                code=code,
                signature=signature,
                message=message,
                body=body,
                start=match.start(),
                end=match.end(),
                evidence=match.group(0),
            )
        )
    return findings


def _path_error(path: Any) -> str | None:
    if not isinstance(path, str) or not path or len(path) > 512:
        return "candidate_path_invalid"
    if "\x00" in path or not PATH_PATTERN.fullmatch(path):
        return "candidate_path_invalid"
    try:
        parts = PurePosixPath(path).parts
    except (TypeError, ValueError):
        return "candidate_path_invalid"
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return "candidate_path_invalid"
    return None


def _decode_file_bytes(item: dict[str, Any]) -> tuple[bytes | None, str | None]:
    encoded = item.get("after_bytes_base64")
    if not isinstance(encoded, str):
        return None, "candidate_after_bytes_missing"
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None, "candidate_after_bytes_invalid"
    if len(data) > MAX_FILE_BYTES:
        return None, "candidate_after_bytes_too_large"
    return data, None


def _manifest_entries(manifest: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if manifest is None:
        return {}, []
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        return {}, ["candidate_manifest_invalid"]
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        return {}, ["candidate_manifest_invalid"]
    by_path: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            failures.append("candidate_manifest_invalid")
            continue
        path = entry.get("path")
        if _path_error(path):
            failures.append("candidate_manifest_invalid")
            continue
        if path in by_path:
            failures.append("candidate_manifest_duplicate_path")
            continue
        expected_hash = entry.get("after_sha256")
        byte_length = entry.get("byte_length")
        if not isinstance(expected_hash, str) or not SHA256_PATTERN.fullmatch(expected_hash):
            failures.append("candidate_manifest_invalid")
            continue
        if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 0:
            failures.append("candidate_manifest_invalid")
            continue
        by_path[path] = {"after_sha256": expected_hash, "byte_length": byte_length}
    return by_path, list(dict.fromkeys(failures))


def _check(
    code: str,
    status: str,
    message: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": status,
        "message": message,
        "evidence_refs": evidence_refs or [],
    }


def _knowledge_validator_bundle(
    raw: Any,
) -> tuple[str | None, list[dict[str, Any]]]:
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        raise ValueError("knowledge_validator_bundle must be an object or null")
    if raw.get("schema") != "zhimeng.knowledge-validator-bundle.v1":
        raise ValueError("knowledge validator bundle schema is invalid")
    declared_hash = raw.get("bundle_hash")
    if not isinstance(declared_hash, str) or not SHA256_PATTERN.fullmatch(declared_hash):
        raise ValueError("knowledge validator bundle hash is invalid")
    hash_material = dict(raw)
    hash_material.pop("bundle_hash", None)
    if _sha256(_canonical_json(hash_material)) != declared_hash:
        raise ValueError("knowledge validator bundle hash does not match its payload")
    for field in (
        "context_pack_hash",
        "route_receipt_hash",
        "knowledge_fingerprint",
    ):
        value = raw.get(field)
        if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
            raise ValueError(f"knowledge validator bundle {field} is invalid")
    validators = raw.get("validators")
    if not isinstance(validators, list) or not 1 <= len(validators) <= 8:
        raise ValueError("knowledge validator bundle validators are invalid")

    normalized: list[dict[str, Any]] = []
    total_rules = 0
    for validator in validators:
        if not isinstance(validator, dict):
            raise ValueError("knowledge validator binding is invalid")
        card_id = validator.get("card_id")
        version_id = validator.get("version_id")
        content_hash = validator.get("content_hash")
        validator_name = validator.get("validator_name")
        provenance = validator.get("provenance")
        if not isinstance(card_id, str) or not card_id or len(card_id) > 160:
            raise ValueError("knowledge validator card_id is invalid")
        if (
            not isinstance(version_id, str)
            or not re.fullmatch(r"knowledge-version-[0-9a-f]{32}", version_id)
            or not isinstance(content_hash, str)
            or not SHA256_PATTERN.fullmatch(content_hash)
            or not isinstance(validator_name, str)
            or not SAFE_ID_PATTERN.fullmatch(validator_name)
        ):
            raise ValueError("knowledge validator version identity is invalid")
        if (
            not isinstance(provenance, dict)
            or provenance.get("card_id") != card_id
            or provenance.get("version_id") != version_id
            or provenance.get("version_content_hash") != content_hash
        ):
            raise ValueError("knowledge validator provenance is invalid")

        configuration_errors = validator.get("configuration_errors")
        if (
            not isinstance(configuration_errors, list)
            or len(configuration_errors) > 16
            or any(
                not isinstance(item, str) or not SAFE_ID_PATTERN.fullmatch(item)
                for item in configuration_errors
            )
            or configuration_errors != sorted(set(configuration_errors))
        ):
            raise ValueError("knowledge validator configuration errors are invalid")
        rules = validator.get("rules")
        if not isinstance(rules, list) or len(rules) > 16:
            raise ValueError("knowledge validator rules are invalid")
        total_rules += len(rules)
        if total_rules > 16:
            raise ValueError("knowledge validator bundle exceeds the rule limit")
        normalized_rules: list[dict[str, Any]] = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError("knowledge validator rule is invalid")
            rule_id = rule.get("rule_id")
            kind = rule.get("kind")
            terms = rule.get("terms")
            failure_signature = rule.get("failure_signature")
            message = rule.get("message")
            case_sensitive = rule.get("case_sensitive")
            if (
                not isinstance(rule_id, str)
                or not SAFE_ID_PATTERN.fullmatch(rule_id)
                or kind not in {"forbid_terms", "require_any_term"}
                or not isinstance(terms, list)
                or not 1 <= len(terms) <= 16
                or any(
                    not isinstance(term, str)
                    or not term
                    or term != term.strip()
                    or len(term) > 160
                    or any(ord(character) < 32 for character in term)
                    for term in terms
                )
                or not isinstance(case_sensitive, bool)
                or not isinstance(failure_signature, str)
                or not SAFE_ID_PATTERN.fullmatch(failure_signature)
                or not isinstance(message, str)
                or not 8 <= len(message) <= 1000
            ):
                raise ValueError("knowledge validator rule contract is invalid")
            comparison_terms = (
                terms if case_sensitive else [term.casefold() for term in terms]
            )
            if len(comparison_terms) != len(set(comparison_terms)):
                raise ValueError("knowledge validator rule terms are duplicated")
            normalized_rules.append(rule)
        rule_ids = [rule["rule_id"] for rule in normalized_rules]
        signatures = [rule["failure_signature"] for rule in normalized_rules]
        if rule_ids != sorted(rule_ids) or len(rule_ids) != len(set(rule_ids)):
            raise ValueError("knowledge validator rules are not canonical")
        if len(signatures) != len(set(signatures)):
            raise ValueError("knowledge validator failure signatures are duplicated")
        rules_hash = validator.get("rules_hash")
        expected_rules_hash = _sha256(
            _canonical_json(
                {
                    "rules": normalized_rules,
                    "configuration_errors": configuration_errors,
                }
            )
        )
        if rules_hash != expected_rules_hash:
            raise ValueError("knowledge validator rules hash does not match")
        if not normalized_rules and not configuration_errors:
            raise ValueError("knowledge validator has no rules or fail-closed error")
        normalized.append(validator)
    card_ids = [item["card_id"] for item in normalized]
    if card_ids != sorted(card_ids) or len(card_ids) != len(set(card_ids)):
        raise ValueError("knowledge validator bindings are not canonical")
    return declared_hash, normalized


def _find_term(body: str, term: str, *, case_sensitive: bool) -> tuple[int, int]:
    haystack = body if case_sensitive else body.casefold()
    needle = term if case_sensitive else term.casefold()
    return haystack.find(needle), haystack.count(needle)


def evaluate(request: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one candidate request and return the domain result object."""

    if request.get("schema") != REQUEST_SCHEMA:
        raise ValueError("unsupported draft gate request schema")
    candidate_id = request.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or not candidate_id
        or len(candidate_id) > 256
        or not CANDIDATE_ID_PATTERN.fullmatch(candidate_id)
    ):
        raise ValueError("candidate_id is invalid")
    raw_files = request.get("candidate_files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("candidate_files must be a non-empty array")
    if len(raw_files) > MAX_FILES:
        raise ValueError("candidate_files exceeds the bounded file limit")
    knowledge_validator_bundle_hash, knowledge_validators = (
        _knowledge_validator_bundle(request.get("knowledge_validator_bundle"))
    )

    manifest = request.get("candidate_manifest")
    manifest_by_path, manifest_failures = _manifest_entries(manifest)
    manifest_hash = None if manifest is None else _sha256(_canonical_json(manifest))
    file_results: list[dict[str, Any]] = []
    validator_file_inputs: list[tuple[str, str, dict[str, Any]]] = []
    seen_paths: set[str] = set()
    total_bytes = 0

    for raw_item in raw_files:
        item = raw_item if isinstance(raw_item, dict) else {}
        path = item.get("path")
        path_key = path if isinstance(path, str) else "<invalid-path>"
        file_findings: list[dict[str, Any]] = []
        path_failure = _path_error(path)
        if path_failure:
            file_findings.append(
                _finding(
                    code="invalid_path",
                    signature=path_failure,
                    message="候选文件路径必须是相对、规范化的 POSIX 路径。",
                    body="",
                    start=0,
                    end=0,
                    evidence="<invalid-path>",
                )
            )
            # Do not echo absolute, traversal, or otherwise untrusted input
            # paths into the public result or staging artifact.
            path_key = "<invalid-path>"
        elif path in seen_paths:
            file_findings.append(
                _finding(
                    code="duplicate_path",
                    signature="candidate_duplicate_path",
                    message="候选文件路径重复。",
                    body="",
                    start=0,
                    end=0,
                    evidence=path,
                )
            )
        else:
            seen_paths.add(path)

        data, decode_failure = _decode_file_bytes(item)
        if decode_failure:
            file_findings.append(
                _finding(
                    code=decode_failure.removeprefix("candidate_"),
                    signature=decode_failure,
                    message=(
                        "候选文件缺少 after bytes。"
                        if decode_failure == "candidate_after_bytes_missing"
                        else "候选文件 after bytes 不是有效的 bounded base64 UTF-8 输入。"
                    ),
                    body="",
                    start=0,
                    end=0,
                    evidence="",
                )
            )
            data = b""
        total_bytes += len(data)
        content_hash = _sha256(data)
        if total_bytes > MAX_TOTAL_BYTES:
            file_findings.append(
                _finding(
                    code="candidate_total_bytes_too_large",
                    signature="candidate_after_bytes_too_large",
                    message="候选文件总大小超过受管 Gate 上限。",
                    body="",
                    start=0,
                    end=0,
                    evidence=str(total_bytes),
                )
            )

        declared_hash = item.get("after_sha256")
        if declared_hash is not None:
            if not isinstance(declared_hash, str) or not SHA256_PATTERN.fullmatch(declared_hash):
                file_findings.append(
                    _finding(
                        code="declared_hash_invalid",
                        signature="candidate_hash_mismatch",
                        message="候选文件声明的 after_sha256 无效。",
                        body="",
                        start=0,
                        end=0,
                        evidence=str(declared_hash),
                    )
                )
            elif declared_hash != content_hash:
                file_findings.append(
                    _finding(
                        code="declared_hash_mismatch",
                        signature="candidate_hash_mismatch",
                        message="候选文件声明的 after_sha256 与实际 bytes 不一致。",
                        body="",
                        start=0,
                        end=0,
                        evidence=f"declared={declared_hash} actual={content_hash}",
                    )
                )

        manifest_entry = manifest_by_path.get(path) if not path_failure else None
        if manifest is not None and manifest_entry is None:
            file_findings.append(
                _finding(
                    code="manifest_file_missing",
                    signature="candidate_manifest_mismatch",
                    message="候选文件未在 manifest 中声明。",
                    body="",
                    start=0,
                    end=0,
                    evidence=path_key,
                )
            )
        elif manifest_entry is not None:
            if manifest_entry["after_sha256"] != content_hash or manifest_entry["byte_length"] != len(data):
                file_findings.append(
                    _finding(
                        code="manifest_hash_mismatch",
                        signature="candidate_manifest_mismatch",
                        message="候选文件 bytes 与 manifest 声明不一致。",
                        body="",
                        start=0,
                        end=0,
                        evidence=(
                            f"manifest_hash={manifest_entry['after_sha256']} actual_hash={content_hash}; "
                            f"manifest_bytes={manifest_entry['byte_length']} actual_bytes={len(data)}"
                        ),
                    )
                )

        if data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                file_findings.append(
                    _finding(
                        code="invalid_utf8",
                        signature="candidate_invalid_utf8",
                        message="候选文件不是有效的 UTF-8 正文。",
                        body="",
                        start=0,
                        end=0,
                        evidence=f"byte_offset={exc.start}",
                    )
                )
            else:
                file_findings.extend(_validate_text(text))
        elif not data and not file_findings:
            # Empty bytes are valid transport but fail the body contract.
            file_findings.extend(_validate_text(""))

        body = ""
        body_line_count = 0
        if data:
            try:
                body_text = data.decode("utf-8")
            except UnicodeDecodeError:
                body_text = ""
            body, _ = _strip_front_matter(body_text)
            body_line_count = len(body.strip().splitlines()) if body.strip() else 0
        file_result = {
            "path": path_key,
            "status": "blocked" if file_findings else "passed",
            "content_hash": content_hash,
            "byte_length": len(data),
            "body_line_count": body_line_count,
            "findings": file_findings,
        }
        file_results.append(file_result)
        validator_file_inputs.append((path_key, body, file_result))

    global_findings: list[dict[str, Any]] = []
    for failure in manifest_failures:
        global_findings.append(
            _finding(
                code="manifest_invalid",
                signature=failure,
                message="候选 manifest 不符合受管文件清单合同。",
                body="",
                start=0,
                end=0,
                evidence="candidate_manifest",
            )
        )
    if manifest is not None:
        file_paths = set(seen_paths)
        undeclared = sorted(set(manifest_by_path) - file_paths)
        for path in undeclared:
            global_findings.append(
                _finding(
                    code="manifest_candidate_missing",
                    signature="candidate_manifest_mismatch",
                    message="manifest 声明的文件没有对应候选 bytes。",
                    body="",
                    start=0,
                    end=0,
                    evidence=path,
                )
            )

    knowledge_validator_results: list[dict[str, Any]] = []
    for validator in knowledge_validators:
        validator_failures: list[str] = list(validator["configuration_errors"])
        for configuration_error in validator["configuration_errors"]:
            global_findings.append(
                _finding(
                    code="knowledge_validator_configuration",
                    signature=configuration_error,
                    message=(
                        "正式 validator 卡缺少可执行规则或规则配置无效，"
                        "Gate 已按 fail-closed 阻断。"
                    ),
                    body="",
                    start=0,
                    end=0,
                    evidence=f"{validator['card_id']}:{configuration_error}",
                )
            )

        rule_results: list[dict[str, Any]] = []
        for rule in validator["rules"]:
            rule_blocked = False
            match_count = 0
            evidence_refs: list[str] = []
            for path, body, file_result in validator_file_inputs:
                term_matches: list[tuple[str, int, int]] = []
                for term in rule["terms"]:
                    first, count = _find_term(
                        body,
                        term,
                        case_sensitive=rule["case_sensitive"],
                    )
                    match_count += count
                    if first >= 0:
                        term_matches.append((term, first, count))

                file_blocked = (
                    bool(term_matches)
                    if rule["kind"] == "forbid_terms"
                    else not term_matches
                )
                if term_matches:
                    matched_term, first_offset, _ = term_matches[0]
                    point = _point(body, first_offset)
                    evidence_refs.append(
                        f"{path}:{point['line']}:{point['column']}"
                    )
                elif rule["kind"] == "require_any_term":
                    evidence_refs.append(f"{path}:1:1")
                if not file_blocked:
                    continue

                rule_blocked = True
                if term_matches:
                    evidence_term, start, _ = term_matches[0]
                    end = start + len(evidence_term)
                    evidence = body[start:end]
                else:
                    start = 0
                    end = 0
                    evidence = "<required-term-missing>"
                file_result["findings"].append(
                    _finding(
                        code="knowledge_validator_rule",
                        signature=rule["failure_signature"],
                        message=rule["message"],
                        body=body,
                        start=start,
                        end=end,
                        evidence=evidence,
                    )
                )
                file_result["status"] = "blocked"

            rule_results.append(
                {
                    "rule_id": rule["rule_id"],
                    "kind": rule["kind"],
                    "failure_signature": rule["failure_signature"],
                    "status": "blocked" if rule_blocked else "passed",
                    "match_count": match_count,
                    "evidence_refs": evidence_refs,
                }
            )
            if rule_blocked:
                validator_failures.append(rule["failure_signature"])

        validator_failures = list(dict.fromkeys(validator_failures))
        knowledge_validator_results.append(
            {
                "card_id": validator["card_id"],
                "version_id": validator["version_id"],
                "content_hash": validator["content_hash"],
                "rules_hash": validator["rules_hash"],
                "status": "blocked" if validator_failures else "passed",
                "configuration_errors": validator["configuration_errors"],
                "rule_results": rule_results,
                "failure_signatures": validator_failures,
            }
        )

    all_findings = global_findings + [
        finding
        for file_result in file_results
        for finding in file_result["findings"]
    ]
    failure_signatures = list(
        dict.fromkeys(finding["failure_signature"] for finding in all_findings)
    )
    blocked = bool(all_findings)
    checks = [
        _check(
            "candidate_manifest",
            "blocked" if manifest_failures or any(
                finding["failure_signature"] == "candidate_manifest_mismatch"
                for finding in all_findings
            ) else "passed",
            "候选 manifest 已校验。" if not manifest_failures and not any(
                finding["failure_signature"] == "candidate_manifest_mismatch"
                for finding in all_findings
            ) else "候选 manifest 与 after bytes 不一致。",
            [manifest_hash] if manifest_hash else [],
        ),
        _check(
            "candidate_file_set",
            "blocked" if any(
                finding["failure_signature"] in {
                    "candidate_path_invalid",
                    "candidate_duplicate_path",
                    "candidate_after_bytes_missing",
                    "candidate_after_bytes_invalid",
                    "candidate_after_bytes_too_large",
                    "candidate_invalid_utf8",
                    "candidate_hash_mismatch",
                }
                for finding in all_findings
            ) else "passed",
            "候选文件集合和 bytes 已校验。" if not any(
                finding["failure_signature"] in {
                    "candidate_path_invalid",
                    "candidate_duplicate_path",
                    "candidate_after_bytes_missing",
                    "candidate_after_bytes_invalid",
                    "candidate_after_bytes_too_large",
                    "candidate_invalid_utf8",
                    "candidate_hash_mismatch",
                }
                for finding in all_findings
            ) else "候选文件集合或 bytes 校验失败。",
        ),
        _check(
            "draft_body_cleanliness",
            "blocked" if any(
                finding["failure_signature"].startswith("draft_")
                for finding in all_findings
            ) else "passed",
            "正文交付合同通过。" if not any(
                finding["failure_signature"].startswith("draft_")
                for finding in all_findings
            ) else "正文包含受管 Gate 阻断项。",
        ),
        _check(
            "formal_knowledge_validators",
            "blocked" if any(
                item["status"] == "blocked"
                for item in knowledge_validator_results
            ) else "passed",
            (
                "已执行 Context/NLCL 绑定的正式 validator 词项规则。"
                if knowledge_validator_results
                else "当前 Context/NLCL 未选择项目级正式 validator 规则。"
            ),
            [
                f"{item['card_id']}:{item['rules_hash']}"
                for item in knowledge_validator_results
            ],
        ),
    ]
    return {
        "schema": SCHEMA,
        "status": "blocked" if blocked else "passed",
        "summary": (
            f"Post-draft Gate blocked {len(all_findings)} finding(s)."
            if blocked
            else f"Post-draft Gate passed {len(file_results)} candidate file(s)."
        ),
        "candidate_id": candidate_id,
        "candidate_manifest_hash": manifest_hash,
        "knowledge_validator_bundle_hash": knowledge_validator_bundle_hash,
        "knowledge_validator_results": knowledge_validator_results,
        "quality_assessed": False,
        "formal_draft_allowed": False,
        "checks": checks,
        "global_findings": global_findings,
        "files": file_results,
        "failure_signatures": failure_signatures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic post-draft delivery-contract gate."
    )
    # SkillExecutor supplies the bound snapshot path as argv[1].  The adapter
    # deliberately does not open it; bytes arrive through the JSON request.
    parser.add_argument("bound_snapshot", nargs="?")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.json:
        raise SystemExit("expected --json")
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError("draft gate request must be an object")
        result = evaluate(request)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc

    artifact_path = Path.cwd() / "draft-gate.json"
    artifact_path.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    blocked = result["status"] == "blocked"
    envelope = {
        "health": "blocked" if blocked else "healthy",
        "summary": result["summary"],
        "result": result,
        "artifacts": [
            {
                "artifact_type": "draft_gate",
                "relative_path": "draft-gate.json",
                "payload": result,
            }
        ],
    }
    print(json.dumps(envelope, ensure_ascii=True, separators=(",", ":")))
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
