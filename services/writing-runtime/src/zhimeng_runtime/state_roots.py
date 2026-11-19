"""Best-effort provisioning for runtime state directories.

Services constructed inside ``create_app`` must not crash application
assembly when ``database_path.parent`` is unusable: readiness reporting is
the single gate for that failure (see
``test_readiness_failure_uses_structured_error_envelope``). Directory
creation at construction time is therefore best-effort; in the healthy case
the directory exists afterwards, and in the corrupted case ``/health/ready``
answers 503 with a structured envelope while individual writes fail loudly.
"""

from __future__ import annotations

from pathlib import Path


def ensure_state_root(root: Path) -> Path:
    """Create ``root`` if possible and return it unchanged."""
    path = Path(root)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 状态根不可用不阻断应用装配；/health/ready 负责报告这个故障。
        pass
    return path
