from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/writing-runtime/src'))
from zhimeng_runtime.agent_tools import MemoryWriteArguments
from zhimeng_runtime.memory_updates import MemoryExtraction
from zhimeng_runtime.memory_changes_api import MemoryChangesRequest, MemoryChangesResponse

MODELS = {
    'memory-write-arguments': MemoryWriteArguments,
    'memory-extraction': MemoryExtraction,
    'memory-changes-request': MemoryChangesRequest,
    'memory-changes-response': MemoryChangesResponse,
}

if __name__ == '__main__':
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        schema['$id'] = f'https://zhimeng.local/contracts/{name}.schema.json'
        target = ROOT / 'packages/contracts' / f'{name}.schema.json'
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes((json.dumps(schema, ensure_ascii=False, indent=2) + '\n').encode())
        temporary.replace(target)
