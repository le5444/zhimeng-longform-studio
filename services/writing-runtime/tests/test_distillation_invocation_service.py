from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime

import pytest

from zhimeng_runtime.agent_provider import (
    AgentProviderError,
    ProviderCompleted,
    ProviderEvent,
    ProviderTextDelta,
    ProviderToolCall,
    ProviderToolDefinition,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.distillation_invocation_service import (
    DistillationInvocationService,
    build_distillation_tool_definitions,
    distillation_provider_tool_definitions_hash,
)
from zhimeng_runtime.distillation_invocation_store import DistillationInvocationStore
from zhimeng_runtime.distillation_receipt_models import DistillationReceiptKind
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)
from zhimeng_runtime.source_import_receipt_models import source_import_receipt_id


fixtures = importlib.import_module("test_distillation_invocation_models")
store_fixtures = importlib.import_module("test_distillation_invocation_store")

SOURCE_IMPORT_ID = f"source-import-{'a' * 32}"
PROJECT_ID = f"project-{'b' * 24}"
IDEMPOTENCY_KEY = f"distillation-idempotency-{'d' * 32}"
NOW = datetime(2026, 7, 23, 3, 0, tzinfo=UTC)


class ScriptedDistillationProvider:
    def __init__(self, turns: list[list[ProviderEvent] | BaseException]) -> None:
        self.turns = turns
        self.start_count = 0
        self.messages_by_turn: list[list[dict[str, object]]] = []
        self.tool_names_by_turn: list[tuple[str, ...]] = []

    @property
    def dispatch_config_fingerprint(self) -> str:
        return "f" * 64

    @property
    def identity(self) -> ExecutorIdentity:
        return ExecutorIdentity(
            executor_id="executor.scripted-distillation",
            kind=ExecutorKind.EMBEDDED,
            implementation="scripted-distillation",
            version="1",
            protocol_version="1",
            instance_id=f"provider-config-{self.dispatch_config_fingerprint}",
        )

    @property
    def capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            protocol_version="1",
            names=[CapabilityName.STREAMING, CapabilityName.CANCEL],
            extensions={},
        )

    async def stream_chat(
        self,
        messages: Sequence[Mapping[str, object]],
        *,
        cancel_event: asyncio.Event,
        tools: Sequence[ProviderToolDefinition] = (),
    ) -> AsyncIterator[ProviderEvent]:
        del cancel_event
        self.start_count += 1
        self.messages_by_turn.append([dict(message) for message in messages])
        self.tool_names_by_turn.append(tuple(tool.name for tool in tools))
        if self.start_count > len(self.turns):
            raise AssertionError("Provider dispatched for too many distillation turns")
        turn = self.turns[self.start_count - 1]
        if isinstance(turn, BaseException):
            raise turn
        for event in turn:
            yield event


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(fixtures, "SOURCE_IMPORT_ID", SOURCE_IMPORT_ID)
    monkeypatch.setattr(
        fixtures,
        "SOURCE_RECEIPT_ID",
        source_import_receipt_id(SOURCE_IMPORT_ID),
    )
    monkeypatch.setattr(fixtures, "PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(fixtures, "IDEMPOTENCY_KEY", IDEMPOTENCY_KEY)
    monkeypatch.setattr(fixtures, "NOW", NOW)

    original_bindings = fixtures._bindings
    tool_hash = distillation_provider_tool_definitions_hash(
        build_distillation_tool_definitions()
    )

    def patched_bindings():
        source, project, knowledge, instruction, provider = original_bindings()
        return (
            source,
            project,
            knowledge,
            instruction,
            provider.model_copy(update={"tool_definitions_hash": tool_hash}),
        )

    monkeypatch.setattr(fixtures, "_bindings", patched_bindings)
    return fixtures._normal_chain()


@pytest.fixture
def prepared_store(tmp_path, chain) -> DistillationInvocationStore:
    sqlite = SqliteStore(tmp_path / "runtime.sqlite3")
    sqlite.initialize()
    store_fixtures._seed_static_bindings(sqlite, chain)
    store = DistillationInvocationStore(sqlite)
    store.create_prepared(
        receipt=chain["prepared"],
        idempotency_key=IDEMPOTENCY_KEY,
    )
    return store


def test_provider_loop_reads_anchor_persists_three_candidates_and_final_success(
    prepared_store: DistillationInvocationStore,
    chain,
) -> None:
    async def scenario() -> None:
        catalog = chain["catalog"]
        read_anchor = catalog.anchors[0]
        submission = fixtures._submission(read_anchor.anchor_id)
        provider = ScriptedDistillationProvider(
            [
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="provider_call_read_0001",
                        tool_name="distillation_anchor_read",
                        arguments={
                            "schema": "zhimeng.distillation-anchor-read-request.v1",
                            "anchor_ids": [read_anchor.anchor_id],
                        },
                    ),
                    ProviderCompleted("tool_calls", None, "gateway-model"),
                ],
                [
                    ProviderToolCall(
                        index=0,
                        tool_call_id="provider_call_submit_0002",
                        tool_name="distillation_output_submit",
                        arguments=submission.model_dump(mode="json", by_alias=True),
                    ),
                    ProviderCompleted("tool_calls", None, "gateway-model"),
                ],
            ]
        )
        service = DistillationInvocationService(
            invocation_store=prepared_store,
            provider=None,
            provider_resolver=lambda: provider,
            clock=lambda: NOW,
        )

        result = await service.run_prepared(
            catalog.invocation_id,
            anchor_catalog=catalog,
            text_by_anchor_id={
                catalog.anchors[0].anchor_id: "alpha",
                catalog.anchors[1].anchor_id: "beta",
            },
        )

        assert result.snapshot.invocation.status.value == "succeeded"
        assert result.snapshot.invocation.provider_replay_count == 0
        assert result.terminal_reason == "succeeded"
        assert provider.start_count == 2
        assert provider.tool_names_by_turn == [
            (
                "distillation_anchor_search",
                "distillation_anchor_read",
                "distillation_output_submit",
            ),
            (
                "distillation_anchor_search",
                "distillation_anchor_read",
                "distillation_output_submit",
            ),
        ]
        assert [receipt.kind for receipt in result.snapshot.receipts] == [
            DistillationReceiptKind.PREPARED,
            DistillationReceiptKind.PROVIDER_DISPATCH,
            DistillationReceiptKind.PROVIDER_TURN_RESULT,
            DistillationReceiptKind.PROVIDER_DISPATCH,
            DistillationReceiptKind.PROVIDER_TURN_RESULT,
            DistillationReceiptKind.CANDIDATE_OUTPUT,
            DistillationReceiptKind.FINAL_RESULT,
        ]
        second_dispatch = result.snapshot.receipts[3]
        assert second_dispatch.payload.prior_tool_result is not None
        assert [
            anchor.anchor_id
            for anchor in second_dispatch.payload.prior_tool_result.read_anchors
        ] == [read_anchor.anchor_id]
        assert provider.messages_by_turn[1][-1]["role"] == "tool"

        with prepared_store.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT source_card_index, card_type, status,
                       runtime_distillation_invocation_id
                FROM knowledge_candidate
                WHERE runtime_distillation_invocation_id = ?
                ORDER BY source_card_index
                """,
                (catalog.invocation_id,),
            ).fetchall()
        assert [tuple(row) for row in rows] == [
            (0, "work", "pending", catalog.invocation_id),
            (1, "voice", "pending", catalog.invocation_id),
            (2, "validator", "pending", catalog.invocation_id),
        ]

    asyncio.run(scenario())


def test_provider_mixed_text_and_submit_fails_closed_without_candidates(
    prepared_store: DistillationInvocationStore,
    chain,
) -> None:
    async def scenario() -> None:
        catalog = chain["catalog"]
        submission = fixtures._submission(catalog.anchors[0].anchor_id)
        provider = ScriptedDistillationProvider(
            [
                [
                    ProviderTextDelta("这里不该输出正文。"),
                    ProviderToolCall(
                        index=0,
                        tool_call_id="provider_call_submit_bad1",
                        tool_name="distillation_output_submit",
                        arguments=submission.model_dump(mode="json", by_alias=True),
                    ),
                    ProviderCompleted("tool_calls", None, "gateway-model"),
                ]
            ]
        )
        service = DistillationInvocationService(
            invocation_store=prepared_store,
            provider=provider,
            clock=lambda: NOW,
        )

        result = await service.run_prepared(
            catalog.invocation_id,
            anchor_catalog=catalog,
            text_by_anchor_id={
                catalog.anchors[0].anchor_id: "alpha",
                catalog.anchors[1].anchor_id: "beta",
            },
        )

        assert result.snapshot.invocation.status.value == "failed"
        assert result.terminal_reason == "distillation_provider_mixed_text_and_submit"
        assert [receipt.kind for receipt in result.snapshot.receipts] == [
            DistillationReceiptKind.PREPARED,
            DistillationReceiptKind.PROVIDER_DISPATCH,
            DistillationReceiptKind.FINAL_RESULT,
        ]
        with prepared_store.database.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM knowledge_candidate").fetchone()[0]
        assert count == 0

    asyncio.run(scenario())


def test_provider_error_after_dispatch_creates_unknown_remote_recovery_dossier(
    prepared_store: DistillationInvocationStore,
    chain,
) -> None:
    async def scenario() -> None:
        catalog = chain["catalog"]
        provider = ScriptedDistillationProvider(
            [
                AgentProviderError(
                    "socket closed after dispatch",
                    code="agent_provider_request_failed",
                    retryable=True,
                )
            ]
        )
        service = DistillationInvocationService(
            invocation_store=prepared_store,
            provider=provider,
            clock=lambda: NOW,
        )

        result = await service.run_prepared(
            catalog.invocation_id,
            anchor_catalog=catalog,
            text_by_anchor_id={
                catalog.anchors[0].anchor_id: "alpha",
                catalog.anchors[1].anchor_id: "beta",
            },
        )

        assert result.snapshot.invocation.status.value == "interrupted"
        assert result.terminal_reason == "recovery_dossier"
        assert [receipt.kind for receipt in result.snapshot.receipts] == [
            DistillationReceiptKind.PREPARED,
            DistillationReceiptKind.PROVIDER_DISPATCH,
            DistillationReceiptKind.RECOVERY_DOSSIER,
        ]
        dossier = result.snapshot.receipts[-1].payload
        assert dossier.remote_result == "unknown"
        assert dossier.provider_replay_count == 0
        assert dossier.candidate_state.value == "absent"

    asyncio.run(scenario())
