from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhimeng_runtime.context_models import Sha256, canonical_json_hash


class DistillationAnchorLevel(StrEnum):
    SMALL = "small"
    BIG = "big"
    SUPER = "super"


_LEVEL_ORDER = {
    DistillationAnchorLevel.SMALL: 0,
    DistillationAnchorLevel.BIG: 1,
    DistillationAnchorLevel.SUPER: 2,
}


class DistillationAnchorModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        strict=True,
    )


class DistillationAnchor(DistillationAnchorModel):
    schema_id: Literal["zhimeng.distillation-anchor.v1"] = Field(
        default="zhimeng.distillation-anchor.v1",
        alias="schema",
    )
    anchor_id: str = Field(pattern=r"^distillation-anchor-[0-9a-f]{32}$")
    level: DistillationAnchorLevel
    ordinal: int = Field(ge=1, strict=True)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(ge=1, strict=True)
    chapter_start: int | None = Field(default=None, ge=1, strict=True)
    chapter_end: int | None = Field(default=None, ge=1, strict=True)
    text_hash: Sha256
    anchor_hash: Sha256

    @model_validator(mode="after")
    def validate_anchor(self) -> "DistillationAnchor":
        if self.char_end <= self.char_start:
            raise ValueError("distillation anchor range must be non-empty")
        if (self.chapter_start is None) != (self.chapter_end is None):
            raise ValueError("distillation anchor chapter bounds must pair")
        if (
            self.chapter_start is not None
            and self.chapter_end is not None
            and self.chapter_end < self.chapter_start
        ):
            raise ValueError("distillation anchor chapter range is reversed")
        if self.anchor_hash != distillation_anchor_hash(self):
            raise ValueError("distillation anchor hash does not match immutable material")
        return self


class DistillationAnchorBinding(DistillationAnchorModel):
    anchor_id: str = Field(pattern=r"^distillation-anchor-[0-9a-f]{32}$")
    anchor_hash: Sha256
    level: DistillationAnchorLevel
    ordinal: int = Field(ge=1, strict=True)
    char_start: int = Field(ge=0, strict=True)
    char_end: int = Field(ge=1, strict=True)
    text_hash: Sha256

    @model_validator(mode="after")
    def validate_range(self) -> "DistillationAnchorBinding":
        if self.char_end <= self.char_start:
            raise ValueError("distillation anchor binding range must be non-empty")
        return self


class DistillationAnchorCatalog(DistillationAnchorModel):
    schema_id: Literal["zhimeng.distillation-anchor-catalog.v1"] = Field(
        default="zhimeng.distillation-anchor-catalog.v1",
        alias="schema",
    )
    catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    invocation_id: str = Field(pattern=r"^distillation-invocation-[0-9a-f]{32}$")
    source_import_id: str = Field(pattern=r"^source-import-[0-9a-f]{32}$")
    source_receipt_id: str = Field(pattern=r"^source-import-receipt-[0-9a-f]{32}$")
    source_receipt_hash: Sha256
    packet_text_hash: Sha256
    packet_char_count: int = Field(ge=1, strict=True)
    anchors: tuple[DistillationAnchor, ...] = Field(
        min_length=1,
        max_length=100_000,
    )
    anchor_count: int = Field(ge=1, le=100_000, strict=True)
    anchors_hash: Sha256
    created_at: datetime
    catalog_hash: Sha256

    @model_validator(mode="after")
    def validate_catalog(self) -> "DistillationAnchorCatalog":
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("distillation anchor catalog time must be timezone-aware")
        if self.anchor_count != len(self.anchors):
            raise ValueError("distillation anchor catalog count does not match anchors")
        expected_order = tuple(sorted(self.anchors, key=_anchor_sort_key))
        if self.anchors != expected_order:
            raise ValueError("distillation anchors must use canonical order")
        identities = [anchor.anchor_id for anchor in self.anchors]
        if len(identities) != len(set(identities)):
            raise ValueError("distillation anchor identities must be unique")
        ordinals_by_level: dict[DistillationAnchorLevel, list[int]] = defaultdict(list)
        for anchor in self.anchors:
            if anchor.char_end > self.packet_char_count:
                raise ValueError("distillation anchor exceeds packet text bounds")
            expected_id = distillation_anchor_id(
                source_receipt_id=self.source_receipt_id,
                packet_text_hash=self.packet_text_hash,
                level=anchor.level,
                ordinal=anchor.ordinal,
                char_start=anchor.char_start,
                char_end=anchor.char_end,
                text_hash=anchor.text_hash,
            )
            if anchor.anchor_id != expected_id:
                raise ValueError("distillation anchor identity does not match its source")
            ordinals_by_level[anchor.level].append(anchor.ordinal)
        for ordinals in ordinals_by_level.values():
            if ordinals != list(range(1, len(ordinals) + 1)):
                raise ValueError("distillation anchor ordinals must be contiguous per level")
        if self.anchors_hash != distillation_anchor_set_hash(self.anchors):
            raise ValueError("distillation anchor set hash does not match anchors")
        expected_catalog_id = distillation_anchor_catalog_id(
            invocation_id=self.invocation_id,
            source_receipt_id=self.source_receipt_id,
            anchors_hash=self.anchors_hash,
        )
        if self.catalog_id != expected_catalog_id:
            raise ValueError("distillation anchor catalog identity does not match")
        if self.catalog_hash != distillation_anchor_catalog_hash(self):
            raise ValueError("distillation anchor catalog hash does not match")
        return self

    def binding_for(self, anchor_id: str) -> DistillationAnchorBinding:
        for anchor in self.anchors:
            if anchor.anchor_id == anchor_id:
                return distillation_anchor_binding(anchor)
        raise KeyError(anchor_id)


class DistillationAnchorSearchRequest(DistillationAnchorModel):
    schema_id: Literal["zhimeng.distillation-anchor-search-request.v1"] = Field(
        default="zhimeng.distillation-anchor-search-request.v1",
        alias="schema",
    )
    query: str = Field(min_length=1, max_length=2_000)
    levels: tuple[DistillationAnchorLevel, ...] = (
        DistillationAnchorLevel.SMALL,
        DistillationAnchorLevel.BIG,
        DistillationAnchorLevel.SUPER,
    )
    limit: int = Field(default=8, ge=1, le=20, strict=True)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("distillation anchor search query must not be blank")
        return normalized

    @field_validator("levels")
    @classmethod
    def validate_levels(
        cls,
        values: tuple[DistillationAnchorLevel, ...],
    ) -> tuple[DistillationAnchorLevel, ...]:
        if not values:
            raise ValueError("distillation anchor search requires at least one level")
        expected = tuple(sorted(set(values), key=_LEVEL_ORDER.__getitem__))
        if values != expected:
            raise ValueError("distillation anchor search levels must be sorted and unique")
        return values


class DistillationAnchorSearchMatch(DistillationAnchorModel):
    rank: int = Field(ge=1, le=20, strict=True)
    anchor: DistillationAnchorBinding
    preview: str = Field(min_length=1, max_length=1_000)
    matched_terms: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("preview")
    @classmethod
    def validate_preview(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("distillation anchor preview must not be blank")
        return value

    @field_validator("matched_terms")
    @classmethod
    def validate_matched_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
        if values != normalized:
            raise ValueError("distillation anchor matched terms must be sorted and unique")
        return values


class DistillationAnchorSearchResult(DistillationAnchorModel):
    schema_id: Literal["zhimeng.distillation-anchor-search-result.v1"] = Field(
        default="zhimeng.distillation-anchor-search-result.v1",
        alias="schema",
    )
    invocation_id: str = Field(pattern=r"^distillation-invocation-[0-9a-f]{32}$")
    catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    catalog_hash: Sha256
    request_hash: Sha256
    matches: tuple[DistillationAnchorSearchMatch, ...] = Field(max_length=20)
    result_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> "DistillationAnchorSearchResult":
        ranks = tuple(match.rank for match in self.matches)
        if ranks != tuple(range(1, len(self.matches) + 1)):
            raise ValueError("distillation anchor search ranks must be contiguous")
        identities = [match.anchor.anchor_id for match in self.matches]
        if len(identities) != len(set(identities)):
            raise ValueError("distillation anchor search matches must be unique")
        if self.result_hash != distillation_anchor_search_result_hash(self):
            raise ValueError("distillation anchor search result hash does not match")
        return self


class DistillationAnchorReadRequest(DistillationAnchorModel):
    schema_id: Literal["zhimeng.distillation-anchor-read-request.v1"] = Field(
        default="zhimeng.distillation-anchor-read-request.v1",
        alias="schema",
    )
    anchor_ids: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("anchor_ids")
    @classmethod
    def validate_anchor_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not isinstance(value, str)
            or not value.startswith("distillation-anchor-")
            or len(value) != len("distillation-anchor-") + 32
            for value in values
        ):
            raise ValueError("distillation anchor read identities are invalid")
        if len(values) != len(set(values)):
            raise ValueError("distillation anchor read identities must be unique")
        return values


class DistillationAnchorReadItem(DistillationAnchorModel):
    anchor: DistillationAnchorBinding
    text: str = Field(min_length=1, max_length=64_000)

    @model_validator(mode="after")
    def validate_text(self) -> "DistillationAnchorReadItem":
        if len(self.text) != self.anchor.char_end - self.anchor.char_start:
            raise ValueError("distillation anchor read text length does not match range")
        if _text_hash(self.text) != self.anchor.text_hash:
            raise ValueError("distillation anchor read text hash does not match")
        return self


class DistillationAnchorReadResult(DistillationAnchorModel):
    schema_id: Literal["zhimeng.distillation-anchor-read-result.v1"] = Field(
        default="zhimeng.distillation-anchor-read-result.v1",
        alias="schema",
    )
    invocation_id: str = Field(pattern=r"^distillation-invocation-[0-9a-f]{32}$")
    catalog_id: str = Field(pattern=r"^distillation-anchor-catalog-[0-9a-f]{32}$")
    catalog_hash: Sha256
    request_hash: Sha256
    items: tuple[DistillationAnchorReadItem, ...] = Field(min_length=1, max_length=8)
    total_char_count: int = Field(ge=1, le=64_000, strict=True)
    result_hash: Sha256

    @model_validator(mode="after")
    def validate_result(self) -> "DistillationAnchorReadResult":
        identities = [item.anchor.anchor_id for item in self.items]
        if len(identities) != len(set(identities)):
            raise ValueError("distillation anchor read items must be unique")
        if self.total_char_count != sum(len(item.text) for item in self.items):
            raise ValueError("distillation anchor read character count does not match")
        if self.result_hash != distillation_anchor_read_result_hash(self):
            raise ValueError("distillation anchor read result hash does not match")
        return self


def _anchor_sort_key(anchor: DistillationAnchor) -> tuple[int, int, int, str]:
    return (
        _LEVEL_ORDER[anchor.level],
        anchor.ordinal,
        anchor.char_start,
        anchor.anchor_id,
    )


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def distillation_anchor_id(
    *,
    source_receipt_id: str,
    packet_text_hash: str,
    level: DistillationAnchorLevel,
    ordinal: int,
    char_start: int,
    char_end: int,
    text_hash: str,
) -> str:
    digest = canonical_json_hash(
        {
            "schema": "zhimeng.distillation-anchor-identity.v1",
            "source_receipt_id": source_receipt_id,
            "packet_text_hash": packet_text_hash,
            "level": level.value,
            "ordinal": ordinal,
            "char_start": char_start,
            "char_end": char_end,
            "text_hash": text_hash,
        }
    )
    return f"distillation-anchor-{digest[:32]}"


def distillation_anchor_hash(anchor: DistillationAnchor) -> str:
    return canonical_json_hash(
        anchor.model_dump(mode="json", by_alias=True, exclude={"anchor_hash"})
    )


def distillation_anchor_binding(anchor: DistillationAnchor) -> DistillationAnchorBinding:
    return DistillationAnchorBinding(
        anchor_id=anchor.anchor_id,
        anchor_hash=anchor.anchor_hash,
        level=anchor.level,
        ordinal=anchor.ordinal,
        char_start=anchor.char_start,
        char_end=anchor.char_end,
        text_hash=anchor.text_hash,
    )


def build_distillation_anchor(
    *,
    source_receipt_id: str,
    packet_text_hash: str,
    level: DistillationAnchorLevel,
    ordinal: int,
    char_start: int,
    text: str,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
) -> DistillationAnchor:
    text_hash = _text_hash(text)
    material = {
        "anchor_id": distillation_anchor_id(
            source_receipt_id=source_receipt_id,
            packet_text_hash=packet_text_hash,
            level=level,
            ordinal=ordinal,
            char_start=char_start,
            char_end=char_start + len(text),
            text_hash=text_hash,
        ),
        "level": level,
        "ordinal": ordinal,
        "char_start": char_start,
        "char_end": char_start + len(text),
        "chapter_start": chapter_start,
        "chapter_end": chapter_end,
        "text_hash": text_hash,
    }
    draft = DistillationAnchor.model_construct(**material, anchor_hash="0" * 64)
    return DistillationAnchor(**material, anchor_hash=distillation_anchor_hash(draft))


def distillation_anchor_set_hash(anchors: tuple[DistillationAnchor, ...]) -> str:
    return canonical_json_hash(
        [anchor.model_dump(mode="json", by_alias=True) for anchor in anchors]
    )


def distillation_anchor_catalog_id(
    *,
    invocation_id: str,
    source_receipt_id: str,
    anchors_hash: str,
) -> str:
    digest = canonical_json_hash(
        {
            "schema": "zhimeng.distillation-anchor-catalog-identity.v1",
            "invocation_id": invocation_id,
            "source_receipt_id": source_receipt_id,
            "anchors_hash": anchors_hash,
        }
    )
    return f"distillation-anchor-catalog-{digest[:32]}"


def distillation_anchor_catalog_hash(catalog: DistillationAnchorCatalog) -> str:
    return canonical_json_hash(
        catalog.model_dump(mode="json", by_alias=True, exclude={"catalog_hash"})
    )


def build_distillation_anchor_catalog(
    *,
    invocation_id: str,
    source_import_id: str,
    source_receipt_id: str,
    source_receipt_hash: str,
    packet_text_hash: str,
    packet_char_count: int,
    anchors: tuple[DistillationAnchor, ...],
    created_at: datetime,
) -> DistillationAnchorCatalog:
    ordered = tuple(sorted(anchors, key=_anchor_sort_key))
    anchors_hash = distillation_anchor_set_hash(ordered)
    material = {
        "catalog_id": distillation_anchor_catalog_id(
            invocation_id=invocation_id,
            source_receipt_id=source_receipt_id,
            anchors_hash=anchors_hash,
        ),
        "invocation_id": invocation_id,
        "source_import_id": source_import_id,
        "source_receipt_id": source_receipt_id,
        "source_receipt_hash": source_receipt_hash,
        "packet_text_hash": packet_text_hash,
        "packet_char_count": packet_char_count,
        "anchors": ordered,
        "anchor_count": len(ordered),
        "anchors_hash": anchors_hash,
        "created_at": created_at,
    }
    draft = DistillationAnchorCatalog.model_construct(**material, catalog_hash="0" * 64)
    return DistillationAnchorCatalog(
        **material,
        catalog_hash=distillation_anchor_catalog_hash(draft),
    )


def distillation_anchor_search_request_hash(
    request: DistillationAnchorSearchRequest,
) -> str:
    return canonical_json_hash(request.model_dump(mode="json", by_alias=True))


def distillation_anchor_search_result_hash(
    result: DistillationAnchorSearchResult,
) -> str:
    return canonical_json_hash(
        result.model_dump(mode="json", by_alias=True, exclude={"result_hash"})
    )


def distillation_anchor_read_request_hash(request: DistillationAnchorReadRequest) -> str:
    return canonical_json_hash(request.model_dump(mode="json", by_alias=True))


def distillation_anchor_read_result_hash(result: DistillationAnchorReadResult) -> str:
    return canonical_json_hash(
        result.model_dump(mode="json", by_alias=True, exclude={"result_hash"})
    )


def build_distillation_anchor_read_result(
    *,
    catalog: DistillationAnchorCatalog,
    request: DistillationAnchorReadRequest,
    text_by_anchor_id: Mapping[str, str],
) -> DistillationAnchorReadResult:
    items: list[DistillationAnchorReadItem] = []
    for anchor_id in request.anchor_ids:
        try:
            text = text_by_anchor_id[anchor_id]
            binding = catalog.binding_for(anchor_id)
        except KeyError as exc:
            raise ValueError("distillation anchor read references an unknown anchor") from exc
        items.append(DistillationAnchorReadItem(anchor=binding, text=text))
    material = {
        "invocation_id": catalog.invocation_id,
        "catalog_id": catalog.catalog_id,
        "catalog_hash": catalog.catalog_hash,
        "request_hash": distillation_anchor_read_request_hash(request),
        "items": tuple(items),
        "total_char_count": sum(len(item.text) for item in items),
    }
    draft = DistillationAnchorReadResult.model_construct(**material, result_hash="0" * 64)
    return DistillationAnchorReadResult(
        **material,
        result_hash=distillation_anchor_read_result_hash(draft),
    )


__all__ = [
    "DistillationAnchor",
    "DistillationAnchorBinding",
    "DistillationAnchorCatalog",
    "DistillationAnchorLevel",
    "DistillationAnchorReadItem",
    "DistillationAnchorReadRequest",
    "DistillationAnchorReadResult",
    "DistillationAnchorSearchMatch",
    "DistillationAnchorSearchRequest",
    "DistillationAnchorSearchResult",
    "build_distillation_anchor",
    "build_distillation_anchor_catalog",
    "build_distillation_anchor_read_result",
    "distillation_anchor_binding",
    "distillation_anchor_catalog_hash",
    "distillation_anchor_hash",
    "distillation_anchor_id",
    "distillation_anchor_read_request_hash",
    "distillation_anchor_read_result_hash",
    "distillation_anchor_search_request_hash",
    "distillation_anchor_search_result_hash",
    "distillation_anchor_set_hash",
]
