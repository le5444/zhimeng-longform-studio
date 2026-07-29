"""Minimal local HTTP Gateway for the novel writing system (B-line skeleton).

This is the first end-to-end runnable surface of the new project. It exposes the
already-migrated ``writing_runtime`` actions over a small JSON HTTP endpoint so a
front end (or curl) can bind a novel project read-only and inspect its state.

Design constraints carried over from the A-line lessons (see docs/08):
- Python standard library only. No third-party dependencies, so the EXE stays clean.
- Single small file with clear boundaries. No 10k-line god-Gateway.
- Trusted writing roots must be configured explicitly; no full filesystem access.
- Every write/preview action already runs through writing_runtime's own guards.

Run:
    python bridge/gateway.py --writing-root "C:/path/to/写小说工作区" --port 8765

Then POST JSON to http://127.0.0.1:8765/tool :
    {"action": "writing_runtime_status"}
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

# Make sure "bridge" package imports resolve whether launched from repo root or
# from inside bridge/. The migrated novel_runtime uses a try/except fallback so
# both "novel_runtime.*" and "bridge.novel_runtime.*" import styles work.
_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for _candidate in (_REPO_ROOT, _THIS_DIR):
    _s = str(_candidate)
    if _s not in sys.path:
        sys.path.insert(0, _s)

try:
    from bridge.writing_runtime import (
        WRITING_ACTIONS,
        configure_writing_roots,
        dispatch_writing_action,
        trusted_writing_roots,
    )
except ModuleNotFoundError:
    from writing_runtime import (  # type: ignore[no-redef]
        WRITING_ACTIONS,
        configure_writing_roots,
        dispatch_writing_action,
        trusted_writing_roots,
    )


# Bridge root: where the runtime SQLite + .codex-runtime live. Kept inside the
# repo so the whole workspace is self-contained and easy to reset.
BRIDGE_ROOT = _REPO_ROOT
WEB_ROOT = _REPO_ROOT / "web" / "workbench"
RUNTIME_WRITING = _REPO_ROOT / ".codex-runtime" / "writing"


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".udiff": "text/plain; charset=utf-8",
    }.get(suffix, "application/octet-stream")


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ZhimengNovelGateway/0.1"

    def _write(self, status: int, payload: Dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Local-only tool; allow a local front end to call it during development.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _write_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _serve_workbench(self, rel: str) -> None:
        rel = rel.lstrip("/")
        if rel in ("", "workbench", "workbench/"):
            rel = "index.html"
        if rel.startswith("workbench/"):
            rel = rel[len("workbench/") :]
        candidate = (WEB_ROOT / rel).resolve()
        try:
            candidate.relative_to(WEB_ROOT.resolve())
        except Exception:
            self._write(403, {"ok": False, "error": "forbidden_path"})
            return
        if not candidate.exists() or not candidate.is_file():
            self._write(404, {"ok": False, "error": "not_found", "path": rel})
            return
        self._write_bytes(200, candidate.read_bytes(), _content_type(candidate))

    def _serve_staging_api(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        # /api/staging
        if parts == ["api", "staging"]:
            staging_root = RUNTIME_WRITING / "staging"
            runs = []
            if staging_root.is_dir():
                for p in sorted(staging_root.iterdir(), key=lambda x: x.name, reverse=True):
                    if not p.is_dir():
                        continue
                    files = sorted([f.name for f in p.iterdir() if f.is_file()])
                    runs.append({"id": p.name, "files": files})
            self._write(200, {"ok": True, "runs": runs[:100]})
            return

        # /api/staging/<run_id>
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "staging":
            run_id = unquote(parts[2])
            run_dir = (RUNTIME_WRITING / "staging" / run_id).resolve()
            try:
                run_dir.relative_to((RUNTIME_WRITING / "staging").resolve())
            except Exception:
                self._write(403, {"ok": False, "error": "forbidden_path"})
                return
            if not run_dir.is_dir():
                self._write(404, {"ok": False, "error": "run_not_found", "run_id": run_id})
                return
            files = sorted([f.name for f in run_dir.iterdir() if f.is_file()])
            self._write(200, {"ok": True, "run_id": run_id, "files": files, "path": str(run_dir)})
            return

        # /api/staging/<run_id>/file?name=
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "staging" and parts[3] == "file":
            run_id = unquote(parts[2])
            qs = parse_qs(parsed.query or "")
            name = unquote((qs.get("name") or [""])[0])
            if not name or "/" in name or "\\" in name or ".." in name:
                self._write(400, {"ok": False, "error": "invalid_name"})
                return
            run_dir = (RUNTIME_WRITING / "staging" / run_id).resolve()
            try:
                run_dir.relative_to((RUNTIME_WRITING / "staging").resolve())
            except Exception:
                self._write(403, {"ok": False, "error": "forbidden_path"})
                return
            file_path = (run_dir / name).resolve()
            try:
                file_path.relative_to(run_dir)
            except Exception:
                self._write(403, {"ok": False, "error": "forbidden_file"})
                return
            if not file_path.exists() or not file_path.is_file():
                self._write(404, {"ok": False, "error": "file_not_found", "name": name})
                return
            self._write_bytes(200, file_path.read_bytes(), _content_type(file_path))
            return

        self._write(404, {"ok": False, "error": "not_found", "path": self.path})

    def _serve_source_api(self) -> None:
        """Read-only access to files under trusted writing roots."""
        from urllib.parse import parse_qs, unquote, urlparse

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = parse_qs(parsed.query or "")
        raw_path = unquote((qs.get("path") or [""])[0]).strip()
        if parts != ["api", "source"] or not raw_path:
            self._write(404, {"ok": False, "error": "not_found", "path": self.path})
            return
        try:
            target = Path(raw_path).expanduser().resolve()
        except Exception as error:  # noqa: BLE001
            self._write(400, {"ok": False, "error": "invalid_path", "detail": str(error)})
            return

        roots = [Path(p).resolve() for p in trusted_writing_roots()]
        if not roots:
            self._write(400, {"ok": False, "error": "no_trusted_roots"})
            return
        allowed = False
        for root in roots:
            try:
                target.relative_to(root)
                allowed = True
                break
            except Exception:
                continue
        if not allowed:
            self._write(403, {"ok": False, "error": "path_not_in_trusted_roots", "path": str(target)})
            return
        if not target.exists() or not target.is_file():
            self._write(404, {"ok": False, "error": "file_not_found", "path": str(target)})
            return
        # size guard: 2MB
        size = target.stat().st_size
        if size > 2 * 1024 * 1024:
            self._write(413, {"ok": False, "error": "file_too_large", "bytes": size})
            return
        data = target.read_bytes()
        # Prefer text responses for markdown manuscripts.
        self._write_bytes(200, data, _content_type(target))

    def _serve_chapters_api(self) -> None:
        from urllib.parse import parse_qs, unquote, urlparse

        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if parts != ["api", "chapters"]:
            self._write(404, {"ok": False, "error": "not_found", "path": self.path})
            return
        qs = parse_qs(parsed.query or "")
        raw_root = unquote((qs.get("root") or [""])[0]).strip()
        if not raw_root:
            self._write(400, {"ok": False, "error": "root_required"})
            return
        try:
            project_root = Path(raw_root).expanduser().resolve()
        except Exception as error:  # noqa: BLE001
            self._write(400, {"ok": False, "error": "invalid_root", "detail": str(error)})
            return
        roots = [Path(p).resolve() for p in trusted_writing_roots()]
        allowed = False
        for root in roots:
            try:
                project_root.relative_to(root)
                allowed = True
                break
            except Exception:
                continue
        if not allowed:
            self._write(403, {"ok": False, "error": "root_not_trusted", "path": str(project_root)})
            return
        if not project_root.is_dir():
            self._write(404, {"ok": False, "error": "root_not_found", "path": str(project_root)})
            return
        try:
            try:
                from novel_runtime.post_writeback import scan_chapter_tree
            except ModuleNotFoundError:
                from bridge.novel_runtime.post_writeback import scan_chapter_tree
            tree = scan_chapter_tree(project_root)
        except Exception as error:  # noqa: BLE001
            self._write(500, {"ok": False, "error": "chapter_scan_failed", "detail": str(error)})
            return
        self._write(200, {"ok": True, **tree})

    def do_OPTIONS(self) -> None:  # noqa: N802 (http.server naming)
        self._write(204, {})

    def do_GET(self) -> None:  # noqa: N802
        path_only = self.path.split("?", 1)[0]
        if path_only.rstrip("/") in ("/health", "/healthz"):
            self._write(
                200,
                {
                    "ok": True,
                    "service": "zhimeng-novel-gateway",
                    "version": "0.2",
                    "trusted_roots": [str(p) for p in trusted_writing_roots()],
                    "actions": sorted(WRITING_ACTIONS),
                    "workbench": "/workbench/",
                },
            )
            return
        if path_only == "/" or path_only.startswith("/workbench"):
            self._serve_workbench(path_only)
            return
        if path_only.startswith("/api/staging"):
            self._serve_staging_api()
            return
        if path_only.startswith("/api/source"):
            self._serve_source_api()
            return
        if path_only.startswith("/api/chapters"):
            self._serve_chapters_api()
            return
        self._write(404, {"ok": False, "error": "not_found", "path": path_only})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/tool":
            self._write(404, {"ok": False, "error": "not_found", "path": self.path})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            request = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            self._write(400, {"ok": False, "error": "invalid_json", "detail": str(error)})
            return
        if not isinstance(request, dict):
            self._write(400, {"ok": False, "error": "request_must_be_object"})
            return

        action = str(request.get("action") or "").strip()
        payload = request.get("payload")
        if not isinstance(payload, dict):
            payload = {k: v for k, v in request.items() if k != "action"}
        if action not in WRITING_ACTIONS:
            self._write(
                400,
                {
                    "ok": False,
                    "error": "unsupported_action",
                    "action": action,
                    "supported": sorted(WRITING_ACTIONS),
                },
            )
            return

        try:
            result = dispatch_writing_action(action, payload, BRIDGE_ROOT)
        except (ValueError, KeyError) as error:
            self._write(400, {"ok": False, "error": "bad_request", "action": action, "detail": str(error)})
            return
        except Exception as error:  # keep the dev Gateway alive on unexpected errors
            self._write(500, {"ok": False, "error": "internal_error", "action": action, "detail": str(error)})
            return
        self._write(200, {"ok": True, "action": action, "result": result})

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter dev logs
        sys.stderr.write("[gateway] " + (fmt % args) + "\n")


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), GatewayHandler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Minimal novel writing Gateway")
    parser.add_argument(
        "--writing-root",
        action="append",
        default=[],
        metavar="PATH",
        help="A trusted novel project root. Repeatable. Required to bind projects.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default 8765)")
    args = parser.parse_args(argv)

    config = configure_writing_roots(args.writing_root)
    print(f"[gateway] trusted roots: {config['roots']}", file=sys.stderr)
    if config["rejected"]:
        print(f"[gateway] rejected (not a dir): {config['rejected']}", file=sys.stderr)
    if not config["roots"]:
        print(
            "[gateway] WARNING: no trusted writing roots. "
            "Project binding will fail until you pass --writing-root PATH.",
            file=sys.stderr,
        )

    server = build_server(args.host, args.port)
    print(f"[gateway] listening on http://{args.host}:{args.port}", file=sys.stderr)
    print(f"[gateway] health:  GET  http://{args.host}:{args.port}/health", file=sys.stderr)
    print(f"[gateway] tool:    POST http://{args.host}:{args.port}/tool", file=sys.stderr)
    print(f"[gateway] ui:      GET  http://{args.host}:{args.port}/workbench/", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gateway] shutting down", file=sys.stderr)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
