"""Minimal project domain asset index for Local v1 FR-ASSET progress.

Indexes character/plot/foreshadowing markdown files under standard project
sections into stable asset records, plus a minimal mention/wiki-link relation
edge list. This is not a full relationship graph and does not claim
timeline/status tracking completeness.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from zhimeng_runtime.state_roots import ensure_state_root
from zhimeng_runtime.project_truth import is_non_truth_project_path
from zhimeng_runtime.project_reader import classify_project_file_kind, prose_directories
from typing import Any, Literal


AssetKind = Literal["character", "plot", "foreshadowing", "timeline", "other"]
WIKI_LINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")


class ProjectAssetError(RuntimeError):
    code = "project_asset_error"
    retryable = False


class ProjectAssetNotFound(ProjectAssetError):
    code = "project_asset_not_found"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _kind_for_path(relative_path: str) -> AssetKind:
    lowered = relative_path.replace("\\", "/").lower()
    if "03_人物" in relative_path or "character" in lowered:
        return "character"
    if "04_剧情" in relative_path or "plot" in lowered or "大纲" in relative_path:
        return "plot"
    if "伏笔" in relative_path or "foreshadow" in lowered:
        return "foreshadowing"
    if "时间线" in relative_path or "timeline" in lowered:
        return "timeline"
    return "other"


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().lower())




def _extract_status(text: str) -> dict[str, object]:
    """Heuristic status markers; never claims full status tracking completeness."""
    status = None
    for pattern in (
        r"(?im)^(?:状态|status)\s*[:：]\s*(.+)$",
        r"(?im)^-\s*(?:状态|status)\s*[:：]\s*(.+)$",
    ):
        match = re.search(pattern, text)
        if match:
            status = match.group(1).strip()[:80]
            break
    tracked = bool(status)
    return {
        "status_tracked": tracked,
        "status_label": status,
    }


def _extract_issue_cards(
    assets: list[dict[str, Any]],
    texts: dict[str, str],
) -> list[dict[str, Any]]:
    """Minimal domain issue cards from missing wiki targets / untracked status."""
    titles = {_normalize_title(str(item["title"])) for item in assets}
    issues: list[dict[str, Any]] = []
    for source in assets:
        text = texts.get(str(source["asset_id"]), "")
        for match in WIKI_LINK_RE.finditer(text):
            target = match.group(1).strip()
            if _normalize_title(target) not in titles:
                issues.append(
                    {
                        "issue_id": "asset-issue-"
                        + _sha256_text(f"missing_ref:{source['asset_id']}:{target}")[:24],
                        "kind": "missing_reference",
                        "severity": "warning",
                        "asset_id": source["asset_id"],
                        "title": f"缺失引用：{target}",
                        "detail": (
                            f"{source['relative_path']} 引用 [[{target}]]，"
                            "但资产索引中不存在对应标题"
                        ),
                        "resolved": False,
                    }
                )
        if not source.get("status_tracked"):
            issues.append(
                {
                    "issue_id": "asset-issue-"
                    + _sha256_text(f"status_untracked:{source['asset_id']}")[:24],
                    "kind": "status_untracked",
                    "severity": "info",
                    "asset_id": source["asset_id"],
                    "title": f"状态未标注：{source['title']}",
                    "detail": f"{source['relative_path']} 未提供状态标记",
                    "resolved": False,
                }
            )
    dedup: dict[str, dict[str, Any]] = {}
    for item in issues:
        dedup[str(item["issue_id"])] = item
    return list(dedup.values())


def _extract_relationships(
    assets: list[dict[str, Any]],
    texts: dict[str, str],
) -> list[dict[str, Any]]:
    by_title: dict[str, list[dict[str, Any]]] = {}
    for item in assets:
        key = _normalize_title(str(item["title"]))
        if not key:
            continue
        by_title.setdefault(key, []).append(item)

    relationships: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in assets:
        text = texts.get(str(source["asset_id"]), "")
        targets: list[tuple[str, str]] = []
        for match in WIKI_LINK_RE.finditer(text):
            targets.append((match.group(1).strip(), "wiki_link"))
        source_key = _normalize_title(str(source["title"]))
        for title_key, candidates in by_title.items():
            if not title_key or len(title_key) < 2:
                continue
            if title_key == source_key:
                continue
            display = str(candidates[0]["title"])
            if display and display in text:
                targets.append((display, "title_mention"))
        for target_title, rel_type in targets:
            matches = by_title.get(_normalize_title(target_title), [])
            for target in matches:
                if target["asset_id"] == source["asset_id"]:
                    continue
                key = (str(source["asset_id"]), str(target["asset_id"]), rel_type)
                if key in seen:
                    continue
                seen.add(key)
                relationships.append(
                    {
                        "relationship_id": "asset-rel-"
                        + _sha256_text(
                            f"{source['asset_id']}:{target['asset_id']}:{rel_type}"
                        )[:24],
                        "from_asset_id": source["asset_id"],
                        "to_asset_id": target["asset_id"],
                        "relation_type": rel_type,
                        "confidence": "heuristic",
                        "complete": False,
                    }
                )
    return relationships


class ProjectAssetService:
    def __init__(self, *, state_root: Path) -> None:
        self.state_root = ensure_state_root(Path(state_root))

    def rebuild(self, *, project_id: str, project_root: Path) -> dict[str, Any]:
        project_root = Path(project_root)
        if not project_root.is_dir():
            raise ProjectAssetNotFound(f"project root missing: {project_root}")
        assets: list[dict[str, Any]] = []
        texts: dict[str, str] = {}
        # 正文不进资产索引：按这本书实际的正文夹（05_正文内容 / 正文 / …）和文件分类判，
        # 不再写死一个夹子名。
        prose_prefixes = tuple(
            folder.relative_to(project_root).as_posix() + "/"
            for folder in prose_directories(project_root)
            if folder != project_root
        )
        for path in sorted(project_root.rglob("*.md")):
            relative = path.relative_to(project_root).as_posix()
            if (
                relative.startswith(prose_prefixes)
                or is_non_truth_project_path(relative)
                or classify_project_file_kind(relative) == "prose"
            ):
                continue
            # 不再按 01–09 编号夹白名单挑文件：不在正文夹、不在排除夹的 .md 都索引，
            # 作者随便怎么摆夹子（设定/、大纲/、素材/…）都找得到。
            kind = _kind_for_path(relative)
            text = path.read_text(encoding="utf-8", errors="replace")
            title = path.stem
            first_heading = re.search(r"^#\s+(.+)$", text, flags=re.M)
            if first_heading:
                title = first_heading.group(1).strip() or title
            content_hash = _sha256_text(text)
            asset_id = "asset-" + _sha256_text(f"{project_id}:{relative}:{content_hash}")[:24]
            status_info = _extract_status(text)
            asset = {
                "asset_id": asset_id,
                "kind": kind,
                "title": title[:200],
                "relative_path": relative,
                "content_hash": content_hash,
                "byte_size": path.stat().st_size,
                "relationship_count": 0,
                "status_tracked": bool(status_info["status_tracked"]),
                "status_label": status_info["status_label"],
            }
            assets.append(asset)
            texts[asset_id] = text

        relationships = _extract_relationships(assets, texts)
        status_tracked_count = sum(1 for item in assets if item.get("status_tracked"))
        issue_cards = _extract_issue_cards(assets, texts)
        counts: dict[str, int] = {}
        for edge in relationships:
            counts[str(edge["from_asset_id"])] = counts.get(str(edge["from_asset_id"]), 0) + 1
            counts[str(edge["to_asset_id"])] = counts.get(str(edge["to_asset_id"]), 0) + 1
        for item in assets:
            item["relationship_count"] = counts.get(str(item["asset_id"]), 0)

        generated_at = _utcnow()
        material = {
            "schema": "zhimeng.project-asset-index.v1",
            "project_id": project_id,
            "project_root": str(project_root),
            "asset_count": len(assets),
            "assets": assets,
            "relationship_count": len(relationships),
            "relationships": relationships,
            "status_tracked_count": status_tracked_count,
            "issue_count": len(issue_cards),
            "issue_cards": issue_cards,
            "domain_issue_cards_complete": False,
            "relationship_graph_complete": False,
            "status_tracking_complete": False,
            "formal_draft_allowed": False,
            "policy_version": "local-v1-project-assets-5",
            "generated_at": generated_at.isoformat(),
        }
        material["index_id"] = "project-asset-index-" + _sha256_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "assets": [
                        {
                            "asset_id": item["asset_id"],
                            "content_hash": item["content_hash"],
                            "relative_path": item["relative_path"],
                            "relationship_count": item["relationship_count"],
                        }
                        for item in assets
                    ],
                    "relationships": [
                        {
                            "relationship_id": item["relationship_id"],
                            "from_asset_id": item["from_asset_id"],
                            "to_asset_id": item["to_asset_id"],
                            "relation_type": item["relation_type"],
                        }
                        for item in relationships
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )[:32]
        material["index_hash"] = _sha256_text(
            json.dumps(material, ensure_ascii=False, sort_keys=True)
        )
        path = self.state_root / project_id / "asset-index.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "schema": "zhimeng.project-asset-index-response.v1",
            "index": material,
            "not_claiming": [
                "relationship_graph_complete",
                "status_tracking_complete",
                "timeline_engine",
                "domain_issue_cards_complete",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }

    def get(self, project_id: str) -> dict[str, Any]:
        path = self.state_root / project_id / "asset-index.json"
        if not path.is_file():
            raise ProjectAssetNotFound("project asset index was not found")
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            "schema": "zhimeng.project-asset-index-response.v1",
            "index": payload,
            "not_claiming": [
                "relationship_graph_complete",
                "status_tracking_complete",
                "timeline_engine",
                "domain_issue_cards_complete",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }


__all__ = ["ProjectAssetError", "ProjectAssetNotFound", "ProjectAssetService"]
