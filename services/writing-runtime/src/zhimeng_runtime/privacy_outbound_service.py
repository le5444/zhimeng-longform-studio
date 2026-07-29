"""Local-first privacy and outbound transparency receipts for Local v1."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


POLICY_VERSION = "local-v1-privacy-outbound-1"


class PrivacyOutboundError(RuntimeError):
    code = "privacy_outbound_error"
    retryable = False


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        return None
    return parsed.hostname.lower()


class PrivacyOutboundService:
    """Explain configured outbound targets and offline-capable local operations.

    This does not perform network capture. It freezes product disclosure receipts
    so authors can see where data would go before remote calls.
    """

    def __init__(
        self,
        *,
        state_root: Path,
        provider_base_url: str | None = None,
        provider_model: str | None = None,
        telemetry_enabled: bool = False,
    ) -> None:
        self.state_root = Path(state_root)
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.provider_base_url = provider_base_url
        self.provider_model = provider_model
        self.telemetry_enabled = bool(telemetry_enabled)

    def disclosure(self) -> dict[str, Any]:
        host = _host_from_url(self.provider_base_url)
        targets: list[dict[str, Any]] = []
        if host is not None:
            targets.append(
                {
                    "target_id": "author_configured_provider",
                    "kind": "model_provider",
                    "host": host,
                    "scheme": urlparse(self.provider_base_url or "").scheme.lower() or None,
                    "model": self.provider_model,
                    "enabled_by_author": True,
                    "data_categories": [
                        "prompt_context",
                        "tool_arguments",
                        "model_usage_metadata",
                    ],
                    "includes_novel_body_by_default": False,
                    "includes_secrets": False,
                }
            )
        offline_ops = [
            {"operation": "open_project", "offline_capable": True},
            {"operation": "edit_local_files", "offline_capable": True},
            {"operation": "review_change_set", "offline_capable": True},
            {"operation": "apply_or_rollback_change_set", "offline_capable": True},
            {"operation": "runtime_backup_restore", "offline_capable": True},
            {"operation": "portable_install_upgrade", "offline_capable": True},
            {"operation": "remote_model_call", "offline_capable": False},
            {"operation": "external_source_download", "offline_capable": False},
        ]
        generated_at = _utcnow()
        material = {
            "schema": "zhimeng.privacy-outbound-disclosure.v1",
            "local_first": True,
            "telemetry_default_off": True,
            "telemetry_enabled": self.telemetry_enabled,
            "outbound_targets": targets,
            "outbound_target_count": len(targets),
            "offline_operations": offline_ops,
            "offline_capable_core_ops": True,
            "requires_author_enabled_target_for_remote": True,
            "formal_draft_allowed": False,
            "quality_assessed": False,
            "policy_version": POLICY_VERSION,
            "generated_at": generated_at.isoformat(),
        }
        receipt_id = "privacy-outbound-" + _hash(material)[:32]
        material["receipt_id"] = receipt_id
        material["receipt_hash"] = _hash(material)
        path = self.state_root / "privacy-outbound-disclosure.json"
        path.write_text(
            json.dumps(material, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "schema": "zhimeng.privacy-outbound-disclosure-response.v1",
            "receipt": material,
            "not_claiming": [
                "packet_capture_proof",
                "desktop_network_firewall",
                "formal_draft_authorization_unlock",
                "local_v1_exe",
            ],
        }


__all__ = ["PrivacyOutboundError", "PrivacyOutboundService"]
