from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError

from zhimeng_runtime.context_models import ContextPack, canonical_json_hash
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.nlcl_models import (
    NlclRouteReceipt,
    NlclRouteSource,
    build_nlcl_route_receipt,
)
from zhimeng_runtime.nlcl_route_execution_models import (
    NlclRouteExecutionReceipt,
    NlclRouteExecutionState,
    build_nlcl_route_execution_receipt,
)
from zhimeng_runtime.prewrite_models import (
    PrewriteOperationPackage,
    build_prewrite_operation_package,
)
from zhimeng_runtime.skill_models import (
    CreationHandoffRequest,
    CreationPrewriteGateRequest,
    SkillExecutionReceipt,
)


class ContextStoreError(RuntimeError):
    pass


class ContextStoreNotFound(ContextStoreError):
    pass


class ContextStoreConflict(ContextStoreError):
    pass


class ContextStoreIntegrity(ContextStoreError):
    pass


@dataclass(frozen=True, slots=True)
class StoredContextPack:
    pack: ContextPack
    consumed_by_run_id: str | None


class ContextStore:
    def __init__(self, database: SqliteStore) -> None:
        self.database = database

    def create_pack(
        self,
        pack: ContextPack,
        *,
        nlcl_route: NlclRouteReceipt | None = None,
        prewrite_package: PrewriteOperationPackage | None = None,
    ) -> StoredContextPack:
        validated, payload_json = self._validated_payload(pack)
        route = nlcl_route or build_nlcl_route_receipt(
            validated,
            source=NlclRouteSource.UNAVAILABLE,
        )
        validated_route, route_payload_json = self._validated_route_payload(
            route,
            validated,
        )
        package = prewrite_package or build_prewrite_operation_package(
            validated,
            validated_route,
        )
        validated_package, package_payload_json = self._validated_prewrite_payload(
            package,
            validated,
            validated_route,
        )
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM context_pack WHERE id = ?",
                    (validated.context_pack_id,),
                ).fetchone()
                if existing is not None:
                    stored = self._stored_from_row(connection, existing)
                    if stored.pack.pack_hash != validated.pack_hash:
                        raise ContextStoreConflict(
                            "Context Pack ID was already used with a different pack hash"
                        )
                    existing_route = self._route_from_connection(
                        connection,
                        validated.context_pack_id,
                    )
                    if existing_route.receipt_hash != validated_route.receipt_hash:
                        raise ContextStoreConflict(
                            "Context Pack ID was already used with a different NLCL route hash"
                        )
                    existing_package = self._prewrite_from_connection(
                        connection,
                        validated.context_pack_id,
                    )
                    self._validate_prewrite_binding(
                        existing_package,
                        stored.pack,
                        existing_route,
                    )
                    if existing_package.package_hash != validated_package.package_hash:
                        raise ContextStoreConflict(
                            "Context Pack ID was already used with a different prewrite package hash"
                        )
                    connection.commit()
                    return stored

                session = connection.execute(
                    "SELECT * FROM execution_session WHERE id = ?",
                    (validated.session_id,),
                ).fetchone()
                if session is None:
                    raise ContextStoreNotFound("Context Pack session was not found")
                project = connection.execute(
                    "SELECT * FROM project_binding WHERE id = ?",
                    (validated.project_id,),
                ).fetchone()
                if project is None:
                    raise ContextStoreNotFound("Context Pack project was not found")
                if str(session["project_binding_id"]) != validated.project_id:
                    raise ContextStoreConflict(
                        "Context Pack project does not match its session binding"
                    )
                if (
                    int(project["revision"]) != validated.project_revision
                    or str(project["fingerprint"]) != validated.project_fingerprint
                ):
                    raise ContextStoreConflict(
                        "Context Pack project revision is stale"
                    )
                if int(session["last_message_ordinal"]) != validated.history_through_ordinal:
                    raise ContextStoreConflict(
                        "Context Pack history ordinal is stale"
                    )

                connection.execute(
                    """
                    INSERT INTO context_pack(
                        id, session_id, project_binding_id,
                        history_through_ordinal, objective, active_chapter_id,
                        profile, project_revision, project_fingerprint,
                        project_kb_state_id, project_kb_manifest_hash,
                        knowledge_fingerprint, readiness, pack_hash,
                        injection_hash, provider_messages_hash, payload_json,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validated.context_pack_id,
                        validated.session_id,
                        validated.project_id,
                        validated.history_through_ordinal,
                        validated.objective,
                        validated.active_chapter_id,
                        validated.profile,
                        validated.project_revision,
                        validated.project_fingerprint,
                        validated.project_kb_state_id,
                        validated.project_kb_manifest_hash,
                        self._pack_knowledge_fingerprint(validated),
                        validated.readiness.value,
                        validated.pack_hash,
                        validated.injection_hash,
                        validated.provider_messages_hash,
                        payload_json,
                        validated.created_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO nlcl_route_receipt(
                        id, context_pack_id, project_binding_id, context_pack_hash,
                        project_revision, project_fingerprint, active_chapter_id,
                        project_kb_state_id, project_kb_manifest_hash,
                        profile, objective_hash, knowledge_fingerprint, source,
                        route_status, receipt_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validated_route.route_receipt_id,
                        validated_route.context_pack_id,
                        validated_route.project_id,
                        validated_route.context_pack_hash,
                        validated_route.project_revision,
                        validated_route.project_fingerprint,
                        validated_route.active_chapter_id,
                        validated_route.project_kb_state_id,
                        validated_route.project_kb_manifest_hash,
                        validated_route.profile,
                        validated_route.objective_hash,
                        validated_route.knowledge_fingerprint,
                        validated_route.source.value,
                        validated_route.status.value,
                        validated_route.receipt_hash,
                        route_payload_json,
                        validated_route.created_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO prewrite_operation_package(
                        id, context_pack_id, route_receipt_id, project_binding_id,
                        context_pack_hash, route_receipt_hash, project_revision,
                        project_fingerprint, active_chapter_id, readiness,
                        package_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validated_package.operation_package_id,
                        validated_package.context_pack_id,
                        validated_package.route_receipt_id,
                        validated_package.project_id,
                        validated_package.context_pack_hash,
                        validated_package.route_receipt_hash,
                        validated_package.project_revision,
                        validated_package.project_fingerprint,
                        validated_package.active_chapter_id,
                        validated_package.readiness,
                        validated_package.package_hash,
                        package_payload_json,
                        validated_package.created_at.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM context_pack WHERE id = ?",
                    (validated.context_pack_id,),
                ).fetchone()
                if row is None:  # pragma: no cover - insert transaction invariant.
                    raise ContextStoreIntegrity(
                        "Context Pack insert produced no persisted row"
                    )
                stored = self._stored_from_row(connection, row)
                connection.commit()
                return stored
            except ContextStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ContextStoreIntegrity(
                    "Context Pack persistence violated the database contract"
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ContextStoreError("Context Pack persistence failed") from exc

    def get_pack(self, context_pack_id: str) -> StoredContextPack:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM context_pack WHERE id = ?",
                (context_pack_id,),
            ).fetchone()
            if row is None:
                raise ContextStoreNotFound("Context Pack was not found")
            return self._stored_from_row(connection, row)

    def get_nlcl_route(self, context_pack_id: str) -> NlclRouteReceipt:
        with self.database.connection() as connection:
            pack_row = connection.execute(
                "SELECT * FROM context_pack WHERE id = ?",
                (context_pack_id,),
            ).fetchone()
            if pack_row is None:
                raise ContextStoreNotFound("Context Pack was not found")
            pack = self._stored_from_row(connection, pack_row).pack
            route = self._route_from_connection(connection, context_pack_id)
            self._validate_route_binding(route, pack)
            return route

    def get_prewrite_operation_package(
        self,
        context_pack_id: str,
    ) -> PrewriteOperationPackage:
        with self.database.connection() as connection:
            pack_row = connection.execute(
                "SELECT * FROM context_pack WHERE id = ?",
                (context_pack_id,),
            ).fetchone()
            if pack_row is None:
                raise ContextStoreNotFound("Context Pack was not found")
            pack = self._stored_from_row(connection, pack_row).pack
            route = self._route_from_connection(connection, context_pack_id)
            package = self._prewrite_from_connection(connection, context_pack_id)
            self._validate_prewrite_binding(package, pack, route)
            return package

    def create_route_execution(
        self,
        receipt: NlclRouteExecutionReceipt,
    ) -> NlclRouteExecutionReceipt:
        validated, payload_json = self._validated_route_execution_payload(receipt)
        if (
            validated.project_kb_state_id is None
            or validated.project_kb_manifest_hash is None
        ):
            raise ContextStoreConflict(
                "Legacy unbound NLCL routes cannot create new execution receipts"
            )
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM nlcl_route_execution_receipt
                    WHERE route_receipt_id = ? AND call_ordinal = ?
                    """,
                    (validated.route_receipt_id, validated.call_ordinal),
                ).fetchone()
                if existing is not None:
                    stored = self._route_execution_from_row(connection, existing)
                    if stored.receipt_hash != validated.receipt_hash:
                        raise ContextStoreConflict(
                            "NLCL route call was already executed with a different receipt"
                        )
                    connection.commit()
                    return stored

                self._validate_route_execution_binding(connection, validated)
                connection.execute(
                    """
                    INSERT INTO nlcl_route_execution_receipt(
                        id, route_receipt_id, context_pack_id,
                        route_receipt_hash, context_pack_hash,
                        project_kb_state_id, project_kb_manifest_hash, call_ordinal,
                        run_id, tool_call_id, skill_call_id,
                        skill_receipt_id, skill_receipt_hash,
                        execution_status, domain_status, output_hash,
                        tool_result_hash, receipt_hash, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        validated.execution_receipt_id,
                        validated.route_receipt_id,
                        validated.context_pack_id,
                        validated.route_receipt_hash,
                        validated.context_pack_hash,
                        validated.project_kb_state_id,
                        validated.project_kb_manifest_hash,
                        validated.call_ordinal,
                        validated.run_id,
                        validated.tool_call_id,
                        validated.skill_call_id,
                        validated.skill_receipt_id,
                        validated.skill_receipt_hash,
                        validated.execution_status,
                        validated.domain_status,
                        validated.output_hash,
                        validated.tool_result_hash,
                        validated.receipt_hash,
                        payload_json,
                        validated.created_at.isoformat(),
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM nlcl_route_execution_receipt WHERE id = ?",
                    (validated.execution_receipt_id,),
                ).fetchone()
                if row is None:  # pragma: no cover - insert invariant.
                    raise ContextStoreIntegrity(
                        "NLCL route execution insert produced no persisted row"
                    )
                stored = self._route_execution_from_row(connection, row)
                connection.commit()
                return stored
            except ContextStoreError:
                connection.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ContextStoreIntegrity(
                    "NLCL route execution violated the database contract"
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise ContextStoreError("NLCL route execution persistence failed") from exc

    def get_route_execution(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int = 1,
    ) -> NlclRouteExecutionReceipt | None:
        if isinstance(call_ordinal, bool) or not 1 <= call_ordinal <= 64:
            raise ValueError("NLCL route call ordinal must be between 1 and 64")
        with self.database.connection() as connection:
            pack = connection.execute(
                "SELECT id FROM context_pack WHERE id = ?",
                (context_pack_id,),
            ).fetchone()
            if pack is None:
                raise ContextStoreNotFound("Context Pack was not found")
            row = connection.execute(
                """
                SELECT * FROM nlcl_route_execution_receipt
                WHERE context_pack_id = ? AND call_ordinal = ?
                """,
                (context_pack_id, call_ordinal),
            ).fetchone()
            if row is None:
                return None
            return self._route_execution_from_row(connection, row)

    def reconcile_route_execution(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int,
        run_id: str,
        tool_call_id: str,
    ) -> NlclRouteExecutionReceipt:
        route = self.get_nlcl_route(context_pack_id)
        matching_calls = [
            call for call in route.chapter_call_list if call.ordinal == call_ordinal
        ]
        if len(matching_calls) != 1:
            raise ContextStoreIntegrity("NLCL route call ordinal is not declared")
        call = matching_calls[0]
        with self.database.connection() as connection:
            tool_call = connection.execute(
                "SELECT * FROM tool_call WHERE id = ? AND run_id = ?",
                (tool_call_id, run_id),
            ).fetchone()
            if tool_call is None:
                raise ContextStoreNotFound("NLCL route ToolCall was not found")
            execution_status = str(tool_call["status"])
            if execution_status not in {"succeeded", "failed", "cancelled"}:
                raise ContextStoreConflict("NLCL route ToolCall is not terminal")
            if tool_call["result_hash"] is None or tool_call["finished_at"] is None:
                raise ContextStoreIntegrity("Terminal NLCL route ToolCall is incomplete")
            try:
                output = json.loads(str(tool_call["output_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContextStoreIntegrity(
                    "NLCL route ToolCall output is not valid JSON"
                ) from exc
            try:
                finished_at = datetime.fromisoformat(
                    str(tool_call["finished_at"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise ContextStoreIntegrity(
                    "NLCL route ToolCall finished_at is invalid"
                ) from exc

            skill_call_id: str | None = None
            skill_receipt_id: str | None = None
            skill_receipt_hash: str | None = None
            domain_status: str | None = None
            if isinstance(output, dict):
                raw_domain = output.get("domain_status")
                if raw_domain in {"healthy", "degraded", "blocked"}:
                    domain_status = str(raw_domain)
                if isinstance(output.get("receipt_id"), str):
                    skill_receipt_id = str(output["receipt_id"])
                if isinstance(output.get("receipt_hash"), str):
                    skill_receipt_hash = str(output["receipt_hash"])

            skill_rows = connection.execute(
                """
                SELECT id, payload_json FROM activity
                WHERE run_id = ? AND kind = 'skill_call' AND causation_id = ?
                ORDER BY sequence
                """,
                (run_id, tool_call["call_activity_id"]),
            ).fetchall()
            if len(skill_rows) > 1:
                raise ContextStoreIntegrity(
                    "NLCL route ToolCall produced multiple direct SkillCalls"
                )
            if skill_rows:
                try:
                    skill_payload = json.loads(str(skill_rows[0]["payload_json"]))
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ContextStoreIntegrity(
                        "NLCL route SkillCall payload is invalid"
                    ) from exc
                if not isinstance(skill_payload, dict):
                    raise ContextStoreIntegrity(
                        "NLCL route SkillCall payload is not an object"
                    )
                skill_payload = skill_payload.get("payload")
                if not isinstance(skill_payload, dict):
                    raise ContextStoreIntegrity(
                        "NLCL route SkillCall activity has no payload"
                    )
                skill_call_id = str(skill_payload.get("skill_call_id") or "") or None
                skill_output = skill_payload.get("output")
                embedded_receipt = (
                    skill_output.get("receipt")
                    if isinstance(skill_output, dict)
                    else None
                )
                if isinstance(embedded_receipt, dict):
                    try:
                        embedded_model = SkillExecutionReceipt.model_validate_json(
                            json.dumps(
                                embedded_receipt,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                            strict=True,
                        )
                    except (TypeError, ValueError, ValidationError) as exc:
                        raise ContextStoreIntegrity(
                            "NLCL route Skill receipt is invalid"
                        ) from exc
                    if (
                        embedded_model.run_id != run_id
                        or embedded_model.tool_call_id != tool_call_id
                        or embedded_model.skill_id != call.skill_id
                    ):
                        raise ContextStoreIntegrity(
                            "NLCL route Skill receipt does not match the ToolCall"
                        )
                    embedded_id = embedded_model.receipt_id
                    if skill_receipt_id is not None and skill_receipt_id != embedded_id:
                        raise ContextStoreIntegrity(
                            "ToolResult and SkillCall disagree on receipt identity"
                        )
                    skill_receipt_id = embedded_id
                    embedded_hash = canonical_json_hash(embedded_model)
                    if (
                        skill_receipt_hash is not None
                        and skill_receipt_hash != embedded_hash
                    ):
                        raise ContextStoreIntegrity(
                            "ToolResult and SkillCall disagree on receipt hash"
                        )
                    skill_receipt_hash = embedded_hash
                    embedded_domain = embedded_model.domain_status
                    if embedded_domain in {"healthy", "degraded", "blocked"}:
                        if domain_status is not None and domain_status != embedded_domain:
                            raise ContextStoreIntegrity(
                                "ToolResult and SkillCall disagree on domain status"
                            )
                        domain_status = str(embedded_domain)

            if (skill_receipt_id is None) != (skill_receipt_hash is None):
                raise ContextStoreIntegrity(
                    "NLCL route Skill receipt identity and hash must be paired"
                )
            if skill_receipt_hash is not None:
                receipt_artifacts = connection.execute(
                    """
                    SELECT content_hash FROM artifact
                    WHERE run_id = ? AND artifact_type = 'skill_execution_receipt'
                        AND content_hash = ?
                    ORDER BY created_at, id
                    """,
                    (run_id, skill_receipt_hash),
                ).fetchall()
                if len(receipt_artifacts) != 1:
                    raise ContextStoreIntegrity(
                        "NLCL route Skill receipt artifact is missing or ambiguous"
                    )
                skill_receipt_hash = str(receipt_artifacts[0]["content_hash"])
            if execution_status != "succeeded":
                domain_status = None

        receipt = build_nlcl_route_execution_receipt(
            route,
            call,
            run_id=run_id,
            tool_call_id=tool_call_id,
            skill_call_id=skill_call_id,
            skill_receipt_id=skill_receipt_id,
            skill_receipt_hash=skill_receipt_hash,
            execution_status=execution_status,
            domain_status=domain_status,
            output_hash=canonical_json_hash(output),
            tool_result_hash=str(tool_call["result_hash"]),
            created_at=finished_at,
        )
        return self.create_route_execution(receipt)

    def get_route_execution_state(
        self,
        context_pack_id: str,
        *,
        call_ordinal: int = 1,
    ) -> NlclRouteExecutionState:
        route = self.get_nlcl_route(context_pack_id)
        matching_calls = [
            call for call in route.chapter_call_list if call.ordinal == call_ordinal
        ]
        if len(matching_calls) != 1:
            raise ContextStoreNotFound("NLCL route call was not found")
        call = matching_calls[0]
        receipt = self.get_route_execution(
            context_pack_id,
            call_ordinal=call_ordinal,
        )
        if receipt is not None:
            return NlclRouteExecutionState(
                route_receipt_id=route.route_receipt_id,
                context_pack_id=context_pack_id,
                call_ordinal=call_ordinal,
                status=receipt.execution_status,
                run_id=receipt.run_id,
                tool_call_id=receipt.tool_call_id,
                receipt=receipt,
            )

        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT * FROM run WHERE context_pack_id = ?",
                (context_pack_id,),
            ).fetchone()
            if run is None:
                return NlclRouteExecutionState(
                    route_receipt_id=route.route_receipt_id,
                    context_pack_id=context_pack_id,
                    call_ordinal=call_ordinal,
                    status="awaiting_execution",
                )
            tool_call = None
            rows = connection.execute(
                """
                SELECT * FROM tool_call
                WHERE run_id = ? AND tool_name = 'skill_execute'
                ORDER BY started_at, id
                """,
                (run["id"],),
            ).fetchall()
            for row in rows:
                try:
                    arguments = json.loads(str(row["input_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(arguments, dict)
                    and arguments.get("skill_id") == call.skill_id
                    and isinstance(arguments.get("input"), dict)
                    and arguments["input"].get("schema") == call.input_contract
                ):
                    tool_call = row
                    break

            run_status = str(run["status"])
            if tool_call is None:
                if run_status not in {
                    "succeeded",
                    "failed",
                    "cancelled",
                    "interrupted",
                }:
                    status = "awaiting_execution"
                    error_code = None
                    error_message = None
                else:
                    status = "cancelled" if run_status == "cancelled" else "failed"
                    error_code, error_message = self._stored_error(
                        run["error_json"],
                        fallback_code="nlcl_route_call_missing",
                        fallback_message="Run ended before the NLCL route call was created",
                    )
                return NlclRouteExecutionState(
                    route_receipt_id=route.route_receipt_id,
                    context_pack_id=context_pack_id,
                    call_ordinal=call_ordinal,
                    status=status,
                    run_id=str(run["id"]),
                    error_code=error_code,
                    error_message=error_message,
                )

            tool_status = str(tool_call["status"])
            if tool_status in {"pending", "running"} or run_status not in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return NlclRouteExecutionState(
                    route_receipt_id=route.route_receipt_id,
                    context_pack_id=context_pack_id,
                    call_ordinal=call_ordinal,
                    status="running",
                    run_id=str(run["id"]),
                    tool_call_id=str(tool_call["id"]),
                )
            status = "cancelled" if tool_status == "cancelled" else "failed"
            error_code, error_message = self._stored_error(
                tool_call["error_json"] or run["error_json"],
                fallback_code="nlcl_route_execution_receipt_missing",
                fallback_message=(
                    "NLCL route ToolCall ended without a verifiable execution receipt"
                ),
            )
            return NlclRouteExecutionState(
                route_receipt_id=route.route_receipt_id,
                context_pack_id=context_pack_id,
                call_ordinal=call_ordinal,
                status=status,
                run_id=str(run["id"]),
                tool_call_id=str(tool_call["id"]),
                error_code=error_code,
                error_message=error_message,
            )

    @staticmethod
    def _stored_error(
        raw: object,
        *,
        fallback_code: str,
        fallback_message: str,
    ) -> tuple[str, str]:
        if raw is not None:
            try:
                payload = json.loads(str(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            detail = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(detail, dict):
                code = detail.get("code")
                message = detail.get("message")
                if isinstance(code, str) and code and isinstance(message, str) and message:
                    return code, message
        return fallback_code, fallback_message

    def list_session_packs(
        self,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[StoredContextPack]:
        if isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("Context Pack list limit must be between 1 and 500")
        with self.database.connection() as connection:
            session = connection.execute(
                "SELECT id FROM execution_session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None:
                raise ContextStoreNotFound("Context Pack session was not found")
            rows = connection.execute(
                """
                SELECT * FROM context_pack
                WHERE session_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
            return [self._stored_from_row(connection, row) for row in rows]

    def get_run_pack(self, run_id: str) -> StoredContextPack:
        with self.database.connection() as connection:
            run = connection.execute(
                "SELECT id, context_pack_id FROM run WHERE id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise ContextStoreNotFound("Agent Run was not found")
            context_pack_id = run["context_pack_id"]
            if context_pack_id is None:
                raise ContextStoreNotFound("Agent Run has no Context Pack")
            row = connection.execute(
                "SELECT * FROM context_pack WHERE id = ?",
                (str(context_pack_id),),
            ).fetchone()
            if row is None:
                raise ContextStoreIntegrity(
                    "Agent Run references a missing Context Pack"
                )
            stored = self._stored_from_row(connection, row)
            if stored.consumed_by_run_id != run_id:
                raise ContextStoreIntegrity(
                    "Context Pack consumption does not match the requested Run"
                )
            return stored

    @staticmethod
    def _validated_payload(pack: ContextPack) -> tuple[ContextPack, str]:
        try:
            payload = pack.model_dump(mode="json", by_alias=True)
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            validated = ContextPack.model_validate_json(payload_json)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Context Pack does not satisfy its model and hash invariants"
            ) from exc
        return validated, payload_json

    @staticmethod
    def _validated_route_payload(
        route: NlclRouteReceipt,
        pack: ContextPack,
    ) -> tuple[NlclRouteReceipt, str]:
        try:
            payload_json = json.dumps(
                route.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            validated = NlclRouteReceipt.model_validate_json(payload_json)
            ContextStore._validate_route_binding(validated, pack)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "NLCL route receipt does not satisfy its model and Context binding"
            ) from exc
        return validated, payload_json

    @staticmethod
    def _validated_prewrite_payload(
        package: PrewriteOperationPackage,
        pack: ContextPack,
        route: NlclRouteReceipt,
    ) -> tuple[PrewriteOperationPackage, str]:
        try:
            payload_json = json.dumps(
                package.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            validated = PrewriteOperationPackage.model_validate_json(payload_json)
            ContextStore._validate_prewrite_binding(validated, pack, route)
            expected = build_prewrite_operation_package(pack, route)
            if validated.package_hash != expected.package_hash:
                raise ValueError("prewrite package does not match deterministic inputs")
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Prewrite operation package does not satisfy its model and bindings"
            ) from exc
        return validated, payload_json

    @staticmethod
    def _validated_route_execution_payload(
        receipt: NlclRouteExecutionReceipt,
    ) -> tuple[NlclRouteExecutionReceipt, str]:
        try:
            payload_json = json.dumps(
                receipt.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            validated = NlclRouteExecutionReceipt.model_validate_json(payload_json)
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "NLCL route execution receipt does not satisfy its model invariants"
            ) from exc
        return validated, payload_json

    @staticmethod
    def _validate_route_binding(
        route: NlclRouteReceipt,
        pack: ContextPack,
    ) -> None:
        expected = {
            "context_pack_id": pack.context_pack_id,
            "context_pack_hash": pack.pack_hash,
            "project_id": pack.project_id,
            "project_revision": pack.project_revision,
            "project_fingerprint": pack.project_fingerprint,
            "project_kb_state_id": pack.project_kb_state_id,
            "project_kb_manifest_hash": pack.project_kb_manifest_hash,
            "active_chapter_id": pack.active_chapter_id,
            "profile": pack.profile,
            "knowledge_fingerprint": pack.knowledge_fingerprint,
            "objective_hash": canonical_json_hash({"objective": pack.objective}),
            "created_at": pack.created_at,
        }
        for field, value in expected.items():
            if getattr(route, field) != value:
                raise ContextStoreIntegrity(
                    f"NLCL route receipt does not match Context Pack: {field}"
                )
        included_knowledge = {
            item.source_id.removeprefix("knowledge-card:"): item
            for item in pack.included
            if item.source_id.startswith("knowledge-card:")
        }
        for card in route.selected_cards:
            fragment = included_knowledge.get(card.card_id)
            if fragment is None:
                raise ContextStoreIntegrity(
                    "NLCL selected card is not included in the Context Pack"
                )
            if (
                fragment.version != card.version_id
                or fragment.source_hash != card.content_hash
                or fragment.knowledge_provenance != card.knowledge_provenance
            ):
                raise ContextStoreIntegrity(
                    "NLCL selected card identity does not match the Context fragment"
                )

    @staticmethod
    def _validate_prewrite_binding(
        package: PrewriteOperationPackage,
        pack: ContextPack,
        route: NlclRouteReceipt,
    ) -> None:
        expected = {
            "context_pack_id": pack.context_pack_id,
            "context_pack_hash": pack.pack_hash,
            "route_receipt_id": route.route_receipt_id,
            "route_receipt_hash": route.receipt_hash,
            "project_id": pack.project_id,
            "project_revision": pack.project_revision,
            "project_fingerprint": pack.project_fingerprint,
            "active_chapter_id": pack.active_chapter_id,
            "task_goal": pack.objective,
            "created_at": pack.created_at,
        }
        for field, value in expected.items():
            if getattr(package, field) != value:
                raise ContextStoreIntegrity(
                    f"Prewrite operation package binding mismatch: {field}"
                )
        included = {item.source_id: item for item in pack.included}
        for ref in package.relevant_evidence_refs:
            fragment = included.get(ref.source_id)
            if fragment is None or (
                ref.source_type != fragment.source_type
                or ref.title != fragment.title
                or ref.authority != fragment.authority
                or ref.version != fragment.version
                or ref.source_hash != fragment.source_hash
                or ref.fragment_hash != fragment.fragment_hash
                or ref.knowledge_provenance != fragment.knowledge_provenance
            ):
                raise ContextStoreIntegrity(
                    "Prewrite evidence does not match an included Context fragment"
                )
        selected_ids = tuple(card.card_id for card in route.selected_cards)
        selected_hashes = tuple(card.content_hash for card in route.selected_cards)
        selected_provenance = tuple(
            card.knowledge_provenance
            for card in route.selected_cards
            if card.knowledge_provenance is not None
        )
        if (
            package.style_output_contract.selected_card_ids != selected_ids
            or package.style_output_contract.selected_card_hashes != selected_hashes
            or package.style_output_contract.selected_card_provenance
            != selected_provenance
        ):
            raise ContextStoreIntegrity(
                "Prewrite style contract does not match the NLCL selected cards"
            )
        budget = package.library_activation_budget
        if (
            budget.max_characters != pack.budget.max_characters
            or budget.used_characters != pack.budget.used_characters
            or budget.max_tokens != pack.budget.max_tokens
            or budget.used_tokens != pack.budget.used_tokens
            or budget.activated_source_ids
            != tuple(item.source_id for item in package.relevant_evidence_refs)
        ):
            raise ContextStoreIntegrity(
                "Prewrite activation budget does not match the Context Pack"
            )
        expected_packages = [build_prewrite_operation_package(pack, route)]
        migration_marker = "migrated_prewrite_package_requires_recompile"
        if migration_marker in package.missing_requirements:
            expected_packages.append(
                build_prewrite_operation_package(
                    pack,
                    route,
                    extra_missing_requirements=(migration_marker,),
                )
            )
        if all(package != expected_package for expected_package in expected_packages):
            raise ContextStoreIntegrity(
                "Prewrite operation package differs from its deterministic inputs"
            )

    def _route_from_connection(
        self,
        connection: sqlite3.Connection,
        context_pack_id: str,
    ) -> NlclRouteReceipt:
        row = connection.execute(
            "SELECT * FROM nlcl_route_receipt WHERE context_pack_id = ?",
            (context_pack_id,),
        ).fetchone()
        if row is None:
            raise ContextStoreIntegrity("Context Pack has no NLCL route receipt")
        try:
            route = NlclRouteReceipt.model_validate_json(str(row["payload_json"]))
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity("Persisted NLCL route receipt is invalid") from exc
        expected_columns: dict[str, object] = {
            "id": route.route_receipt_id,
            "context_pack_id": route.context_pack_id,
            "project_binding_id": route.project_id,
            "context_pack_hash": route.context_pack_hash,
            "project_revision": route.project_revision,
            "project_fingerprint": route.project_fingerprint,
            "project_kb_state_id": route.project_kb_state_id,
            "project_kb_manifest_hash": route.project_kb_manifest_hash,
            "active_chapter_id": route.active_chapter_id,
            "profile": route.profile,
            "objective_hash": route.objective_hash,
            "knowledge_fingerprint": route.knowledge_fingerprint,
            "source": route.source.value,
            "route_status": route.status.value,
            "receipt_hash": route.receipt_hash,
        }
        for column, expected in expected_columns.items():
            if row[column] != expected:
                raise ContextStoreIntegrity(
                    f"Persisted NLCL route column does not match payload: {column}"
                )
        try:
            created_at = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ContextStoreIntegrity(
                "Persisted NLCL route created_at is invalid"
            ) from exc
        if created_at != route.created_at:
            raise ContextStoreIntegrity(
                "Persisted NLCL route column does not match payload: created_at"
            )
        return route

    def _prewrite_from_connection(
        self,
        connection: sqlite3.Connection,
        context_pack_id: str,
    ) -> PrewriteOperationPackage:
        row = connection.execute(
            "SELECT * FROM prewrite_operation_package WHERE context_pack_id = ?",
            (context_pack_id,),
        ).fetchone()
        if row is None:
            raise ContextStoreIntegrity(
                "Context Pack has no prewrite operation package"
            )
        try:
            package = PrewriteOperationPackage.model_validate_json(
                str(row["payload_json"])
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Persisted prewrite operation package is invalid"
            ) from exc
        expected_columns: dict[str, object] = {
            "id": package.operation_package_id,
            "context_pack_id": package.context_pack_id,
            "route_receipt_id": package.route_receipt_id,
            "project_binding_id": package.project_id,
            "context_pack_hash": package.context_pack_hash,
            "route_receipt_hash": package.route_receipt_hash,
            "project_revision": package.project_revision,
            "project_fingerprint": package.project_fingerprint,
            "active_chapter_id": package.active_chapter_id,
            "readiness": package.readiness,
            "package_hash": package.package_hash,
        }
        for column, expected_value in expected_columns.items():
            if row[column] != expected_value:
                raise ContextStoreIntegrity(
                    "Persisted prewrite package column does not match payload: "
                    f"{column}"
                )
        try:
            created_at = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ContextStoreIntegrity(
                "Persisted prewrite package created_at is invalid"
            ) from exc
        if created_at != package.created_at:
            raise ContextStoreIntegrity(
                "Persisted prewrite package column does not match payload: created_at"
            )
        return package

    def _validate_route_execution_binding(
        self,
        connection: sqlite3.Connection,
        receipt: NlclRouteExecutionReceipt,
    ) -> None:
        route = self._route_from_connection(connection, receipt.context_pack_id)
        if (
            route.route_receipt_id != receipt.route_receipt_id
            or route.receipt_hash != receipt.route_receipt_hash
            or route.context_pack_hash != receipt.context_pack_hash
            or route.project_kb_state_id != receipt.project_kb_state_id
            or route.project_kb_manifest_hash != receipt.project_kb_manifest_hash
        ):
            raise ContextStoreIntegrity(
                "NLCL route execution does not match its immutable route receipt"
            )
        matching_calls = [
            call for call in route.chapter_call_list if call.ordinal == receipt.call_ordinal
        ]
        if len(matching_calls) != 1:
            raise ContextStoreIntegrity("NLCL route execution call ordinal is not declared")
        call = matching_calls[0]
        if call.skill_id != receipt.skill_id or call.input_contract != receipt.input_contract:
            raise ContextStoreIntegrity(
                "NLCL route execution does not match its declared chapter call"
            )

        run = connection.execute(
            "SELECT * FROM run WHERE id = ?",
            (receipt.run_id,),
        ).fetchone()
        if run is None:
            raise ContextStoreNotFound("NLCL route execution Run was not found")
        if (
            run["context_pack_id"] != receipt.context_pack_id
            or run["project_binding_id"] != route.project_id
            or int(run["project_revision"]) != route.project_revision
            or run["project_fingerprint"] != route.project_fingerprint
        ):
            raise ContextStoreIntegrity(
                "NLCL route execution Run does not match the route project truth"
            )

        tool_call = connection.execute(
            "SELECT * FROM tool_call WHERE id = ?",
            (receipt.tool_call_id,),
        ).fetchone()
        if tool_call is None:
            raise ContextStoreNotFound("NLCL route ToolCall was not found")
        if (
            tool_call["run_id"] != receipt.run_id
            or tool_call["tool_name"] != "skill_execute"
            or tool_call["status"] != receipt.execution_status
            or tool_call["result_hash"] != receipt.tool_result_hash
        ):
            raise ContextStoreIntegrity(
                "NLCL route execution does not match its terminal ToolCall"
            )
        try:
            arguments = json.loads(str(tool_call["input_json"]))
            output = json.loads(str(tool_call["output_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextStoreIntegrity(
                "NLCL route ToolCall payload is not valid JSON"
            ) from exc
        if (
            not isinstance(arguments, dict)
            or arguments.get("skill_id") != receipt.skill_id
            or not isinstance(arguments.get("input"), dict)
            or arguments["input"].get("schema") != receipt.input_contract
        ):
            raise ContextStoreIntegrity(
                "NLCL route ToolCall arguments do not match the declared call"
            )
        if canonical_json_hash(output) != receipt.output_hash:
            raise ContextStoreIntegrity("NLCL route ToolCall output hash does not match")

        if receipt.skill_id == "novel-creation-suite.prewrite-gate":
            self._validate_prewrite_audit_binding(
                connection,
                receipt,
                arguments["input"],
            )
        if receipt.skill_id == "novel-creation-suite.creation-handoff":
            self._validate_creation_handoff_binding(
                connection,
                receipt,
                arguments["input"],
            )

        if receipt.execution_status == "succeeded":
            if not isinstance(output, dict) or (
                output.get("skill_id") != receipt.skill_id
                or output.get("receipt_id") != receipt.skill_receipt_id
                or output.get("receipt_hash") != receipt.skill_receipt_hash
                or output.get("domain_status") != receipt.domain_status
            ):
                raise ContextStoreIntegrity(
                    "NLCL route ToolCall result does not match the Skill receipt"
                )
        if receipt.skill_call_id is not None:
            self._validate_skill_call_activity(connection, receipt)

    def _validate_prewrite_audit_binding(
        self,
        connection: sqlite3.Connection,
        receipt: NlclRouteExecutionReceipt,
        raw_input: object,
    ) -> None:
        try:
            request = CreationPrewriteGateRequest.model_validate_json(
                json.dumps(
                    raw_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                strict=True,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Prewrite gate ToolCall input is invalid"
            ) from exc

        package = self._prewrite_from_connection(connection, receipt.context_pack_id)
        if request.operation_package != package:
            raise ContextStoreIntegrity(
                "Prewrite gate ToolCall does not contain the persisted operation package"
            )

        audit_row = connection.execute(
            """
            SELECT * FROM nlcl_route_execution_receipt
            WHERE context_pack_id = ? AND call_ordinal = 1
            """,
            (receipt.context_pack_id,),
        ).fetchone()
        if audit_row is None:
            raise ContextStoreIntegrity(
                "Prewrite gate has no preceding project-audit execution receipt"
            )
        audit_receipt = self._route_execution_from_row(connection, audit_row)
        if (
            audit_receipt.execution_status != "succeeded"
            or audit_receipt.skill_id != "novel-creation-suite.project-audit"
            or audit_receipt.run_id != receipt.run_id
            or audit_receipt.skill_receipt_hash is None
            or audit_receipt.domain_status is None
        ):
            raise ContextStoreIntegrity(
                "Prewrite gate does not follow a succeeded project-audit receipt"
            )

        audit_tool_call = connection.execute(
            "SELECT output_json FROM tool_call WHERE id = ?",
            (audit_receipt.tool_call_id,),
        ).fetchone()
        if audit_tool_call is None:
            raise ContextStoreIntegrity(
                "Project-audit ToolCall was not found for prewrite binding"
            )
        try:
            audit_output = json.loads(str(audit_tool_call["output_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextStoreIntegrity(
                "Project-audit ToolResult is invalid for prewrite binding"
            ) from exc
        audit_summary = (
            audit_output.get("audit")
            if isinstance(audit_output, dict)
            else None
        )
        finding_count = (
            audit_summary.get("finding_count")
            if isinstance(audit_summary, dict)
            else None
        )
        if isinstance(finding_count, bool) or not isinstance(finding_count, int):
            raise ContextStoreIntegrity(
                "Project-audit ToolResult has no durable finding count"
            )

        expected = {
            "project_audit_execution_receipt_hash": audit_receipt.receipt_hash,
            "project_audit_skill_receipt_hash": audit_receipt.skill_receipt_hash,
            "project_audit_tool_result_hash": audit_receipt.tool_result_hash,
            "project_audit_domain_status": audit_receipt.domain_status,
            "project_audit_finding_count": finding_count,
        }
        for field, value in expected.items():
            if getattr(request, field) != value:
                raise ContextStoreIntegrity(
                    f"Prewrite gate project-audit binding mismatch: {field}"
                )

    def _validate_creation_handoff_binding(
        self,
        connection: sqlite3.Connection,
        receipt: NlclRouteExecutionReceipt,
        raw_input: object,
    ) -> None:
        try:
            request = CreationHandoffRequest.model_validate_json(
                json.dumps(
                    raw_input,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                strict=True,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Creation handoff ToolCall input is invalid"
            ) from exc

        package = self._prewrite_from_connection(connection, receipt.context_pack_id)
        pack_row = connection.execute(
            "SELECT * FROM context_pack WHERE id = ?",
            (receipt.context_pack_id,),
        ).fetchone()
        if pack_row is None:
            raise ContextStoreIntegrity("Creation handoff Context Pack is missing")
        pack = self._stored_from_row(connection, pack_row).pack
        if request.operation_package != package:
            raise ContextStoreIntegrity(
                "Creation handoff ToolCall does not contain the persisted operation package"
            )

        audit_receipt = self._required_route_execution(
            connection,
            receipt.context_pack_id,
            call_ordinal=1,
            expected_skill_id="novel-creation-suite.project-audit",
            run_id=receipt.run_id,
        )
        prewrite_receipt = self._required_route_execution(
            connection,
            receipt.context_pack_id,
            call_ordinal=2,
            expected_skill_id="novel-creation-suite.prewrite-gate",
            run_id=receipt.run_id,
        )
        audit_output = self._tool_output(connection, audit_receipt.tool_call_id)
        prewrite_output = self._tool_output(connection, prewrite_receipt.tool_call_id)
        audit_summary = audit_output.get("audit") if isinstance(audit_output, dict) else None
        finding_count = (
            audit_summary.get("finding_count")
            if isinstance(audit_summary, dict)
            else None
        )
        if isinstance(finding_count, bool) or not isinstance(finding_count, int):
            raise ContextStoreIntegrity(
                "Project-audit ToolResult has no durable finding count for handoff"
            )
        prewrite_gate = (
            prewrite_output.get("prewrite_gate")
            if isinstance(prewrite_output, dict)
            else None
        )
        if not isinstance(prewrite_gate, dict):
            raise ContextStoreIntegrity(
                "Prewrite-gate ToolResult has no durable gate payload for handoff"
            )
        failure_signatures = prewrite_gate.get("failure_signatures")
        if not isinstance(failure_signatures, list) or any(
            not isinstance(item, str) for item in failure_signatures
        ):
            raise ContextStoreIntegrity(
                "Prewrite-gate ToolResult has invalid failure signatures for handoff"
            )

        expected = {
            "workflow_plan_id": self._workflow_plan_id(connection, receipt.run_id),
            "knowledge_fingerprint": pack.knowledge_fingerprint,
            "operation_package": package,
            "project_audit_execution_receipt_hash": audit_receipt.receipt_hash,
            "project_audit_skill_receipt_hash": audit_receipt.skill_receipt_hash,
            "project_audit_tool_result_hash": audit_receipt.tool_result_hash,
            "project_audit_checkpoint_hash": self._workflow_step_checkpoint_hash(
                connection,
                receipt.run_id,
                step_ordinal=1,
            ),
            "project_audit_domain_status": audit_receipt.domain_status,
            "project_audit_finding_count": finding_count,
            "prewrite_gate_execution_receipt_hash": prewrite_receipt.receipt_hash,
            "prewrite_gate_skill_receipt_hash": prewrite_receipt.skill_receipt_hash,
            "prewrite_gate_tool_result_hash": prewrite_receipt.tool_result_hash,
            "prewrite_gate_checkpoint_hash": self._workflow_step_checkpoint_hash(
                connection,
                receipt.run_id,
                step_ordinal=2,
            ),
            "prewrite_gate_domain_status": prewrite_receipt.domain_status,
            "prewrite_gate_status": prewrite_gate.get("gate_status"),
            "prewrite_gate_formal_draft_allowed": prewrite_gate.get(
                "formal_draft_allowed"
            ),
            "prewrite_gate_failure_signatures": failure_signatures,
        }
        for field, value in expected.items():
            if getattr(request, field) != value:
                raise ContextStoreIntegrity(
                    f"Creation handoff binding mismatch: {field}"
                )
        plan_hash = self._workflow_plan_hash(connection, receipt.run_id)
        if request.workflow_plan_hash != plan_hash:
            raise ContextStoreIntegrity(
                "Creation handoff binding mismatch: workflow_plan_hash"
            )

    def _required_route_execution(
        self,
        connection: sqlite3.Connection,
        context_pack_id: str,
        *,
        call_ordinal: int,
        expected_skill_id: str,
        run_id: str,
    ) -> NlclRouteExecutionReceipt:
        row = connection.execute(
            """
            SELECT * FROM nlcl_route_execution_receipt
            WHERE context_pack_id = ? AND call_ordinal = ?
            """,
            (context_pack_id, call_ordinal),
        ).fetchone()
        if row is None:
            raise ContextStoreIntegrity("Creation handoff is missing a prior receipt")
        prior = self._route_execution_from_row(connection, row)
        if (
            prior.execution_status != "succeeded"
            or prior.skill_id != expected_skill_id
            or prior.run_id != run_id
            or prior.skill_receipt_hash is None
            or prior.domain_status is None
        ):
            raise ContextStoreIntegrity(
                "Creation handoff does not follow the required succeeded receipts"
            )
        return prior

    def _tool_output(
        self,
        connection: sqlite3.Connection,
        tool_call_id: str,
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT output_json FROM tool_call WHERE id = ?",
            (tool_call_id,),
        ).fetchone()
        if row is None:
            raise ContextStoreIntegrity("Creation handoff prior ToolCall was not found")
        try:
            output = json.loads(str(row["output_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextStoreIntegrity(
                "Creation handoff prior ToolResult is invalid"
            ) from exc
        if not isinstance(output, dict):
            raise ContextStoreIntegrity("Creation handoff prior ToolResult is not an object")
        return output

    def _workflow_plan_id(self, connection: sqlite3.Connection, run_id: str) -> str:
        payload = self._workflow_checkpoint_payload(connection, run_id, sequence=0)
        plan_id = payload.get("workflow_plan_id")
        if not isinstance(plan_id, str):
            raise ContextStoreIntegrity("Workflow Plan payload has no plan ID")
        return plan_id

    def _workflow_plan_hash(self, connection: sqlite3.Connection, run_id: str) -> str:
        payload = self._workflow_checkpoint_payload(connection, run_id, sequence=0)
        plan_hash = payload.get("plan_hash")
        if not isinstance(plan_hash, str):
            raise ContextStoreIntegrity("Workflow Plan payload has no plan hash")
        return plan_hash

    def _workflow_step_checkpoint_hash(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        *,
        step_ordinal: int,
    ) -> str:
        payload = self._workflow_checkpoint_payload(
            connection,
            run_id,
            sequence=step_ordinal,
        )
        if payload.get("step_ordinal") != step_ordinal:
            raise ContextStoreIntegrity(
                "Workflow checkpoint payload does not match the requested step"
            )
        checkpoint_hash = payload.get("checkpoint_hash")
        if not isinstance(checkpoint_hash, str):
            raise ContextStoreIntegrity("Workflow checkpoint payload has no hash")
        return checkpoint_hash

    @staticmethod
    def _workflow_checkpoint_payload(
        connection: sqlite3.Connection,
        run_id: str,
        *,
        sequence: int,
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT state_json FROM checkpoint
            WHERE run_id = ? AND sequence = ?
            """,
            (run_id, sequence),
        ).fetchone()
        if row is None:
            raise ContextStoreIntegrity("Workflow checkpoint is missing for handoff")
        try:
            payload = json.loads(str(row["state_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ContextStoreIntegrity("Workflow checkpoint payload is invalid") from exc
        if not isinstance(payload, dict):
            raise ContextStoreIntegrity("Workflow checkpoint payload is not an object")
        return payload

    @staticmethod
    def _validate_skill_call_activity(
        connection: sqlite3.Connection,
        receipt: NlclRouteExecutionReceipt,
    ) -> None:
        rows = connection.execute(
            """
            SELECT payload_json FROM activity
            WHERE run_id = ? AND kind = 'skill_call'
            ORDER BY sequence
            """,
            (receipt.run_id,),
        ).fetchall()
        matched: dict[str, object] | None = None
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ContextStoreIntegrity("Persisted SkillCall activity is invalid") from exc
            if isinstance(payload, dict):
                activity_payload = payload.get("payload")
                if (
                    isinstance(activity_payload, dict)
                    and activity_payload.get("skill_call_id") == receipt.skill_call_id
                ):
                    matched = activity_payload
                    break
        if matched is None:
            raise ContextStoreIntegrity("NLCL route SkillCall activity was not found")
        skill_output = matched.get("output")
        skill_receipt = (
            skill_output.get("receipt")
            if isinstance(skill_output, dict)
            else None
        )
        if (
            matched.get("skill_id") != receipt.skill_id
            or not isinstance(skill_receipt, dict)
            or skill_receipt.get("receipt_id") != receipt.skill_receipt_id
        ):
            raise ContextStoreIntegrity(
                "NLCL route execution does not match the persisted SkillCall"
            )
        if (
            receipt.skill_receipt_hash is None
            or canonical_json_hash(skill_receipt) != receipt.skill_receipt_hash
        ):
            raise ContextStoreIntegrity(
                "NLCL route Skill receipt hash does not match the SkillCall"
            )
        artifacts = connection.execute(
            """
            SELECT id FROM artifact
            WHERE run_id = ? AND artifact_type = 'skill_execution_receipt'
                AND content_hash = ?
            """,
            (receipt.run_id, receipt.skill_receipt_hash),
        ).fetchall()
        if len(artifacts) != 1:
            raise ContextStoreIntegrity(
                "NLCL route Skill receipt artifact is missing or ambiguous"
            )

    def _route_execution_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> NlclRouteExecutionReceipt:
        try:
            receipt = NlclRouteExecutionReceipt.model_validate_json(
                str(row["payload_json"])
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Persisted NLCL route execution receipt is invalid"
            ) from exc
        expected_columns: dict[str, object] = {
            "id": receipt.execution_receipt_id,
            "route_receipt_id": receipt.route_receipt_id,
            "context_pack_id": receipt.context_pack_id,
            "route_receipt_hash": receipt.route_receipt_hash,
            "context_pack_hash": receipt.context_pack_hash,
            "project_kb_state_id": receipt.project_kb_state_id,
            "project_kb_manifest_hash": receipt.project_kb_manifest_hash,
            "call_ordinal": receipt.call_ordinal,
            "run_id": receipt.run_id,
            "tool_call_id": receipt.tool_call_id,
            "skill_call_id": receipt.skill_call_id,
            "skill_receipt_id": receipt.skill_receipt_id,
            "skill_receipt_hash": receipt.skill_receipt_hash,
            "execution_status": receipt.execution_status,
            "domain_status": receipt.domain_status,
            "output_hash": receipt.output_hash,
            "tool_result_hash": receipt.tool_result_hash,
            "receipt_hash": receipt.receipt_hash,
        }
        for column, expected in expected_columns.items():
            if row[column] != expected:
                raise ContextStoreIntegrity(
                    f"Persisted NLCL route execution column does not match payload: {column}"
                )
        try:
            created_at = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ContextStoreIntegrity(
                "Persisted NLCL route execution created_at is invalid"
            ) from exc
        if created_at != receipt.created_at:
            raise ContextStoreIntegrity(
                "Persisted NLCL route execution column does not match payload: created_at"
            )
        self._validate_route_execution_binding(connection, receipt)
        return receipt

    def _stored_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> StoredContextPack:
        try:
            pack = ContextPack.model_validate_json(str(row["payload_json"]))
        except (TypeError, ValueError, ValidationError) as exc:
            raise ContextStoreIntegrity(
                "Persisted Context Pack payload is invalid"
            ) from exc

        expected_columns: dict[str, object] = {
            "id": pack.context_pack_id,
            "session_id": pack.session_id,
            "project_binding_id": pack.project_id,
            "history_through_ordinal": pack.history_through_ordinal,
            "objective": pack.objective,
            "active_chapter_id": pack.active_chapter_id,
            "profile": pack.profile,
            "project_revision": pack.project_revision,
            "project_fingerprint": pack.project_fingerprint,
            "project_kb_state_id": pack.project_kb_state_id,
            "project_kb_manifest_hash": pack.project_kb_manifest_hash,
            "knowledge_fingerprint": self._pack_knowledge_fingerprint(pack),
            "readiness": pack.readiness.value,
            "pack_hash": pack.pack_hash,
            "injection_hash": pack.injection_hash,
            "provider_messages_hash": pack.provider_messages_hash,
        }
        for column, expected in expected_columns.items():
            actual = row[column]
            if actual != expected:
                raise ContextStoreIntegrity(
                    f"Persisted Context Pack column does not match payload: {column}"
                )
        try:
            created_at = datetime.fromisoformat(
                str(row["created_at"]).replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise ContextStoreIntegrity(
                "Persisted Context Pack created_at is invalid"
            ) from exc
        if created_at != pack.created_at:
            raise ContextStoreIntegrity(
                "Persisted Context Pack column does not match payload: created_at"
            )

        consumers = connection.execute(
            """
            SELECT id, session_id, project_binding_id, objective,
                   project_revision, project_fingerprint
            FROM run WHERE context_pack_id = ?
            ORDER BY id
            """,
            (pack.context_pack_id,),
        ).fetchall()
        if len(consumers) > 1:
            raise ContextStoreIntegrity(
                "Context Pack is consumed by more than one Agent Run"
            )
        consumed_by_run_id: str | None = None
        if consumers:
            consumer = consumers[0]
            run_binding = {
                "session_id": pack.session_id,
                "project_binding_id": pack.project_id,
                "objective": pack.objective,
                "project_revision": pack.project_revision,
                "project_fingerprint": pack.project_fingerprint,
            }
            for column, expected in run_binding.items():
                if consumer[column] != expected:
                    raise ContextStoreIntegrity(
                        f"Context Pack consumer does not match payload: {column}"
                    )
            consumed_by_run_id = str(consumer["id"])
        return StoredContextPack(
            pack=pack,
            consumed_by_run_id=consumed_by_run_id,
        )

    @staticmethod
    def _pack_knowledge_fingerprint(pack: ContextPack) -> str | None:
        if "knowledge_fingerprint" not in ContextPack.model_fields:
            return None
        value = getattr(pack, "knowledge_fingerprint", None)
        return None if value is None else str(value)


__all__ = [
    "ContextStore",
    "ContextStoreConflict",
    "ContextStoreError",
    "ContextStoreIntegrity",
    "ContextStoreNotFound",
    "StoredContextPack",
]
