from pathlib import Path

from test_change_set_api import _client, _inspect
from zhimeng_runtime.project_instruction_context import read_project_instruction_snapshot


def test_settings_create_read_context_and_undo_instructions(tmp_path: Path):
    project = tmp_path / 'ordinary-project'
    project.mkdir()
    original = b'item,quantity\npen,2\n'
    (project / 'items.csv').write_bytes(original)
    content = '# 工作约定\n\n处理表格时先核对来源和单位。\n'
    with _client(tmp_path, tmp_path) as client:
        snapshot = _inspect(client, project)['project']
        created = client.post(f"/api/v1/projects/{snapshot['project_id']}/change-sets", json={
            'base_revision': snapshot['revision'], 'base_fingerprint': snapshot['fingerprint'],
            'title': '创建项目说明 AGENTS.md', 'summary': '设置中创建说明', 'origin': 'manual',
            'changes': [{'operation': 'create', 'relative_path': 'AGENTS.md', 'after_content': content, 'rationale': '用户填写的项目说明'}],
        })
        assert created.status_code == 201, created.text
        record = created.json()
        applied = client.post(f"/api/v1/change-sets/{record['change_set_id']}/apply-direct", json={'expected_review_revision': record['review_revision']})
        assert applied.status_code == 200, applied.text
        assert applied.json()['status'] == 'applied'
        assert (project / 'AGENTS.md').read_bytes() == content.encode('utf-8')
        assert dict(read_project_instruction_snapshot(project).files)['AGENTS.md'] == content.strip()
        refreshed = _inspect(client, project)
        assert any(item['relative_path'] == 'AGENTS.md' for item in refreshed['files'])
        receipt = client.get(f"/api/v1/change-sets/{record['change_set_id']}").json()
        assert receipt['status'] == 'applied'
        rolled = client.post(f"/api/v1/change-sets/{record['change_set_id']}/rollback", json={'expected_review_revision': receipt['review_revision']})
        assert rolled.status_code == 200, rolled.text
        assert not (project / 'AGENTS.md').exists()
        assert (project / 'items.csv').read_bytes() == original
        assert not any(item['relative_path'] == 'AGENTS.md' for item in _inspect(client, project)['files'])
        assert dict(read_project_instruction_snapshot(project).states)['AGENTS.md'] == 'missing'
