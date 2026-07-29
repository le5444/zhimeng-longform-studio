from __future__ import annotations
import json, os, shutil, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'services' / 'writing-runtime' / 'tests' / 'fixtures' / 'minimal-novel'
STAMP = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
WORK = ROOT / '.runtime' / f'author-prose-live-{STAMP}'
LIBRARY = WORK / 'library'
PROJECT = LIBRARY / 'minimal-novel'
DB = WORK / 'runtime.sqlite3'
EVIDENCE = ROOT / 'evidence' / f'author-prose-save-live-{STAMP}.json'
RUNTIME_PORT = int(os.environ.get('AUTHOR_PROSE_RUNTIME_PORT', '8796'))
WORKBENCH_PORT = int(os.environ.get('AUTHOR_PROSE_WORKBENCH_PORT', '5196'))
RUNTIME_URL = f'http://127.0.0.1:{RUNTIME_PORT}'
WORKBENCH_URL = f'http://127.0.0.1:{WORKBENCH_PORT}'
MARKER = f'[author-prose-live {STAMP}]'
NL = chr(10)

def http_json(method, url, body=None, timeout=60.0):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, method=method, headers={'Accept':'application/json','Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = {'raw': raw}
        return exc.code, payload

def wait_ready(url, timeout_s=45.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            status, payload = http_json('GET', url, timeout=3)
            if status == 200:
                return
            last = (status, payload)
        except Exception as exc:
            last = str(exc)
        time.sleep(0.4)
    raise RuntimeError(f'service not ready: {url} last={last}')


def wait_http(url, timeout_s=60.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method='GET', headers={'Accept': 'text/html,*/*'})
            with urllib.request.urlopen(req, timeout=3) as resp:
                if 200 <= resp.status < 500:
                    return
                last = resp.status
        except Exception as exc:
            last = str(exc)
        time.sleep(0.4)
    raise RuntimeError(f'http not ready: {url} last={last}')

def find_chapter(project: Path) -> Path:
    matches = sorted(project.rglob('CH001_*.md'))
    if not matches:
        raise RuntimeError('CH001 prose file missing in project copy')
    return matches[0]

def find_prose(snapshot: dict) -> dict:
    for item in snapshot['files']:
        rel = item['relative_path'].replace('\\', '/')
        if 'CH001_' in rel and rel.endswith('.md'):
            return item
    raise RuntimeError('CH001 prose missing from snapshot')

def main():
    if not FIXTURE.is_dir():
        raise SystemExit(f'missing fixture: {FIXTURE}')
    if WORK.exists():
        shutil.rmtree(WORK)
    LIBRARY.mkdir(parents=True)
    shutil.copytree(FIXTURE, PROJECT)
    chapter = find_chapter(PROJECT)
    before = chapter.read_text(encoding='utf-8')
    after = before.rstrip(NL) + NL + NL + MARKER + NL
    python = ROOT / '.venv' / 'Scripts' / 'python.exe'
    env = os.environ.copy()
    env.update({
        'PYTHONPATH': str(ROOT / 'services' / 'writing-runtime' / 'src'),
        'ZHIMENG_ENVIRONMENT': 'development',
        'ZHIMENG_HOST': '127.0.0.1',
        'ZHIMENG_PORT': str(RUNTIME_PORT),
        'ZHIMENG_DATABASE_PATH': str(DB),
        'ZHIMENG_TRUSTED_PROJECT_ROOTS': str(LIBRARY),
        'ZHIMENG_ALLOWED_ORIGINS': ','.join([WORKBENCH_URL, 'http://127.0.0.1:5173', 'http://localhost:5173']),
    })
    runtime_out = (WORK / 'runtime.stdout.log').open('w', encoding='utf-8')
    runtime_err = (WORK / 'runtime.stderr.log').open('w', encoding='utf-8')
    runtime = subprocess.Popen([str(python), '-m', 'zhimeng_runtime'], cwd=str(ROOT), env=env, stdout=runtime_out, stderr=runtime_err)
    vite = None
    result = {
        'schema': 'zhimeng.author-prose-save-live.v1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'runtime_url': RUNTIME_URL,
        'workbench_url': WORKBENCH_URL,
        'project_root': str(PROJECT),
        'marker': MARKER,
        'ok': False,
    }
    try:
        wait_ready(f'{RUNTIME_URL}/health/ready')
        status, snapshot = http_json('POST', f'{RUNTIME_URL}/api/v1/projects/inspect', {'root_path': str(PROJECT)})
        if status != 200:
            raise RuntimeError(f'inspect failed: {status} {snapshot}')
        project_id = snapshot['project']['project_id']
        prose = find_prose(snapshot)
        status, created = http_json('POST', f'{RUNTIME_URL}/api/v1/projects/{project_id}/change-sets', {
            'base_revision': snapshot['project']['revision'],
            'base_fingerprint': snapshot['project']['fingerprint'],
            'title': 'author edit CH001',
            'summary': 'author prose save live acceptance',
            'origin': 'manual',
            'gate_required': False,
            'changes': [{
                'operation': 'update',
                'file_id': prose['file_id'],
                'after_content': after,
                'rationale': 'author manual prose edit',
            }],
        })
        if status != 201:
            raise RuntimeError(f'create failed: {status} {created}')
        decisions = [{'hunk_id': h['hunk_id'], 'decision': 'accepted'} for c in created['changes'] for h in c['hunks']]
        status, ready = http_json('PATCH', f"{RUNTIME_URL}/api/v1/change-sets/{created['change_set_id']}/decisions", {
            'expected_review_revision': created['review_revision'],
            'decisions': decisions,
        })
        if status != 200:
            raise RuntimeError(f'decide failed: {status} {ready}')
        if ready.get('gate_required'):
            raise RuntimeError('manual author save unexpectedly required gate')
        status, applied = http_json('POST', f"{RUNTIME_URL}/api/v1/change-sets/{ready['change_set_id']}/apply", {'expected_review_revision': ready['review_revision']}, timeout=120)
        if status != 200:
            raise RuntimeError(f'apply failed: {status} {applied}')
        disk_after = chapter.read_text(encoding='utf-8')
        status, snapshot2 = http_json('POST', f'{RUNTIME_URL}/api/v1/projects/inspect', {'root_path': str(PROJECT)})
        if status != 200:
            raise RuntimeError(f'reinspect failed: {status} {snapshot2}')
        prose2 = find_prose(snapshot2)
        status, content = http_json('GET', f"{RUNTIME_URL}/api/v1/projects/{project_id}/files/{prose2['file_id']}")
        if status != 200:
            raise RuntimeError(f'reread failed: {status} {content}')
        disk_ok = MARKER in disk_after and disk_after != before
        api_ok = MARKER in content['content'] and content['content'] == disk_after
        apply_ok = applied.get('status') == 'applied'
        result.update({
            'project_id': project_id,
            'change_set_id': applied['change_set_id'],
            'apply_status': applied.get('status'),
            'before_excerpt': before[:120],
            'after_excerpt': disk_after[-160:],
            'disk_contains_marker': disk_ok,
            'api_contains_marker': MARKER in content['content'],
            'api_matches_disk': content['content'] == disk_after,
            'result_revision': content.get('snapshot_revision'),
            'ok': bool(api_ok and disk_ok and apply_ok),
        })
        node = shutil.which('node')
        if node is None:
            raise RuntimeError('node not found')
        vite_js = ROOT / 'apps' / 'workbench' / 'node_modules' / 'vite' / 'bin' / 'vite.js'
        if not vite_js.is_file():
            raise RuntimeError(f'missing vite: {vite_js}')
        wb_env = os.environ.copy()
        wb_env.update({'VITE_RUNTIME_URL': RUNTIME_URL, 'VITE_PROJECT_ROOT': str(PROJECT)})
        wb_out = (WORK / 'workbench.stdout.log').open('w', encoding='utf-8')
        wb_err = (WORK / 'workbench.stderr.log').open('w', encoding='utf-8')
        vite = subprocess.Popen([node, str(vite_js), '--host', '127.0.0.1', '--port', str(WORKBENCH_PORT), '--strictPort'], cwd=str(ROOT / 'apps' / 'workbench'), env=wb_env, stdout=wb_out, stderr=wb_err)
        wait_http(WORKBENCH_URL, timeout_s=60)
        result['workbench_ready'] = True
        result['keep_alive'] = {'runtime_pid': runtime.pid, 'workbench_pid': vite.pid}
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + NL, encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print(f'EVIDENCE={EVIDENCE}')
        print(f'WORKBENCH={WORKBENCH_URL}')
        print(f'RUNTIME={RUNTIME_URL}')
        return 0 if result['ok'] else 1
    except Exception as exc:
        key = 'workbench_error' if result.get('ok') else 'error'
        result[key] = str(exc)
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(result, ensure_ascii=False, indent=2) + NL, encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        if result.get('ok'):
            if vite is not None and vite.poll() is None:
                vite.terminate()
            print(f'EVIDENCE={EVIDENCE}')
            print(f'RUNTIME={RUNTIME_URL}')
            return 0
        for proc in (vite, runtime):
            if proc is not None and proc.poll() is None:
                proc.terminate()
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
