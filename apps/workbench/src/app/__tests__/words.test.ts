/**
 * 后台送上来的字到作者面前要统一写法：修改记录一个标题格式、改动说明和题目不重复、
 * Agent 连做几遍同一件事合成一条。这里钉住这几条转换。
 */
import { describe, expect, it } from 'vitest'
import type { AgentActivity } from '../../api/agent'
import { actionWords, changeHeadline, dedupeChangeLines, shortDay, splitChangeSummary, tidyChangeTitle } from '../lib/words'
import { T } from '../strings'
import { makeSnapshot } from './fixtures'

const PROSE_PATH = '05_正文内容/第001卷_起/001-050章/001_山门将倾.md'
const noEnglish = (text: string) => !/[A-Za-z]{2,}/u.test(text.replace(/\bAI\b/gu, ''))

describe('旧对话检索的真实出处', () => {
  const activities = (name: string, output: Record<string, unknown>) => ([
    { activity_id: 'recall-call', run_id: 'r1', sequence: 1, kind: 'tool_call', payload: { tool_call_id: 'recall-1', tool_name: name, arguments: { query: '采购' } } },
    { activity_id: 'recall-result', run_id: 'r1', sequence: 2, kind: 'tool_result', payload: { tool_call_id: 'recall-1', status: 'succeeded', output } },
  ] as unknown as AgentActivity[])

  it('区分用户原话和助手建议并保留项目及日期', () => {
    const words = actionWords(activities('session_search', { scope: 'project', project_id: 'p1', has_more: true, matches: [
      { session_id: 's1', message_id: 'm1', session_title: '采购决定', project_name: '办公室', role: 'user', created_at: '2026-10-17T10:00:00Z', content: '预算是 730 元。' },
      { session_title: '采购决定', project_name: '办公室', role: 'assistant', created_at: '2026-10-17T10:01:00Z', content: '可以考虑 900 元。' },
    ] }), null)
    expect(words[0].text).toBe('找到 2 条历史消息')
    expect(words[0].sources?.[0]).toMatchObject({ sessionId: 's1', messageId: 'm1', content: '预算是 730 元。' })
    expect(words[0].sources?.[0].label).toContain('采购决定 · 办公室 · 用户')
    expect(words[0].sources?.[1].label).toContain('采购决定 · 办公室 · Agent')
    expect(words[0].detail).toContain('当前项目')
    expect(words[0].detail).toContain('还有更多历史消息')
  })

  it('空结果和读取原消息各自显示实际条数', () => {
    expect(actionWords(activities('session_search', { matches: [] }), null)[0].text).toBe('找到 0 条历史消息')
    const words = actionWords(activities('session_read', { messages: [
      { session_title: '采购决定', role: 'user', ordinal: 3, next_start_offset: 1000, content: '保留原始决定。' },
    ] }), null)
    expect(words[0].text).toBe('读取了 1 条历史消息')
    expect(words[0].sources?.[0].content).toBe('保留原始决定。')
    expect(words[0].sources?.[0].label).toContain('第 3 条 · 部分原文')
  })

  it('相邻读取保留各自来源，不按相同标题合并', () => {
    const first = activities('session_read', { messages: [{ session_id: 's1', message_id: 'm1', content: 'first' }] })
    const second = activities('session_read', { messages: [{ session_id: 's1', message_id: 'm2', content: 'second' }] })
    const renamed = JSON.parse(JSON.stringify(second).replaceAll('recall-1', 'recall-2')) as AgentActivity[]
    renamed.forEach((item) => { item.sequence += 2 })
    const words = actionWords([...first, ...renamed], null)
    expect(words.map((item) => item.sources?.[0].messageId)).toEqual(['m1', 'm2'])
  })
})

describe('修改记录的统一标题', () => {
  const snapshot = makeSnapshot()

  it('改了一个文件：说文件名（带扩展名），Agent 自己起的题目留在第二行', () => {
    const words = changeHeadline({ title: '重写开头段落，增强画面感', origin: 'agent' }, snapshot, [PROSE_PATH])
    expect(words.headline).toBe('Agent 改了 001_山门将倾.md')
    expect(words.detail).toBe('重写开头段落，增强画面感')
  })

  it('改了几个文件：说几个文件', () => {
    const many = changeHeadline({ title: '整理', origin: 'agent' }, snapshot, [PROSE_PATH, '02_人物/主要人物表.md'])
    expect(many.headline).toBe(`Agent 改了 ${T.changes.files(2)}`)
  })

  it('列表里只有标题：标题就是路径时用它的文件名；你手动保存的那条没有第二行', () => {
    const manual = changeHeadline({ title: PROSE_PATH, origin: 'manual', file_count: 1 }, snapshot)
    expect(manual.headline).toBe('你改了 001_山门将倾.md')
    expect(manual.detail).toBeNull()
  })

  it('说不出是哪个文件：写几个文件，原题目留第二行（不再把章号翻成章名）', () => {
    const words = changeHeadline({ title: 'AI 整理 人物资料（11 份）', origin: 'skill_workflow', file_count: 11 }, snapshot)
    expect(words.headline).toBe(`Agent 整理了 ${T.changes.files(11)}`)
    expect(words.detail).toBe('AI 整理 人物资料（11 份）')
    const written = changeHeadline({ title: '第 1 章正文候选', origin: 'model', file_count: 1 }, snapshot)
    expect(written.headline).toBe(`Agent 写了 ${T.changes.files(1)}`)
    expect(written.detail).toBe('第 1 章正文')
  })

  it('机器标题里的「候选」不到作者面前', () => {
    expect(tidyChangeTitle('第005章正文候选。')).toBe('第005章正文')
    expect(tidyChangeTitle('候选：润色开头')).toBe('修改：润色开头')
  })
})

describe('题目和说明同屏只留一遍', () => {
  it('后台说明里抄了题目：不再单独出题目那行', () => {
    // 真机看到的卡片：「创建走查灵感文件」在第二行和第三行各出现一遍。
    expect(dedupeChangeLines('创建走查灵感文件', '改写 08_素材库/走查灵感.md。创建走查灵感文件'))
      .toEqual({ detail: null, summary: '改写 08_素材库/走查灵感.md。创建走查灵感文件' })
  })

  it('题目和说明一模一样：只留说明这一行', () => {
    expect(dedupeChangeLines('把第 4 章开头改得更有画面感', '把第 4 章开头改得更有画面感'))
      .toEqual({ detail: null, summary: '把第 4 章开头改得更有画面感' })
  })

  it('两句各说各的：都留', () => {
    expect(dedupeChangeLines('润色开头三段', '改写 05_正文内容/004_玉佩上的青云.md'))
      .toEqual({ detail: '润色开头三段', summary: '改写 05_正文内容/004_玉佩上的青云.md' })
  })

  it('没有题目：说明照常', () => {
    expect(dedupeChangeLines(null, '改写 004_玉佩上的青云.md'))
      .toEqual({ detail: null, summary: '改写 004_玉佩上的青云.md' })
  })
})

describe('改动说明：后台接上来的检查结论拆掉', () => {
  const NOTE = 'AI 味 52 分（medium）；对照这本书自己的写法，高出本书人写基线 14 分，建议看例句；'
    + '判罚主因「语义完整性」在本书人写正文里同样偏高，属本书风格而非 AI 病；例如「背面两个字。」（短碎句、节奏短碎/句尾单一/谓语缺失/词组拼接）'

  it('改动说明和后台接上来的检查结论拆成两段', () => {
    const parts = splitChangeSummary(`只改开头一段，其余一字未动。；${NOTE}`)
    expect(parts.text).toBe('只改开头一段，其余一字未动。')
    expect(parts.note).toBe(NOTE)
    expect(splitChangeSummary('只改开头一段。')).toEqual({ text: '只改开头一段。', note: null })
    expect(splitChangeSummary(null)).toEqual({ text: '', note: null })
  })

  it('旧版手动保存写死的说明是机器套话，不显示', () => {
    expect(splitChangeSummary('作者在工作台修改并保存 05_正文内容/第001卷_起/001-050章/001_山门将倾.md')).toEqual({ text: '', note: null })
  })

})

describe('Agent 做了什么：连着的重复合成一条', () => {
  const call = (id: string, sequence: number, tool: string, args: Record<string, unknown>) => ({
    activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'tool_call', payload: { tool_call_id: id, tool_name: tool, arguments: args },
  } as unknown as AgentActivity)
  const result = (id: string, sequence: number, status = 'succeeded') => ({
    activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'tool_result', payload: { tool_call_id: id, status },
  } as unknown as AgentActivity)

  it('同一章读了三遍显示成一条带次数，正在做的那条不合', () => {
    const snapshot = makeSnapshot()
    const activities = [
      call('c1', 1, 'project_read_file', { relative_path: PROSE_PATH }), result('c1', 2),
      call('c2', 3, 'project_read_file', { relative_path: PROSE_PATH }), result('c2', 4),
      call('c3', 5, 'project_read_file', { relative_path: PROSE_PATH }), result('c3', 6),
      call('c4', 7, 'project_search', { query: '柳青烟' }),
    ]
    const steps = actionWords(activities, snapshot)
    expect(steps.map((step) => step.text)).toEqual([
      `${T.agent.act.readFile('001_山门将倾.md')}${T.agent.act.times(3)}`,
      T.agent.act.searching('柳青烟'),
    ])
    expect(steps.map((step) => step.state)).toEqual(['succeeded', 'running'])
  })

  it('直接写文件和电脑访问也有标签，文件名就显示真文件名', () => {
    const snapshot = makeSnapshot()
    const activities = [
      call('w1', 1, 'project_write_file', { relative_path: '08_素材库/灵感.md' }), result('w1', 2),
      call('w2', 3, 'project_edit_file', { relative_path: PROSE_PATH }),
      call('w3', 4, 'computer_list_files', { path: '' }), result('w3', 5),
      call('w4', 6, 'computer_read_file', { path: 'D:/书库/参考/桥段.txt' }), result('w4', 7),
      call('w5', 8, 'computer_run_command', { command: 'dir /b' }), result('w5', 9),
      call('w6', 10, 'list_files', { path: 'D:/书库/参考' }), result('w6', 11),
      call('w7', 12, 'read_file', { path: '05_正文内容/001_山门将倾.md' }), result('w7', 13),
    ]
    const steps = actionWords(activities, snapshot)
    expect(steps.map((step) => step.text)).toEqual([
      T.agent.act.writeFile('灵感.md'),
      T.agent.act.editingFile('001_山门将倾.md'),
      // 旧对话里的 computer_* 按 9-27 合好的 list_files / read_file 显示。
      T.agent.act.listFiles,
      T.agent.act.readFile('桥段.txt'),
      T.agent.act.runCommand('dir /b'),
      T.agent.act.listFolder('参考'),
      T.agent.act.readFile('001_山门将倾.md'),
    ])
    for (const step of steps) expect(noEnglish(step.text.replace(/\.(txt|md)$|dir \/b/u, '')), step.text).toBe(true)
  })

  it('删除/移动/建文件夹/上网/一次写多份，各是一句人话', () => {
    const snapshot = makeSnapshot()
    const activities = [
      call('d1', 1, 'project_delete_file', { relative_path: PROSE_PATH }), result('d1', 2),
      call('v1', 3, 'project_move_file', {
        relative_path: PROSE_PATH,
        target_relative_path: '05_正文内容/第一卷/第 003 章 试剑.md',
      }), result('v1', 4),
      call('k1', 5, 'project_mkdir', { relative_path: '08_素材库/导入' }), result('k1', 6),
      call('f1', 7, 'web_fetch', { url: 'https://example.com/hello' }), result('f1', 8),
      call('w9', 9, 'project_write_file', {
        files: [
          { relative_path: '08_素材库/一.md', content: '一' },
          { relative_path: '08_素材库/二.md', content: '二' },
        ],
      }), result('w9', 10),
    ]
    const steps = actionWords(activities, snapshot)
    expect(steps.map((step) => step.text)).toEqual([
      T.agent.act.deleteFile('001_山门将倾.md'),
      T.agent.act.moveFile('001_山门将倾.md', '第 003 章 试剑.md'),
      T.agent.act.makeDir('08_素材库/导入'),
      T.agent.act.fetchPage('example.com'),
      T.agent.act.writeFiles(2),
    ])
    // 移动完成后的跳转指到新位置；多文件写入指到第一份。
    expect(steps[1].targetPath).toBe('05_正文内容/第一卷/第 003 章 试剑.md')
    expect(steps[4].targetPath).toBe('08_素材库/一.md')
  })

  it('联网搜索说查了什么、几条；展开看得到每条的标题和网址；读网页展开有完整网址；AI 味打分说分数', () => {
    const snapshot = makeSnapshot()
    const withOutput = (id: string, sequence: number, output: Record<string, unknown>) => ({
      activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'tool_result', payload: { tool_call_id: id, status: 'succeeded', output },
    } as unknown as AgentActivity)
    const activities = [
      call('s1', 1, 'search_web', { query: '番茄小说 征文' }),
      withOutput('s1', 2, {
        provider: 'exa', searched_at: '2026-09-26T13:31+08:00',
        results: [
          { title: '盛夏寻章征文', url: 'https://example.org/zhengwen', published: '2026-07-01T00:00:00Z' },
          { title: null, url: 'https://fanqienovel.com/', published: null },
        ],
      }),
      call('f1', 3, 'web_fetch', { url: 'https://example.org/zhengwen' }),
      withOutput('f1', 4, { final_url: 'https://example.org/zhengwen?from=1', title: '征文公告', content: '7 月 1 日开赛。' }),
      call('a1', 5, 'ai_smell_score', { text: '一段正文' }),
      withOutput('a1', 6, { score: 21.4, level: 'low', note: '比这本书平时低一点' }),
      call('s2', 7, 'search_web', { query: '还在搜' }),
    ]
    const steps = actionWords(activities, snapshot)
    expect(steps.map((step) => step.text)).toEqual([
      T.agent.act.searchWeb('番茄小说 征文', 2),
      T.agent.act.fetchPage('example.org'),
      T.agent.act.aiSmellScored(21, 'low'),
      T.agent.act.searchingWeb('还在搜'),
    ])
    expect(steps[0].detail).toBe([
      T.agent.act.searchSource('exa', '2026-09-26 13:31'),
      '盛夏寻章征文（2026-07-01）\nhttps://example.org/zhengwen',
      'https://fanqienovel.com/\nhttps://fanqienovel.com/',
    ].join('\n\n'))
    expect(steps[1].detail).toBe('https://example.org/zhengwen?from=1\n\n征文公告\n\n7 月 1 日开赛。')
    expect(steps[2].text).toBe('AI 味打分：21 分（偏低）')
    expect(steps[2].detail).toBe('比这本书平时低一点')
    for (const step of steps) expect(noEnglish(step.text.replace(/example\.org/u, '')), step.text).toBe(true)

    // 模型拿 web_fetch 开谷歌搜索页，后台改用了联网搜索：这一步按搜索显示。
    const routed = actionWords([
      call('g1', 1, 'web_fetch', { url: 'https://www.google.com/search?q=codex+cli' }),
      withOutput('g1', 2, {
        schema: 'zhimeng.tool-result.web-search.v1', query: 'codex cli', provider: 'exa', searched_at: '2026-09-26T14:02+08:00',
        routed_from_url: 'https://www.google.com/search?q=codex+cli',
        results: [{ title: 'Releases · openai/codex', url: 'https://github.com/openai/codex/releases', published: null }],
      }),
    ], snapshot)
    expect(routed[0].text).toBe(T.agent.act.searchWeb('codex cli', 1))
    expect(routed[0].detail).toContain('https://github.com/openai/codex/releases')
  })

  it('memory_write 显示「记住了：…」，长句子收成一行、全文留进详情', () => {
    const snapshot = makeSnapshot()
    const long = '这本书的主角叫沈砚，他每章结尾都要留一个钩子，章节名一律四个字，写对话的时候不要堆形容词，节奏要快，场景里先有动作再有情绪。'
    const activities = [
      call('m1', 1, 'memory_write', { scope: 'author', text: '我不喜欢每句另起一行' }), result('m1', 2),
      call('m2', 3, 'memory_write', { scope: 'book', text: long }),
    ]
    const steps = actionWords(activities, snapshot)
    expect(steps[0].text).toBe(T.agent.act.remember('我不喜欢每句另起一行'))
    expect(steps[0].state).toBe('succeeded')
    expect(steps[1].text).toBe(T.agent.act.remembering(`${long.slice(0, 60)}…`))
    expect(steps[1].detail).toBe(long)
  })

  it('中间隔了别的事就不合并', () => {
    const snapshot = makeSnapshot()
    const activities = [
      call('c1', 1, 'project_read_file', { relative_path: PROSE_PATH }), result('c1', 2),
      call('c2', 3, 'project_list_files', {}), result('c2', 4),
      call('c3', 5, 'project_read_file', { relative_path: PROSE_PATH }), result('c3', 6),
    ]
    expect(actionWords(activities, snapshot).map((step) => step.text)).toEqual([
      T.agent.act.readFile('001_山门将倾.md'),
      T.agent.act.listFiles,
      T.agent.act.readFile('001_山门将倾.md'),
    ])
  })

  it('步骤带耗时：发起和结果的时间差；合并时累加；没结果或没时间就是 null', () => {
    const at = (seconds: number) => new Date(seconds * 1000).toISOString()
    const timedCall = (id: string, sequence: number, startedSeconds: number) => ({
      ...call(id, sequence, 'project_search', { query: '柳青烟' }),
      created_at: at(startedSeconds),
    } as unknown as AgentActivity)
    const timedResult = (id: string, sequence: number, endedSeconds: number) => ({
      ...result(id, sequence),
      created_at: at(endedSeconds),
    } as unknown as AgentActivity)

    const activities = [
      timedCall('c1', 1, 10), timedResult('c1', 2, 13),
      timedCall('c2', 3, 20), timedResult('c2', 4, 25),
      timedCall('c3', 5, 30), // 还在跑
      call('c4', 6, 'project_search', { query: '铁牛' }), result('c4', 7), // 老数据没有时间
    ]
    const steps = actionWords(activities, null)
    // c1+c2 是同一件事连着做，合成一条，耗时 3+5=8 秒
    expect(steps[0].text).toBe(`${T.agent.act.search('柳青烟')}${T.agent.act.times(2)}`)
    expect(steps[0].durationMs).toBe(8000)
    expect(steps[1].durationMs).toBeNull() // 还在跑
    expect(steps[2].durationMs).toBeNull() // 没有时间戳的老数据不瞎编
  })

  it('命令执行结果带完整终端输出详情（退出代码、标准输出与标准错误）', () => {
    const snapshot = makeSnapshot()
    const activities = [
      call('cmd1', 1, 'computer_run_command', { command: 'git status' }),
      {
        activity_id: 'a2',
        run_id: 'r1',
        sequence: 2,
        kind: 'tool_result',
        payload: {
          tool_call_id: 'cmd1',
          status: 'succeeded',
          output: {
            schema: 'zhimeng.tool-result.computer-run-command.v1',
            command: 'git status',
            exit_code: 0,
            stdout: 'On branch main\nnothing to commit',
            stderr: '',
          },
        },
      } as unknown as AgentActivity,
    ]
    const steps = actionWords(activities, snapshot)
    expect(steps).toHaveLength(1)
    expect(steps[0].text).toBe(T.agent.act.runCommand('git status'))
    expect(steps[0].detail).toContain('$ git status')
    expect(steps[0].detail).toContain('[退出代码: 0]')
    expect(steps[0].detail).toContain('On branch main')
  })

  it.each(['computer_run_command', 'command_session'])('退出为零但日志失败时仍显示输出不完整：%s', (tool) => {
    const steps = actionWords([
      call('cmd-incomplete', 1, tool, { command: 'python build.py' }),
      {
        activity_id: 'a2', run_id: 'r1', sequence: 2, kind: 'tool_result',
        payload: { tool_call_id: 'cmd-incomplete', status: 'succeeded', output: {
          exit_code: 0, stdout: 'head\n…\ntail', stderr: '',
          output_error: '保存 stdout 输出失败：OSError；完整日志不可用。',
        } },
      } as unknown as AgentActivity,
    ], null)
    expect(steps[0].text).toContain('输出不完整')
    expect(steps[0].detail).toContain('[退出代码: 0]')
    expect(steps[0].detail).toContain('完整日志不可用')
    expect(steps[0].detail).toContain('head\n…\ntail')
    expect(steps[0].detail).not.toContain('完整输出：')
  })
})

describe('试错不是失败：没找到、没对上照实写，只有真出错才标红', () => {
  const call = (id: string, sequence: number, tool: string, args: Record<string, unknown>) => ({
    activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'tool_call', payload: { tool_call_id: id, tool_name: tool, arguments: args },
  } as unknown as AgentActivity)
  const failed = (id: string, sequence: number, code: string, message: string) => ({
    activity_id: `a${sequence}`, run_id: 'r1', sequence, kind: 'tool_result',
    payload: { tool_call_id: id, status: 'failed', error: { error: { code, message, retryable: false } } },
  } as unknown as AgentActivity)

  it('读一个不存在的文件：普通的一步「……：没找到」，展开是工具原话，不贴「执行失败」', () => {
    const steps = actionWords([
      call('r1', 1, 'computer_read_file', { path: 'C:/Users/demo/作者偏好.md' }),
      failed('r1', 2, 'agent_tool_computer_path_not_found', '没有找到这个文件：C:/Users/demo/作者偏好.md。「C:\\Users\\demo」里有：.claude/、Desktop/'),
    ], null)
    expect(steps).toHaveLength(1)
    expect(steps[0].state).toBe('succeeded')
    expect(steps[0].text).toBe(T.agent.act.readFile('作者偏好.md'))
    expect(steps[0].text).not.toContain(T.agent.act.failed)
    expect(steps[0].detail).toContain('「C:\\Users\\demo」里有')
    expect(steps[0].detail).not.toContain('执行失败')
  })

  it('改一段没对上、撤回时没得撤：也是普通的一步', () => {
    const steps = actionWords([
      call('e1', 1, 'project_edit_file', { path: '设定/人物.md', find: '旧的', replace: '新的' }),
      failed('e1', 2, 'agent_tool_edit_find_missing', '在文件里没有找到要替换的这段文字。'),
      call('u1', 3, 'project_undo_change', {}),
      failed('u1', 4, 'agent_tool_undo_nothing', '这个对话里没有还能撤回的修改。'),
    ], null)
    expect(steps.map((step) => step.state)).toEqual(['succeeded', 'succeeded'])
    expect(steps[0].text).toBe(T.agent.act.editFile('人物.md'))
    expect(steps[1].text).toBe(T.agent.act.undoChange(''))
    expect(steps[0].detail).toContain('在文件里没有找到要替换的这段文字')
    expect(steps[1].detail).toContain('没有还能撤回的修改')
  })

  it('超时、断网、没权限保留真实结果和详情，摘要不追加失败提示', () => {
    const steps = actionWords([
      call('t1', 1, 'search_web', { query: '今天的新闻' }),
      failed('t1', 2, 'agent_tool_timeout', 'tool call timed out after 90s'),
      call('w1', 3, 'project_write_file', { path: 'C:/Users/demo/Desktop/测试.txt', content: 'x' }),
      failed('w1', 4, 'writer_change_set_forbidden', '现在是只读挡位，写不了文件。'),
    ], null)
    expect(steps.map((step) => step.state)).toEqual(['failed', 'failed'])
    expect(steps[0].text.endsWith(T.agent.act.failed)).toBe(false)
    expect(steps[1].detail).toContain('[执行失败]')
  })

  it('9-26 起写文件工具叫 write_file / move_file…、参数叫 path / target_path（旧名照认）；写在打开的文件夹里时跳转用结果里的相对路径', () => {
    const snapshot = makeSnapshot()
    const steps = actionWords([
      call('w1', 1, 'write_file', { path: 'C:/书/08_素材库/灵感.md', content: '一' }),
      {
        activity_id: 'a2', run_id: 'r1', sequence: 2, kind: 'tool_result',
        payload: { tool_call_id: 'w1', status: 'succeeded', output: { schema: 'zhimeng.tool-result.project-write-file.v1', relative_path: '08_素材库/灵感.md', applied: true } },
      } as unknown as AgentActivity,
      call('m1', 3, 'move_file', { path: 'C:/Users/demo/Desktop/a.txt', target_path: 'C:/Users/demo/Desktop/b.txt' }),
      {
        activity_id: 'a4', run_id: 'r1', sequence: 4, kind: 'tool_result',
        payload: { tool_call_id: 'm1', status: 'succeeded', output: { schema: 'zhimeng.tool-result.file-change.v1', change_id: 'filechange-1', operation: 'moved' } },
      } as unknown as AgentActivity,
    ], snapshot)
    expect(steps.map((step) => step.text)).toEqual([
      T.agent.act.writeFile('灵感.md'),
      T.agent.act.moveFile('a.txt', 'b.txt'),
    ])
    expect(steps[0].targetPath).toBe('08_素材库/灵感.md')
    // 旧名字（旧对话里记的）显示一样。
    const legacy = actionWords([call('w2', 1, 'project_undo_change', {})], snapshot)
    expect(legacy[0].text).toBe(T.agent.act.undoingChange)
  })
})

describe('左栏的小字日期', () => {
  const now = new Date('2026-09-15T20:00:00')
  it('今天 / 昨天 / N 天前 / 今年 / 跨年', () => {
    expect(shortDay('2026-09-15T09:00:00', now)).toBe('今天')
    expect(shortDay('2026-09-14T23:59:00', now)).toBe('昨天')
    expect(shortDay('2026-09-11T10:00:00', now)).toBe('4 天前')
    expect(shortDay('2026-08-01T10:00:00', now)).toBe('8月1日')
    expect(shortDay('2025-12-31T10:00:00', now)).toBe('2025年12月31日')
    expect(shortDay('', now)).toBe('')
    expect(shortDay('不是日期', now)).toBe('')
  })
})
