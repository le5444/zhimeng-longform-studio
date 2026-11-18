import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import Terminal from '../panel/Terminal'
import { inputCommandSession, readCommandSession, resizeCommandSession, stopCommandSession, type CommandSessionOutput, type CommandSessionSummary } from '../../api/commandSessions'

const mocks = vi.hoisted(() => ({ dimensions: { rows:30, cols:100 }, resizeNotice: () => {}, sizes: [] as number[][],
  terminals: [] as Array<{ onInput: (s:string)=>void; onSize:(s:{rows:number;cols:number})=>void; writes:string[]; disposed:boolean }> }))
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  options = { disableStdin: true }
  parser = { registerCsiHandler: () => ({ dispose() {} }) }
  resize(cols:number,rows:number) { mocks.sizes.push([cols,rows]); if (!Number.isInteger(cols) || !Number.isInteger(rows)) throw new Error('invalid dimensions') }
  state = { onInput: (_s:string)=>{}, onSize: (_s:{rows:number;cols:number})=>{}, writes: [] as string[], disposed: false }
  constructor() { mocks.terminals.push(this.state) }
  loadAddon() {}
  open() {}
  onData(callback:(s:string)=>void) { this.state.onInput=callback; return {dispose(){}} }
  onResize(callback:(s:{rows:number;cols:number})=>void) { this.state.onSize=callback; return {dispose(){}} }
  write(text:string, callback:()=>void) { this.state.writes.push(text); this.state.onInput('\x1b[1;1R'); callback() }
  dispose() { this.state.disposed=true }
} }))
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { proposeDimensions() { return mocks.dimensions } } }))
vi.mock('../../api/commandSessions', () => ({ inputCommandSession:vi.fn(), readCommandSession:vi.fn(), resizeCommandSession:vi.fn(), stopCommandSession:vi.fn() }))
const summary:CommandSessionSummary={ command_session_id:'cmd-A',run_id:'run-A',command:'python interactive.py',cwd:'C:/temp',status:'running',exit_code:null,created_at:1,ended_at:null,duration_ms:100,stdout_bytes:0,stderr_bytes:0,timed_out:false,interruption_reason:null,transport:'pty',terminal_columns:80,terminal_rows:24 }
const output=(text='ready', end=5, more=false):CommandSessionOutput=>({...summary,stdout:{text,start_offset:0,next_offset:end,total_bytes:more?end+5:end,truncated_before:false,has_more:more},stderr:{text:'',start_offset:0,next_offset:0,total_bytes:0,truncated_before:false,has_more:false}})
const observed=vi.fn()
beforeEach(()=>{
  vi.resetAllMocks(); mocks.terminals.length=0
  mocks.dimensions={rows:30,cols:100}; mocks.sizes=[]
  vi.stubGlobal('ResizeObserver',class { constructor(callback:()=>void) { mocks.resizeNotice=callback } observe(){} disconnect(){} })
  vi.mocked(readCommandSession).mockResolvedValue(output())
  vi.mocked(inputCommandSession).mockResolvedValue(summary)
  vi.mocked(resizeCommandSession).mockResolvedValue(summary)
  vi.mocked(stopCommandSession).mockResolvedValue({...summary,status:'cancelled'})
})
afterEach(()=>{cleanup();vi.unstubAllGlobals()})
const mount=(sessionId='A')=><Terminal key={sessionId} sessionId={sessionId} summary={summary} active onObserved={observed}/>

it('replays full history without sending old terminal replies, then sends typed Unicode once',async()=>{
  vi.mocked(readCommandSession).mockResolvedValueOnce(output('first',5,true)).mockResolvedValueOnce(output('second',10))
  render(mount())
  await screen.findByText(/点击终端后可输入/)
  expect(mocks.terminals[0].writes).toEqual(['first','second'])
  expect(vi.mocked(readCommandSession).mock.calls[1][2]?.stdout_offset).toBe(5)
  expect(inputCommandSession).not.toHaveBeenCalled()
  act(()=>mocks.terminals[0].onInput('中文\r'))
  await waitFor(()=>expect(inputCommandSession).toHaveBeenCalledTimes(1))
  expect(vi.mocked(inputCommandSession).mock.calls[0].slice(0,3)).toEqual(['A','cmd-A','中文\r'])
})

it('keeps uncertain input visible and never retries or sends queued keys automatically',async()=>{
  vi.mocked(inputCommandSession).mockRejectedValue(new Error('connection lost'))
  render(mount())
  await screen.findByText(/点击终端后可输入/)
  act(()=>{mocks.terminals[0].onInput('first');mocks.terminals[0].onInput('second')})
  await screen.findByRole('alert')
  expect(inputCommandSession).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button',{name:'Ctrl+C'})).toBeDisabled()
  fireEvent.click(screen.getByRole('button',{name:'重新连接终端'}))
  await screen.findByText(/点击终端后可输入/)
  expect(inputCommandSession).toHaveBeenCalledTimes(1)
})

it('ignores an old session read and detaches its keyboard before another session opens',async()=>{
  let finish!:(v:CommandSessionOutput)=>void
  vi.mocked(readCommandSession).mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
  const {rerender}=render(mount('A'))
  const old=mocks.terminals[0]
  rerender(mount('B'))
  await screen.findByText(/点击终端后可输入/)
  await act(async()=>finish(output('stale-A')))
  expect(old.disposed).toBe(true)
  expect(old.writes).not.toContain('stale-A')
  act(()=>old.onInput('stale key'))
  expect(inputCommandSession).not.toHaveBeenCalled()
  await waitFor(()=>expect(resizeCommandSession).toHaveBeenCalledTimes(1))
  expect(vi.mocked(resizeCommandSession).mock.calls[0].slice(0,4)).toEqual(['B','cmd-A',30,100])
})

it('sends Ctrl+C as terminal input and separately stops the command',async()=>{
  render(mount())
  await screen.findByText(/点击终端后可输入/)
  fireEvent.click(screen.getByRole('button',{name:'Ctrl+C'}))
  await waitFor(()=>expect(inputCommandSession).toHaveBeenCalledTimes(1))
  expect(vi.mocked(inputCommandSession).mock.calls[0][2]).toBe('\x03')
  fireEvent.click(screen.getByRole('button',{name:'停止命令'}))
  await waitFor(()=>expect(stopCommandSession).toHaveBeenCalledWith('A','cmd-A'))
})

it.each(['running','exited'] as const)('ignores hidden terminal measurements and fits again when visible (%s)',async status=>{
  mocks.dimensions={rows:NaN,cols:NaN}
  vi.mocked(readCommandSession).mockResolvedValue({...output(),status,exit_code:status==='exited'?0:null})
  render(mount())
  await waitFor(()=>expect(mocks.terminals[0].writes).toHaveLength(1))
  expect(resizeCommandSession).not.toHaveBeenCalled()
  expect(mocks.sizes).toEqual([])
  mocks.dimensions={rows:30,cols:100}
  act(()=>mocks.resizeNotice())
  if (status==='running') await waitFor(()=>expect(resizeCommandSession).toHaveBeenCalledTimes(1))
  else await waitFor(()=>expect(mocks.sizes).toEqual([[100,30]]))
})
