import { createServer } from 'node:http'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { writeFile, rename, unlink } from 'node:fs/promises'
import path from 'node:path'

export async function startBridge(host,directory) {
  const token=randomBytes(32).toString('hex'), file=path.join(directory,'browser-bridge.json')
  const server=createServer(async(req,res)=>{
    res.setHeader('Content-Type','application/json; charset=utf-8');res.setHeader('Cache-Control','no-store')
    try {
      const supplied=Buffer.from(req.headers.authorization?.replace(/^Bearer /,'')||'')
      if(req.headers.origin||supplied.length!==token.length||!timingSafeEqual(supplied,Buffer.from(token))){res.writeHead(401);res.end(JSON.stringify({error:'需要本机浏览器连接凭据。'}));return}
      const url=new URL(req.url,'http://127.0.0.1')
      let body={}
      if(req.method==='POST'){
        let size=0,parts=[]
        for await(const chunk of req){size+=chunk.length;if(size>1024*1024)throw new TypeError('单次请求过大。');parts.push(chunk)}
        body=JSON.parse(Buffer.concat(parts).toString()||'{}')
      }
      const match=url.pathname.match(/^\/sessions\/([^/]+)\/tabs(?:\/([^/]+)(?:\/(observe|action|screenshot))?)?$/)
      const receipt=url.pathname.match(/^\/sessions\/([^/]+)\/requests\/([^/]+)$/)
      const cancel=url.pathname.match(/^\/sessions\/([^/]+)\/cancel$/)
      let result
      if(req.method==='GET'&&url.pathname==='/status')result={status:'ready',pid:process.pid}
      else if(req.method==='GET'&&receipt)result=host.ledger.get(decodeURIComponent(receipt[1]),decodeURIComponent(receipt[2]))||undefined
      else if(req.method==='POST'&&cancel)result=host.cancel(decodeURIComponent(cancel[1]))
      else if(match){
        const sessionId=decodeURIComponent(match[1]),id=match[2]&&decodeURIComponent(match[2]),verb=match[3]
        if(req.method==='GET'&&!id)result=host.state(sessionId)
        else if(req.method==='POST'&&(!id||verb==='action')){
          let disconnected=false
          res.once('close',()=>{if(!res.writableEnded){disconnected=true;host.cancel(sessionId)}})
          const saved=await host.ledger.execute(sessionId,body.request_id,{id,verb,body},async()=>{
            if(disconnected){const error=new Error('连接已中断，尚未开始的浏览器动作已取消。');error.code='browser_cancelled';throw error}
            if(id)return host.action(sessionId,id,body)
            const tab=host.create(sessionId,body.url);tab.control.transfer('agent');host.changed();return host.summary(tab)
          })
          if(saved.status==='failed'){const error=new Error(saved.error.message);Object.assign(error,saved.error);throw error}
          if(saved.status!=='completed'){const error=new Error('先前操作的结果尚未确认，请重新观察页面核对，原操作不会重放。');error.status=409;error.code='action_uncertain';throw error}
          result={...saved.result,request_id:saved.request_id}
        }
        else if(req.method==='POST'&&verb==='observe')result=await host.observe(sessionId,id)
        else if(req.method==='POST'&&verb==='screenshot'){
          const tab=host.get(sessionId,id)
          res.once('close',()=>{if(!res.writableEnded)host.capture.cancel(tab,'截图连接已中断，页面仍保留。')})
          result={mime_type:'image/png',data:await host.screenshot(sessionId,id)}
        }
      }
      if(result===undefined){res.writeHead(404);result={error:'找不到浏览器接口。'}}
      res.end(JSON.stringify(result))
    } catch(error){res.writeHead(error.status||400);res.end(JSON.stringify({code:error.code||'browser_error',error:error.message}))}
  })
  server.requestTimeout=30000
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve)})
  await writeFile(file+'.tmp',JSON.stringify({version:1,url:`http://127.0.0.1:${server.address().port}`,token,pid:process.pid}),{mode:0o600})
  await rename(file+'.tmp',file)
  return async()=>{server.close();await unlink(file).catch(error=>{if(error.code!=='ENOENT')throw error})}
}
