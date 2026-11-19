import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CommandsPanel } from '../panel/Commands'
import { mergeOutput } from '../panel/useCommandOutput'
import type { CommandOutputPage, CommandSessionOutput, CommandSessionSummary } from '../../api/commandSessions'

const api = vi.hoisted(() => ({ listCommandSessions: vi.fn(), readCommandSession: vi.fn(), stopCommandSession: vi.fn() }))
vi.mock('../../api/commandSessions', () => api)

function command(overrides: Partial<CommandSessionSummary> = {}): CommandSessionSummary {
  return { command_session_id: 'cmd-1', run_id: 'run-1', command: 'python -u job.py', cwd: 'C:/work', status: 'running',
    exit_code: null, created_at: 1791690000, ended_at: null, duration_ms: 4200, stdout_bytes: 0, stderr_bytes: 0, timed_out: false, interruption_reason: null, ...overrides }
}
function page(text: string, start = 0, total?: number): CommandOutputPage {
  const next = start + new TextEncoder().encode(text).length
  return { text, start_offset: start, next_offset: next, total_bytes: total ?? next, truncated_before: start > 0, has_more: total !== undefined && next < total }
}
function output(stdout = page('第一行\n'), stderr = page(''), metadata: Partial<CommandSessionSummary> = {}): CommandSessionOutput {
  return { ...command(metadata), stdout, stderr, stdout_bytes: stdout.total_bytes, stderr_bytes: stderr.total_bytes }
}
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  Object.values(api).forEach((mock) => mock.mockReset())
  api.listCommandSessions.mockResolvedValue({ commands: [command()] })
  api.readCommandSession.mockResolvedValue(output())
  api.stopCommandSession.mockResolvedValue(command({ status: 'cancelled', exit_code: 1, ended_at: 1791690005 }))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('命令面板', () => {
  it('指定记录缺失时不自动打开另一条命令，刷新后可恢复或手动选择', async () => {
    const view = render(<CommandsPanel sessionId="a" focusId="cmd-missing" focusRequest={1} />)
    expect(await screen.findByRole('status')).toHaveTextContent('找不到所选命令')
    expect(api.readCommandSession).not.toHaveBeenCalled()
    expect(screen.getByRole('option')).toHaveAttribute('aria-selected', 'false')
    fireEvent.click(screen.getByRole('option'))
    await screen.findByText('第一行')
    view.rerender(<CommandsPanel sessionId="a" focusId="cmd-missing" focusRequest={2} />)
    expect(screen.queryByRole('region', { name: '标准输出内容' })).toBeNull()
    api.listCommandSessions.mockResolvedValue({ commands: [command(), command({ command_session_id: 'cmd-missing', command: 'recovered command' })] })
    api.readCommandSession.mockResolvedValue(output(page('恢复的目标日志\n'), page(''), { command_session_id: 'cmd-missing' }))
    fireEvent.click(screen.getByRole('button', { name: '刷新命令列表' }))
    await screen.findByText('恢复的目标日志')
    expect(screen.queryByText(/找不到所选命令/)).toBeNull()
    expect(api.readCommandSession).toHaveBeenLastCalledWith('a', 'cmd-missing', expect.anything(), expect.anything())
  })

  it('选中记录从列表消失时停止读旧日志并保留缺失事实', async () => {
    api.listCommandSessions.mockResolvedValueOnce({ commands: [command()] }).mockResolvedValue({ commands: [command({ command_session_id: 'cmd-2' })] })
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('第一行')
    fireEvent.click(screen.getByRole('button', { name: '刷新命令列表' }))
    expect(await screen.findByRole('status')).toHaveTextContent('找不到所选命令')
    expect(screen.queryByRole('region', { name: '标准输出内容' })).toBeNull()
    expect(api.readCommandSession.mock.calls.every((call) => call[1] === 'cmd-1')).toBe(true)
  })

  it('日志缺失只影响对应命令，保留退出码并允许重新读取', async () => {
    const damaged = command({ status: 'exited', exit_code: 0, stdout_bytes: null, output_error: 'stdout.log 缺失或无法读取' })
    api.listCommandSessions.mockResolvedValue({ commands: [damaged, command({ command_session_id: 'cmd-2', command: 'another command' })] })
    api.readCommandSession.mockRejectedValue(new Error('日志不可读'))
    render(<CommandsPanel sessionId="a" focusId="cmd-1" />)
    expect(await screen.findByRole('option', { name: /输出不可读/ })).toHaveTextContent('退出码 0')
    expect(screen.getByRole('option', { name: /another command/ })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: '重新读取输出' })).toBeInTheDocument()
    expect(screen.queryByText('没有输出')).not.toBeInTheDocument()
    expect(screen.queryByText('输出已保存')).not.toBeInTheDocument()
    api.readCommandSession.mockResolvedValue(output(page('恢复的日志\n'), page(''), { status: 'exited', exit_code: 0 }))
    fireEvent.click(screen.getByRole('button', { name: '重新读取输出' }))
    expect(await screen.findByText('恢复的日志')).toBeInTheDocument()
  })

  it('读取真实状态、完整命令和目录；stdout/stderr分别显示与复制，不凭stderr判失败', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    api.listCommandSessions.mockResolvedValue({ commands: [command({ status: 'exited', exit_code: 0 })] })
    api.readCommandSession.mockResolvedValue(output(page('正常\n'), page('warning: just a warning\n'), { status: 'exited', exit_code: 0 }))
    render(<CommandsPanel sessionId="a" />)
    expect(await screen.findByText('正常')).toBeInTheDocument()
    expect(screen.getAllByText('已完成 · 退出码 0')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: '停止命令' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByText('命令详情'))
    expect(screen.getByText('C:/work')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('tab', { name: /错误输出/ }))
    expect(screen.getByRole('region', { name: '错误输出内容' })).toHaveTextContent('warning: just a warning')
    fireEvent.click(screen.getByRole('button', { name: '复制输出' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('warning: just a warning\n'))
    expect(screen.queryByText('失败')).not.toBeInTheDocument()
  })

  it('按UTF8字节读取增量，向前翻页不重叠丢字，复制标明只加载部分', async () => {
    const initial = output(page('尾\n', 6), page(''))
    api.readCommandSession.mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(output(page('你好', 0, 10), page('')))
      .mockResolvedValue(output(page('新\n', 10), page('')))
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('尾')
    expect(screen.getByRole('button', { name: '复制已加载输出' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '加载更早输出' }))
    await waitFor(() => expect(screen.getByRole('region', { name: '标准输出内容' }).textContent).toBe('你好尾\n'))
    expect(api.readCommandSession.mock.calls[1][2]).toEqual({ stdout_before: 6, stderr_offset: 0 })
    expect(screen.queryByRole('button', { name: '加载更早输出' })).not.toBeInTheDocument()
    await advance(1500)
    expect(api.readCommandSession.mock.calls[2][2]).toEqual({ stdout_offset: 10, stderr_offset: 0 })
    expect(screen.getByRole('region', { name: '标准输出内容' }).textContent).toBe('你好尾\n新\n')
  })

  it('尚未追上输出末尾时显示补读状态，读完后才显示最近输出', async () => {
    let finish!: (value: CommandSessionOutput) => void
    api.listCommandSessions.mockResolvedValue({ commands: [command({ status: 'exited', exit_code: 0 })] })
    api.readCommandSession.mockResolvedValueOnce(output(page('中', 3, 9), page(''), { status: 'exited', exit_code: 0 }))
      .mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('中')
    expect(screen.getByText('正在读取后续输出…')).toBeInTheDocument()
    expect(screen.queryByText('显示最近输出')).not.toBeInTheDocument()
    await advance(50)
    await act(async () => { finish(output(page('尾', 6), page(''), { status: 'exited', exit_code: 0 })) })
    expect(screen.getByRole('region', { name: '标准输出内容' })).toHaveTextContent('中尾')
    expect(screen.getByText('显示最近输出')).toBeInTheDocument()
    expect(api.readCommandSession.mock.calls[1][2]).toEqual({ stdout_offset: 6, stderr_offset: 0 })
  })

  it('切对话abort旧请求，迟到日志和停止结果不会串入新对话；关闭停止轮询', async () => {
    let finish!: (value: CommandSessionOutput) => void
    api.readCommandSession.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
      .mockResolvedValue(output(page('B 的日志\n')))
    const view = render(<CommandsPanel sessionId="a" />)
    await waitFor(() => expect(api.readCommandSession).toHaveBeenCalledOnce())
    const oldSignal = api.readCommandSession.mock.calls[0][3].signal as AbortSignal
    view.rerender(<CommandsPanel sessionId="b" />)
    await screen.findByText('B 的日志')
    expect(oldSignal.aborted).toBe(true)
    await act(async () => finish(output(page('A 的迟到日志\n'))))
    expect(screen.queryByText('A 的迟到日志')).not.toBeInTheDocument()
    view.unmount()
    const calls = api.readCommandSession.mock.calls.length
    const lists = api.listCommandSessions.mock.calls.length
    await advance(10000)
    expect(api.readCommandSession).toHaveBeenCalledTimes(calls)
    expect(api.listCommandSessions).toHaveBeenCalledTimes(lists)
  })

  it('网络失败保留输出和字节游标，可明确重试；不能把缓存当最新状态', async () => {
    api.readCommandSession.mockResolvedValueOnce(output()).mockRejectedValueOnce(new Error('连接暂时断开'))
      .mockResolvedValue(output(page('恢复\n', 10)))
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('第一行')
    await advance(1500)
    expect(screen.getByRole('alert')).toHaveTextContent('输出暂未更新：连接暂时断开')
    expect(screen.getByRole('region', { name: '标准输出内容' }).textContent).toBe('第一行\n')
    fireEvent.click(screen.getByRole('button', { name: '重新读取输出' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(api.readCommandSession.mock.calls[2][2]).toEqual({ stdout_offset: 10, stderr_offset: 0 })
    expect(screen.getByRole('region', { name: '标准输出内容' }).textContent).toBe('第一行\n恢复\n')
  })

  it('停止只调用选中命令；读到终态才更新且通知文件刷新，保留最终输出', async () => {
    const onChanged = vi.fn()
    const onSummary = vi.fn()
    let finishOldList!: (value: { commands: CommandSessionSummary[] }) => void
    api.listCommandSessions.mockResolvedValueOnce({ commands: [command()] })
      .mockImplementationOnce(() => new Promise((resolve) => { finishOldList = resolve }))
    api.readCommandSession.mockResolvedValueOnce(output()).mockResolvedValue(output(page('停止前最后输出\n', 10), page(''), { status: 'cancelled', exit_code: 1 }))
    render(<CommandsPanel sessionId="a" onChanged={onChanged} onSummary={onSummary} />)
    await screen.findByText('第一行')
    fireEvent.click(screen.getByRole('button', { name: '刷新命令列表' }))
    await waitFor(() => expect(api.listCommandSessions).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: '停止命令' }))
    await waitFor(() => expect(api.stopCommandSession).toHaveBeenCalledWith('a', 'cmd-1', expect.objectContaining({ signal: expect.any(AbortSignal) })))
    await waitFor(() => expect(screen.queryByRole('button', { name: '停止命令' })).not.toBeInTheDocument())
    expect(screen.getByRole('region', { name: '标准输出内容' }).textContent).toBe('第一行\n停止前最后输出\n')
    await act(async () => finishOldList({ commands: [command()] }))
    expect(onSummary).toHaveBeenLastCalledWith({ total: 1, running: 0 })
    expect(within(screen.getByRole('listbox')).getByRole('option')).toHaveTextContent('已停止')
    expect(onChanged).toHaveBeenCalledOnce()
  })

  it('命令已停止但文件刷新失败时分开说明，重试刷新不会再次停止或取消任务', async () => {
    const onChanged = vi.fn().mockRejectedValueOnce(new Error('文件正在被占用')).mockResolvedValue(undefined)
    api.readCommandSession.mockResolvedValueOnce(output()).mockResolvedValue(output(page('末尾\n', 10), page(''), { status: 'cancelled', exit_code: 1 }))
    render(<CommandsPanel sessionId="a" onChanged={onChanged} />)
    await screen.findByText('第一行')
    fireEvent.click(screen.getByRole('button', { name: '停止命令' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('命令已结束，文件列表刷新失败：文件正在被占用')
    expect(screen.queryByRole('button', { name: '停止命令' })).not.toBeInTheDocument()
    expect(screen.queryByText('状态待刷新')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新文件列表' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(onChanged).toHaveBeenCalledTimes(2)
    expect(api.stopCommandSession).toHaveBeenCalledOnce()
  })

  it('停止失败不伪造已停止，重新尝试成功后仍保留原日志', async () => {
    api.stopCommandSession.mockRejectedValueOnce(new Error('停止请求未收到回音')).mockResolvedValue(command({ status: 'cancelled', exit_code: 1 }))
    api.readCommandSession.mockResolvedValueOnce(output()).mockResolvedValue(output(page('', 10), page(''), { status: 'cancelled', exit_code: 1 }))
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('第一行')
    fireEvent.click(screen.getByRole('button', { name: '停止命令' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('停止命令未确认')
    expect(screen.getByRole('button', { name: '停止命令' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试停止' }))
    await waitFor(() => expect(screen.queryByRole('button', { name: '停止命令' })).not.toBeInTheDocument())
    expect(screen.getByRole('region', { name: '标准输出内容' }).textContent).toBe('第一行\n')
  })

  it('隐藏但不卸载也停止读取，重新打开重新确认状态', async () => {
    const view = render(<CommandsPanel sessionId="a" />)
    await screen.findByText('第一行')
    view.rerender(<CommandsPanel sessionId="a" active={false} />)
    const reads = api.readCommandSession.mock.calls.length
    const lists = api.listCommandSessions.mock.calls.length
    await advance(10000)
    expect(api.readCommandSession).toHaveBeenCalledTimes(reads)
    expect(api.listCommandSessions).toHaveBeenCalledTimes(lists)
    view.rerender(<CommandsPanel sessionId="a" />)
    await waitFor(() => expect(api.listCommandSessions).toHaveBeenCalledTimes(lists + 1))
    await waitFor(() => expect(api.readCommandSession).toHaveBeenCalledTimes(reads + 1))
  })

  it('向上读旧输出不被增量抢滚，跟随最新可以恢复滚动', async () => {
    api.readCommandSession.mockResolvedValueOnce(output()).mockResolvedValue(output(page('第二行\n', 10)))
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('第一行')
    const region = screen.getByRole('region', { name: '标准输出内容' })
    Object.defineProperties(region, { scrollHeight: { configurable: true, value: 1000 }, clientHeight: { configurable: true, value: 200 } })
    region.scrollTop = 120
    fireEvent.scroll(region)
    await advance(1500)
    expect(region.scrollTop).toBe(120)
    fireEvent.click(screen.getByRole('button', { name: '跟随最新' }))
    expect(region.scrollTop).toBe(1000)
  })

  it('列表失败不会清掉已有命令，未知退出码及重启中断不冒充完成', async () => {
    api.listCommandSessions.mockResolvedValueOnce({ commands: [command({ status: 'interrupted', interruption_reason: '运行时已重启' })] }).mockRejectedValue(new Error('本机未连接'))
    api.readCommandSession.mockResolvedValue(output(page('旧日志\n'), page(''), { status: 'interrupted', interruption_reason: '运行时已重启' }))
    render(<CommandsPanel sessionId="a" />)
    await screen.findByText('旧日志')
    await advance(2500)
    expect(screen.getByText('状态待刷新')).toBeInTheDocument()
    expect(screen.getByText('运行时已重启')).toBeInTheDocument()
    expect(within(screen.getByRole('listbox')).getByRole('option')).toHaveTextContent('已中断 · 待刷新')
    expect(screen.queryByText(/已完成/)).not.toBeInTheDocument()
  })
})

describe('输出窗口的UTF8合并', () => {
  it('重叠页和重复页不重复文字，字节位置不同于中文字符数', () => {
    const middle = mergeOutput(null, page('乙丙', 3))
    expect(mergeOutput(middle, page('甲乙', 0, 9))).toEqual({ text: '甲乙丙', start: 0, end: 9, total: 9 })
    expect(mergeOutput(middle, page('丙丁', 6))).toEqual({ text: '乙丙丁', start: 3, end: 12, total: 12 })
    expect(mergeOutput(middle, page('乙丙', 3))).toEqual(middle)
  })
})
