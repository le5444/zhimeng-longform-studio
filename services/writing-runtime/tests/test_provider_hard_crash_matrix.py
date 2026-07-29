from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

import pytest
from fastapi.testclient import TestClient

import test_workflow_checkpoint_store as workflow_fixture_module
from test_agent_candidate_output import (
    _completed_workflow,
    _persist_candidate_cutpoint,
)
from test_workflow_checkpoint_store import (
    StartupTrapProvider,
    WorkflowFixture,
    app_settings,
)
from zhimeng_runtime.agent_candidate_models import (
    AgentCandidateContent,
    AgentCandidateSubmission,
)
from zhimeng_runtime.agent_store import AgentStore, AgentStoreNotFound
from zhimeng_runtime.agent_tools import CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import RunStatus


CRASH_EXIT_CODE = 86
RUNTIME_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_TOOL_CALL_ID = "tool_hard_crash_candidate_submit"
CANDIDATE_TOOL_OUTPUT = {
    "schema": "zhimeng.tool-result.candidate-output-submit.v1",
    "accepted": True,
    "candidate_kind": "repair_plan",
    "requires_author_review": True,
    "formal_draft_allowed": False,
}


class _ProviderProbeHandler(BaseHTTPRequestHandler):
    server: "_ProviderProbeServer"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
        content_length = int(self.headers.get("content-length", "0"))
        self.rfile.read(content_length)
        self.server.request_count += 1
        payload = b'{"remote_result":"accepted"}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: object) -> None:
        return


class _ProviderProbeServer(ThreadingHTTPServer):
    request_count: int


class _ProviderProbe:
    def __init__(self) -> None:
        self.server = _ProviderProbeServer(
            ("127.0.0.1", 0),
            _ProviderProbeHandler,
        )
        self.server.request_count = 0
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}/provider"

    def __enter__(self) -> "_ProviderProbe":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _project_tree_hash(project_root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(project_root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(project_root).as_posix()
        digest.update(("dir\0" if path.is_dir() else "file\0").encode("ascii"))
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _candidate_submission() -> AgentCandidateSubmission:
    return AgentCandidateSubmission(
        kind="repair_plan",
        content=AgentCandidateContent(
            title="Repair the prewrite evidence",
            summary="The workflow remains bounded to repair planning.",
            body="Repair the missing chapter control evidence before formal drafting.",
        ),
    )


def _runtime_settings(state_root: Path) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=state_root / "runtime.sqlite3",
        trusted_project_roots=((state_root / "library").resolve(),),
        managed_skill_root=(state_root / "managed-skills").resolve(),
        allowed_origins=(),
    )


def _completed_workflow_with_published_project_kb(
    tmp_path: Path,
    *,
    persist_provider_receipt: bool,
):
    original_compile = workflow_fixture_module.compile_context_pack

    def compile_with_published_project_kb(*args, **kwargs):
        request = kwargs["request"]
        project_id = str(kwargs["project_id"])
        first_app = create_app(
            _runtime_settings(tmp_path),
            agent_provider=StartupTrapProvider(),
        )
        with TestClient(first_app) as client:
            knowledge_response = client.get(
                f"/api/v1/projects/{project_id}/knowledge/cards"
            )
            assert knowledge_response.status_code == 200, knowledge_response.text
            knowledge = knowledge_response.json()
            rebuild_response = client.post(
                f"/api/v1/projects/{project_id}/knowledge/kb-state/rebuild",
                json={
                    "expected_project_revision": request.expected_project_revision,
                    "expected_project_fingerprint": (
                        request.expected_project_fingerprint
                    ),
                    "expected_scope_state_revision": knowledge["scope"][
                        "state_revision"
                    ],
                    "expected_knowledge_fingerprint": knowledge["fingerprint"],
                },
            )
            assert rebuild_response.status_code == 200, rebuild_response.text
            manifest = rebuild_response.json()["record"]["manifest"]

        kwargs["request"] = request.model_copy(
            update={
                "project_kb_state_id": manifest["state_id"],
                "project_kb_manifest_hash": manifest["manifest_hash"],
            }
        )
        kwargs["knowledge_fingerprint"] = manifest["knowledge_fingerprint"]
        return original_compile(*args, **kwargs)

    with patch.object(
        workflow_fixture_module,
        "compile_context_pack",
        side_effect=compile_with_published_project_kb,
    ):
        return _completed_workflow(
            tmp_path,
            persist_provider_receipt=persist_provider_receipt,
        )


def _begin_candidate_submission(fixture: WorkflowFixture) -> None:
    submission = _candidate_submission()
    pending, replayed = fixture.store.begin_tool_call(
        fixture.run.run_id,
        tool_call_id=CANDIDATE_TOOL_CALL_ID,
        tool_name=CANDIDATE_OUTPUT_SUBMIT_TOOL_NAME,
        tool_version="2",
        arguments=submission.model_dump(mode="json", by_alias=True),
        request_hash="8" * 64,
        idempotency_key=CANDIDATE_TOOL_CALL_ID,
    )
    assert replayed is False
    assert pending.status == "running"


def _crash_worker(
    crash_point: str,
    database_path: Path,
    run_id: str,
    provider_endpoint: str | None,
) -> None:
    database = SqliteStore(database_path)
    database.initialize()
    store = AgentStore(database)

    if crash_point == "after_boundary_before_provider_request":
        os._exit(CRASH_EXIT_CODE)

    if crash_point == "after_provider_response_before_receipt":
        if provider_endpoint is None:
            raise RuntimeError("Provider endpoint is required for this crash point")
        request = Request(
            provider_endpoint,
            data=b'{"request":"candidate"}',
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=5) as response:  # noqa: S310 - loopback fixture.
            if response.status != 200:
                raise RuntimeError(f"Provider fixture returned {response.status}")
            response.read()
        os._exit(CRASH_EXIT_CODE)

    submission = _candidate_submission()
    candidate = store.build_agent_candidate_output(run_id, submission)

    if crash_point == "inside_candidate_transaction_before_commit":
        original_finish = AgentStore._finish_tool_call_in_transaction

        def crash_before_candidate_commit(self, *args, **kwargs):
            original_finish(self, *args, **kwargs)
            os._exit(CRASH_EXIT_CODE)

        AgentStore._finish_tool_call_in_transaction = crash_before_candidate_commit
        store.persist_candidate_recovery_cutpoint(
            candidate,
            candidate_tool_call_id=CANDIDATE_TOOL_CALL_ID,
            candidate_tool_output=CANDIDATE_TOOL_OUTPUT,
            candidate_tool_duration_ms=12,
        )
    elif crash_point == "after_candidate_commit_before_finalize":
        store.persist_candidate_recovery_cutpoint(
            candidate,
            candidate_tool_call_id=CANDIDATE_TOOL_CALL_ID,
            candidate_tool_output=CANDIDATE_TOOL_OUTPUT,
            candidate_tool_duration_ms=12,
        )
        os._exit(CRASH_EXIT_CODE)
    elif crash_point == "inside_finalize_transaction_before_commit":
        persisted = store.get_agent_candidate_output(run_id)
        original_complete = AgentStore._complete_run_in_connection

        def crash_before_finalize_commit(self, *args, **kwargs):
            result = original_complete(self, *args, **kwargs)
            os._exit(CRASH_EXIT_CODE)
            return result  # pragma: no cover

        AgentStore._complete_run_in_connection = crash_before_finalize_commit
        store.finalize_persisted_candidate(
            run_id,
            expected_candidate_output_id=persisted.candidate_output_id,
            expected_candidate_output_hash=persisted.envelope_hash,
            message_id="message_hard_crash_finalize_before_commit",
            usage=None,
        )
    elif crash_point == "after_finalize_commit":
        persisted = store.get_agent_candidate_output(run_id)
        store.finalize_persisted_candidate(
            run_id,
            expected_candidate_output_id=persisted.candidate_output_id,
            expected_candidate_output_hash=persisted.envelope_hash,
            message_id="message_hard_crash_finalize_after_commit",
            usage=None,
        )
        os._exit(CRASH_EXIT_CODE)
    else:  # pragma: no cover - the parent validates every supplied name.
        raise ValueError(f"Unknown crash point: {crash_point}")

    raise AssertionError(f"Crash point did not terminate the process: {crash_point}")


def _run_worker(
    crash_point: str,
    database_path: Path,
    run_id: str,
    *,
    provider_endpoint: str | None = None,
) -> None:
    env = os.environ.copy()
    python_path = [str(RUNTIME_ROOT / "src"), str(RUNTIME_ROOT / "tests")]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--crash-worker",
        crash_point,
        str(database_path),
        run_id,
        provider_endpoint or "-",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert completed.returncode == CRASH_EXIT_CODE, (
        f"worker returned {completed.returncode}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _assert_recovery_after_crash(
    fixture: WorkflowFixture,
    *,
    expected_candidate_state: str,
    expected_tool_call_status: str | None,
) -> None:
    provider = StartupTrapProvider()
    app = create_app(
        _runtime_settings(fixture.database.path.parent),
        agent_provider=provider,
    )
    with TestClient(app) as client:
        kb_response = client.get(
            f"/api/v1/projects/{fixture.pack.project_id}/knowledge/kb-state"
        )
        assert kb_response.status_code == 200, kb_response.text
        kb_record = kb_response.json()
        assert kb_record["status"] == "current"
        assert kb_record["manifest"]["state_id"] == fixture.pack.project_kb_state_id
        assert (
            kb_record["manifest"]["manifest_hash"]
            == fixture.pack.project_kb_manifest_hash
        )

        run_response = client.get(f"/api/v1/agent/runs/{fixture.run.run_id}")
        assert run_response.status_code == 200, run_response.text
        assert run_response.json()["run"]["status"] == "interrupted"

        recovery_response = client.get(
            f"/api/v1/agent/runs/{fixture.run.run_id}/provider-recovery"
        )
        assert recovery_response.status_code == 200, recovery_response.text
        recovery = recovery_response.json()
        dossier = recovery["dossier"]
        assert recovery["decision"] is None
        assert dossier["candidate_state"] == expected_candidate_state
        assert dossier["remote_result"] == "unknown"
        assert dossier["provider_replay_count"] == 0
        assert provider.call_count == 0

        action = (
            "adopt_persisted_candidate"
            if expected_candidate_state == "persisted"
            else "abandon_and_recompile"
        )
        decision_request = {
            "schema": "zhimeng.provider-recovery-decision-request.v1",
            "expected_dossier_revision": dossier["dossier_revision"],
            "expected_dossier_hash": dossier["dossier_hash"],
            "action": action,
        }
        decision_response = client.post(
            f"/api/v1/agent/runs/{fixture.run.run_id}/provider-recovery/decision",
            json=decision_request,
        )
        assert decision_response.status_code == 200, decision_response.text
        expected_status = "succeeded" if action == "adopt_persisted_candidate" else "failed"
        assert decision_response.json()["decision"]["resulting_run_status"] == expected_status
        assert provider.call_count == 0

        repeated = client.post(
            f"/api/v1/agent/runs/{fixture.run.run_id}/provider-recovery/decision",
            json=decision_request,
        )
        assert repeated.status_code == 409, repeated.text
        assert repeated.json()["error"]["code"] == "provider_recovery_already_decided"

    reopened = AgentStore(SqliteStore(fixture.database.path))
    if expected_candidate_state == "persisted":
        candidate = reopened.get_agent_candidate_output(fixture.run.run_id)
        result = reopened.get_writer_candidate_result(fixture.run.run_id)
        assert result.candidate_output_id == candidate.candidate_output_id
    else:
        with pytest.raises(AgentStoreNotFound):
            reopened.get_agent_candidate_output(fixture.run.run_id)
        with pytest.raises(AgentStoreNotFound):
            reopened.get_writer_candidate_result(fixture.run.run_id)
    if expected_tool_call_status is not None:
        expected_terminal_tool_status = (
            "failed" if expected_tool_call_status == "running" else expected_tool_call_status
        )
        assert (
            reopened.get_tool_call(CANDIDATE_TOOL_CALL_ID).status
            == expected_terminal_tool_status
        )


def _assert_hard_crash_case(
    tmp_path: Path,
    crash_point: str,
    *,
    provider_receipt: bool,
    prepare_candidate: str,
    expected_candidate_state: str | None,
    expected_tool_call_status: str | None,
) -> None:
    fixture, checkpoints, _ = _completed_workflow_with_published_project_kb(
        tmp_path,
        persist_provider_receipt=provider_receipt,
    )
    project_root = tmp_path / "library" / "novel"
    project_hash_before = _project_tree_hash(project_root)

    if prepare_candidate == "pending":
        _begin_candidate_submission(fixture)
    elif prepare_candidate == "persisted":
        _persist_candidate_cutpoint(
            fixture,
            checkpoints[-1],
            tool_call_id=CANDIDATE_TOOL_CALL_ID,
        )
    elif prepare_candidate != "none":
        raise AssertionError(f"Unknown candidate preparation: {prepare_candidate}")

    if crash_point in {
        "after_boundary_before_provider_request",
        "after_provider_response_before_receipt",
    }:
        with _ProviderProbe() as probe:
            _run_worker(
                crash_point,
                fixture.database.path,
                fixture.run.run_id,
                provider_endpoint=probe.endpoint,
            )
            expected_requests = (
                1 if crash_point == "after_provider_response_before_receipt" else 0
            )
            assert probe.server.request_count == expected_requests
    else:
        _run_worker(crash_point, fixture.database.path, fixture.run.run_id)

    assert _project_tree_hash(project_root) == project_hash_before
    raw_store = AgentStore(SqliteStore(fixture.database.path))
    assert len(raw_store.list_provider_turn_receipts(fixture.run.run_id)) == (
        1 if provider_receipt else 0
    )
    if expected_candidate_state == "persisted" or crash_point == "after_finalize_commit":
        raw_store.get_agent_candidate_output(fixture.run.run_id)
    else:
        with pytest.raises(AgentStoreNotFound):
            raw_store.get_agent_candidate_output(fixture.run.run_id)
    if expected_tool_call_status is not None:
        assert (
            raw_store.get_tool_call(CANDIDATE_TOOL_CALL_ID).status
            == expected_tool_call_status
        )
    if crash_point == "after_finalize_commit":
        raw_store.get_writer_candidate_result(fixture.run.run_id)
        assert raw_store.get_run(fixture.run.run_id).status is RunStatus.SUCCEEDED
    else:
        with pytest.raises(AgentStoreNotFound):
            raw_store.get_writer_candidate_result(fixture.run.run_id)
        assert raw_store.get_run(fixture.run.run_id).status is RunStatus.RUNNING
    with pytest.raises(AgentStoreNotFound):
        raw_store.get_provider_recovery(fixture.run.run_id)

    if crash_point == "after_finalize_commit":
        provider = StartupTrapProvider()
        app = create_app(app_settings(fixture), agent_provider=provider)
        with TestClient(app) as client:
            run_response = client.get(f"/api/v1/agent/runs/{fixture.run.run_id}")
            assert run_response.status_code == 200, run_response.text
            assert run_response.json()["run"]["status"] == "succeeded"
            recovery_response = client.get(
                f"/api/v1/agent/runs/{fixture.run.run_id}/provider-recovery"
            )
            assert recovery_response.status_code == 404, recovery_response.text
            assert provider.call_count == 0
        reopened = AgentStore(SqliteStore(fixture.database.path))
        candidate = reopened.get_agent_candidate_output(fixture.run.run_id)
        result = reopened.get_writer_candidate_result(fixture.run.run_id)
        assert result.candidate_output_id == candidate.candidate_output_id
        assert reopened.get_run(fixture.run.run_id).status is RunStatus.SUCCEEDED
        assert reopened.get_tool_call(CANDIDATE_TOOL_CALL_ID).status == "succeeded"
    else:
        assert expected_candidate_state is not None
        _assert_recovery_after_crash(
            fixture,
            expected_candidate_state=expected_candidate_state,
            expected_tool_call_status=expected_tool_call_status,
        )

    assert _project_tree_hash(project_root) == project_hash_before


def test_hard_crash_01_after_boundary_before_provider_request(tmp_path: Path) -> None:
    _assert_hard_crash_case(
        tmp_path,
        "after_boundary_before_provider_request",
        provider_receipt=False,
        prepare_candidate="none",
        expected_candidate_state="absent",
        expected_tool_call_status=None,
    )


def test_hard_crash_02_after_provider_response_before_receipt(tmp_path: Path) -> None:
    _assert_hard_crash_case(
        tmp_path,
        "after_provider_response_before_receipt",
        provider_receipt=False,
        prepare_candidate="none",
        expected_candidate_state="absent",
        expected_tool_call_status=None,
    )


def test_hard_crash_03_inside_candidate_transaction_before_commit(tmp_path: Path) -> None:
    _assert_hard_crash_case(
        tmp_path,
        "inside_candidate_transaction_before_commit",
        provider_receipt=True,
        prepare_candidate="pending",
        expected_candidate_state="absent",
        expected_tool_call_status="running",
    )


def test_hard_crash_04_after_candidate_commit_before_finalize(tmp_path: Path) -> None:
    _assert_hard_crash_case(
        tmp_path,
        "after_candidate_commit_before_finalize",
        provider_receipt=True,
        prepare_candidate="pending",
        expected_candidate_state="persisted",
        expected_tool_call_status="succeeded",
    )


def test_hard_crash_05_inside_finalize_transaction_before_commit(tmp_path: Path) -> None:
    _assert_hard_crash_case(
        tmp_path,
        "inside_finalize_transaction_before_commit",
        provider_receipt=True,
        prepare_candidate="persisted",
        expected_candidate_state="persisted",
        expected_tool_call_status="succeeded",
    )


def test_hard_crash_06_after_finalize_commit(tmp_path: Path) -> None:
    _assert_hard_crash_case(
        tmp_path,
        "after_finalize_commit",
        provider_receipt=True,
        prepare_candidate="persisted",
        expected_candidate_state=None,
        expected_tool_call_status="succeeded",
    )


if __name__ == "__main__" and len(sys.argv) >= 2 and sys.argv[1] == "--crash-worker":
    _, _, crash_point_arg, database_path_arg, run_id_arg, endpoint_arg = sys.argv
    _crash_worker(
        crash_point_arg,
        Path(database_path_arg),
        run_id_arg,
        None if endpoint_arg == "-" else endpoint_arg,
    )
