"""Persisted backup formats and the repaired Gate response stay in sync."""
from __future__ import annotations

import json
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[3]
EXPORT = runpy.run_path(str(ROOT / 'services/writing-runtime/scripts/export_backup_contracts.py'))


@pytest.mark.parametrize('file_name,model', EXPORT['MODELS'].items())
def test_backup_contract_matches_runtime(file_name, model):
    expected = json.loads(EXPORT['render_schema'](file_name, model))
    saved = json.loads((ROOT / 'packages/contracts' / file_name).read_text(encoding='utf-8'))
    assert saved == expected


def test_gate_pipeline_response_contract_matches_current_runtime():
    export = runpy.run_path(str(ROOT / 'services/writing-runtime/scripts/export_change_set_contracts.py'))
    file_name = 'gate-pipeline-response.schema.json'
    expected = json.loads(export['render_schema'](file_name, export['MODELS'][file_name]))
    saved = json.loads((ROOT / 'packages/contracts' / file_name).read_text(encoding='utf-8'))
    assert saved == expected
