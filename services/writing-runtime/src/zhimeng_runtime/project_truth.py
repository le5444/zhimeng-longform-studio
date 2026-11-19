from __future__ import annotations


NON_TRUTH_PATH_MARKERS = ("勿作为真值", "嵌套项目副本")


def is_non_truth_project_path(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    return any(marker in normalized for marker in NON_TRUTH_PATH_MARKERS)


__all__ = ["NON_TRUTH_PATH_MARKERS", "is_non_truth_project_path"]
