"""当前运行的用户补充：只在模型/完整工具批边界消费，用户消息和往来记录一起提交。"""
from __future__ import annotations

from zhimeng_runtime.agent_images import claim_images

import json
from dataclasses import dataclass

from zhimeng_runtime.agent_store import AgentStore, _new_id, _now
from zhimeng_runtime.execution_protocol import ActivityKind, MessagePayload


@dataclass(frozen=True)
class SteeringInput:
    item_id: str
    payload: dict


class AgentSteeringStore:
    def __init__(self, store: AgentStore):
        self.store = store

    def pending(self, run_id: str) -> list[SteeringInput]:
        with self.store.database.connection() as connection:
            rows = connection.execute(
                "SELECT item_id,input_json FROM session_steering WHERE run_id = ? AND status = 'pending' ORDER BY created_at,item_id",
                (run_id,),
            ).fetchall()
        return [SteeringInput(row['item_id'], json.loads(row['input_json'])) for row in rows]

    def consume(self, run_id: str, prepared: list[tuple[str, list[dict[str, object]]]],
                transcript_prefix: list[dict[str, object]]) -> list[dict[str, object]]:
        appended: list[dict[str, object]] = []
        with self.store.database.connection() as connection:
            connection.execute('BEGIN IMMEDIATE')
            try:
                run = self.store._require_run_row(connection, run_id)
                if run['status'] != 'running':
                    connection.rollback()
                    return []
                session = connection.execute('SELECT * FROM execution_session WHERE id = ?', (run['session_id'],)).fetchone()
                sequence, ordinal = int(run['last_activity_sequence']), int(session['last_message_ordinal'])
                now = _now()
                for item_id, parts in prepared:
                    receipt = connection.execute(
                        "SELECT * FROM session_steering WHERE run_id = ? AND item_id = ? AND status = 'pending'", (run_id, item_id),
                    ).fetchone()
                    if receipt is None:
                        continue
                    payload = json.loads(receipt['input_json'])
                    sequence += 1
                    ordinal += 1
                    message_id = _new_id('message')
                    activity = self.store._insert_activity(
                        connection, run, sequence=sequence, kind=ActivityKind.MESSAGE,
                        payload=MessagePayload(role='user', content=payload['message'], name=None,
                            message_id=message_id, phase='completed', delta=None, format='plain',
                            finish_reason=None, usage=None).model_dump(mode='json'),
                        causation_id=self.store._last_activity_id(connection, run_id), created_at=now,
                    )
                    connection.execute(
                        "INSERT INTO session_message(activity_id,session_id,run_id,message_id,ordinal,role,format,created_at) "
                        "VALUES(?,?,?,?,?,'user','plain',?)",
                        (activity.root.activity_id, run['session_id'], run_id, message_id, ordinal, now.isoformat()),
                    )
                    images = claim_images(connection, run['session_id'], payload.get('image_ids'))
                    if images:
                        connection.execute("UPDATE session_message SET images_json=? WHERE message_id=?", (json.dumps(images, ensure_ascii=False), message_id))
                    connection.execute(
                        "UPDATE session_steering SET status='consumed', message_id=?, updated_at=? WHERE session_id=? AND item_id=?",
                        (message_id, now.isoformat(), run['session_id'], item_id),
                    )
                    appended.extend(parts)
                if appended:
                    from zhimeng_runtime.session_goal import GoalStore
                    GoalStore.invalidate_outcome(connection, run_id)
                    connection.execute('UPDATE run SET last_activity_sequence=?,updated_at=? WHERE id=?', (sequence, now.isoformat(), run_id))
                    connection.execute('UPDATE execution_session SET last_message_ordinal=?,updated_at=? WHERE id=?', (ordinal, now.isoformat(), run['session_id']))
                    self.store._save_run_transcript_in_connection(connection, run_id, [*transcript_prefix, *appended])
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return appended
