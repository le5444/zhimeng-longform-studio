from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.agent_tools import AgentToolExecutor, ComputerReadFileArguments, ProjectReadFileArguments
from zhimeng_runtime.change_set_service import ChangeSetService
from zhimeng_runtime.context_service import ContextService
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.execution_protocol import CapabilityProfile, ExecutorIdentity, ExecutorKind, RunStatus
from zhimeng_runtime.project_locks import ProjectLockRegistry
from zhimeng_runtime.project_models import ProjectFileContentResponse
from zhimeng_runtime.project_reader import ProjectReader, ProjectChangedDuringReadError, WRITING_CONTEXT_FILE_KINDS
from test_office_reader import word, workbook_parts
from test_correction_p0_20260926 import ScriptedProvider
from test_open_folder_chat_20260927 import _bound_turn


def package(path: Path, parts: dict[str, str]) -> bytes:
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return path.read_bytes()


@pytest.fixture
def office_project(tmp_path):
    root = tmp_path / '采购资料'
    root.mkdir()
    doc = root / '说明.docx'
    doc_bytes = package(doc, {'word/document.xml': word('<w:p><w:r><w:t>采购核对：按工作表的原始数值汇总。</w:t></w:r></w:p>')})
    sheet = root / '明细.xlsx'
    sheet_bytes = package(sheet, workbook_parts('<row r="1"><c r="A1" t="inlineStr"><is><t>纸张</t></is></c><c r="B1"><v>12.5</v></c></row>'
        '<row r="2"><c r="B2"><f>B1*2</f><v>25</v></c><c r="C2"><f>1+1</f></c></row>',
        second='<row r="4"><c r="D4"><v>7</v></c></row>'))
    reader = ProjectReader((tmp_path,))
    snapshot = reader.inspect(root)
    database = SqliteStore(tmp_path / 'runtime.sqlite3')
    database.initialize()
    identity = snapshot['project']
    binding = database.sync_project_binding(project_id=identity['project_id'], root_path=str(root),
        display_name='采购资料', fingerprint=identity['fingerprint'])
    store = AgentStore(database)
    session = store.create_session(project_id=binding.project_id, title='核对采购资料',
        executor=ExecutorIdentity(executor_id='executor.office-test', kind=ExecutorKind.EMBEDDED,
            implementation='test', version='1', protocol_version='1', instance_id=None),
        capabilities=CapabilityProfile(protocol_version='1', names=[], extensions={}))
    run, _, _ = store.create_run(session_id=session.session_id, message='核对采购资料',
        expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
        active_chapter_id=None, idempotency_key='office-integration-turn')
    store.transition_run(run.run_id, RunStatus.STARTING, detail='test')
    run = store.transition_run(run.run_id, RunStatus.RUNNING, detail='test')
    executor = AgentToolExecutor(reader, database, ProjectLockRegistry(), ChangeSetService(reader, database, tmp_path / 'changes'))
    return root, reader, snapshot, executor, run, {doc: doc_bytes, sheet: sheet_bytes}


def test_project_preview_contract_context_and_binary_version(office_project):
    root, reader, snapshot, executor, run, originals = office_project
    service = object.__new__(ContextService)
    service.reader = reader
    for path, original in originals.items():
        record = next(f for f in snapshot['files'] if f['relative_path'] == path.name)
        result = reader.read_text_file_from_snapshot(snapshot, record['file_id'], allowed_kinds=WRITING_CONTEXT_FILE_KINDS)
        parsed = ProjectFileContentResponse.model_validate(result)
        assert parsed.etag == hashlib.sha256(original).hexdigest() == parsed.extraction.source_sha256
        candidate = service._read_project_candidate(snapshot, record, source_type='manual_project_file', priority=100, mandatory=True, project_revision=1)
        assert '文档文字提取视图' in candidate.content
        assert candidate.source_hash == parsed.etag
        assert candidate.eligible
        if path.suffix == '.xlsx':
            assert '公式缓存' in parsed.content and '公式无缓存' in parsed.content
            assert 'D4\t7' in parsed.content and '（隐藏）' in parsed.content
            assert '不重新计算' in candidate.content
        assert path.read_bytes() == original
    # A formatting-only edit still invalidates the old binary snapshot.
    doc = root / '说明.docx'
    with zipfile.ZipFile(doc, 'a') as archive:
        archive.writestr('docProps/changed.xml', '<changed/>')
    record = next(f for f in snapshot['files'] if f['relative_path'] == doc.name)
    with pytest.raises(ProjectChangedDuringReadError):
        reader.read_text_file_from_snapshot(snapshot, record['file_id'], allowed_kinds=WRITING_CONTEXT_FILE_KINDS)


def test_project_and_external_tools_preserve_scope_paging_and_original(office_project):
    root, reader, snapshot, executor, run, originals = office_project
    async def read_project():
        result = await executor._read_file(run, ProjectReadFileArguments(relative_path='明细.xlsx', max_lines=2), asyncio.Event())
        return result.output
    result = asyncio.run(read_project())
    assert result['extraction']['format'] == 'xlsx'
    assert result['truncated'] and result['next_offset'] > 0
    assert 'hashline_content' not in result and 'file_tag' not in result
    first = executor._read_computer_file(ComputerReadFileArguments(path=str(root / '明细.xlsx'), max_lines=2))
    second = executor._read_computer_file(ComputerReadFileArguments(path=str(root / '明细.xlsx'), start_line=first['next_start_line'], max_lines=100))
    assert first['content'] + second['content'] == reader.read_text_file(root, next(f['file_id'] for f in snapshot['files'] if f['relative_path']=='明细.xlsx'))['content']
    assert '公式无缓存' in second['content']
    assert first['sha256'] == second['sha256']
    for path, original in originals.items():
        assert path.read_bytes() == original


def test_ordinary_chat_office_references_include_scope_and_retain_it_next_turn(office_project, tmp_path):
    root, _, _, _, _, originals = office_project
    provider = ScriptedProvider([None, None])
    app = create_app(RuntimeSettings(
        environment='test', database_path=tmp_path / 'chat.sqlite3',
        trusted_project_roots=(tmp_path,), allowed_origins=(), managed_skill_root=None,
    ), agent_provider=provider)
    with TestClient(app) as client:
        snapshot = client.post('/api/v1/projects/inspect', json={'root_path': str(root)}).raise_for_status().json()
        session = client.post(f"/api/v1/projects/{snapshot['project']['project_id']}/agent/sessions",
            json={'title': '采购核对'}).raise_for_status().json()
        first = _bound_turn(client, session['session_id'], snapshot, {
            'message': '核对文档和表格', 'writer_mode': 'repair_only',
            'manual_file_ids': [item['file_id'] for item in snapshot['files']],
        }, 'office-chat-reference-first')
        second = _bound_turn(client, session['session_id'], snapshot, {
            'message': '继续核对', 'writer_mode': 'repair_only',
        }, 'office-chat-reference-followup')
        assert first['status'] == second['status'] == 'succeeded'
    attached = next(message for message in provider.requests[0]
        if '用户附上的文件' in str(message.get('content', '')))
    assert '[文档文字提取视图：docx；' in attached['content']
    assert '[文档文字提取视图：xlsx；' in attached['content']
    assert '图片、页眉页脚、批注和完整排版不包含' in attached['content']
    assert '公式只显示文件内已有缓存，可能过期；不重新计算' in attached['content']
    assert '〔公式无缓存，未计算〕' in attached['content']
    assert provider.requests[1].count(attached) == 1
    for path, original in originals.items():
        assert path.read_bytes() == original
