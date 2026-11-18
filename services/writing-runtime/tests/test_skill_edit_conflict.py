"""Real HTTP and file checks for settings edits, without touching shared skills."""
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.user_skill_api import SaveSkillInstructionsRequest, create_user_skill_router
from zhimeng_runtime.user_skill_folder_service import MAX_USER_SKILL_MD_BYTES, UserSkillFolderService


def test_edit_checks_version_preserves_bom_newlines_and_backup(tmp_path: Path):
    service = UserSkillFolderService(tmp_path / 'skills', tmp_path / 'state')
    assert service.create_skill('weekly-report', '整理周报', '# 方法\n读取来源\n')[0]
    path = tmp_path / 'skills' / 'weekly-report' / 'SKILL.md'
    original = path.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8-sig')
    path.write_bytes(original)
    app = FastAPI()
    app.include_router(create_user_skill_router(service))
    with TestClient(app) as client:
        url = '/api/v1/user-skills/weekly-report/instructions'
        before = client.get(url).json()
        external = original.replace('读取来源'.encode(), '外部更新'.encode())
        path.write_bytes(external)
        conflict = client.put(url, json={'content': before['content'] + '\n编辑草稿', 'expected_sha256': before['sha256']})
        assert conflict.status_code == 409
        assert path.read_bytes() == external
        assert not path.with_name('SKILL.md.bak').exists()
        latest = client.get(url).json()
        text = latest['content'].replace('\r\n', '\n').replace('外部更新', '核对来源')
        saved = client.put(url, json={'content': text, 'expected_sha256': latest['sha256']})
        assert saved.status_code == 200, saved.text
        assert path.read_bytes() == external.replace('外部更新'.encode(), '核对来源'.encode())
        assert path.with_name('SKILL.md.bak').read_bytes() == external
        assert '核对来源' in client.get(url).json()['content']


def test_skill_save_contract_matches_request():
    root = Path(__file__).resolve().parents[3]
    contract = json.loads((root / 'packages/contracts/save-skill-instructions-request.schema.json').read_text(encoding='utf-8'))
    assert contract == SaveSkillInstructionsRequest.model_json_schema()


def test_preserving_crlf_cannot_create_an_unreadable_oversized_skill(tmp_path: Path):
    service = UserSkillFolderService(tmp_path / 'skills', tmp_path / 'state')
    assert service.create_skill('notes', 'Read notes', '# Notes\n')[0]
    path = tmp_path / 'skills' / 'notes' / 'SKILL.md'
    original = path.read_bytes().replace(b'\n', b'\r\n')
    path.write_bytes(original)
    text = original.decode().replace('\r\n', '\n') + 'x\n' * (MAX_USER_SKILL_MD_BYTES // 3)
    ok, message = service.save_instructions('notes', text)
    assert not ok and '大小上限' in message
    assert path.read_bytes() == original
    assert not path.with_name('SKILL.md.bak').exists()
