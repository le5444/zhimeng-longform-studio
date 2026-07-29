from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from test_knowledge_api import (
    _reviewed_work_card,
    _seed_source_import,
    _settings,
    _write_project,
)
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_models import canonical_json_hash
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.project_kb_models import (
    ProjectKbRebuildRequest,
    ProjectKbStatus,
)
from zhimeng_runtime.project_kb_service import (
    ProjectKbConflictError,
    ProjectKbService,
    ProjectKbTamperedError,
)
from zhimeng_runtime.project_reader import ProjectReader


@dataclass(slots=True)
class _KbHarness:
    settings: RuntimeSettings
    client: TestClient
    database: SqliteStore
    projects_root: Path
    project_root: Path
    project: dict[str, object]
    service: ProjectKbService

    @property
    def project_id(self) -> str:
        return str(self.project["project_id"])

    @property
    def kb_root(self) -> Path:
        return self.service.state_root / f"{self.project_id}.kb"


@pytest.fixture
def kb_harness(tmp_path: Path) -> Iterator[_KbHarness]:
    projects_root = tmp_path / "projects"
    project_root = _write_project(projects_root / "novel")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    with TestClient(create_app(settings)) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_root)},
        )
        assert inspected.status_code == 200, inspected.text
        database = SqliteStore(settings.database_path)
        database.initialize()
        service = ProjectKbService(
            database,
            ProjectReader((projects_root,)),
            settings.database_path.parent / "project-kb",
        )
        service.store.ensure_project_scope(inspected.json()["project"]["project_id"])
        yield _KbHarness(
            settings=settings,
            client=client,
            database=database,
            projects_root=projects_root,
            project_root=project_root,
            project=inspected.json()["project"],
            service=service,
        )


def _request_for(service: ProjectKbService, project_id: str) -> ProjectKbRebuildRequest:
    snapshot = service.store.project_state_snapshot(project_id)
    return ProjectKbRebuildRequest(
        expected_project_revision=snapshot.project.revision,
        expected_project_fingerprint=snapshot.project.fingerprint,
        expected_scope_state_revision=snapshot.scope.state_revision,
        expected_knowledge_fingerprint=canonical_json_hash(
            snapshot.fingerprint_material
        ),
    )


def _seed_pending_candidates(
    harness: _KbHarness,
    *,
    suffix: str,
) -> tuple[str, object, dict[str, object]]:
    source_import_id = f"source-import-{suffix * 32}"
    receipt = _seed_source_import(
        harness.database,
        harness.settings.database_path,
        import_id=source_import_id,
        card_id=f"work-project-kb-{suffix}",
    )
    knowledge_path = f"/api/v1/projects/{harness.project_id}/knowledge"
    imported = harness.client.post(
        f"{knowledge_path}/source-imports/{source_import_id}/candidates"
    )
    assert imported.status_code == 201, imported.text
    return source_import_id, receipt, imported.json()


def _formalize_work_card(harness: _KbHarness, *, suffix: str) -> dict[str, object]:
    source_import_id, receipt, batch = _seed_pending_candidates(
        harness,
        suffix=suffix,
    )
    knowledge_path = f"/api/v1/projects/{harness.project_id}/knowledge"
    work = next(
        candidate
        for candidate in batch["candidates"]
        if candidate["card_type"] == "work"
    )
    reviewed = harness.client.put(
        f"{knowledge_path}/candidates/{work['candidate_id']}",
        json={
            "expected_revision": work["revision"],
            "expected_content_hash": work["content_hash"],
            "card": _reviewed_work_card(str(work["card_id"])),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    promoted = harness.client.post(
        f"{knowledge_path}/candidates/{work['candidate_id']}/promote",
        json={
            "expected_revision": reviewed.json()["revision"],
            "expected_content_hash": reviewed.json()["content_hash"],
            "replace_active": False,
        },
    )
    assert promoted.status_code == 200, promoted.text

    candidates = harness.client.get(
        f"{knowledge_path}/candidates",
        params={"source_import_id": source_import_id},
    )
    assert candidates.status_code == 200, candidates.text
    for candidate in candidates.json()["candidates"]:
        if candidate["status"] != "pending":
            continue
        rejected = harness.client.post(
            f"{knowledge_path}/candidates/{candidate['candidate_id']}/reject",
            json={
                "expected_revision": candidate["revision"],
                "expected_content_hash": candidate["content_hash"],
            },
        )
        assert rejected.status_code == 200, rejected.text

    terminal = harness.client.get(
        f"{knowledge_path}/candidates",
        params={"source_import_id": source_import_id},
    )
    assert terminal.status_code == 200, terminal.text
    finalized = harness.client.post(
        f"{knowledge_path}/source-imports/{source_import_id}/formalization/finalize",
        json={
            "expected_source_receipt_hash": receipt.receipt_hash,
            "expected_draft_hash": receipt.cards_draft_hash,
            "expected_candidate_set_hash": terminal.json()["candidate_set_hash"],
        },
    )
    assert finalized.status_code == 200, finalized.text
    return finalized.json()


def _current_snapshot_root(harness: _KbHarness) -> Path:
    pointer = json.loads((harness.kb_root / "current.json").read_text("utf-8"))
    return harness.kb_root / "snapshots" / pointer["state_id"]


def test_missing_then_empty_rebuild_is_current_and_idempotent(
    kb_harness: _KbHarness,
) -> None:
    missing = kb_harness.service.get_state(kb_harness.project_id)
    assert missing.status is ProjectKbStatus.MISSING
    assert missing.pointer is None
    assert missing.manifest is None

    request = _request_for(kb_harness.service, kb_harness.project_id)
    first = kb_harness.service.rebuild(kb_harness.project_id, request)
    pointer_bytes = (kb_harness.kb_root / "current.json").read_bytes()
    second = kb_harness.service.rebuild(kb_harness.project_id, request)

    assert first.reused_snapshot is False
    assert second.reused_snapshot is True
    assert first.record.status is ProjectKbStatus.CURRENT
    assert second.record.pointer == first.record.pointer
    assert second.record.manifest == first.record.manifest
    assert second.record.manifest is not None
    assert second.record.manifest.counts.model_dump() == {
        "candidates": 0,
        "cards": 0,
        "active_cards": 0,
        "formalization_receipts": 0,
        "active_index_entries": 0,
    }
    assert (kb_harness.kb_root / "current.json").read_bytes() == pointer_bytes
    assert kb_harness.service.get_state(kb_harness.project_id).status is ProjectKbStatus.CURRENT


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("expected_project_revision", 999),
        ("expected_project_fingerprint", "f" * 64),
        ("expected_scope_state_revision", 999),
        ("expected_knowledge_fingerprint", "e" * 64),
    ],
)
def test_rebuild_rejects_stale_compare_and_swap_expectations(
    kb_harness: _KbHarness,
    field: str,
    replacement: object,
) -> None:
    request = _request_for(kb_harness.service, kb_harness.project_id)
    stale = request.model_copy(update={field: replacement})

    with pytest.raises(ProjectKbConflictError) as raised:
        kb_harness.service.rebuild(kb_harness.project_id, stale)

    assert raised.value.retryable is True
    assert [item["field"] for item in raised.value.details] == [
        field.removeprefix("expected_")
    ]
    assert kb_harness.service.get_state(kb_harness.project_id).status is ProjectKbStatus.MISSING


def test_formalized_knowledge_and_project_file_changes_make_snapshot_stale(
    kb_harness: _KbHarness,
) -> None:
    empty = kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    finalized = _formalize_work_card(kb_harness, suffix="a")

    knowledge_stale = kb_harness.service.get_state(kb_harness.project_id)
    assert knowledge_stale.status is ProjectKbStatus.STALE
    assert "knowledge_scope_revision_changed" in knowledge_stale.reasons
    assert "knowledge_fingerprint_changed" in knowledge_stale.reasons

    rebuilt = kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    assert rebuilt.record.manifest is not None
    assert rebuilt.record.manifest.state_id != empty.record.manifest.state_id
    assert rebuilt.record.manifest.counts.candidates == 3
    assert rebuilt.record.manifest.counts.cards == 1
    assert rebuilt.record.manifest.counts.active_cards == 1
    assert rebuilt.record.manifest.counts.formalization_receipts == 1
    assert rebuilt.record.manifest.counts.active_index_entries == 1
    assert finalized["formalization_receipt_id"] in (
        _current_snapshot_root(kb_harness) / "receipts" / "formalization.jsonl"
    ).read_text("utf-8")
    assert empty.record.manifest is not None
    assert (
        kb_harness.kb_root / "snapshots" / empty.record.manifest.state_id
    ).is_dir()
    assert not (kb_harness.kb_root / "publication.pending.json").exists()

    manuscript = kb_harness.project_root / "05_正文内容" / "第一卷" / "CH001_正文.md"
    manuscript.write_text("第一章，项目文件已经发生变化。", encoding="utf-8")
    project_stale = kb_harness.service.get_state(kb_harness.project_id)
    assert project_stale.status is ProjectKbStatus.STALE
    assert "project_files_changed" in project_stale.reasons


def test_pending_candidate_only_drift_marks_projection_stale(
    kb_harness: _KbHarness,
) -> None:
    before = kb_harness.service.store.project_state_snapshot(kb_harness.project_id)
    kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    _seed_pending_candidates(kb_harness, suffix="b")
    after = kb_harness.service.store.project_state_snapshot(kb_harness.project_id)

    assert after.scope.state_revision == before.scope.state_revision
    assert canonical_json_hash(after.fingerprint_material) == canonical_json_hash(
        before.fingerprint_material
    )
    assert len(before.candidates) == 0
    assert len(after.candidates) == 3
    stale = kb_harness.service.get_state(kb_harness.project_id)
    assert stale.status is ProjectKbStatus.STALE
    assert "knowledge_projection_changed" in stale.reasons


@pytest.mark.parametrize("target", ["pointer", "manifest", "artifact", "extra"])
def test_pointer_manifest_and_artifact_tampering_fail_closed(
    kb_harness: _KbHarness,
    target: str,
) -> None:
    kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    snapshot_root = _current_snapshot_root(kb_harness)
    if target == "pointer":
        path = kb_harness.kb_root / "current.json"
    elif target == "manifest":
        path = snapshot_root / "manifest.json"
    elif target == "artifact":
        path = snapshot_root / "state" / "scope.json"
    else:
        path = snapshot_root / "unexpected.json"
    path.write_bytes(path.read_bytes() + b" " if path.exists() else b"{}\n")

    state = kb_harness.service.get_state(kb_harness.project_id)
    assert state.status is ProjectKbStatus.TAMPERED
    assert state.reasons


def test_restart_uses_sqlite_revision_when_project_reader_revision_restarts_at_one(
    kb_harness: _KbHarness,
) -> None:
    manuscript = kb_harness.project_root / "05_正文内容" / "第一卷" / "CH001_正文.md"
    manuscript.write_text("第一章，第二次受管项目快照。", encoding="utf-8")
    refreshed = kb_harness.client.get(
        f"/api/v1/projects/{kb_harness.project_id}/inspect"
    )
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["project"]["revision"] == 2

    first_process = ProjectKbService(
        SqliteStore(kb_harness.settings.database_path),
        ProjectReader((kb_harness.projects_root,)),
        kb_harness.service.state_root,
    )
    rebuilt = first_process.rebuild(
        kb_harness.project_id,
        _request_for(first_process, kb_harness.project_id),
    )
    assert rebuilt.record.manifest is not None
    assert rebuilt.record.manifest.project.revision == 2

    restarted_database = SqliteStore(kb_harness.settings.database_path)
    restarted_database.initialize()
    restarted = ProjectKbService(
        restarted_database,
        ProjectReader((kb_harness.projects_root,)),
        kb_harness.service.state_root,
    )
    recovered = restarted.get_state(kb_harness.project_id)
    assert recovered.status is ProjectKbStatus.CURRENT
    assert recovered.manifest is not None
    assert recovered.manifest.project.revision == 2


def test_concurrent_rebuilds_publish_one_immutable_snapshot(
    kb_harness: _KbHarness,
) -> None:
    request = _request_for(kb_harness.service, kb_harness.project_id)
    services = [
        ProjectKbService(
            SqliteStore(kb_harness.settings.database_path),
            ProjectReader((kb_harness.projects_root,)),
            kb_harness.service.state_root,
        )
        for _ in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda service: service.rebuild(kb_harness.project_id, request),
                services,
            )
        )

    assert sorted(result.reused_snapshot for result in results) == [False, True]
    assert len({result.record.manifest.state_id for result in results}) == 1
    snapshots = [
        path
        for path in (kb_harness.kb_root / "snapshots").iterdir()
        if path.is_dir() and not path.name.startswith(".tmp-")
    ]
    assert len(snapshots) == 1
    assert kb_harness.service.get_state(kb_harness.project_id).status is ProjectKbStatus.CURRENT


def test_current_binding_lease_blocks_rebuild_until_release(
    kb_harness: _KbHarness,
) -> None:
    published = kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    assert published.record.manifest is not None
    manifest = published.record.manifest
    lease_entered = Event()
    release_lease = Event()
    rebuild_started = Event()
    rebuild_finished = Event()

    def hold_current_binding() -> None:
        with kb_harness.service.current_binding_lease(
            kb_harness.project_id,
            project_kb_state_id=manifest.state_id,
            project_kb_manifest_hash=manifest.manifest_hash,
        ):
            lease_entered.set()
            assert release_lease.wait(5)

    def rebuild_while_leased() -> None:
        rebuild_started.set()
        kb_harness.service.rebuild(
            kb_harness.project_id,
            _request_for(kb_harness.service, kb_harness.project_id),
        )
        rebuild_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        lease_future = pool.submit(hold_current_binding)
        assert lease_entered.wait(5)
        rebuild_future = pool.submit(rebuild_while_leased)
        assert rebuild_started.wait(5)
        assert not rebuild_finished.wait(0.2)
        release_lease.set()
        lease_future.result(timeout=5)
        rebuild_future.result(timeout=5)

    assert rebuild_finished.is_set()


def test_current_binding_verifies_inside_caller_write_transaction(
    kb_harness: _KbHarness,
) -> None:
    published = kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    assert published.record.manifest is not None
    expected = published.record.manifest

    with kb_harness.database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        verified = kb_harness.service.require_current_binding_in_connection(
            connection,
            kb_harness.project_id,
            project_kb_state_id=expected.state_id,
            project_kb_manifest_hash=expected.manifest_hash,
            expected_knowledge_fingerprint=expected.knowledge_fingerprint,
        )
        with pytest.raises(
            ProjectKbConflictError,
            match="fingerprint does not match",
        ):
            kb_harness.service.require_current_binding_in_connection(
                connection,
                kb_harness.project_id,
                project_kb_state_id=expected.state_id,
                project_kb_manifest_hash=expected.manifest_hash,
                expected_knowledge_fingerprint="f" * 64,
            )
        connection.rollback()

    assert verified.state_id == expected.state_id
    assert verified.manifest_hash == expected.manifest_hash


def test_pointer_publication_failure_preserves_current_and_cleans_orphans(
    kb_harness: _KbHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    current_path = kb_harness.kb_root / "current.json"
    current_before = current_path.read_bytes()
    snapshots_root = kb_harness.kb_root / "snapshots"
    snapshots_before = {path.name for path in snapshots_root.iterdir()}
    historical_tmp = snapshots_root / ".tmp-abandoned-before-rebuild"
    historical_tmp.mkdir()
    (historical_tmp / "partial").write_text("partial", encoding="utf-8")

    with kb_harness.database.connection() as connection:
        connection.execute(
            "UPDATE knowledge_scope SET state_revision = state_revision + 1 "
            "WHERE project_binding_id = ?",
            (kb_harness.project_id,),
        )

    def fail_pointer_publication(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected current pointer failure")

    monkeypatch.setattr(
        kb_harness.service,
        "_atomic_write_current",
        fail_pointer_publication,
    )
    with pytest.raises(ProjectKbTamperedError):
        kb_harness.service.rebuild(
            kb_harness.project_id,
            _request_for(kb_harness.service, kb_harness.project_id),
        )

    assert current_path.read_bytes() == current_before
    assert {path.name for path in snapshots_root.iterdir()} == snapshots_before
    assert not list(snapshots_root.glob(".tmp-*"))
    assert not list(kb_harness.kb_root.glob(".current-*.tmp"))


def test_restart_reclaims_final_snapshot_left_before_pointer_publication(
    kb_harness: _KbHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    def crash_before_pointer(*_args: object, **_kwargs: object) -> None:
        raise SimulatedProcessCrash

    monkeypatch.setattr(
        kb_harness.service,
        "_atomic_write_current",
        crash_before_pointer,
    )
    with pytest.raises(SimulatedProcessCrash):
        kb_harness.service.rebuild(
            kb_harness.project_id,
            _request_for(kb_harness.service, kb_harness.project_id),
        )

    snapshots_root = kb_harness.kb_root / "snapshots"
    crashed_snapshots = {path.name for path in snapshots_root.iterdir()}
    assert len(crashed_snapshots) == 1
    assert not (kb_harness.kb_root / "current.json").exists()
    assert (kb_harness.kb_root / "publication.pending.json").is_file()

    monkeypatch.undo()
    with kb_harness.database.connection() as connection:
        connection.execute(
            "UPDATE knowledge_scope SET state_revision = state_revision + 1 "
            "WHERE project_binding_id = ?",
            (kb_harness.project_id,),
        )
    restarted = ProjectKbService(
        SqliteStore(kb_harness.settings.database_path),
        ProjectReader((kb_harness.projects_root,)),
        kb_harness.service.state_root,
    )
    recovered = restarted.rebuild(
        kb_harness.project_id,
        _request_for(restarted, kb_harness.project_id),
    )

    assert recovered.record.status is ProjectKbStatus.CURRENT
    assert recovered.record.manifest is not None
    assert recovered.record.manifest.state_id not in crashed_snapshots
    assert not crashed_snapshots.intersection(
        path.name for path in snapshots_root.iterdir()
    )
    assert not (kb_harness.kb_root / "publication.pending.json").exists()


def test_get_state_rechecks_knowledge_after_project_observation(
    kb_harness: _KbHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    original_snapshot = kb_harness.service.store.project_state_snapshot
    calls = 0

    def mutate_after_first_snapshot(project_id: str):
        nonlocal calls
        snapshot = original_snapshot(project_id)
        calls += 1
        if calls == 1:
            with kb_harness.database.connection() as connection:
                connection.execute(
                    "UPDATE knowledge_scope SET state_revision = state_revision + 1 "
                    "WHERE project_binding_id = ?",
                    (kb_harness.project_id,),
                )
        return snapshot

    monkeypatch.setattr(
        kb_harness.service.store,
        "project_state_snapshot",
        mutate_after_first_snapshot,
    )
    state = kb_harness.service.get_state(kb_harness.project_id)

    assert calls == 2
    assert state.status is ProjectKbStatus.STALE
    assert "knowledge_changed_during_read" in state.reasons
    assert "knowledge_projection_changed" in state.reasons


def test_snapshot_is_runtime_owned_and_excludes_project_path_and_manuscript(
    kb_harness: _KbHarness,
) -> None:
    _formalize_work_card(kb_harness, suffix="c")
    rebuilt = kb_harness.service.rebuild(
        kb_harness.project_id,
        _request_for(kb_harness.service, kb_harness.project_id),
    )
    assert rebuilt.record.manifest is not None
    assert rebuilt.record.manifest.source_of_truth.model_dump() == {
        "schema_id": "zhimeng.project-kb-source-of-truth.v1",
        "project_files": "work_truth",
        "runtime_sqlite": "run_truth",
        "derived": True,
        "rebuildable": True,
    }
    assert kb_harness.service.state_root == (
        kb_harness.settings.database_path.parent / "project-kb"
    ).resolve()
    assert not kb_harness.service.state_root.is_relative_to(kb_harness.project_root)
    assert not list(kb_harness.project_root.rglob("*.kb"))

    snapshot_root = _current_snapshot_root(kb_harness)
    serialized = b"\n".join(
        path.read_bytes() for path in snapshot_root.rglob("*") if path.is_file()
    ).decode("utf-8")
    absolute_root = str(kb_harness.project_root.resolve())
    manuscript = (
        kb_harness.project_root / "05_正文内容" / "第一卷" / "CH001_正文.md"
    ).read_text("utf-8")
    assert absolute_root not in serialized
    assert absolute_root.replace("\\", "/") not in serialized
    assert manuscript not in serialized
