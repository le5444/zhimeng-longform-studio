"""按钮用语：界面上那些按钮点下去往输入框里填的整句话，作者可以自己改。

这些话原来硬写在前端 `lib/writingProcess.ts` 里，作者要改措辞就得改代码。
现在放进一份 Markdown，界面里能编辑，存在本机状态目录下。

**刻意不做成技能**：技能的名字和用途会进每一轮的技能目录，这份用语表放进去纯属噪音；
而停用的技能又读不出来。所以单开一对接口，跟提示词彻底无关。

格式很简单，一段一条，`## 标识` 下面写整句话：

    ## chapter
    按总纲写当前章的章纲……

读不出来、格式坏了、文件不存在，前端一律退回内置的那套，不会因为这份文件写坏了就用不了。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

MAX_PHRASES_BYTES = 128 * 1024
FILE_NAME = "按钮用语.md"


class SavePhrasesRequest(BaseModel):
    content: str = Field(max_length=MAX_PHRASES_BYTES)


def create_author_phrases_router(state_root: Path) -> APIRouter:
    router = APIRouter(prefix="/api/v1/author-phrases", tags=["author-phrases"])
    path = Path(state_root) / FILE_NAME

    def read() -> tuple[bool, str]:
        try:
            raw = path.read_bytes()
        except OSError:
            return False, ""
        if len(raw) > MAX_PHRASES_BYTES:
            raw = raw[:MAX_PHRASES_BYTES]
        return True, raw.decode("utf-8-sig", errors="replace")

    @router.get("")
    async def read_author_phrases() -> dict:
        exists, content = read()
        return {
            "schema": "zhimeng.author-phrases.v1",
            "path": str(path),
            "exists": exists,
            "content": content,
        }

    @router.put("")
    async def save_author_phrases(request: SavePhrasesRequest) -> dict:
        path.parent.mkdir(parents=True, exist_ok=True)
        # write_bytes 而不是 write_text：Windows 的文本写入会把 \n 翻成 \r\n，
        # 作者读到再存回来就变样了；这份文件按字节原样进出。
        path.write_bytes(request.content.encode("utf-8"))
        return {"schema": "zhimeng.author-phrases.v1", "path": str(path), "ok": True}

    return router
