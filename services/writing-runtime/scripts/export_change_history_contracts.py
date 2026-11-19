import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT/'services/writing-runtime/src'))
from zhimeng_runtime.change_history import ChangeHistoryDetail, ChangeHistoryPage

for name,model in [('change-history-page',ChangeHistoryPage),('change-history-detail',ChangeHistoryDetail)]:
    target=ROOT/'packages/contracts'/f'{name}.schema.json'
    schema=model.model_json_schema()
    schema.update({'$id':f'https://zhimeng.local/contracts/{name}.schema.json','$schema':'https://json-schema.org/draft/2020-12/schema'})
    if target.exists() and json.loads(target.read_bytes())==schema: continue
    temporary=target.with_suffix('.tmp')
    temporary.write_bytes((json.dumps(schema,ensure_ascii=False,indent=2,sort_keys=True)+'\n').encode('utf-8'))
    temporary.replace(target)
