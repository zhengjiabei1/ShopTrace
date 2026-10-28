const $ = (id) => document.getElementById(id);
const isPreview = document.documentElement.dataset.preview === 'true';
const prevAPI = isPreview ? import('./preview-runtime.js') : null;
const state = { status: null, tasks: [], runs: [], run: null, events: [], annotations: [], index: -1, poll: null, playback: null, selection: 0, view: 'workspace', busy: false, displayedEventKey: null };
const lessMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const labels = {pending:'Queued',running:'Running',completed:'Completed',failed:'Failed',cancelled:'Cancelled',budget_exceeded:'Budget reached',interrupted:'Interrupted',observation:'Observation',model_request:'Model input',model_response:'Model output',action:'Action',environment_result:'Environment response',error:'Error',status:'Status'};
const categories = {evidence:'Evidence check',retrieval:'Search',memory:'Memory',specification:'Option selection',tool:'Tool execution',environment:'Environment',scoring:'Scoring',other:'Other'};
const active = (run) => run && ['pending','running'].includes(run.status);
const pretty = (value) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const fmt = (value, suffix='') => value === null || value === undefined ? 'Unknown' : `${value}${suffix}`;
const bool = (value) => value === true ? 'Yes' : value === false ? 'No' : 'Unknown';
const modeLabel = (run) => run.source === 'imported' ? `${run.mode === 'fixture' ? 'Fixture test' : 'ShopSimulator'} · Imported history` : run.mode === 'fixture' ? 'Fixture test' : 'ShopSimulator';
const srcClass = (run) => run.mode === 'fixture' ? 'amber' : 'green';
function node(tag, text, className) { const el=document.createElement(tag); if(text !== undefined) el.textContent=text; if(className) el.className=className; return el; }
function clear(el) { el.replaceChildren(); return el; }
function notice(message, error=false) { $('notice').textContent=message; $('notice').classList.toggle('error',error); $('notice').hidden=!message; }
async function api(path, options={}) {
  if(isPreview)return (await prevAPI).request(path,options);
  const response=await fetch(path,{...options,headers:{...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
  if(!response.ok) { let detail; try { const body=await response.json(); detail=typeof body.detail==='string'?body.detail:pretty(body.detail || body); } catch { detail=`HTTP ${response.status}`; } throw new Error(detail); }
  return response.status === 204 ? null : response.json();
}
function guarded(fn) { return async (...args)=>{try{await fn(...args);}catch(error){notice(error.message || String(error),true);}}; }
function savePicked(id) { try {localStorage.setItem('shopping-lab.run',id);} catch {} }
function readPicked() { try{return localStorage.getItem('shopping-lab.run');}catch{return null;} }
function shortId(id) { return String(id || '').slice(0,12); }
function date(value) { if(!value)return ''; const d=new Date(value); return Number.isNaN(d.getTime())?String(value):d.toLocaleString('en-US',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}); }
function drawConfig() {
  const real=$('mode').value==='shopsimulator';
  $('fixture-task-field').hidden=real; $('live-task-field').hidden=!real;
  $('mode-description').textContent=real?'Use a preset task and the configured model.':'Check the interface and task flow.';
  $('mode').querySelector('option[value="shopsimulator"]').disabled=isPreview;
  $('provider').querySelector('option[value="api"]').disabled=isPreview;
  if(isPreview)$('mode-description').textContent='This preview runs fixture scripts.';
  $('provider').options[0].disabled=real;
  if(real && $('provider').value==='scripted') $('provider').value='api';
  const scripted=$('provider').value==='scripted';
  $('model-name').readOnly=true;
  if(scripted) $('model-name').value='fixture-script';
  else $('model-name').value=state.status?.model_name || (state.status?.capabilities?.llm_configured?'configured-model':'Not configured');
  $('start-button').textContent=real?'▶ Start run':scripted?'▶ Run fixture test':'▶ Run model on fixture';
  if(isPreview)$('start-button').textContent='▶ Run demo';
  $('start-button').classList.toggle('is-busy', state.busy);
  $('start-button').setAttribute('aria-busy', String(state.busy));
  if(state.busy) $('start-button').textContent='Creating run…';
  const caps=state.status?.capabilities || {};
  let reason='';
  if(real && !caps.shopsimulator_configured) reason='Set SHOPSIM_BASE_URL on the server to connect ShopSimulator.';
  else if(!scripted && !caps.llm_configured) reason='Set LLM_BASE_URL, LLM_MODEL and credentials on the server.';
  $('start-hint').textContent=reason;
  $('start-button').disabled=Boolean(reason)||state.busy;
  if(!state.run){$('mode-badge').textContent=real?'ShopSimulator · preset tasks':'Fixture test';$('mode-badge').className=`badge ${real?'green':'amber'}`;}
  const task=state.tasks.find(t=>String(t.id)===String($('task').value));
  $('task-query').textContent=task?.query || 'No tasks available';
}
function drawTasks() { clear($('task')); for(const task of state.tasks){const option=node('option',task.title || task.id);option.value=task.id;$('task').append(option);} drawConfig(); }
function runOption(run) { return `${run.task_id} · ${run.model?.name || 'Unknown model'} · ${labels[run.status]||run.status} · ${shortId(run.run_id)}`; }
function renderRuns() {
  const list=clear($('run-list'));
  if(!state.runs.length) list.append(node('p','No runs yet','empty-small'));
  for(const run of state.runs){const item=node('button',undefined,`run-item ${state.run?.run_id===run.run_id?'selected':''}`);item.type='button';const line=node('div',undefined,'run-line');const title=node('strong',`${run.task_id} · ${run.model?.name || 'Unknown model'}`);title.title=runOption(run);const status=node('span',labels[run.status]||run.status,'muted');status.prepend(node('i',undefined,`status-dot ${run.status}`));line.append(title,status);item.append(line,node('small',`${run.source==='imported'?'Imported history / ':''}${run.mode==='fixture'?'Fixture test':'Environment run'} · ${date(run.created_at)}`));item.onclick=guarded(()=>selectRun(run.run_id));list.append(item);}
  for(const id of ['compare-left','compare-right']){const select=$(id),previous=select.value;clear(select);for(const run of state.runs){const option=node('option',runOption(run));option.value=run.run_id;select.append(option);}if(state.runs.some(r=>r.run_id===previous))select.value=previous;else if(id==='compare-right'&&state.runs.length>1)select.selectedIndex=1;}
  if(state.runs.length>1 && $('compare-left').value===$('compare-right').value){$('compare-right').value=state.runs.find(run=>run.run_id!==$('compare-left').value).run_id;}
  const evaluation=clear($('evaluation-runs'));
  for(const run of state.runs){const label=node('label',undefined,'evaluation-run');const input=node('input');input.type='checkbox';input.value=run.run_id;input.checked=!active(run);input.disabled=Boolean(active(run));const text=node('span',`${run.task_id} · ${run.model?.name || 'Unknown model'}`);text.append(node('small',`${modeLabel(run)} / ${labels[run.status]||run.status}`));label.append(input,text);evaluation.append(label);}
}
async function loadRuns(){const data=await api('/api/runs');state.runs=data.runs || [];renderRuns();}
function stat(label,value){const el=node('div');el.append(node('span',label,'stat-label'),node('strong',value,'stat-value'));return el;}
function resultLine(label,value){const line=node('div',undefined,'result-line');line.append(node('dt',label),node('dd',value));return line;}
function renderRun(){
  const run=state.run;if(!run)return;
  document.body.dataset.runStatus=run.status;
  $('mode-badge').textContent=modeLabel(run);$('mode-badge').className=`badge ${srcClass(run)}`;
  $('run-subtitle').textContent=`${run.task_id} / ${run.model?.name || 'Unknown model'} / ${shortId(run.run_id)} · ${labels[run.status]||run.status}`;
  $('export-button').disabled=false;$('cancel-button').hidden=!active(run);$('run-summary').hidden=false;
  clear($('run-summary')).append(stat('Steps',fmt(run.steps)),stat('Model calls',fmt(run.model_calls)),stat(isPreview?'Demo time':'Elapsed time',run.elapsed_seconds==null?'Unknown':`${Number(run.elapsed_seconds).toFixed(1)}s`),stat('Status',labels[run.status]||run.status));
  const result=run.result || {},panel=clear($('result-panel')),dl=node('dl');
  dl.append(resultLine('Source',run.source==='imported'?'Imported history':run.mode==='fixture'?'Fixture test':'ShopSimulator'),resultLine('Model stage',(run.model?.stage || 'other').toUpperCase()),resultLine('Environment reward',fmt(result.reward)),resultLine('Reward valid',bool(result.reward_valid)),resultLine('Target match',bool(result.strict_success)),resultLine('Purchase success',bool(result.purchase_success)),resultLine('Input / output tokens',`${fmt(run.usage?.input_tokens)} / ${fmt(run.usage?.output_tokens)}`),resultLine('API cost',isPreview?'No API calls':'Unknown'),resultLine('Stop reason',run.termination_reason || 'Not finished'));
  panel.append(dl);
  if(run.mode==='fixture'){dl.prepend(resultLine('Fixture result',result.fixture_success===true?'Passed':result.fixture_success===false?'Failed':'Not finished'));panel.append(node('p','Fixture results are counted separately.','result-note'));}
  if(run.error)panel.append(node('p',`${run.error.kind || 'Error'}: ${run.error.message || ''}`,'result-note'));
  if(!state.events.length){$('trajectory-empty').hidden=false;$('trajectory-content').hidden=true;}
  drawEvents();
}
function eventText(event){
  const p=event?.payload || {};
  if(event.type==='model_request')return pretty(p.messages || p);
  if(event.type==='model_response')return pretty(p.message || p);
  if(event.type==='action')return `${p.tool_name || 'Tool action'}\n${pretty(p.arguments || {})}${p.env_action?`\n\nEnvironment action: ${p.env_action}`:''}`;
  return p.observation !== undefined ? pretty(p.observation) : p.message !== undefined ? pretty(p.message) : pretty(p);
}
function drawObs(event){
  const target=clear($('observation')),data=event.payload?.state;target.classList.remove('structured-observation');
  if(!data || !['observation','environment_result'].includes(event.type)){target.textContent=eventText(event);return;}
  target.classList.add('structured-observation');
  if(state.run?.query)target.append(node('div',state.run.query,'visible-query'));
  const pageNames={search_home:'Search home',search_results:'Search results',product_detail:'Product details',information_subpage:'Product information',terminal:'Environment finished'};
  target.append(node('div',pageNames[data.page_type] || data.page_type,'state-page-label'));
  const products=data.products || (data.product?[data.product]:[]);
  for(const product of products){const card=node('div',undefined,'product-card');const title=node('div',undefined,'product-title');title.append(node('strong',product.title || 'Unknown product name'),node('span',product.price==null?'Unknown price':state.run?.mode==='fixture'&&state.run?.source!=='imported'?`CNY ${product.price}`:String(product.price),'product-price'));card.append(title,node('div',product.asin || 'Unknown product ID','product-id mono'));if(product.key_attributes?.length){const attrs=node('div',undefined,'attribute-list');for(const attribute of product.key_attributes)attrs.append(node('span',pretty(attribute)));card.append(attrs);}target.append(card);}
  if(data.content)target.append(node('p',pretty(data.content),'state-content'));
  if(data.selected_options && Object.keys(data.selected_options).length)target.append(node('div',`Selected options: ${Object.values(data.selected_options).map(pretty).join(' / ')}`,'state-options'));
  if(data.available_options && Object.keys(data.available_options).length)target.append(node('div',`Available options: ${Object.entries(data.available_options).map(([key,value])=>`${key} ${Array.isArray(value)?value.join(', '):pretty(value)}`).join('; ')}`,'state-options'));
  if(!products.length && data.page_type==='search_home')target.append(node('p','Waiting for the agent to search.','empty-small'));
  if(data.page_type==='terminal')target.append(node('p','See the result panel for rewards and task outcomes.','empty-small'));
}
function drawEvents(){
  const events=state.events;
  if(!events.length){setProg();return;}
  $('trajectory-empty').hidden=true;$('trajectory-content').hidden=false;
  state.index=Math.max(0,Math.min(state.index,events.length-1));const selected=events[state.index];
  $('event-range').max=String(events.length-1);$('event-range').value=String(state.index);$('event-position').textContent=`${state.index+1} / ${events.length}`;
  $('previous-event').disabled=state.index<=0;$('next-event').disabled=state.index>=events.length-1;$('save-annotation').disabled=false;
  const step=selected.payload?.step;
  $('event-label').textContent=`${labels[selected.type]||selected.type}${step===undefined?'':` · Step ${step}`}`;
  drawObs(selected);$('event-payload').textContent=pretty(selected);$('event-count').textContent=`${events.length} events`;
  const eventKey=`${state.run?.run_id || ''}:${selected.event_id}`;
  const newEvent=eventKey!==state.displayedEventKey;
  if(newEvent){
    const obs=$('observation');
    obs.classList.remove('event-transition');
    if(!lessMotion.matches){void obs.offsetWidth;obs.classList.add('event-transition');}
    state.displayedEventKey=eventKey;
  }
  setProg();
  const list=$('event-list'),prevScroll=list.scrollTop;
  clear(list);
  events.forEach((event,index)=>{const item=node('button',undefined,`event-item ${index===state.index?'selected':''}`);item.type='button';item.setAttribute('aria-current',index===state.index?'step':'false');const desc=node('div',undefined,'event-description');desc.append(node('strong',labels[event.type]||event.type),node('p',eventText(event).replace(/\s+/g,' ').slice(0,160)));item.append(node('span',String(event.sequence??index+1).padStart(3,'0'),'event-number mono'),desc,node('span',event.payload?.step===undefined?'':`Step ${event.payload.step}`,'event-time'));item.onclick=()=>{stopPlay();state.index=index;drawEvents();};list.append(item);});
  list.scrollTop=prevScroll;
  if(newEvent)requestAnimationFrame(()=>{
    if(state.displayedEventKey!==eventKey)return;
    const item=list.querySelector('.selected');
    if(!item)return;
    const bounds=list.getBoundingClientRect(),target=item.getBoundingClientRect();
    if(target.top<bounds.top || target.bottom>bounds.bottom){
      list.scrollTo({top:list.scrollTop+target.top-bounds.top-12,behavior:lessMotion.matches?'auto':'smooth'});
    }
  });
  drawNotes();
}
function setProg(){
  const total=state.events.length;
  const position=total?Math.max(0,Math.min(state.index+1,total)):0;
  const percent=total?Math.round(position/total*100):0;
  const progress=$('replay-progress'),label=$('replay-progress-label');
  if(progress){
    progress.style.setProperty('--progress',`${percent}%`);
    progress.setAttribute('aria-valuenow',String(percent));
    progress.setAttribute('aria-valuetext',`Event ${position} / ${total}`);
  }
  if(label)label.textContent=`Event ${position} / ${total} · ${percent}%`;
}
function drawNotes(){const list=clear($('annotation-list')),selected=state.events[state.index];for(const annotation of state.annotations){const card=node('div',undefined,'annotation-card');card.append(node('strong',`${annotation.event_id===selected?.event_id?'Current event · ':''}${categories[annotation.category]||annotation.category} / ${annotation.event_id}`),node('p',annotation.note));list.append(card);} }
function stopPoll(){if(state.poll)clearTimeout(state.poll);state.poll=null;}
function stopPlay(){if(state.playback)clearInterval(state.playback);state.playback=null;document.body.dataset.replaying='false';$('play-events').textContent='▶ Replay';$('play-events').setAttribute('aria-pressed','false');}
async function selectRun(id){
  stopPoll();stopPlay();const generation=++state.selection;
  const [run,log,notes2]=await Promise.all([api(`/api/runs/${encodeURIComponent(id)}`),api(`/api/runs/${encodeURIComponent(id)}/events?after=0`),api(`/api/runs/${encodeURIComponent(id)}/annotations`)]);
  if(generation!==state.selection)return;
  state.run=run;state.events=log.events || [];state.annotations=notes2.annotations || [];state.index=state.events.length-1;savePicked(id);renderRuns();renderRun();
  if(active(run))queuePoll(generation);
}
function queuePoll(generation){stopPoll();state.poll=setTimeout(()=>poll(generation),900);}
async function poll(generation){
  if(generation!==state.selection||!active(state.run))return;
  try{const id=encodeURIComponent(state.run.run_id),after=state.events.length?state.events.at(-1).sequence:0;const [run,log]=await Promise.all([api(`/api/runs/${id}`),api(`/api/runs/${id}/events?after=${after}`)]);if(generation!==state.selection)return;const atEnd=state.index>=state.events.length-1;const known=new Set(state.events.map(e=>e.event_id));state.events.push(...(log.events || []).filter(e=>!known.has(e.event_id)));state.run=run;if(atEnd&&!state.playback)state.index=state.events.length-1;renderRun();if(active(run))queuePoll(generation);else await loadRuns();}catch(error){notice(`Could not load run status: ${error.message}. Select the run to reconnect.`,true);}
}
async function startRun(event){
  event.preventDefault();notice('');state.busy=true;drawConfig();
  try{const mode=$('mode').value;const taskId=mode==='fixture'?$('task').value:$('task-id').value.trim();if(!taskId)throw new Error('Select a task or enter a preset task_id.');const spec={task_id:taskId,mode,model:{provider:$('provider').value,name:$('model-name').value.trim(),stage:$('stage').value},runtime:{max_steps:Number($('max-steps').value),max_seconds:Number($('max-seconds').value),max_calls:Number($('max-calls').value),max_output_tokens:Number($('max-output').value)},strategy:'default'};const run=await api('/api/runs',{method:'POST',body:JSON.stringify(spec)});await loadRuns();await selectRun(run.run_id);}finally{state.busy=false;drawConfig();}
}
async function cancelRun(){if(!state.run)return;await api(`/api/runs/${encodeURIComponent(state.run.run_id)}/cancel`,{method:'POST'});notice(isPreview?'Demo stopped. Completed steps were saved.':'Stop requested. Waiting for the current call and cleanup.');if(!state.poll)queuePoll(state.selection);}
async function importFile(event){const file=event.target.files?.[0];event.target.value='';if(!file)return;if(file.size>10*1024*1024)throw new Error('Trace exceeds 10 MB. Export a smaller single run.');const parsed=JSON.parse(await file.text());const trajectory=parsed && typeof parsed==='object' ? (parsed.trajectory || parsed) : parsed;if(!trajectory || typeof trajectory!=='object' || Array.isArray(trajectory))throw new Error('Trace must be a JSON object. The server validates its format.');const run=await api('/api/import',{method:'POST',body:JSON.stringify({trajectory})});await loadRuns();await selectRun(run.run_id);notice('Trace imported and ready to replay.');}
async function annotate(event){event.preventDefault();const selected=state.events[state.index],note=$('annotation-note').value.trim();if(!state.run||!selected)return;if(!note)throw new Error('Enter a note.');const response=await api(`/api/runs/${encodeURIComponent(state.run.run_id)}/annotations`,{method:'POST',body:JSON.stringify({event_id:selected.event_id,category:$('annotation-category').value,note})});state.annotations=response.annotations || [];if(!response.annotations){const updated=await api(`/api/runs/${encodeURIComponent(state.run.run_id)}/annotations`);state.annotations=updated.annotations || [];}drawNotes();$('annotation-note').value='';notice('Note saved.');}
function showView(view){
  const changed=state.view!==view;
  state.view=view;
  for(const id of ['workspace','comparison','evaluation'])$(id+'-view').hidden=id!==view;
  for(const tab of document.querySelectorAll('[data-view]')){tab.classList.toggle('active',tab.dataset.view===view);tab.setAttribute('aria-current',tab.dataset.view===view?'page':'false');}
  if(changed && !lessMotion.matches){
    const panel=$(view+'-view');
    if(typeof panel.animate==='function')panel.animate([{opacity:0,transform:'translateY(7px)'},{opacity:1,transform:'translateY(0)'}],{duration:220,easing:'cubic-bezier(.2,.7,.3,1)'});
  }
  if(view!=='workspace')stopPlay();
}
async function compare(event){event.preventDefault();const left=$('compare-left').value,right=$('compare-right').value;if(!left||!right||left===right)throw new Error('Select two different runs.');const data=await api(`/api/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);const result=clear($('comparison-result'));const runs=[state.runs.find(r=>r.run_id===left),state.runs.find(r=>r.run_id===right)];if(runs[0]?.task_id!==runs[1]?.task_id || runs[0]?.mode!==runs[1]?.mode)result.append(node('p','These runs use different tasks or environments.','comparison-warning'));else result.append(node('p','Review model, budget and strategy differences. Step numbers may refer to different states.','comparison-warning'));const columns=node('div',undefined,'compare-columns');for(let i=0;i<runs.length;i++){const run=runs[i];const card=node('div',undefined,'compare-card');card.append(node('h3',`${i===0?'A':'B'} · ${run.model?.name || 'Unknown model'}`),node('span',modeLabel(run),`badge ${srcClass(run)}`));const dl=node('dl');dl.append(resultLine('Task',run.task_id),resultLine('Status',labels[run.status]||run.status),resultLine('Steps',fmt(run.steps)),resultLine('Reward',fmt(run.result?.reward)),resultLine('Purchase success',bool(run.result?.purchase_success)));card.append(dl);const config=node('details',undefined,'payload-details');config.append(node('summary','View run settings'),node('pre',pretty({model:run.model,runtime:run.runtime,strategy:run.strategy})));card.append(config);const open=node('button','Inspect trace','button secondary compact');open.onclick=guarded(async()=>{showView('workspace');await selectRun(run.run_id);});card.append(open);columns.append(card);}result.append(columns);if(data.differences?.length){const diffs=node('div',undefined,'configuration-differences');diffs.append(node('h3','Configuration differences'));for(const diff of data.differences)diffs.append(node('p',`${diff.field}: ${pretty(diff.left)} → ${pretty(diff.right)}`));result.append(diffs);}for(const warning of data.warnings || [])result.append(node('p',warning,'field-note'));const details=node('details',undefined,'payload-details');details.append(node('summary','View comparison report'),node('pre',pretty(data)));result.append(details);}
function drawReport(report){
  const target=clear($('evaluation-result'));
  if(!report){target.append(node('p','No evaluation report yet.','empty-small'));return;}
  const cards=node('div',undefined,'report-metrics');
  cards.append(stat('Selected runs',fmt(report.total_runs)),stat('Evaluated runs',fmt(report.formal_run_count)),stat('Fixtures excluded',fmt(report.excluded?.fixture ?? 0)),stat('Imports excluded',fmt(report.excluded?.imported ?? 0)));target.append(cards);
  if(!report.formal_run_count)target.append(node('p','No valid model results to summarize yet.','comparison-warning'));
  for(const group of report.groups || []){const card=node('div',undefined,'compare-card');card.append(node('h3',`${group.model?.name || 'Unknown model'} · ${(group.model?.stage || 'other').toUpperCase()} · ${group.total_started} runs`));const dl=node('dl');const rate=(metric)=>metric?.value==null?'Unknown':`${(metric.value*100).toFixed(1)}%(${metric.numerator}/${metric.denominator}, ${metric.unknown} unknown)`;dl.append(resultLine('Target match rate',rate(group.strict_success)),resultLine('Purchase success rate',rate(group.purchase_success)),resultLine('Execution errors',fmt(group.infrastructure_errors)),resultLine('Mean reward',group.reward?.mean==null?'Unknown':Number(group.reward.mean).toFixed(3)),resultLine('Mean steps',group.steps?.mean==null?'Unknown':Number(group.steps.mean).toFixed(1)),resultLine('API cost','Unknown'));card.append(dl);for(const warning of group.warnings || [])card.append(node('p',warning,'field-note'));target.append(card);}
  const details=node('details',undefined,'payload-details');details.append(node('summary','View full report'),node('pre',pretty(report)));target.append(details);
}
async function loadEval(){drawReport(await api('/api/evaluation'));}
async function evalPicked(){const runIds=[...$('evaluation-runs').querySelectorAll('input:checked')].map(el=>el.value);if(!runIds.length)throw new Error('Select at least one finished run.');drawReport(await api('/api/evaluation',{method:'POST',body:JSON.stringify({run_ids:runIds})}));}

$('run-form').addEventListener('submit',guarded(startRun));
for(const id of ['mode','provider','task'])$(id).addEventListener('change',drawConfig);
$('refresh-runs').onclick=guarded(loadRuns);$('cancel-button').onclick=guarded(cancelRun);
$('export-button').onclick=guarded(async()=>{
  if(!state.run)return;
  const anchor=node('a');
  anchor.download=`${state.run.run_id}.json`;
  if(isPreview){
    const envelope=(await prevAPI).getExport(state.run.run_id);
    const url=URL.createObjectURL(new Blob([JSON.stringify(envelope,null,2)],{type:'application/json'}));
    anchor.href=url;anchor.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }else{anchor.href=`/api/runs/${encodeURIComponent(state.run.run_id)}/export`;anchor.click();}
});
$('import-button').onclick=()=>$('import-file').click();$('import-file').addEventListener('change',guarded(importFile));
$('annotation-form').addEventListener('submit',guarded(annotate));
$('previous-event').onclick=()=>{stopPlay();state.index=Math.max(0,state.index-1);drawEvents();};$('next-event').onclick=()=>{stopPlay();state.index=Math.min(state.events.length-1,state.index+1);drawEvents();};$('event-range').oninput=()=>{stopPlay();state.index=Number($('event-range').value);drawEvents();};
function play(){if(state.playback){stopPlay();return;}if(!state.events.length)return;if(state.index>=state.events.length-1)state.index=0;drawEvents();$('play-events').textContent='Ⅱ Pause';$('play-events').setAttribute('aria-pressed','true');document.body.dataset.replaying='true';state.playback=setInterval(()=>{if(state.index>=state.events.length-1){stopPlay();return;}state.index++;drawEvents();},Number($('play-speed').value));}
$('play-events').onclick=play;$('play-speed').onchange=()=>{if(state.playback){stopPlay();play();}};
for(const tab of document.querySelectorAll('[data-view]'))tab.onclick=guarded(async()=>{showView(tab.dataset.view);if(tab.dataset.view==='evaluation')await loadEval();});
$('compare-form').addEventListener('submit',guarded(compare));$('refresh-evaluation').onclick=guarded(loadEval);$('evaluate-selected').onclick=guarded(evalPicked);
window.addEventListener('beforeunload',()=>{stopPoll();stopPlay();});
async function init(){try{const [status,tasks]=await Promise.all([api('/api/status'),api('/api/tasks')]);state.status=status;state.tasks=tasks.tasks || [];drawTasks();$('connection').textContent=isPreview?'Interactive preview':'Local server connected';$('connection').prepend(node('i'));$('connection').classList.add('connected');$('upstream-info').textContent=isPreview?'Browser fixture demo':`Upstream ${shortId(status.upstream?.commit)} · ${status.upstream?.license_status || 'License unavailable'}`;await loadRuns();const saved=readPicked();if(saved && state.runs.some(r=>r.run_id===saved))await selectRun(saved);if(status.limitations?.length)notice(status.limitations.join('; '));}catch(error){$('connection').textContent=isPreview?'Preview failed to load':'Server disconnected';notice(isPreview?`Preview failed to load: ${error.message}. Refresh the page to retry.`:`Could not connect to the local server: ${error.message}. Start the backend to run tasks.`,true);}}
if(isPreview){
  const notes=document.querySelectorAll('.system-note p');
  if(notes[0])notes[0].textContent='This page demonstrates fixture tasks and traces.';
  if(notes[1])notes[1].textContent='Records, imports and exports stay in this browser.';
  for(const [index,pill] of [...document.querySelectorAll('.empty-pills span')].entries())pill.textContent=['No installation','Runs in browser','Trace replay'][index] || pill.textContent;
}
init();
