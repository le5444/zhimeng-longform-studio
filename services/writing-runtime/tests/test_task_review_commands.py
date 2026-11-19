from __future__ import annotations

import asyncio
import json
import sys

import pytest

from test_agent_store import begin_tool_call, start_run
from test_agent_parallel_tools import TurnProvider, final_turn, tool_batch_turn
from test_agent_service import wait_terminal
from test_task_review import complete, create, review, setup
from test_task_review_evidence import evidence
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.command_completion import CommandCompletionStore


def command_call(store, run, identifier, output, *, status='succeeded'):
    begin_tool_call(store, run.run_id, tool_call_id=identifier, tool_name='computer_run_command',
                    arguments={'command': 'python check.py', 'workdir': '/project'}, idempotency_key=identifier)
    store.finish_tool_call(run.run_id, tool_call_id=identifier, status=status, output=output,
                           error=None, duration_ms=1)


def test_review_uses_durable_completion_over_running_tool_and_scopes_owners(setup):
    _service, store, session, _binding, folder = setup
    source, _, _ = create(setup)
    start_run(store, source.run_id)
    command_call(store, source, 'command-source', {'schema': 'zhimeng.tool-result.command-session.v1',
        'session_id': 'cmd-review', 'status': 'running', 'exit_code': None, 'stdout': 'OLD_TOOL_BODY'})
    store.complete_run(source.run_id, content='Tests passed.', message_id='candidate-command', finish_reason='stop', usage=None)
    secret = 'sk-' + 'abcd1234' * 8
    completions = CommandCompletionStore(store.database)
    completions.record({'completion_version': 1, 'session_id': 'cmd-review',
        'owner_session_id': session.session_id, 'owner_run_id': source.run_id,
        'command': 'python check.py --token ' + secret, 'cwd': str(folder),
        'status': 'exited', 'exit_code': 7, 'stdout_file': str(folder / 'stdout.log'),
        'stderr_file': str(folder / 'stderr.log'), 'stdout': 'OLD_TOOL_BODY', 'timed_out': False})
    unrelated, _, _ = create(setup)
    complete(store, unrelated)
    completions.record({'completion_version': 1, 'session_id': 'cmd-other',
        'owner_session_id': session.session_id, 'owner_run_id': unrelated.run_id,
        'command': 'UNRELATED_COMMAND', 'status': 'exited', 'exit_code': 0})
    child, _, _ = review(setup, source)
    facts = evidence(AgentStore(store.database), child.run_id)['execution']['commands']
    assert facts['record_count'] == 1 and facts['omitted_count'] == 0
    item = facts['records'][0]
    assert item['source'] == 'command_completion'
    assert item['source_run_id'] == source.run_id
    assert item['command_session_id'] == 'cmd-review'
    assert item['status'] == 'exited' and item['exit_code'] == 7
    assert item['stdout_file'] == str(folder / 'stdout.log')
    assert item['stderr_file'] == str(folder / 'stderr.log')
    assert item['workdir'] == str(folder)
    text = json.dumps(facts)
    assert 'OLD_TOOL_BODY' not in text and 'UNRELATED_COMMAND' not in text and secret not in text
    assert facts['log_paths_are_not_current_file_verification'] is True
    assert facts['exit_code_is_not_task_verification'] is True


@pytest.mark.parametrize(('output', 'expected'), [
    ({'exit_code': 0}, 'exited'),
    ({'exit_code': 3}, 'exited'),
    ({'exit_code': 1, 'timed_out': True}, 'timed_out'),
    ({'error': 'not started'}, 'unverified'),
    ({'schema': 'zhimeng.tool-result.command-session.v1', 'session_id': 'cmd-running', 'status': 'running'}, 'running'),
])
def test_review_distinguishes_command_result_from_tool_success(setup, output, expected):
    _service, store, _session, _binding, _folder = setup
    source, _, _ = create(setup)
    start_run(store, source.run_id)
    command_call(store, source, 'command-sync', {'stdout': 'OLD_OUTPUT', **output})
    store.complete_run(source.run_id, content='Done.', message_id='candidate-sync', finish_reason='stop', usage=None)
    child, _, _ = review(setup, source)
    item = evidence(store, child.run_id)['execution']['commands']['records'][0]
    assert item['source'] == 'tool_call' and item['tool_call_id'] == 'command-sync'
    assert item['tool_status'] == 'succeeded'
    assert item['command'] == 'python check.py' and item['workdir'] == '/project'
    assert item['status'] == expected and item['exit_code'] == output.get('exit_code')
    assert 'OLD_OUTPUT' not in json.dumps(item)


def test_review_command_budget_exposes_omitted_records_and_keeps_original_request(setup):
    _service, store, _session, _binding, _folder = setup
    source, request, _ = create(setup)
    start_run(store, source.run_id)
    for index in range(40):
        command_call(store, source, f'command-budget-{index}', {'exit_code': 0, 'command': 'long command ' * 2000})
    store.complete_run(source.run_id, content='Done.', message_id='candidate-budget', finish_reason='stop', usage=None)
    child, _, _ = review(setup, source)
    facts = evidence(store, child.run_id, budget=9000)['execution']['commands']
    assert facts['record_count'] == 40 and facts['omitted_count'] > 0
    assert facts['records'][0]['command_omitted_characters'] > 0
    assert facts['records'][0]['tool_call_id'] == 'command-budget-0'
    assert len(facts['records']) + facts['omitted_count'] == 40
    messages = json.dumps(store.review_provider_messages(child.run_id, max_characters=9000), ensure_ascii=False)
    assert request.message_id in messages and source.objective in messages
    assert len(messages) < 11000


def test_review_includes_commands_from_original_and_continuation(setup):
    _service, store, _session, _binding, _folder = setup
    source, _, _ = create(setup)
    start_run(store, source.run_id)
    command_call(store, source, 'command-before-interruption', {'exit_code': 2})
    store.mark_interrupted(source.run_id, detail='Interrupted')
    continuation, _, _ = create(setup, continuation_source_run_id=source.run_id, message='Continue fixing the script.')
    start_run(store, continuation.run_id)
    command_call(store, continuation, 'command-after-continuation', {'exit_code': 0})
    store.complete_run(continuation.run_id, content='Fixed.', message_id='candidate-continued', finish_reason='stop', usage=None)
    child, _, _ = review(setup, continuation)
    commands = evidence(store, child.run_id)['execution']['commands']
    assert commands['record_count'] == 2
    assert [(item['source_run_id'], item['exit_code']) for item in commands['records']] == [
        (source.run_id, 2), (continuation.run_id, 0)]


def test_real_command_tool_and_completion_are_one_review_record(setup):
    service, store, session, binding, folder = setup
    service.provider = TurnProvider([
        tool_batch_turn([('real-command-evidence', 'computer_run_command', {
            'command': f'"{sys.executable}" -c "print(123)"', 'workdir': str(folder), 'yield_time_ms': 1000,
        })]), final_turn('Command finished.'),
    ])

    async def scenario():
        try:
            await service.update_session(session.session_id, rebind_provider=True)
            accepted = await service.start_run(session_id=session.session_id, message='Run the check.',
                expected_project_revision=binding.revision, expected_project_fingerprint=binding.fingerprint,
                active_chapter_id=None, writer_mode='author_review_change_proposal', idempotency_key='real-command-review')
            source = await wait_terminal(service, accepted.run.run_id)
            if task := service._tasks.get(source.run_id):
                await task
            child, _, _ = review(setup, source)
            facts = evidence(store, child.run_id)['execution']['commands']
            assert facts['record_count'] == 1 and facts['omitted_count'] == 0
            item = facts['records'][0]
            assert item['source'] == 'command_completion' and item['exit_code'] == 0
            from pathlib import Path
            assert Path(item['stdout_file']).read_text(encoding='utf-8').strip() == '123'
        finally:
            await service.shutdown()

    asyncio.run(scenario())
