from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest

from test_agent_interrupted_transcript import assert_protocol_pairs
from test_agent_service import ScriptedProvider, lifecycle_events
from test_memory_permissions import _harness
from zhimeng_runtime.agent_provider import AgentProviderCancelled, AgentProviderError, ProviderCompleted, ProviderTextDelta, ProviderToolCall
from zhimeng_runtime.agent_service import AgentService
from zhimeng_runtime.execution_protocol import MessageUsage
from zhimeng_runtime.run_compaction import COMPACTED_SCHEMA, SUMMARY_PROMPT, compact_tool_results


def exchange(content, status='succeeded'):
    return [{'role': 'assistant', 'content': 'Reading sources.', 'tool_calls': [
        {'id': 'read', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"source.txt"}'}}]},
        {'role': 'tool', 'tool_call_id': 'read', 'content': json.dumps({'status': status, 'output': {'text': content},
          'error': {'code': 'source_incomplete'} if status == 'failed' else None}, ensure_ascii=False)}]


def test_complete_middle_sources_archived_and_requirements_receipts_preserved(tmp_path):
    async def scenario():
        instructions = [{'role': 'system', 'content': 'General Agent.'}, {'role': 'user', 'content': 'Find the corrected amount; do not use the rejected offer.'}]
        source = '甲' * 30000 + 'REJECT 900; ACCEPT 317.25' + '乙' * 30000
        messages = instructions + exchange(source, 'failed') + [{'role': 'user', 'content': 'Also retain the source location.'}]
        original = json.loads(json.dumps(messages))
        seen = []
        async def summarize(request):
            seen.append(request[-1]['content'].split('：\n', 2)[-1])
            return 'REJECT 900; ACCEPT 317.25' if 'ACCEPT' in request[-1]['content'] else 'This part has no corrected amount.'
        compacted, stats = await compact_tool_results(messages, archive_root=tmp_path, objective=instructions[-1]['content'],
            token_budget=30000, estimate=AgentService._estimate_provider_messages_tokens,
            summarize=summarize, cancel_event=asyncio.Event())
        assert messages == original
        assert compacted[0] is instructions[0] and compacted[1] is instructions[1]
        assert compacted[-1] is messages[-1]
        assert_protocol_pairs(compacted)
        payload = json.loads(compacted[3]['content'])
        assert payload['status'] == 'failed' and payload['error'] == {'code': 'source_incomplete'}
        archive = payload['output']['source']
        restored = ''.join(Path(part['path']).read_bytes().decode('utf-8') for part in archive['parts'])
        assert restored == messages[3]['content']
        assert hashlib.sha256(restored.encode()).hexdigest() == archive['sha256']
        assert 'ACCEPT 317.25' in compacted[3]['content']
        assert len(seen) == len(archive['parts'])
        assert all(part['characters'] <= 5000 for part in archive['parts'])
        assert stats['tokens_after'] < 30000 < stats['tokens_before']
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['truncated', 'cancel', 'commit_failure', 'cancel_after_commit'])
def test_active_compaction_failures_and_commit_cancellation_keep_durable_truth(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('ZHIMENG_AGENT_CONTEXT_TOKEN_BUDGET', '40000')
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        path = tmp_path / 'large.txt'
        path.write_text('甲' * 60000, encoding='utf-8')
        entered, release = threading.Event(), threading.Event()
        class Provider(ScriptedProvider):
            async def stream_chat(self, messages, *, cancel_event, tools=()):
                if messages[0].get('content') == SUMMARY_PROMPT:
                    if mode == 'cancel':
                        entered.set()
                        await asyncio.Event().wait()
                    yield ProviderTextDelta('Read this source fragment.')
                    yield ProviderCompleted('length' if mode == 'truncated' else 'stop', None)
                else:
                    assert not any(m.get('role') == 'tool' for m in messages), 'No task dispatch after failed compaction'
                    yield ProviderToolCall(0, 'large-read', 'read_file', {'path': str(path)})
                    yield ProviderCompleted('tool_calls', None)
        service = AgentService(base.store, Provider([]), tool_executor=base.tool_executor, auto_memory=False)
        commit = service.store.commit_run_compaction
        if mode == 'commit_failure':
            insert = service.store._insert_provider_recovery_event
            def fail_event(connection, run_id, **kw):
                if kw.get('event_type') == 'run_context_compaction' and kw['payload']['state'] == 'succeeded':
                    raise OSError('commit injection')
                return insert(connection, run_id, **kw)
            monkeypatch.setattr(service.store, '_insert_provider_recovery_event', fail_event)
        if mode == 'cancel_after_commit':
            def held_commit(*args):
                commit(*args)
                entered.set()
                assert release.wait(15)
            monkeypatch.setattr(service.store, 'commit_run_compaction', held_commit)
        try:
            session = await service.create_session(project_id=None, title='Compaction failure')
            result = await service.start_run(session_id=session.session_id, message='Read the large file.',
                expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None, idempotency_key='failure')
            task = service._tasks[result.run.run_id]
            if mode in ('cancel', 'cancel_after_commit'):
                assert await asyncio.to_thread(entered.wait, 20)
                await service.cancel_run(result.run.run_id)
                release.set()
            await asyncio.gather(task, return_exceptions=True)
            final = service.store.get_run(result.run.run_id)
            assert final.status.value == ('cancelled' if mode.startswith('cancel') else 'failed')
            states = [v['state'] for k,v in lifecycle_events(service, final.run_id) if k == 'run_context_compaction']
            assert states[-1] == ('succeeded' if mode == 'cancel_after_commit' else 'cancelled' if mode == 'cancel' else 'failed')
            with service.store.database.connection() as con:
                transcript = json.loads(con.execute('SELECT messages_json FROM run_transcript WHERE run_id=?', (final.run_id,)).fetchone()[0])
            assert_protocol_pairs(transcript)
            assert (COMPACTED_SCHEMA in json.dumps(transcript)) == (mode == 'cancel_after_commit')
            assert session.session_id not in service._compacting_sessions
        finally:
            release.set()
            await service.shutdown()
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['empty', 'long', 'cancel', 'io', 'provider'])
def test_failed_summary_never_replaces_original_context(tmp_path, monkeypatch, failure):
    async def scenario():
        messages = exchange('甲' * 60000)
        original = json.loads(json.dumps(messages))
        cancel = asyncio.Event()
        async def summarize(_request):
            if failure == 'cancel': cancel.set()
            if failure == 'provider': raise AgentProviderError('upstream failed', code='upstream_failed', retryable=True)
            return '' if failure == 'empty' else 'x' * 2500 if failure == 'long' else 'summary'
        if failure == 'io':
            def broken(*_a): raise OSError('disk full')
            monkeypatch.setattr('zhimeng_runtime.run_compaction._write_atomic', broken)
        with pytest.raises((AgentProviderError, AgentProviderCancelled, OSError)):
            await compact_tool_results(messages, archive_root=tmp_path, objective='Summarize sources.', token_budget=30000,
                estimate=AgentService._estimate_provider_messages_tokens, summarize=summarize, cancel_event=cancel)
        assert messages == original
    asyncio.run(scenario())


def test_large_requirements_are_not_silently_truncated(tmp_path):
    async def scenario():
        messages = [{'role': 'user', 'content': '甲' * 40000}]
        async def summarize(_request): raise AssertionError('No tool material to summarize')
        with pytest.raises(AgentProviderError, match='仍超出'):
            await compact_tool_results(messages, archive_root=tmp_path, objective='Read.', token_budget=30000,
                estimate=AgentService._estimate_provider_messages_tokens, summarize=summarize, cancel_event=asyncio.Event())
        assert len(messages[0]['content']) == 40000
    asyncio.run(scenario())


def test_small_summary_window_splits_every_source_character_without_tool_catalog_reservation(tmp_path):
    async def scenario():
        messages = exchange('甲' * 20000 + '中段金额317.25' + '乙' * 20000)
        seen = []
        async def summarize(request):
            assert AgentService._estimate_provider_messages_tokens(request) <= 7000
            seen.append(request[-1]['content'].split('）：\n', 1)[1])
            return 'The source fragment is retained.'
        result, _ = await compact_tool_results(messages, archive_root=tmp_path, objective='Find amount.',
            token_budget=20000, summary_token_budget=7000, estimate=AgentService._estimate_provider_messages_tokens,
            summarize=summarize, cancel_event=asyncio.Event())
        assert ''.join(seen) == messages[-1]['content']
        assert len(seen) > 2
        summaries = json.loads(result[-1]['content'])['output']['summaries']
        restored = ''.join(Path(s['source_path']).read_text(encoding='utf-8')[s['start_offset']:s['start_offset']+s['characters']] for s in summaries)
        assert restored == messages[-1]['content']
    asyncio.run(scenario())


def test_reused_call_ids_keep_their_own_source_arguments(tmp_path):
    async def scenario():
        first, second = exchange('甲' * 30000), exchange('乙' * 30000)
        first[0]['tool_calls'][0]['function']['arguments'] = '{"path":"first.txt"}'
        second[0]['tool_calls'][0]['function']['arguments'] = '{"path":"second.txt"}'
        async def summarize(_request): return 'Source fragment.'
        result, _ = await compact_tool_results(first + second, archive_root=tmp_path, objective='Compare sources.',
            token_budget=30000, estimate=AgentService._estimate_provider_messages_tokens,
            summarize=summarize, cancel_event=asyncio.Event())
        for message, path in [(result[1], 'first.txt'), (result[3], 'second.txt')]:
            source = json.loads(message['content'])['output']['source']
            manifest = json.loads(Path(source['manifest_path']).read_text(encoding='utf-8'))
            assert json.loads(manifest['tool_call']['function']['arguments'])['path'] == path
    asyncio.run(scenario())


@pytest.mark.parametrize('stop_after_compaction', [False, True])
@pytest.mark.parametrize('budget, file_count, half_length, minimum_folded', [
    (209920, 8, 30000, 6),
    (45000, 4, 9000, 3),
])
def test_large_files_continue_with_real_archive_read_write_and_recovery(
    tmp_path, monkeypatch, stop_after_compaction, budget, file_count, half_length, minimum_folded,
):
    monkeypatch.setenv('ZHIMENG_AGENT_CONTEXT_TOKEN_BUDGET', str(budget))
    async def scenario():
        base, _, _, _ = _harness(tmp_path)
        paths = []
        for number in range(file_count):
            path = tmp_path / f'source-{number}.txt'
            path.write_text('甲' * half_length + f'\nMIDDLE-{number}=317.25\n' + '乙' * half_length, encoding='utf-8')
            paths.append(path)
        hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
        report = tmp_path / 'report.md'
        class Provider(ScriptedProvider):
            reasoning_effort = 'high'
            requests = 0
            summary_requests = 0
            request_sizes = []
            archive_part = None
            def with_reasoning_effort(self, effort):
                assert effort == 'low'
                parent = self
                class SummaryProvider:
                    reasoning_effort = effort
                    async def stream_chat(self, messages, *, cancel_event, tools=()):
                        assert messages[0].get('content') == SUMMARY_PROMPT
                        assert not tools
                        async for event in parent.stream_chat(messages, cancel_event=cancel_event, tools=tools):
                            yield event
                return SummaryProvider()

            async def stream_chat(self, messages, *, cancel_event, tools=()):
                size = AgentService._estimate_provider_messages_tokens(messages)
                self.request_sizes.append(size)
                assert size < budget, 'An oversized request reached the provider'
                if messages[0].get('content') == SUMMARY_PROMPT:
                    self.summary_requests += 1
                    yield ProviderTextDelta('Read source fragment; MIDDLE-0=317.25.' if 'MIDDLE-0' in messages[-1]['content'] else 'Other source fragment.')
                    yield ProviderCompleted('stop', MessageUsage(input_tokens=100, output_tokens=10, total_tokens=110),
                                            reported_model='controlled-summary', effective_reasoning_effort='low')
                    return
                assert_protocol_pairs(messages)
                assert self.reasoning_effort == 'high'
                self.requests += 1
                if self.requests == 1:
                    for i, path in enumerate(paths):
                        yield ProviderToolCall(i, f'read-{i}', 'read_file', {'path': str(path)})
                    yield ProviderCompleted('tool_calls', None)
                elif self.requests == 2:
                    result = json.loads(next(m['content'] for m in messages if m.get('tool_call_id') == 'read-0'))
                    assert result['output']['schema'] == COMPACTED_SCHEMA
                    source = result['output']['source']
                    self.archive_part = next(p['path'] for p in source['parts'] if 'MIDDLE-0' in Path(p['path']).read_text(encoding='utf-8'))
                    # The original file changes: archive reads must still see the earlier observation.
                    paths[0].write_text('Current disk version.', encoding='utf-8')
                    if stop_after_compaction:
                        raise AgentProviderError('Stop after durable compaction.', code='injected_stop', retryable=True)
                    yield ProviderToolCall(0, 'read-archive', 'read_file', {'path': self.archive_part})
                    yield ProviderCompleted('tool_calls', None)
                elif self.requests == 3:
                    # Rereading a relevant fragment must not immediately cause
                    # another summary and lose the exact evidence again.
                    assert 'MIDDLE-0=317.25' in messages[-1]['content']
                    assert COMPACTED_SCHEMA not in messages[-1]['content']
                    yield ProviderToolCall(0, 'write-report', 'write_file', {'path': str(report), 'content': 'Verified original source: 317.25\n'})
                    yield ProviderCompleted('tool_calls', None)
                elif self.requests == 4:
                    yield ProviderToolCall(0, 'read-report', 'read_file', {'path': str(report)})
                    yield ProviderCompleted('tool_calls', None)
                else:
                    assert '317.25' in messages[-1]['content']
                    yield ProviderTextDelta('Saved and read back the report: 317.25.')
                    yield ProviderCompleted('stop', None)
        provider = Provider([])
        service = AgentService(base.store, provider, tool_executor=base.tool_executor, auto_memory=False)
        async def no_title(*_a, **_kw): pass
        service._maybe_name_session = no_title
        try:
            session = await service.create_session(project_id=None, title='Large document comparison')
            run = await service.start_run(session_id=session.session_id, message='Compare eight sources, verify the middle amount and save report.md.',
                expected_project_revision=None, expected_project_fingerprint=None, active_chapter_id=None,
                writer_mode='author_review_change_proposal', idempotency_key='large-files')
            await asyncio.wait_for(service._tasks[run.run.run_id], 45)
            finished = service.store.get_run(run.run.run_id)
            assert finished.status.value == ('failed' if stop_after_compaction else 'succeeded'), finished.error
            receipt = [v for k,v in lifecycle_events(service, finished.run_id) if k == 'run_context_compaction'][-1]
            assert receipt['state'] == 'succeeded' and receipt['folded_results'] >= minimum_folded
            assert receipt['tokens_before'] > file_count * half_length * 2 and receipt['tokens_after'] < budget
            history = service.store.provider_history(session.session_id)
            assert_protocol_pairs(history)
            compacted = [json.loads(m['content']) for m in history if m.get('role') == 'tool' and COMPACTED_SCHEMA in m.get('content','')]
            assert len(compacted) >= minimum_folded
            assert provider.summary_requests >= minimum_folded
            summary_started = [v for k,v in lifecycle_events(service, finished.run_id) if k == 'run_context_summary_started']
            summary_completed = [v for k,v in lifecycle_events(service, finished.run_id) if k == 'run_context_summary_completed']
            assert len(summary_started) == len(summary_completed) == provider.summary_requests
            assert all(v['requested_reasoning_effort'] == 'low' for v in summary_started)
            assert all(v['usage']['total_tokens'] == 110 for v in summary_completed)
            assert 'MIDDLE-0=317.25' in Path(provider.archive_part).read_text(encoding='utf-8')
            assert [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths[1:]] == hashes[1:]
            if not stop_after_compaction:
                assert report.read_text(encoding='utf-8') == 'Verified original source: 317.25\n'
                changes = base.tool_executor.file_journal.open_changes(session.session_id)
                assert len(changes) == 1
                base.tool_executor.file_journal.undo(changes[0].change_id)
                assert not report.exists()
            # Fresh store process state reconstructs compacted pairs without adding old full results.
            from zhimeng_runtime.agent_store import AgentStore
            restored = AgentStore(base.store.database).provider_history(session.session_id)
            assert_protocol_pairs(restored)
            assert sum(COMPACTED_SCHEMA in str(m.get('content')) for m in restored) >= minimum_folded
        finally:
            await service.shutdown()
    asyncio.run(scenario())
