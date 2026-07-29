from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


Sha256 = str


class ProvenanceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class KnowledgeProvenanceRef(ProvenanceModel):
    schema_id: Literal["zhimeng.knowledge-provenance-ref.v1"] = Field(
        default="zhimeng.knowledge-provenance-ref.v1",
        alias="schema",
    )
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    source_receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    source_packet_fingerprint: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    cards_draft_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_id: str = Field(pattern=r"^knowledge-candidate-[0-9a-f]{32}$")
    candidate_origin_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    card_id: str = Field(min_length=1, max_length=160)
    version_id: str = Field(pattern=r"^knowledge-version-[0-9a-f]{32}$")
    version_content_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    version_lineage_receipt_id: str = Field(
        pattern=r"^knowledge-lineage-[0-9a-f]{32}$"
    )
    version_lineage_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    formalization_receipt_id: str = Field(
        pattern=r"^knowledge-formalization-[0-9a-f]{32}$"
    )
    formalization_receipt_hash: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_distinct_receipts(self) -> "KnowledgeProvenanceRef":
        receipt_ids = {
            self.source_receipt_id,
            self.version_lineage_receipt_id,
            self.formalization_receipt_id,
        }
        if len(receipt_ids) != 3:
            raise ValueError("knowledge provenance receipt identities must be distinct")
        if self.source_receipt_id != source_import_receipt_id(self.source_import_id):
            raise ValueError("knowledge provenance source receipt belongs to another import")
        if self.formalization_receipt_id != knowledge_formalization_receipt_id(
            self.source_import_id
        ):
            raise ValueError(
                "knowledge provenance formalization receipt belongs to another import"
            )
        return self


def knowledge_formalization_receipt_id(source_import_id: str) -> str:
    digest = hashlib.sha256(
        f"zhimeng.knowledge-formalization-receipt.v1\0{source_import_id}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"knowledge-formalization-{digest[:32]}"


__all__ = [
    "KnowledgeProvenanceRef",
    "ProvenanceModel",
    "Sha256",
    "knowledge_formalization_receipt_id",
]
