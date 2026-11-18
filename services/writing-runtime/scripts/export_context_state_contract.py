from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/writing-runtime/src'))
from zhimeng_runtime.context_state_api import SessionContextState

if __name__ == '__main__':
    schema = SessionContextState.model_json_schema()
    schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
    schema['$id'] = 'https://zhimeng.local/contracts/session-context-state.schema.json'
    (ROOT / 'packages/contracts/session-context-state.schema.json').write_bytes((json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)+'\n').encode('utf-8'))
