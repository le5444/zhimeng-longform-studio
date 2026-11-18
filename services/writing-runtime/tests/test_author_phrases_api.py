"""按钮用语接口：作者自己改按钮说什么的那份 Markdown。

两条红线：
- 文件不存在 / 读不出来时 GET 照常 200，`exists=false`，前端退回内置那套；
- 这份文件跟提示词彻底无关（所以单开接口，不做成技能），读写都不能碰别的东西。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from zhimeng_runtime.author_phrases_api import (
    FILE_NAME,
    MAX_PHRASES_BYTES,
    create_author_phrases_router,
)


@pytest.fixture()
def client(tmp_path):
    app = FastAPI()
    app.include_router(create_author_phrases_router(tmp_path))
    return TestClient(app), tmp_path


def test_missing_file_reports_exists_false(client):
    http, root = client
    response = http.get("/api/v1/author-phrases")
    assert response.status_code == 200
    body = response.json()
    assert body["schema"] == "zhimeng.author-phrases.v1"
    assert body["exists"] is False
    assert body["content"] == ""
    assert body["path"].endswith(FILE_NAME)
    # 没保存之前不该把文件建出来
    assert not (root / FILE_NAME).exists()


def test_put_then_get_round_trips_author_markdown(client):
    http, root = client
    markdown = "# 按钮用语\n\n## story\n先陪我聊三个方向，别动笔。\n"
    saved = http.put("/api/v1/author-phrases", json={"content": markdown})
    assert saved.status_code == 200
    assert saved.json()["ok"] is True

    loaded = http.get("/api/v1/author-phrases").json()
    assert loaded["exists"] is True
    assert loaded["content"] == markdown
    # 落盘的是 UTF-8，作者用记事本打开也不会乱码
    assert (root / FILE_NAME).read_text(encoding="utf-8") == markdown


def test_oversized_content_is_rejected(client):
    http, _ = client
    response = http.put(
        "/api/v1/author-phrases",
        json={"content": "长" * (MAX_PHRASES_BYTES + 1)},
    )
    assert response.status_code == 422
