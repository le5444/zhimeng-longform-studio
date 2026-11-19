"""Export local-file preview and reveal contracts without rewriting unchanged files."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'services/writing-runtime/src'))
from zhimeng_runtime.local_file_api import LocalFilePreview, RevealLocalFileRequest, RevealLocalFileResponse

if __name__ == '__main__':
    for name, model in {
        'local-file-preview.schema.json': LocalFilePreview,
        'local-file-reveal-request.schema.json': RevealLocalFileRequest,
        'local-file-reveal-response.schema.json': RevealLocalFileResponse,
    }.items():
        schema = model.model_json_schema()
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        schema['$id'] = f'https://zhimeng.local/contracts/{name}'
        target = ROOT / 'packages/contracts' / name
        if target.exists() and json.loads(target.read_bytes()) == schema:
            continue
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes((json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8'))
        temporary.replace(target)
