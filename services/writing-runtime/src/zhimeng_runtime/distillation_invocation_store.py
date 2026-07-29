from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.distillation_invocation_models import (
    DistillationCandidateCard,
    DistillationInvocation,
    DistillationInvocationStatus,
    DistillationOutputSubmission,
    distillation_invocation_head_hash,
    distillation_invocation_id,
)
from zhimeng_runtime.distillation_receipt_models import (
    DistillationCandidateOutputPayload,
    DistillationFinalResultPayload,
    DistillationInvocationReceipt,
    DistillationPreparedPayload,
    DistillationProviderDispatchPayload,
    DistillationProviderTurnResultPayload,
    DistillationReceiptKind,
    DistillationRecoveryDecisionPayload,
    DistillationRecoveryDossierPayload,
    distillation_provider_turn_chain_hash,
    validate_distillation_receipt_chain,
)
from zhimeng_runtime.knowledge_models import (
    KnowledgeCandidateOrigin,
    knowledge_candidate_origin_hash,
)
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


class DistillationInvocationStoreError(RuntimeError):
    pass


class DistillationInvocationStoreNotFound(DistillationInvocationStoreError):
    pass


class DistillationInvocationStoreConflict(DistillationInvocationStoreError):
    pass


class DistillationInvocationStoreIntegrity(DistillationInvocationStoreError):
    pass


@dataclass(frozen=True, slots=True)
class DistillationInvocationSnapshot:
    invocation: DistillationInvocation
    last_receipt_id: str
    receipts: tuple[DistillationInvocationReceipt, ...]


class DistillationInvocationStore:
    """Runtime-owned persistence for deep distillation invocations.

    The database schema intentionally makes the ledger hard to mutate through
    SQLite triggers.  This store is the single normal write path: create the
    prepared root, append one immutable receipt at a time, then CAS-update the
    invocation head so the scalar head and typed receipt chain prove each other.
    """

    def __init__(self, database: SqliteStore) -> None:
        self.database = database

    def create_prepared(
        self,
        *,
        receipt: DistillationInvocationReceipt,
        idempotency_key: str,
    ) -> DistillationInvocationSnapshot:
        if receipt.kind is not DistillationReceiptKind.PREPARED:
            raise ValueError("distillation invocation root must be a prepared receipt")
        validate_distillation_receipt_chain((receipt,))
        payload = receipt.payload
        if not isinstance(payload, DistillationPreparedPayload):
            raise ValueError("distillation prepared receipt has the wrong payload")
        expected_invocation_id = distillation_invocation_id(
            source_receipt_id=payload.source.source_receipt_id,
            project_id=payload.project.project_id,
            idempotency_key=idempotency_key,
        )
        if receipt.invocation_id != expected_invocation_id:
            raise ValueError("distillation root receipt does not match idempotency key")

        now = receipt.created_at.astimezone(UTC)
        material = {
            "invocation_id": receipt.invocation_id,
            "source": payload.source,
            "project": payload.project,
            "knowledge": payload.knowledge,
            "instruction": payload.instruction,
            "provider": payload.provider,
            "anchor_catalog_id": payload.anchor_catalog_id,
            "anchor_catalog_hash": payload.anchor_catalog_hash,
            "retry_of": payload.retry_of,
            "request_hash": payload.request_hash,
            "idempotency_key": idempotency_key,
            "status": DistillationInvocationStatus.PREPARED,
            "state_revision": 1,
            "provider_replay_count": 0,
            "last_receipt_ordinal": 1,
            "last_receipt_hash": receipt.receipt_hash,
            "created_at": now,
            "updated_at": now,
        }
        draft = DistillationInvocation.model_construct(**material, head_hash="0" * 64)
        invocation = DistillationInvocation(
            **material,
            head_hash=distillation_invocation_head_hash(draft),
        )

        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self._invocation_row(connection, invocation.invocation_id) is not None:
                    raise DistillationInvocationStoreConflict(
                        "distillation invocation already exists"
                    )
                self._assert_static_bindings(connection, invocation)
                self._insert_invocation_root(connection, invocation, receipt.receipt_id)
                self._insert_receipt(connection, receipt)
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DistillationInvocationStoreConflict(
                    f"distillation invocation root conflicted: {exc}"
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return self.get(invocation.invocation_id)

    def append_receipt(
        self,
        receipt: DistillationInvocationReceipt,
        *,
        persist_candidates: bool = True,
    ) -> DistillationInvocationSnapshot:
        if receipt.kind is DistillationReceiptKind.PREPARED:
            raise ValueError("prepared receipt must be created through create_prepared")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._require_invocation_row(connection, receipt.invocation_id)
                head = self._invocation_from_row(row)
                old_last_receipt_id = str(row["last_receipt_id"])
                prior_receipts = self._receipts_in_connection(
                    connection,
                    receipt.invocation_id,
                )
                self._validate_snapshot_parts(
                    head=head,
                    last_receipt_id=old_last_receipt_id,
                    receipts=prior_receipts,
                )
                if self._receipt_row(connection, receipt.receipt_id) is not None:
                    raise DistillationInvocationStoreConflict(
                        "distillation receipt already exists"
                    )
                idem = connection.execute(
                    """
                    SELECT id FROM distillation_invocation_receipt
                    WHERE idempotency_key = ?
                    """,
                    (receipt.idempotency_key,),
                ).fetchone()
                if idem is not None:
                    raise DistillationInvocationStoreConflict(
                        "distillation receipt idempotency key was already used"
                    )
                self._assert_receipt_matches_head(
                    receipt,
                    head=head,
                    last_receipt_id=old_last_receipt_id,
                )

                self._insert_receipt(connection, receipt)
                receipts_after = (*prior_receipts, receipt)
                validate_distillation_receipt_chain(receipts_after)
                if (
                    persist_candidates
                    and receipt.kind is DistillationReceiptKind.CANDIDATE_OUTPUT
                ):
                    payload = receipt.payload
                    assert isinstance(payload, DistillationCandidateOutputPayload)
                    self._insert_candidate_output(connection, head, payload, receipt)
                new_head = self._head_after_receipt(
                    head,
                    receipt,
                    receipts_after=receipts_after,
                )
                self._update_invocation_head(
                    connection,
                    invocation=new_head,
                    last_receipt_id=receipt.receipt_id,
                    old_revision=head.state_revision,
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DistillationInvocationStoreConflict(
                    f"distillation receipt append conflicted: {exc}"
                ) from exc
            except Exception:
                connection.rollback()
                raise
        return self.get(receipt.invocation_id)

    def get(self, invocation_id: str) -> DistillationInvocationSnapshot:
        with self.database.connection() as connection:
            row = self._require_invocation_row(connection, invocation_id)
            invocation = self._invocation_from_row(row)
            receipts = self._receipts_in_connection(connection, invocation_id)
            self._validate_snapshot_parts(
                head=invocation,
                last_receipt_id=str(row["last_receipt_id"]),
                receipts=receipts,
            )
            return DistillationInvocationSnapshot(
                invocation=invocation,
                last_receipt_id=str(row["last_receipt_id"]),
                receipts=receipts,
            )

    def list_receipts(self, invocation_id: str) -> tuple[DistillationInvocationReceipt, ...]:
        return self.get(invocation_id).receipts

    def _assert_static_bindings(
        self,
        connection: sqlite3.Connection,
        invocation: DistillationInvocation,
    ) -> None:
        source_row = connection.execute(
            "SELECT * FROM source_import_execution_receipt WHERE id = ?",
            (invocation.source.source_receipt_id,),
        ).fetchone()
        if source_row is None:
            raise DistillationInvocationStoreNotFound(
                "distillation source execution receipt was not found"
            )
        if (
            str(source_row["source_import_id"]) != invocation.source.source_import_id
            or str(source_row["receipt_hash"]) != invocation.source.source_receipt_hash
            or str(source_row["source_packet_fingerprint"])
            != invocation.source.source_packet_fingerprint
        ):
            raise DistillationInvocationStoreConflict(
                "distillation source binding is stale"
            )
        project_row = connection.execute(
            "SELECT * FROM project_binding WHERE id = ?",
            (invocation.project.project_id,),
        ).fetchone()
        if project_row is None:
            raise DistillationInvocationStoreNotFound(
                "distillation project binding was not found"
            )
        if (
            int(project_row["revision"]) != invocation.project.project_revision
            or str(project_row["fingerprint"]) != invocation.project.project_fingerprint
        ):
            raise DistillationInvocationStoreConflict(
                "distillation project binding is stale"
            )
        scope_row = connection.execute(
            "SELECT * FROM knowledge_scope WHERE id = ?",
            (invocation.knowledge.scope_id,),
        ).fetchone()
        if scope_row is None:
            raise DistillationInvocationStoreNotFound(
                "distillation knowledge scope was not found"
            )
        if int(scope_row["state_revision"]) != invocation.knowledge.scope_state_revision:
            raise DistillationInvocationStoreConflict(
                "distillation knowledge scope is stale"
            )
        version_row = connection.execute(
            "SELECT * FROM skill_version WHERE id = ?",
            (invocation.instruction.skill_version_id,),
        ).fetchone()
        if version_row is None:
            raise DistillationInvocationStoreNotFound(
                "distillation instruction skill version was not found"
            )
        if (
            str(version_row["skill_id"]) != invocation.instruction.skill_id
            or str(version_row["version"]) != invocation.instruction.skill_version
            or str(version_row["manifest_fingerprint"])
            != invocation.instruction.manifest_fingerprint
            or str(version_row["fingerprint"])
            != invocation.instruction.package_fingerprint
        ):
            raise DistillationInvocationStoreConflict(
                "distillation instruction skill version is stale"
            )
        activation_row = connection.execute(
            "SELECT * FROM skill_activation_receipt WHERE id = ?",
            (invocation.instruction.activation_receipt_id,),
        ).fetchone()
        if activation_row is None:
            raise DistillationInvocationStoreNotFound(
                "distillation instruction activation receipt was not found"
            )
        if (
            str(activation_row["receipt_hash"])
            != invocation.instruction.activation_receipt_hash
            or int(activation_row["state_revision"])
            != invocation.instruction.activation_state_revision
            or str(activation_row["next_version_id"])
            != invocation.instruction.skill_version_id
        ):
            raise DistillationInvocationStoreConflict(
                "distillation instruction activation is stale"
            )

    @staticmethod
    def _assert_receipt_matches_head(
        receipt: DistillationInvocationReceipt,
        *,
        head: DistillationInvocation,
        last_receipt_id: str,
    ) -> None:
        if receipt.invocation_id != head.invocation_id:
            raise DistillationInvocationStoreConflict(
                "distillation receipt belongs to another invocation"
            )
        if receipt.expected_invocation_revision != head.state_revision:
            raise DistillationInvocationStoreConflict(
                "distillation receipt CAS revision is stale"
            )
        if receipt.ordinal != head.state_revision + 1:
            raise DistillationInvocationStoreConflict(
                "distillation receipt ordinal does not advance the head"
            )
        if receipt.parent_receipt_id != last_receipt_id:
            raise DistillationInvocationStoreConflict(
                "distillation receipt parent does not match the current head"
            )

    @classmethod
    def _validate_snapshot_parts(
        cls,
        *,
        head: DistillationInvocation,
        last_receipt_id: str,
        receipts: tuple[DistillationInvocationReceipt, ...],
    ) -> None:
        validate_distillation_receipt_chain(receipts)
        latest = receipts[-1]
        if (
            latest.receipt_id != last_receipt_id
            or latest.ordinal != head.last_receipt_ordinal
            or latest.receipt_hash != head.last_receipt_hash
            or latest.resulting_invocation_revision != head.state_revision
            or latest.resulting_status != head.status
        ):
            raise DistillationInvocationStoreIntegrity(
                "distillation invocation head no longer matches its ledger"
            )

    @classmethod
    def _head_after_receipt(
        cls,
        head: DistillationInvocation,
        receipt: DistillationInvocationReceipt,
        *,
        receipts_after: tuple[DistillationInvocationReceipt, ...],
    ) -> DistillationInvocation:
        material = {
            name: getattr(head, name)
            for name in DistillationInvocation.model_fields
            if name != "head_hash"
        }
        material.update(
            {
                "status": receipt.resulting_status,
                "state_revision": receipt.resulting_invocation_revision,
                "last_receipt_ordinal": receipt.ordinal,
                "last_receipt_hash": receipt.receipt_hash,
                "updated_at": receipt.created_at.astimezone(UTC),
            }
        )
        if receipt.kind is DistillationReceiptKind.PROVIDER_DISPATCH:
            payload = receipt.payload
            assert isinstance(payload, DistillationProviderDispatchPayload)
            material.update(
                {
                    "last_dispatch_receipt_id": receipt.receipt_id,
                    "last_dispatch_receipt_hash": receipt.receipt_hash,
                    "last_dispatch_turn_ordinal": payload.turn_ordinal,
                }
            )
        elif receipt.kind is DistillationReceiptKind.PROVIDER_TURN_RESULT:
            payload = receipt.payload
            assert isinstance(payload, DistillationProviderTurnResultPayload)
            material.update(
                {
                    "last_turn_result_receipt_id": receipt.receipt_id,
                    "last_turn_result_receipt_hash": receipt.receipt_hash,
                    "provider_turn_count": payload.turn_ordinal,
                    "provider_turn_chain_hash": distillation_provider_turn_chain_hash(
                        receipts_after
                    ),
                }
            )
        elif receipt.kind is DistillationReceiptKind.CANDIDATE_OUTPUT:
            payload = receipt.payload
            assert isinstance(payload, DistillationCandidateOutputPayload)
            material.update(
                {
                    "candidate_output_receipt_id": receipt.receipt_id,
                    "candidate_output_receipt_hash": receipt.receipt_hash,
                    "candidate_set_hash": payload.candidate_set.candidate_set_hash,
                    "work_count": 1,
                    "voice_count": 1,
                    "validator_count": 1,
                }
            )
        elif receipt.kind is DistillationReceiptKind.RECOVERY_DOSSIER:
            payload = receipt.payload
            assert isinstance(payload, DistillationRecoveryDossierPayload)
            material.update(
                {
                    "recovery_dossier_receipt_id": receipt.receipt_id,
                    "recovery_dossier_receipt_hash": receipt.receipt_hash,
                    "recovery_dossier_revision": payload.dossier_revision,
                }
            )
        elif receipt.kind is DistillationReceiptKind.RECOVERY_DECISION:
            payload = receipt.payload
            assert isinstance(payload, DistillationRecoveryDecisionPayload)
            material.update(
                {
                    "recovery_decision_receipt_id": receipt.receipt_id,
                    "recovery_decision_receipt_hash": receipt.receipt_hash,
                    "recovery_dossier_revision": payload.new_dossier_revision,
                }
            )
        elif receipt.kind is DistillationReceiptKind.FINAL_RESULT:
            payload = receipt.payload
            assert isinstance(payload, DistillationFinalResultPayload)
            material.update(
                {
                    "final_result_receipt_id": receipt.receipt_id,
                    "final_result_receipt_hash": receipt.receipt_hash,
                }
            )
            if payload.outcome == "succeeded":
                material.update(
                    {
                        "candidate_set_hash": payload.candidate_set_hash,
                        "work_count": payload.work_count,
                        "voice_count": payload.voice_count,
                        "validator_count": payload.validator_count,
                    }
                )
        draft = DistillationInvocation.model_construct(**material, head_hash="0" * 64)
        return DistillationInvocation(
            **material,
            head_hash=distillation_invocation_head_hash(draft),
        )

    def _insert_candidate_output(
        self,
        connection: sqlite3.Connection,
        invocation: DistillationInvocation,
        payload: DistillationCandidateOutputPayload,
        receipt: DistillationInvocationReceipt,
    ) -> None:
        expected_source_receipt_id = source_import_receipt_id(
            invocation.source.source_import_id
        )
        if invocation.source.source_receipt_id != expected_source_receipt_id:
            raise DistillationInvocationStoreIntegrity(
                "distillation candidates require a verified source receipt identity"
            )
        for card in payload.candidate_set.cards:
            self._insert_candidate_card(
                connection,
                invocation=invocation,
                candidate=card,
                candidate_set_hash=payload.candidate_set.candidate_set_hash,
                submission=payload.submission,
                created_at=receipt.created_at,
            )

    @staticmethod
    def _insert_candidate_card(
        connection: sqlite3.Connection,
        *,
        invocation: DistillationInvocation,
        candidate: DistillationCandidateCard,
        candidate_set_hash: str,
        submission: DistillationOutputSubmission,
        created_at: datetime,
    ) -> None:
        runtime_draft_hash = canonical_json_hash(
            {
                "schema": "zhimeng.distillation-runtime-draft.v1",
                "invocation_id": invocation.invocation_id,
                "candidate_set_hash": candidate_set_hash,
            }
        )
        origin_material = {
            "source_receipt_id": invocation.source.source_receipt_id,
            "source_receipt_hash": invocation.source.source_receipt_hash,
            "source_packet_fingerprint": invocation.source.source_packet_fingerprint,
            "draft_fingerprint": runtime_draft_hash,
            "cards_draft_hash": candidate_set_hash,
            "source_card_index": candidate.output_index,
            "source_card_hash": candidate.card_hash,
        }
        origin_draft = KnowledgeCandidateOrigin.model_construct(
            **origin_material,
            origin_hash="0" * 64,
        )
        origin = KnowledgeCandidateOrigin(
            **origin_material,
            origin_hash=knowledge_candidate_origin_hash(origin_draft),
        )
        connection.execute(
            """
            INSERT INTO knowledge_candidate(
                id, source_import_id, source_card_index, source_draft_hash,
                lineage_status, origin_json, origin_hash,
                runtime_distillation_invocation_id,
                card_id, card_type, status, revision, content_hash,
                payload_json, issues_json, promoted_version_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'verified', ?, ?, ?, ?, ?, 'pending', 1, ?,
                      ?, '[]', NULL, ?, ?)
            """,
            (
                candidate.candidate_id,
                invocation.source.source_import_id,
                candidate.output_index,
                candidate_set_hash,
                _json(origin.model_dump(mode="json", by_alias=True)),
                origin.origin_hash,
                invocation.invocation_id,
                candidate.card_id,
                candidate.card_type.value,
                candidate.card_hash,
                _json(candidate.card),
                created_at.astimezone(UTC).isoformat(),
                created_at.astimezone(UTC).isoformat(),
            ),
        )

    @classmethod
    def _insert_invocation_root(
        cls,
        connection: sqlite3.Connection,
        invocation: DistillationInvocation,
        last_receipt_id: str,
    ) -> None:
        row = cls._invocation_columns(invocation, last_receipt_id=last_receipt_id)
        connection.execute(
            """
            INSERT INTO distillation_invocation(
                id, source_import_id, source_receipt_id, source_receipt_hash,
                source_packet_fingerprint, packet_text_hash,
                project_binding_id, project_revision, project_fingerprint,
                knowledge_scope_id, knowledge_scope_state_revision,
                knowledge_fingerprint, project_kb_state_id, project_kb_manifest_hash,
                skill_id, skill_version_id, skill_version,
                skill_manifest_fingerprint, skill_package_fingerprint,
                activation_receipt_id, activation_receipt_hash,
                activation_state_revision, effective_scope,
                instruction_materialization_id, instruction_materialization_hash,
                managed_instruction_hash, provider_executor_json,
                provider_capabilities_json, requested_model,
                accepted_reported_models_json, dispatch_config_fingerprint,
                credential_reference, tool_definitions_hash, anchor_catalog_id,
                anchor_catalog_hash, retry_of_invocation_id, idempotency_key,
                request_hash, status, state_revision, provider_replay_count,
                last_receipt_ordinal, last_receipt_id, last_receipt_hash,
                last_dispatch_receipt_id, last_dispatch_receipt_hash,
                last_dispatch_turn_ordinal, last_turn_result_receipt_id,
                last_turn_result_receipt_hash, provider_turn_count,
                provider_turn_chain_hash, candidate_output_receipt_id,
                candidate_output_receipt_hash, recovery_dossier_receipt_id,
                recovery_dossier_revision, recovery_dossier_receipt_hash,
                recovery_decision_receipt_id, recovery_decision_receipt_hash,
                final_result_receipt_id, final_result_receipt_hash,
                candidate_set_hash, work_count, voice_count, validator_count,
                payload_json, head_hash, created_at, updated_at
            ) VALUES (
                :id, :source_import_id, :source_receipt_id, :source_receipt_hash,
                :source_packet_fingerprint, :packet_text_hash,
                :project_binding_id, :project_revision, :project_fingerprint,
                :knowledge_scope_id, :knowledge_scope_state_revision,
                :knowledge_fingerprint, :project_kb_state_id, :project_kb_manifest_hash,
                :skill_id, :skill_version_id, :skill_version,
                :skill_manifest_fingerprint, :skill_package_fingerprint,
                :activation_receipt_id, :activation_receipt_hash,
                :activation_state_revision, :effective_scope,
                :instruction_materialization_id, :instruction_materialization_hash,
                :managed_instruction_hash, :provider_executor_json,
                :provider_capabilities_json, :requested_model,
                :accepted_reported_models_json, :dispatch_config_fingerprint,
                :credential_reference, :tool_definitions_hash, :anchor_catalog_id,
                :anchor_catalog_hash, :retry_of_invocation_id, :idempotency_key,
                :request_hash, :status, :state_revision, :provider_replay_count,
                :last_receipt_ordinal, :last_receipt_id, :last_receipt_hash,
                :last_dispatch_receipt_id, :last_dispatch_receipt_hash,
                :last_dispatch_turn_ordinal, :last_turn_result_receipt_id,
                :last_turn_result_receipt_hash, :provider_turn_count,
                :provider_turn_chain_hash, :candidate_output_receipt_id,
                :candidate_output_receipt_hash, :recovery_dossier_receipt_id,
                :recovery_dossier_revision, :recovery_dossier_receipt_hash,
                :recovery_decision_receipt_id, :recovery_decision_receipt_hash,
                :final_result_receipt_id, :final_result_receipt_hash,
                :candidate_set_hash, :work_count, :voice_count, :validator_count,
                :payload_json, :head_hash, :created_at, :updated_at
            )
            """,
            row,
        )

    @classmethod
    def _insert_receipt(
        cls,
        connection: sqlite3.Connection,
        receipt: DistillationInvocationReceipt,
    ) -> None:
        connection.execute(
            """
            INSERT INTO distillation_invocation_receipt(
                id, invocation_id, ordinal, kind, turn_ordinal,
                parent_receipt_id, expected_invocation_revision,
                resulting_invocation_revision, resulting_status, idempotency_key,
                payload_hash, receipt_hash, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.receipt_id,
                receipt.invocation_id,
                receipt.ordinal,
                receipt.kind.value,
                receipt.turn_ordinal,
                receipt.parent_receipt_id,
                receipt.expected_invocation_revision,
                receipt.resulting_invocation_revision,
                receipt.resulting_status.value,
                receipt.idempotency_key,
                receipt.payload_hash,
                receipt.receipt_hash,
                _json(receipt.model_dump(mode="json", by_alias=True)),
                receipt.created_at.astimezone(UTC).isoformat(),
            ),
        )

    @classmethod
    def _update_invocation_head(
        cls,
        connection: sqlite3.Connection,
        *,
        invocation: DistillationInvocation,
        last_receipt_id: str,
        old_revision: int,
    ) -> None:
        row = cls._invocation_columns(invocation, last_receipt_id=last_receipt_id)
        row["old_revision"] = old_revision
        result = connection.execute(
            """
            UPDATE distillation_invocation
            SET status = :status,
                state_revision = :state_revision,
                provider_replay_count = :provider_replay_count,
                last_receipt_ordinal = :last_receipt_ordinal,
                last_receipt_id = :last_receipt_id,
                last_receipt_hash = :last_receipt_hash,
                last_dispatch_receipt_id = :last_dispatch_receipt_id,
                last_dispatch_receipt_hash = :last_dispatch_receipt_hash,
                last_dispatch_turn_ordinal = :last_dispatch_turn_ordinal,
                last_turn_result_receipt_id = :last_turn_result_receipt_id,
                last_turn_result_receipt_hash = :last_turn_result_receipt_hash,
                provider_turn_count = :provider_turn_count,
                provider_turn_chain_hash = :provider_turn_chain_hash,
                candidate_output_receipt_id = :candidate_output_receipt_id,
                candidate_output_receipt_hash = :candidate_output_receipt_hash,
                recovery_dossier_receipt_id = :recovery_dossier_receipt_id,
                recovery_dossier_revision = :recovery_dossier_revision,
                recovery_dossier_receipt_hash = :recovery_dossier_receipt_hash,
                recovery_decision_receipt_id = :recovery_decision_receipt_id,
                recovery_decision_receipt_hash = :recovery_decision_receipt_hash,
                final_result_receipt_id = :final_result_receipt_id,
                final_result_receipt_hash = :final_result_receipt_hash,
                candidate_set_hash = :candidate_set_hash,
                work_count = :work_count,
                voice_count = :voice_count,
                validator_count = :validator_count,
                payload_json = :payload_json,
                head_hash = :head_hash,
                updated_at = :updated_at
            WHERE id = :id AND state_revision = :old_revision
            """,
            row,
        )
        if result.rowcount != 1:
            raise DistillationInvocationStoreConflict(
                "distillation invocation CAS update lost the head race"
            )

    @classmethod
    def _invocation_columns(
        cls,
        invocation: DistillationInvocation,
        *,
        last_receipt_id: str,
    ) -> dict[str, Any]:
        return {
            "id": invocation.invocation_id,
            "source_import_id": invocation.source.source_import_id,
            "source_receipt_id": invocation.source.source_receipt_id,
            "source_receipt_hash": invocation.source.source_receipt_hash,
            "source_packet_fingerprint": invocation.source.source_packet_fingerprint,
            "packet_text_hash": invocation.source.packet_text_hash,
            "project_binding_id": invocation.project.project_id,
            "project_revision": invocation.project.project_revision,
            "project_fingerprint": invocation.project.project_fingerprint,
            "knowledge_scope_id": invocation.knowledge.scope_id,
            "knowledge_scope_state_revision": invocation.knowledge.scope_state_revision,
            "knowledge_fingerprint": invocation.knowledge.knowledge_fingerprint,
            "project_kb_state_id": invocation.knowledge.project_kb_state_id,
            "project_kb_manifest_hash": invocation.knowledge.project_kb_manifest_hash,
            "skill_id": invocation.instruction.skill_id,
            "skill_version_id": invocation.instruction.skill_version_id,
            "skill_version": invocation.instruction.skill_version,
            "skill_manifest_fingerprint": invocation.instruction.manifest_fingerprint,
            "skill_package_fingerprint": invocation.instruction.package_fingerprint,
            "activation_receipt_id": invocation.instruction.activation_receipt_id,
            "activation_receipt_hash": invocation.instruction.activation_receipt_hash,
            "activation_state_revision": invocation.instruction.activation_state_revision,
            "effective_scope": invocation.instruction.effective_scope,
            "instruction_materialization_id": (
                invocation.instruction.instruction_materialization_id
            ),
            "instruction_materialization_hash": (
                invocation.instruction.instruction_materialization_hash
            ),
            "managed_instruction_hash": invocation.instruction.instruction_hash,
            "provider_executor_json": _json(
                invocation.provider.executor.model_dump(mode="json")
            ),
            "provider_capabilities_json": _json(
                invocation.provider.capabilities.model_dump(mode="json")
            ),
            "requested_model": invocation.provider.requested_model,
            "accepted_reported_models_json": _json(
                list(invocation.provider.accepted_reported_models)
            ),
            "dispatch_config_fingerprint": (
                invocation.provider.dispatch_config_fingerprint
            ),
            "credential_reference": invocation.provider.credential_reference,
            "tool_definitions_hash": invocation.provider.tool_definitions_hash,
            "anchor_catalog_id": invocation.anchor_catalog_id,
            "anchor_catalog_hash": invocation.anchor_catalog_hash,
            "retry_of_invocation_id": invocation.retry_of,
            "idempotency_key": invocation.idempotency_key,
            "request_hash": invocation.request_hash,
            "status": invocation.status.value,
            "state_revision": invocation.state_revision,
            "provider_replay_count": invocation.provider_replay_count,
            "last_receipt_ordinal": invocation.last_receipt_ordinal,
            "last_receipt_id": last_receipt_id,
            "last_receipt_hash": invocation.last_receipt_hash,
            "last_dispatch_receipt_id": invocation.last_dispatch_receipt_id,
            "last_dispatch_receipt_hash": invocation.last_dispatch_receipt_hash,
            "last_dispatch_turn_ordinal": invocation.last_dispatch_turn_ordinal,
            "last_turn_result_receipt_id": invocation.last_turn_result_receipt_id,
            "last_turn_result_receipt_hash": invocation.last_turn_result_receipt_hash,
            "provider_turn_count": invocation.provider_turn_count,
            "provider_turn_chain_hash": invocation.provider_turn_chain_hash,
            "candidate_output_receipt_id": invocation.candidate_output_receipt_id,
            "candidate_output_receipt_hash": invocation.candidate_output_receipt_hash,
            "recovery_dossier_receipt_id": invocation.recovery_dossier_receipt_id,
            "recovery_dossier_revision": invocation.recovery_dossier_revision,
            "recovery_dossier_receipt_hash": invocation.recovery_dossier_receipt_hash,
            "recovery_decision_receipt_id": invocation.recovery_decision_receipt_id,
            "recovery_decision_receipt_hash": invocation.recovery_decision_receipt_hash,
            "final_result_receipt_id": invocation.final_result_receipt_id,
            "final_result_receipt_hash": invocation.final_result_receipt_hash,
            "candidate_set_hash": invocation.candidate_set_hash,
            "work_count": invocation.work_count,
            "voice_count": invocation.voice_count,
            "validator_count": invocation.validator_count,
            "payload_json": _json(
                invocation.model_dump(mode="json", by_alias=True)
            ),
            "head_hash": invocation.head_hash,
            "created_at": invocation.created_at.astimezone(UTC).isoformat(),
            "updated_at": invocation.updated_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def _invocation_from_row(cls, row: sqlite3.Row) -> DistillationInvocation:
        payload_json = str(row["payload_json"])
        try:
            invocation = DistillationInvocation.model_validate_json(payload_json)
        except ValidationError as exc:
            raise DistillationInvocationStoreIntegrity(
                "stored distillation invocation head failed its contract"
            ) from exc
        expected = cls._invocation_columns(
            invocation,
            last_receipt_id=str(row["last_receipt_id"]),
        )
        for key, value in expected.items():
            if key == "payload_json":
                continue
            observed = row[key]
            if observed != value:
                raise DistillationInvocationStoreIntegrity(
                    f"distillation invocation scalar column drifted: {key}"
                )
        return invocation

    @staticmethod
    def _receipt_from_row(row: sqlite3.Row) -> DistillationInvocationReceipt:
        payload_json = str(row["payload_json"])
        try:
            receipt = DistillationInvocationReceipt.model_validate_json(payload_json)
        except ValidationError as exc:
            raise DistillationInvocationStoreIntegrity(
                "stored distillation receipt failed its contract"
            ) from exc
        expected = {
            "id": receipt.receipt_id,
            "invocation_id": receipt.invocation_id,
            "ordinal": receipt.ordinal,
            "kind": receipt.kind.value,
            "turn_ordinal": receipt.turn_ordinal,
            "parent_receipt_id": receipt.parent_receipt_id,
            "expected_invocation_revision": receipt.expected_invocation_revision,
            "resulting_invocation_revision": receipt.resulting_invocation_revision,
            "resulting_status": receipt.resulting_status.value,
            "idempotency_key": receipt.idempotency_key,
            "payload_hash": receipt.payload_hash,
            "receipt_hash": receipt.receipt_hash,
            "created_at": receipt.created_at.astimezone(UTC).isoformat(),
        }
        for key, value in expected.items():
            observed = row[key]
            if observed != value:
                raise DistillationInvocationStoreIntegrity(
                    f"distillation receipt scalar column drifted: {key}"
                )
        return receipt

    def _receipts_in_connection(
        self,
        connection: sqlite3.Connection,
        invocation_id: str,
    ) -> tuple[DistillationInvocationReceipt, ...]:
        rows = connection.execute(
            """
            SELECT * FROM distillation_invocation_receipt
            WHERE invocation_id = ?
            ORDER BY ordinal
            """,
            (invocation_id,),
        ).fetchall()
        if not rows:
            raise DistillationInvocationStoreIntegrity(
                "distillation invocation has no receipt ledger"
            )
        return tuple(self._receipt_from_row(row) for row in rows)

    @staticmethod
    def _invocation_row(
        connection: sqlite3.Connection,
        invocation_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM distillation_invocation WHERE id = ?",
            (invocation_id,),
        ).fetchone()

    @classmethod
    def _require_invocation_row(
        cls,
        connection: sqlite3.Connection,
        invocation_id: str,
    ) -> sqlite3.Row:
        row = cls._invocation_row(connection, invocation_id)
        if row is None:
            raise DistillationInvocationStoreNotFound(
                "distillation invocation was not found"
            )
        return row

    @staticmethod
    def _receipt_row(
        connection: sqlite3.Connection,
        receipt_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM distillation_invocation_receipt WHERE id = ?",
            (receipt_id,),
        ).fetchone()


def _json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _load_json_object(value: object, *, label: str) -> dict[str, Any]:
    payload = json.loads(str(value))
    if not isinstance(payload, dict):
        raise DistillationInvocationStoreIntegrity(
            f"stored {label} payload is not an object"
        )
    return payload


__all__ = [
    "DistillationInvocationSnapshot",
    "DistillationInvocationStore",
    "DistillationInvocationStoreConflict",
    "DistillationInvocationStoreError",
    "DistillationInvocationStoreIntegrity",
    "DistillationInvocationStoreNotFound",
]
