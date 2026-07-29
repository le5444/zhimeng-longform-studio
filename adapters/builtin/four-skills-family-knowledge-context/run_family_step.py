#!/usr/bin/env python3
import json
import sys
from pathlib import Path

request = json.load(sys.stdin)
if not isinstance(request, dict):
    print(json.dumps({"error": "invalid request"}, ensure_ascii=False))
    raise SystemExit(1)
family = str(request.get("family") or "")
expected = "knowledge_context"
if family != expected:
    print(json.dumps({
        "health": "blocked",
        "summary": f"family mismatch expected={expected} actual={family}",
        "result": {"family": family, "expected": expected},
        "artifacts": [],
    }, ensure_ascii=False))
    raise SystemExit(1)
artifact = {
    "schema": "zhimeng.four-skills-family-step-result.v1",
    "family": family,
    "batch_id": request.get("batch_id"),
    "probe_id": request.get("probe_id"),
    "executed": True,
    "writes_project_truth": False,
}
Path("family-step-result.json").write_text(
    json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps({
    "health": "healthy",
    "summary": f"four-skills family step executed for {family}",
    "result": artifact,
    "artifacts": [{
        "artifact_type": "four_skills_family_step",
        "relative_path": "family-step-result.json",
        "payload": artifact,
    }],
}, ensure_ascii=False, separators=(",", ":")))
