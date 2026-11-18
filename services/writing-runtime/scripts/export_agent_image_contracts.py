from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services/writing-runtime/src"))
from zhimeng_runtime.agent_images import AgentImage, AgentImageUpload
from zhimeng_runtime.agent_models import AgentRunCreateRequest, AgentMessageListResponse, AgentRunAdmissionWithdrawResponse
from zhimeng_runtime.agent_queue import QueueInput, QueueUpdate, QueueState, QueueDispatchResponse, QueueSteerResponse


MODELS = {
    'agent-image.schema.json': AgentImage,
    'agent-image-upload.schema.json': AgentImageUpload,
    'agent-run-create-request.schema.json': AgentRunCreateRequest,
    'agent-message-list.schema.json': AgentMessageListResponse,
    'agent-run-admission-withdraw.schema.json': AgentRunAdmissionWithdrawResponse,
    'agent-queue-input.schema.json': QueueInput,
    'agent-queue-update.schema.json': QueueUpdate,
    'agent-queue-state.schema.json': QueueState,
    'agent-queue-dispatch.schema.json': QueueDispatchResponse,
    'agent-queue-steer-response.schema.json': QueueSteerResponse,
}

if __name__ == '__main__':
    for name, model in MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        schema['$schema'] = 'https://json-schema.org/draft/2020-12/schema'
        schema['$id'] = f'https://zhimeng.local/contracts/{name}'
        target = ROOT / 'packages/contracts' / name
        if target.exists() and json.loads(target.read_text(encoding='utf-8')) == schema:
            continue
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes((json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True)+'\n').encode('utf-8'))
        temporary.replace(target)
