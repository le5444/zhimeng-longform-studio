from __future__ import annotations

from pathlib import Path
import json

from test_search_api import _client
from test_agent_api import ApiProvider, wait_for_success
from zhimeng_runtime.agent_provider import ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.app import create_app
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.search_models import SearchHit, SearchQueryResponse
from fastapi.testclient import TestClient


def test_search_and_open_general_project_text_files_with_real_line_context(tmp_path: Path):
    root = tmp_path / "ordinary-project"
    root.mkdir()
    names = ["src/app.py", "web/main.js", "settings.json", "settings.yaml", "data/report.csv", "README.md", "notes.txt", "Dockerfile", "unknown.custom-extension", ".env"]
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("before\nSearchNeedle42 示例\nafter\n".encode("utf-8"))
    (root / "binary.dat").write_bytes(b"SearchNeedle42\x00\x01\x02binary")
    with _client(tmp_path, root) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(root)})
        assert inspected.status_code == 200, inspected.text
        project_id = inspected.json()["project"]["project_id"]
        searched = client.post(f"/api/v1/projects/{project_id}/search", json={"query": "SearchNeedle42", "limit": 100})
        assert searched.status_code == 200, searched.text
        hits = searched.json()["hits"]
        assert {hit["relative_path"] for hit in hits} == set(names)
        for hit in hits:
            assert hit["line_number"] == 2
            assert hit["line_text"] == "SearchNeedle42 示例"
            assert hit["context"] == "before\nSearchNeedle42 示例\nafter"
            opened = client.get(f"/api/v1/projects/{project_id}/files/{hit['source_id']}")
            assert opened.status_code == 200, opened.text
            assert opened.json()["content"] == "before\nSearchNeedle42 示例\nafter\n"


def test_general_search_refreshes_modified_renamed_and_deleted_files(tmp_path: Path):
    root = tmp_path / "ordinary-project"
    root.mkdir()
    file = root / "config.json"
    file.write_bytes(b'{"marker": "OriginalNeedle"}\n')
    with _client(tmp_path, root) as client:
        project_id = client.post("/api/v1/projects/inspect", json={"root_path": str(root)}).json()["project"]["project_id"]
        url = f"/api/v1/projects/{project_id}/search"
        assert client.post(url, json={"query": "OriginalNeedle"}).json()["hit_count"] == 1
        renamed = root / "renamed.custom"
        file.rename(renamed)
        renamed.write_bytes(b'{"marker": "NewNeedle"}\n')
        assert client.post(url, json={"query": "OriginalNeedle"}).json()["hit_count"] == 0
        changed = client.post(url, json={"query": "NewNeedle"}).json()
        assert changed["hits"][0]["relative_path"] == "renamed.custom"
        renamed.unlink()
        assert client.post(url, json={"query": "NewNeedle"}).json()["hit_count"] == 0


def test_filename_only_match_has_no_invented_line_and_unicode_casefold_has_real_line(tmp_path: Path):
    root = tmp_path / "ordinary-project"
    root.mkdir()
    (root / "FilenameNeedle.py").write_bytes("Straße\r\n第二行 TokenNeedle\r\n".encode("utf-8"))
    with _client(tmp_path, root) as client:
        project_id = client.post("/api/v1/projects/inspect", json={"root_path": str(root)}).json()["project"]["project_id"]
        url = f"/api/v1/projects/{project_id}/search"
        file_hit = client.post(url, json={"query": "FilenameNeedle"}).json()["hits"][0]
        assert file_hit["line_number"] is None
        assert file_hit["line_text"] is None
        content_hit = client.post(url, json={"query": "TokenNeedle"}).json()["hits"][0]
        assert content_hit["line_number"] == 2
        assert content_hit["line_text"] == "第二行 TokenNeedle"


def test_general_search_excludes_binary_contents_even_with_text_extension(tmp_path: Path):
    root = tmp_path / "ordinary-project"
    root.mkdir()
    (root / "misleading.md").write_bytes(b"BinaryNeedle\x00more bytes")
    (root / "invalid.py").write_bytes(b"BinaryNeedle\xff\xfe\xfa")
    with _client(tmp_path, root) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(root)}).json()
        project_id = inspected["project"]["project_id"]
        response = client.post(f"/api/v1/projects/{project_id}/search", json={"query": "BinaryNeedle"})
        assert response.status_code == 200, response.text
        assert response.json()["hit_count"] == 0
        for file in inspected["files"]:
            opened = client.get(f"/api/v1/projects/{project_id}/files/{file['file_id']}")
            assert opened.status_code == 422, opened.text


class SearchProvider(ApiProvider):
    def __init__(self):
        self.result = None

    async def stream_chat(self, messages, *, cancel_event, tools=()):
        result = next((part for part in messages if part.get("role") == "tool"), None)
        if result is None:
            yield ProviderToolCall(0, "general-search-0001", "project_search", {"query": "API_KEY", "source_types": ["project_file"]})
            yield ProviderCompleted("tool_calls", None)
        else:
            receipt = json.loads(result["content"])
            assert receipt["status"] == "succeeded", receipt
            self.result = receipt["output"]
            yield ProviderTextDelta("找到了配置文件与所在行。")
            yield ProviderCompleted("stop", None)


def test_general_agent_search_returns_code_locations_and_masks_fixture_secret(tmp_path: Path):
    root = tmp_path / "ordinary-project"
    root.mkdir()
    fake_secret = "sk-" + "FictionalSearchToken1234567890"
    (root / "settings.json").write_bytes(f'{{\n  "API_KEY": "{fake_secret}"\n}}\n'.encode("utf-8"))
    provider = SearchProvider()
    settings = RuntimeSettings(environment="test", database_path=tmp_path / "runtime.sqlite3", trusted_project_roots=(root,), allowed_origins=())
    with TestClient(create_app(settings, agent_provider=provider)) as client:
        inspected = client.post("/api/v1/projects/inspect", json={"root_path": str(root)}).json()
        project = inspected["project"]
        sid = client.post(f"/api/v1/projects/{project['project_id']}/agent/sessions", json={"title": "配置检查"}).json()["session_id"]
        started = client.post(f"/api/v1/agent/sessions/{sid}/runs", headers={"Idempotency-Key": "general-project-search-1"}, json={
            "message": "查找配置项 API_KEY 所在的文件和行号。",
            "expected_project_revision": project["revision"], "expected_project_fingerprint": project["fingerprint"],
        })
        assert started.status_code == 202, started.text
        terminal = wait_for_success(client, started.json()["run"]["run_id"])
        assert terminal["status"] == "succeeded", terminal
        assert provider.result["hit_count"] == 1
        hit = provider.result["hits"][0]
        assert hit["relative_path"] == "settings.json"
        assert hit["line_number"] == 2
        assert "API_KEY" in hit["line_text"]
        assert fake_secret not in json.dumps(provider.result)
        assert "7890" in hit["snippet"]
        assert fake_secret in (root / "settings.json").read_text(encoding="utf-8")


def test_general_search_contracts_match_models():
    contracts = Path(__file__).resolve().parents[3] / "packages/contracts"
    for filename, model in [("project-search-hit.schema.json", SearchHit), ("project-search-query-response.schema.json", SearchQueryResponse)]:
        schema = json.loads((contracts / filename).read_text(encoding="utf-8"))
        schema.pop("$schema")
        schema.pop("$id")
        assert schema == model.model_json_schema(by_alias=True)


def test_unicode_bom_files_search_and_open_without_changing_original_bytes(tmp_path: Path):
    from zhimeng_runtime.text_encoding import decode_project_text

    root = tmp_path / "ordinary-project"
    root.mkdir()
    content = "before\r\nUnicodeNeedle 中文\r\nafter\r\n"
    original = {}
    for codec, bom in [("utf-8", b""), ("utf-8-sig", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff"), ("utf-32-le", b"\xff\xfe\x00\x00"), ("utf-32-be", b"\x00\x00\xfe\xff")]:
        name = f"{codec}.custom"
        data = bom + content.encode(codec)
        original[name] = data
        (root / name).write_bytes(data)
        text, encoding, marker = decode_project_text(data)
        assert text == content
        assert marker + text.encode(encoding) == data
    with _client(tmp_path, root) as client:
        project_id = client.post("/api/v1/projects/inspect", json={"root_path": str(root)}).json()["project"]["project_id"]
        found = client.post(f"/api/v1/projects/{project_id}/search", json={"query": "UnicodeNeedle"}).json()["hits"]
        assert {item["relative_path"] for item in found} == set(original)
        for hit in found:
            assert hit["line_number"] == 2
            opened = client.get(f"/api/v1/projects/{project_id}/files/{hit['source_id']}")
            assert opened.status_code == 200, opened.text
            assert opened.json()["content"] == content
            assert (root / hit["relative_path"]).read_bytes() == original[hit["relative_path"]]
