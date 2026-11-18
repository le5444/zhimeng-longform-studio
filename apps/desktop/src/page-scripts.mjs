// Isolated world 42 has DOM access, without the page's ability to replace this map.
export function observationScript(identity) {
  return `(() => {
    const visible = element => { const rect=element.getBoundingClientRect(); const css=getComputedStyle(element); return rect.width>0 && rect.height>0 && css.visibility!=='hidden' && css.display!=='none' };
    const nodes = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[contenteditable="true"],iframe')].filter(visible);
    const refs = new Map(nodes.map((element,index)=>[String(index+1),element]));
    globalThis.zhimengObservation = {id:${JSON.stringify(identity)},refs};
    const elements=nodes.map((element,index)=>({ref:String(index+1),tag:element.tagName.toLowerCase(),role:element.getAttribute('role'),
      name:element.getAttribute('aria-label') || element.getAttribute('placeholder') || element.innerText || element.getAttribute('title') || '',
      value:element.type==='password'?null:('value' in element?element.value:null),href:element.href||element.src||null,disabled:Boolean(element.disabled)}));
    return {title:document.title,url:location.href,text:document.body?.innerText||'',elements,viewport:{width:innerWidth,height:innerHeight,scrollX,scrollY}};
  })()`
}

export function actionScript(request) {
  return `(() => {
    const request=${JSON.stringify(request)};
    const observation=globalThis.zhimengObservation;
    if(!observation || observation.id!==request.observation_id) throw new Error('页面观察已失效，请重新观察。');
    const element=request.ref?observation.refs.get(request.ref):null;
    if(request.ref && (!element || !element.isConnected)) throw new Error('原页面元素已移除，请重新观察。');
    globalThis.zhimengObservation=null;
    if(request.action==='click') { if(!element) throw new Error('点击需要元素编号。'); element.focus(); element.click(); }
    else if(request.action==='fill') {
      if(!element) throw new Error('输入需要元素编号。'); element.focus();
      if(element.isContentEditable) { element.textContent=request.text; }
      else {
        const prototype=element instanceof HTMLTextAreaElement?HTMLTextAreaElement.prototype:element instanceof HTMLInputElement?HTMLInputElement.prototype:HTMLSelectElement.prototype;
        const setter=Object.getOwnPropertyDescriptor(prototype,'value')?.set;
        if(!setter) throw new Error('这个元素不能输入文字。'); setter.call(element,request.text);
      }
      element.dispatchEvent(new Event('input',{bubbles:true})); element.dispatchEvent(new Event('change',{bubbles:true}));
    } else if(request.action==='scroll') { (element||window).scrollBy({left:request.x||0,top:request.y||0,behavior:'instant'}); }
    else throw new Error('未知页面动作。');
    return {url:location.href};
  })()`
}
