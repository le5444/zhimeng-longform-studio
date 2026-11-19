import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readChangeHistory, readHistoryDetail, undoHistoryChange, type ChangeHistoryDetail, type ChangeHistoryPage } from '../../api/changeHistory'
import { RuntimeApiError } from '../../api/runtime'
import { ChangeWorkspace as ChangeHistory } from '../panel/ChangeWorkspace'
import { makeSnapshot } from './fixtures'
import type { ComponentProps } from 'react'

vi.mock('../../api/changeHistory',()=>({readChangeHistory:vi.fn(),readHistoryDetail:vi.fn(),undoHistoryChange:vi.fn()}))
const read=vi.mocked(readChangeHistory),detail=vi.mocked(readHistoryDetail),undo=vi.mocked(undoHistoryChange)
const openFile=vi.fn(),changed=vi.fn()
const record:ChangeHistoryDetail={id:'filechange-one',source:'file_change',session_id:'chat-a',project_id:'project-a',run_id:null,
 title:'导出 C:/results/report.csv',status:'applied',created_at:'2026-11-19T03:00:00Z',
 files:[{path:'C:/results/report.csv',target_path:null,operation:'create'}],undo_available:true,undo_reason:null,review_revision:null,
 previews:[{path:'C:/results/report.csv',target_path:null,operation:'create',current_state:'file',open_path:'C:/results/report.csv',note:null,hunks:[{before:'',after:'Total: 49.50'}]}]}
const projectRecord:ChangeHistoryDetail={...record,id:'changeset-two',source:'change_set',title:'更新预算',review_revision:2}
const page=(changes=[record,projectRecord],scope:'session'|'project'='session'):ChangeHistoryPage=>({session_id:'chat-a',scope,project_id:'project-a',revision:'v1',total:changes.length,changes,next_offset:null})
const project={snapshot:makeSnapshot(),review:{refresh:vi.fn()},onRefreshProject:vi.fn(),notify:vi.fn(),askConfirm:vi.fn(),focusId:null} as unknown as NonNullable<ComponentProps<typeof ChangeHistory>['project']>
function mount(sessionId='chat-a',focusId:string|null=null,focusRequest=0){
 return <><textarea aria-label="草稿" defaultValue="继续核对"/><ChangeHistory project={project} session={{sessionId,focusId,focusRequest,onChanged:changed}} onOpenFile={openFile}/></>
}
beforeEach(()=>{vi.clearAllMocks();read.mockResolvedValue(page());detail.mockImplementation(async(_sid,id)=>id===projectRecord.id?projectRecord:record);undo.mockResolvedValue({} as never)})
afterEach(cleanup)

it('默认按对话列出两类文件，共用详情、打开和撤回操作并保持草稿',async()=>{
 render(mount())
 await screen.findByRole('button',{name:/导出 report.csv/})
 expect(read.mock.calls[0].slice(0,2)).toEqual(['chat-a','session'])
 fireEvent.click(screen.getByRole('button',{name:/导出 report.csv/}))
 await screen.findByText('Total: 49.50')
 fireEvent.click(screen.getByRole('button',{name:'打开 report.csv'}))
 expect(openFile).toHaveBeenCalledWith(record.files[0].path)
 detail.mockResolvedValue({...record,status:'undone',undo_available:false,previews:[{...record.previews[0],current_state:'missing',open_path:null,hunks:[]}]})
 fireEvent.click(screen.getByRole('button',{name:'撤回这次修改'}))
 await screen.findByText('已撤回')
 expect(undo).toHaveBeenCalledWith(record)
 expect(changed).toHaveBeenCalled()
 expect(screen.queryByRole('button',{name:'打开 report.csv'})).toBeNull()
 expect(screen.getByRole('textbox')).toHaveValue('继续核对')
 fireEvent.click(screen.getByRole('button',{name:'全部修改'}))
 expect(screen.getByRole('button',{name:'当前对话'})).toHaveAttribute('aria-pressed','true')
})

it('项目修改与文件journal使用同样详情，不要求当前项目重新打开',async()=>{
 render(<ChangeHistory project={null} session={{sessionId:'chat-a',focusId:projectRecord.id,onChanged:changed}} onOpenFile={openFile}/>)
 await screen.findByText('Total: 49.50')
 fireEvent.click(screen.getByRole('button',{name:'撤回这次修改'}))
 await waitFor(()=>expect(undo).toHaveBeenCalledWith(projectRecord))
})

it('切换范围取消迟到请求，整个项目的记录不会污染当前对话',async()=>{
 let finish!:(v:ChangeHistoryPage)=>void
 read.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve}))
 render(mount())
 const signal=read.mock.calls[0][4]!.signal!
 read.mockResolvedValue(page([{...projectRecord,session_id:'another'}],'project'))
 fireEvent.click(screen.getByRole('button',{name:'整个项目'}))
 await screen.findByRole('button',{name:/更新预算/})
 expect(signal.aborted).toBe(true)
 await act(async()=>finish(page()))
 expect(screen.queryByRole('button',{name:/导出 report.csv/})).toBeNull()
 expect(read.mock.calls.at(-1)?.slice(0,2)).toEqual(['chat-a','project'])
})

it('撤回遇到外部改动时保留错误并刷新磁盘状态',async()=>{
 render(mount('chat-a',record.id))
 await screen.findByText('Total: 49.50')
 undo.mockRejectedValue(new Error('文件已由其他程序修改'))
 detail.mockResolvedValue({...record,undo_available:false,undo_reason:'文件已改变，请刷新核对'})
 fireEvent.click(screen.getByRole('button',{name:'撤回这次修改'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('其他程序修改')
 await screen.findByText('文件已改变，请刷新核对')
 expect(screen.queryByRole('button',{name:'撤回这次修改'})).toBeNull()
})

it('列表网络失败不冒充空记录，分页变化后刷新再继续',async()=>{
 read.mockRejectedValueOnce(new Error('连接失败'))
 render(mount())
 await screen.findByRole('alert')
 expect(screen.queryByText('还没有文件修改')).toBeNull()
 read.mockResolvedValueOnce({...page([record]),total:2,next_offset:1})
 fireEvent.click(screen.getByRole('button',{name:'重试'}))
 await screen.findByRole('button',{name:'显示更多修改'})
 read.mockRejectedValueOnce(new RuntimeApiError(409,{error:{message:'记录已变化'}}))
 fireEvent.click(screen.getByRole('button',{name:'显示更多修改'}))
 await screen.findByRole('alert')
 expect(screen.getByRole('button',{name:'显示更多修改'})).toBeDisabled()
 read.mockResolvedValueOnce(page())
 fireEvent.click(screen.getByRole('button',{name:'刷新修改记录'}))
 await screen.findByRole('button',{name:/更新预算/})
 expect(within(screen.getByRole('list')).getAllByRole('button')).toHaveLength(2)
})

it('新会话同步清除旧详情和撤回操作，重开同一记录仍定位',async()=>{
 const view=render(mount('chat-a',record.id,1))
 await screen.findByText('Total: 49.50')
 fireEvent.click(screen.getByRole('button',{name:'全部修改'}))
 view.rerender(mount('chat-a',record.id,2))
 await screen.findByText('Total: 49.50')
 read.mockResolvedValue({...page([]),session_id:'chat-b'})
 view.rerender(mount('chat-b'))
 expect(screen.queryByText('Total: 49.50')).toBeNull()
 expect(screen.queryByRole('button',{name:'撤回这次修改'})).toBeNull()
 await screen.findByText('还没有文件修改')
})
