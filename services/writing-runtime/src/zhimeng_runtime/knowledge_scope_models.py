from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeScopeType(StrEnum):
    """The ownership boundary used by the native knowledge store."""

    APPLICATION = "application"
    PROJECT = "project"


APPLICATION_KNOWLEDGE_SCOPE_ID = "knowledge-scope-application"


def knowledge_scope_id(
    project_binding_id: str | None = None,
    *,
    scope_type: KnowledgeScopeType | str | None = None,
) -> str:
    """Return the stable id for an application or project knowledge scope.

    The application scope is a singleton. Project ids are intentionally hashed
    so a path or other mutable display value never becomes part of a scope id.
    ``scope_type`` is accepted for callers that want an explicit assertion; a
    positional ``"application"`` is also accepted for convenience.
    """

    if project_binding_id in {KnowledgeScopeType.APPLICATION.value, "application"}:
        if scope_type not in (None, KnowledgeScopeType.APPLICATION, "application"):
            raise ValueError("application scope cannot have a project binding")
        project_binding_id = None
    kind = KnowledgeScopeType(scope_type) if scope_type is not None else None
    if kind is KnowledgeScopeType.APPLICATION:
        if project_binding_id is not None:
            raise ValueError("application scope cannot have a project binding")
        return APPLICATION_KNOWLEDGE_SCOPE_ID
    if kind is KnowledgeScopeType.PROJECT and project_binding_id is None:
        raise ValueError("project scope requires a project binding")
    if project_binding_id is None:
        return APPLICATION_KNOWLEDGE_SCOPE_ID
    if not str(project_binding_id).strip():
        raise ValueError("project binding id cannot be empty")
    digest = hashlib.sha256(str(project_binding_id).encode("utf-8")).hexdigest()[:32]
    return f"knowledge-scope-project-{digest}"


deterministic_knowledge_scope_id = knowledge_scope_id


def application_scope_id() -> str:
    return APPLICATION_KNOWLEDGE_SCOPE_ID


def project_scope_id(project_binding_id: str) -> str:
    return knowledge_scope_id(project_binding_id)


class KnowledgeScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class KnowledgeScopeRef(KnowledgeScopeModel):
    schema_id: Literal["zhimeng.knowledge-scope-ref.v1"] = Field(
        default="zhimeng.knowledge-scope-ref.v1", alias="schema"
    )
    scope_id: str = Field(min_length=1, max_length=160)
    scope_type: KnowledgeScopeType
    project_binding_id: str | None = Field(default=None, min_length=1, max_length=160)
    state_revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_scope_identity(self) -> "KnowledgeScopeRef":
        if self.scope_type is KnowledgeScopeType.APPLICATION:
            if self.project_binding_id is not None:
                raise ValueError("application scope cannot reference a project binding")
            if self.scope_id != APPLICATION_KNOWLEDGE_SCOPE_ID:
                raise ValueError("application scope id is not the singleton id")
        else:
            if self.project_binding_id is None:
                raise ValueError("project scope requires a project binding")
            if self.scope_id != knowledge_scope_id(self.project_binding_id):
                raise ValueError("project scope id is not deterministic")
        return self


class KnowledgeScopeRecord(KnowledgeScopeRef):
    schema_id: Literal["zhimeng.knowledge-scope-record.v1"] = Field(
        default="zhimeng.knowledge-scope-record.v1", alias="schema"
    )
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_dates(self) -> "KnowledgeScopeRecord":
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("knowledge scope timestamps must be timezone-aware")
        if self.created_at > self.updated_at:
            raise ValueError("knowledge scope updated_at cannot precede created_at")
        return self

    def to_ref(self) -> KnowledgeScopeRef:
        return KnowledgeScopeRef(
            scope_id=self.scope_id,
            scope_type=self.scope_type,
            project_binding_id=self.project_binding_id,
            state_revision=self.state_revision,
        )


class KnowledgeSourceScopeRecord(KnowledgeScopeModel):
    schema_id: Literal["zhimeng.knowledge-source-scope.v1"] = Field(
        default="zhimeng.knowledge-source-scope.v1", alias="schema"
    )
    source_import_id: str = Field(min_length=1, max_length=160)
    scope_id: str = Field(min_length=1, max_length=160)
    bound_at: datetime

    @model_validator(mode="after")
    def validate_date(self) -> "KnowledgeSourceScopeRecord":
        if self.bound_at.tzinfo is None:
            raise ValueError("source scope binding time must be timezone-aware")
        return self


__all__ = [
    "APPLICATION_KNOWLEDGE_SCOPE_ID",
    "KnowledgeScopeType",
    "KnowledgeScopeRef",
    "KnowledgeScopeRecord",
    "KnowledgeSourceScopeRecord",
    "application_scope_id",
    "deterministic_knowledge_scope_id",
    "knowledge_scope_id",
    "project_scope_id",
]
