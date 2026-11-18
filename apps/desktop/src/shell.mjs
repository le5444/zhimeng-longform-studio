const bridge=window.zhimengBrowser
const find=id=>document.getElementById(id)
let state={tabs:[],active:null,downloads:[]}, editing=false
const current=()=>state.tabs.find(t=>t.id===state.active)
async function command(request){try{if(request.action!=='layout')find('error').hidden=true;return await bridge.command(request)}catch(error){find('error').textContent=error.message;find('error').hidden=false}}
function layout(){const r=find('page').getBoundingClientRect();void command({action:'layout',bounds:{x:r.x,y:r.y,width:r.width,height:r.height}})}
function render(next){
  state=next;const tab=current(), tabs=find('tabs')
  tabs.replaceChildren(...state.tabs.map(t=>{const button=document.createElement('button');button.textContent=t.title;button.title=t.url;button.setAttribute('aria-pressed',String(t.id===state.active));button.onclick=()=>void command({action:'select',session_id:t.session_id,id:t.id});return button}))
  if(!editing)find('url').value=tab?.url==='about:blank'?'':tab?.url||''
  find('back').disabled=!tab?.can_back;find('forward').disabled=!tab?.can_forward;find('reload').disabled=!tab
  find('control').disabled=!tab;find('control').textContent=tab?.mode==='agent'?'我来操作':'交给 Agent'
  find('close').disabled=!tab;find('page').querySelector('.empty').hidden=Boolean(tab)
  find('notice').textContent=tab?(tab.mode==='agent'?'Agent 可操作此页。点击「我来操作」或直接操作页面即可接手。':'你正在操作此页。交给 Agent 后会重新观察当前页面。')+(tab.restored?' 重开时已恢复网址，未提交表单未恢复。':''):'用户与 Agent 操作同一个页面。'
  const error=tab?.error||state.error;if(error){find('error').textContent=error;find('error').hidden=false}
  find('downloads').querySelector('ul').replaceChildren(...state.downloads.map(d=>{const li=document.createElement('li');li.textContent=`${d.name} · ${d.status==='completed'?'已保存':d.status==='progressing'?'下载中':d.status} · ${d.bytes} 字节${d.status==='completed'?' · '+d.path:''}`;return li}))
  requestAnimationFrame(layout)
}
find('new').onclick=()=>void command({action:'create',session_id:current()?.session_id||'browser-lab',url:'about:blank'})
find('url').onfocus=()=>{editing=true};find('url').onblur=()=>{editing=false}
find('address').onsubmit=async event=>{event.preventDefault();const tab=current(),url=find('url').value;editing=false;if(tab)await command({action:'navigate',session_id:tab.session_id,id:tab.id,url});else await command({action:'create',session_id:'browser-lab',url})}
for(const action of ['back','forward','reload'])find(action).onclick=()=>{const t=current();if(t)void command({action,session_id:t.session_id,id:t.id})}
find('control').onclick=()=>{const t=current();if(t)void command({action:'control',session_id:t.session_id,id:t.id,mode:t.mode==='agent'?'user':'agent'})}
find('close').onclick=()=>{const t=current();if(t)void command({action:'close',session_id:t.session_id,id:t.id})}
find('downloads').ontoggle=layout
new ResizeObserver(layout).observe(find('page'))
bridge.onChange(render);bridge.onLayout(layout);render(await bridge.state())
