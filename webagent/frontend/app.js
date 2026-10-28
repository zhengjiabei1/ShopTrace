const $ = id => document.getElementById(id);
const preview = document.documentElement.dataset.preview === 'true';
const activeSet = new Set(['pending', 'running', 'queued', 'cancelling']);
const statLabels = {pending: 'Queued', queued: 'Queued', running: 'Running', completed: 'Completed', finished: 'Completed', failed: 'Failed', cancelled: 'Stopped', budget_exceeded: 'Budget reached', interrupted: 'Interrupted'};
const actLabels = {click: 'Click element', fill: 'Fill input', select: 'Select option', select_option: 'Select option', press: 'Press key', scroll: 'Scroll page', back: 'Go back', wait: 'Wait', finish: 'Finish task'};
const state = {group: 'original', split: 'development', shot: '', shotKey: 0, shotDone: false, zoom: null, drag: null, status: null, tasks: [], runs: [], run: null, events: [], frames: [], selectedTask: null, selectedFrame: 0, selectedSequence: null, token: 0, poll: null, playing: null, creating: false, signature: '', previewEvents: new Map()};
const lessMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const taskNames = {
  'browser-headphones-commute': 'Commuter headphones', 'browser-thermos-variant': 'Thermos options',
  'browser-powerbank-spec': 'Power bank specifications', 'browser-lamp-adjustable': 'Reading lamp',
  'browser-thermos-budget': 'No suitable item', 'browser-headphones-unknown': 'Missing connection details'
};
const taskNeeds = {
  'browser-headphones-commute': 'Find black wireless headphones at most CNY 300. Check the details and leave only that item in the cart.',
  'browser-thermos-variant': 'Find a blue 500ml thermos with a final price at most CNY 100. Select the options and check the cart.',
  'browser-powerbank-spec': 'Find a black 10000mAh power bank with USB-C, at least 20W output, and a price at most CNY 150. Check the cart.',
  'browser-lamp-adjustable': 'Find a dimmable warm-light USB lamp at most CNY 150. Check its details and the final cart.',
  'browser-thermos-budget': 'Filter thermoses to a maximum of CNY 30. Look for a red 500ml model; if none match, keep the cart empty and stop.',
  'browser-headphones-unknown': 'Check whether grey headphones at most CNY 280 support wireless connections. If the details are missing, keep the uncertainty and leave the cart empty.'
};
function taskTitle(task) {return taskNames[task?.id] || task?.title || task?.id || 'Task';}
function taskGroup(task) {return task?.group || (String(task?.id || '').startsWith('procure-') ? 'procure' : String(task?.id || '').startsWith('case-') ? 'expanded' : 'original');}
function isProc(task = pickedTask()) {return taskGroup(task) === 'procure';}
function needsAPI(task = pickedTask()) {return !!task && taskGroup(task) !== 'original';}
function taskQuery(task) {return taskNeeds[task?.id] || task?.query || 'Select a task to view its requirements.';}

function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
function empty(node) { node.replaceChildren(); return node; }
function active(run = state.run) { return !!run && activeSet.has(run.status); }
function isModel(run) { return run?.source === 'browser_model' || run?.model?.provider === 'api' || run?.provider === 'api'; }
function srcLabel(run) { return isModel(run) ? 'Model' : 'Script'; }
function taskFor(id) { return state.tasks.find(t => String(t.id) === String(id)); }
function pickedTask() { return taskFor(state.selectedTask); }
function findRecord(id) {
  return state.runs.filter(run => String(run.task_id) === String(id))
    .sort((left, right) => (Date.parse(right.created_at || right.completed_at) || 0) - (Date.parse(left.created_at || left.completed_at) || 0))[0];
}
function errMsg(error) { return error?.message || 'This action could not be completed. Try again.'; }
function showError(message) { $('global-error').textContent = message || ''; $('global-error').hidden = !message; }
function showError2(error) { showError(errMsg(error)); }
function guard(fn) { return async (...args) => { try { await fn(...args); } catch (error) { showError2(error); } }; }
function stopPoll() { clearTimeout(state.poll); state.poll = null; }
function stopPlay() { clearInterval(state.playing); state.playing = null; $('play-button').textContent = '▶ Replay'; }
function readReason(raw, run) {
  if (!raw) return 'Use the replay slider to inspect each page and action.';
  const kind = run?.result?.outcome_kind;
  if (kind === 'cart_verified') return 'The cart item and selected options meet the task requirements.';
  if (kind === 'uncertainty_reported') return 'Missing information was left unknown, and the cart remained empty.';
  if (kind === 'no_suitable_product') return 'The filters returned no suitable item, and the cart remained empty.';
  if (run?.status === 'cancelled') return 'The run has stopped. Saved events are available for replay.';
  if (run?.status === 'budget_exceeded') return 'The run stopped at its budget limit.';
  if (raw === 'requires_human_review') return 'Custom tasks need review; the outcome remains unknown.';
  if (raw === 'not_finished') return 'The task did not reach a known outcome.';
  if (raw === 'agent_finished') return 'The run ended. Review its page and result.';
  return /[\u3400-\u9fff]/.test(raw) ? 'Open the original record for details.' : raw;
}

async function jsonFetch(url, options = {}) {
  const response = await fetch(url, {...options, headers: {'Content-Type': 'application/json', ...options.headers}});
  let data;
  try { data = await response.json(); } catch { throw new Error(`The service returned an unreadable response (HTTP ${response.status}).`); }
  if (!response.ok) {
    const detail = data.detail || data.message || data.error;
    const text = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map(item => item.msg || 'Invalid parameter').join('; ') : null;
    throw new Error(text || `Request failed (HTTP ${response.status}).`);
  }
  return data;
}

function shotURL(value) {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value, window.location.href);
    return url.origin === location.origin && ['http:', 'https:'].includes(url.protocol) ? url.href : null;
  } catch { return null; }
}
function getObs(event) {
  const payload = event.payload || {};
  const obs = payload.observation || payload.snapshot;
  if (!obs || typeof obs !== 'object' || Array.isArray(obs)) return null;
  return {event, observation:obs, step: payload.step ?? obs.step ?? 0,
    screenshot: shotURL(payload.screenshot_url || obs.screenshot_url || obs.screenshot || payload.screenshot)};
}
function getAction(event) {
  const payload = event?.payload || {};
  if (payload.action && typeof payload.action === 'object') return payload.action;
  if (event?.type === 'action' && (payload.type || payload.tool_name)) return payload;
  return null;
}
function actionType(action) { return action?.type || action?.name || action?.tool_name || 'action'; }
function briefAct(action) {
  if (!action) return '';
  const type = actionType(action), parts = [];
  if (action.ref) parts.push(`Element ${action.ref}`);
  if (action.text !== undefined) parts.push(`Fill: ${String(action.text).slice(0, 130)}`);
  if (action.value !== undefined) parts.push(`Option: ${String(action.value).slice(0, 100)}`);
  if (action.key) parts.push(`Key ${action.key}`);
  if (action.direction) parts.push(action.direction === 'down' ? 'Scroll down' : 'Scroll up');
  if (action.milliseconds) parts.push(`Wait ${action.milliseconds} ms`);
  if (action.reason) parts.push(String(action.reason));
  return parts.join(' · ') || actLabels[type] || type;
}

function groupTasks() {
  return state.tasks.filter(task => taskGroup(task) === state.group && (state.group === 'original' || (task.split || 'development') === state.split));
}
function drawTasks() {
  const list = empty($('task-list')), tasks = groupTasks();
  $('task-count').textContent = `${tasks.length} tasks`;
  $('split-field').hidden = state.group === 'original';
  $('task-split').value = state.split;
  $('group-note').textContent = {original: 'Single-item tasks and information checks.', expanded: 'New catalogs with varied requirements.', procure: 'Compare details across pages and build a complete cart.'}[state.group];
  for (const button of $('task-groups').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.group === state.group));
    button.disabled = active();
  }
  if (!tasks.length) list.append(el('p', 'No tasks are available in this set.', 'muted loading-note'));
  tasks.forEach((task, index) => {
    const button = el('button', null, 'task-card'); button.type = 'button'; button.dataset.taskId = task.id;
    const icon = el('span', taskIcon(task, index), 'task-icon'); icon.setAttribute('aria-hidden', 'true');
    const hasRecord = !preview || !!findRecord(task.id);
    const body = el('span'); body.append(el('strong', taskTitle(task)), el('small', preview ? hasRecord ? 'View saved trace' : 'No saved trace' : diffLabel(task.difficulty)));
    button.append(icon, body, el('span', '✓', 'task-check'));
    button.onclick = guard(() => chooseTask(task.id));
    list.append(button);
  });
  markTask();
}
function taskIcon(task, index) {
  const text = `${task.title || ''} ${task.id || ''}`;
  if (/\u8033\u673a|headphone/.test(text)) return '◖';
  if (/\u676f|thermos/.test(text)) return '▥';
  if (/\u7535\u6e90|power|\u7535\u6c60/.test(text)) return '▯';
  if (/\u706f|lamp/.test(text)) return '⌁';
  if (/\u65e0|\u6ca1\u6709|\u4e0d\u8db3|unknown/.test(text)) return '⌕';
  return ['⌕', '▤', '◇', '▥', '⌁', '⊞'][index % 6];
}
function diffLabel(value) {
  const values = {easy: 'Easy · browse and act', medium: 'Medium · check requirements', hard: 'Hard · multi-step reasoning', basic: 'Easy · browse and act'};
  return values[value] || (typeof value === 'string' && value ? value : 'Browser task');
}
async function chooseTask(id) {
  if (preview) {
    const run = findRecord(id);
    if (run) { showError(''); await loadRun(run.run_id); }
    return;
  }
  const before = taskGroup(pickedTask());
  state.selectedTask = String(id);
  const task = pickedTask();
  if (before !== taskGroup(task)) setBudget(task);
  if (needsAPI(task)) document.querySelector('input[name=provider][value=api]').checked = true;
  markTask();
  showError('');
}
function setBudget(task) {
  $('max-steps').value = isProc(task) ? '70' : '35';
  $('max-seconds').value = '180';
  $('context-mode').value = isProc(task) ? 'page_memory' : 'full_history';
  $('run-fault').value = 'none'; $('run-recovery').value = 'verify'; $('run-seed').value = '7';
}
async function pickGroup(group, split = state.split) {
  if (active()) return;
  state.group = group; state.split = split;
  const task = groupTasks()[0];
  if (task) await chooseTask(task.id); else state.selectedTask = null;
  drawTasks();
}
function markTask() {
  for (const button of $('task-list').querySelectorAll('button')) {
    const selected = button.dataset.taskId === String(state.selectedTask);
    button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected));
    button.disabled = preview ? !findRecord(button.dataset.taskId) : active();
    button.querySelector('.task-check').hidden = !selected;
  }
  const task = pickedTask();
  $('task-query').textContent = task ? taskQuery(task) : 'Select a task to view its requirements.';
  $('sandbox-link').hidden = false;
  $('sandbox-link').textContent = preview ? 'Open task page ↗' : 'Open store ↗';
  const path = typeof task?.start_path === 'string' && task.start_path.startsWith('/sandbox') ? task.start_path : '/sandbox/';
  $('sandbox-link').href = path;
  drawCtrls();
}

function drawStatus() {
  const caps = state.status?.capabilities || {};
  const hasBrowser = caps.browser_available === true;
  const engine = $('engine-state'); engine.className = `engine-state ${preview || hasBrowser ? 'ready' : 'unavailable'}`;
  engine.querySelector('span').textContent = preview ? 'Trace replay' : hasBrowser ? 'Chromium ready' : 'Browser unavailable';
  const configured = caps.llm_configured === true;
  const api = document.querySelector('input[name=provider][value=api]');
  api.disabled = preview || !configured || !hasBrowser;
  $('provider-api-label').setAttribute('aria-disabled', String(api.disabled));
  $('model-note').textContent = preview ? 'View saved traces.' : configured
    ? `Model: ${state.status.model_name || 'Configured model'}.`
    : 'Configure a model or try the script test.';
  if (!configured && api.checked && !needsAPI()) document.querySelector('input[name=provider][value=browser_smoke]').checked = true;
  drawCtrls();
}
function provider() { return document.querySelector('input[name=provider]:checked')?.value || 'browser_smoke'; }
function drawCtrls() {
  const caps = state.status?.capabilities || {}, running = active();
  const apiMode = provider() === 'api', onlyAPI = needsAPI();
  $('start-button').disabled = preview || state.creating || running || !pickedTask() || caps.browser_available !== true || (apiMode && caps.llm_configured !== true) || (onlyAPI && !apiMode);
  $('start-label').textContent = preview ? 'Trace replay' : state.creating ? 'Creating run…' : running ? 'Browser running…' : apiMode ? 'Run model' : 'Run script test';
  $('start-hint').textContent = preview ? 'Select a saved run to replay.' : onlyAPI && !caps.llm_configured ? 'Configure a model on the server to run this task.' : apiMode ? 'Server credentials are used; actions and results are saved.' : 'Test the browser with a fixed action sequence.';
  $('cancel-button').hidden = preview || !running;
  $('export-button').disabled = !state.run;
  $('export-inline').disabled = !state.run;
  for (const input of $('provider-field').querySelectorAll('input')) {
    input.disabled = preview || running || (input.value === 'browser_smoke' && onlyAPI) || (input.value === 'api' && (caps.llm_configured !== true || caps.browser_available !== true));
  }
  for (const input of $('budget-details').querySelectorAll('input, select')) input.disabled = preview || running;
  $('procure-fields').hidden = !isProc();
  $('context-note').textContent = {full_history: 'Keep earlier page observations and actions.', page_memory: 'Keep facts from visited pages with their source steps.', latest_observation: 'Use the current page and an action summary.'}[$('context-mode').value];
  $('task-split').disabled = running;
  for (const button of $('task-groups').querySelectorAll('button')) button.disabled = running;
  document.querySelector('input[name=provider][value=browser_smoke]').closest('label').title = onlyAPI ? 'This task needs a model.' : '';
  $('run-count').textContent = String(state.runs.length);
}

function runInfo(run) {
  const cfg = run?.runtime || {}, data = run?.benchmark_case || {}, parts = [srcLabel(run)];
  if (isProc({id: run?.task_id})) {
    parts.push({none: 'Normal', ack_lost: 'Confirmation lost', no_effect: 'Add not applied', stockout: 'Stock changed'}[data.fault || 'none']);
    parts.push({baseline: 'Model decides', verify: 'Cart check', adaptive: 'Page feedback'}[cfg.recovery || 'verify']);
    if (data.seed !== undefined) parts.push(`Seed ${data.seed}`);
  }
  if (cfg.context_mode) parts.push({full_history: 'Full history', page_memory: 'Page memory', latest_observation: 'Latest page'}[cfg.context_mode] || cfg.context_mode);
  return parts.filter(Boolean).join(' · ');
}
function renderRun() {
  const run = state.run;
  $('run-status').textContent = run ? statLabels[run.status] || run.status : 'Not started';
  $('run-status').className = `status-badge ${run?.status || 'idle'}`;
  $('current-run-title').textContent = run ? taskTitle(taskFor(run.task_id) || {id: run.task_id}) : 'Waiting for the first run';
  $('run-source').textContent = run ? runInfo(run) + (preview ? ' · saved record' : '') : 'Choose a task to start.';
  $('run-source').className = run ? isModel(run) ? 'source-model' : 'source-smoke' : '';
  $('metric-steps').textContent = typeof run?.steps === 'number' ? String(run.steps) : '—';
  $('metric-calls').textContent = typeof run?.model_calls === 'number' ? String(run.model_calls) : '—';
  $('metric-time').textContent = typeof run?.elapsed_seconds === 'number' && run.elapsed_seconds > 0 ? `${run.elapsed_seconds.toFixed(1)}s` : active(run) ? 'Running' : '—';
  const success = run?.result?.success;
  $('metric-result').textContent = !run || active(run) ? 'Pending' : success === true ? isModel(run) ? 'Task passed' : 'Test passed' : success === false ? 'Requirements unmet' : 'Unknown outcome';
  const resPanel = $('result-panel'); resPanel.hidden = !run || active(run);
  if (run && !active(run)) {
    const failed = ['failed', 'budget_exceeded', 'interrupted'].includes(run.status);
    resPanel.classList.toggle('error', failed);
    $('result-symbol').textContent = failed ? '!' : success === true ? '✓' : 'i';
    $('result-title').textContent = failed ? statLabels[run.status] || 'Run incomplete'
      : run.status === 'cancelled' ? 'The run stopped. Its saved trace remains available.'
      : isModel(run) ? success === true ? 'Task passed.' : success === false ? 'Task requirements were not met.' : 'The task outcome is unknown.'
      : success === true ? 'Script test passed.' : 'The test ended. Review its pages and actions.';
    const error = run.error?.message || (typeof run.error === 'string' ? run.error : null);
    const rawReason = error || run.result?.reason || run.termination_reason || '';
    const reason = readReason(rawReason, run);
    $('result-reason').textContent = reason;
    $('result-detail').hidden = !rawReason || reason === rawReason;
    $('result-detail-text').textContent = rawReason;
    $('result-scope').textContent = isModel(run)
      ? typeof success === 'boolean' ? 'Checked against the task page.' : 'No completed task check is available.'
      : 'Script results are reported separately.';
  }
  if (!state.frames.length) {
    $('empty-title').textContent = active(run) ? 'Opening task page…' : run ? 'This run has no saved observations.' : 'Waiting for the task page';
    $('empty-copy').textContent = run?.error?.message ? readReason(run.error.message, run) : 'Each action saves a screenshot and page observation for replay.';
  }
  drawCtrls();
}

function recText(data) {
  return data?.guidance || {uncertain: 'Add result uncertain. The model chooses the next action.', stockout: 'Item unavailable. The model chooses the next action.', confirmed: 'Add confirmed. The model chooses the next action.'}[data?.state] || 'The model chooses the next action from the page.';
}
function eventDesc(event) {
  const p = event.payload || {}, obs = getObs(event), action = getAction(event);
  if (obs) return {title: `Read page · step ${obs.step}`, detail: obs.observation.title || obs.observation.url || 'Save visible page content', icon: '▤', kind: 'observation'};
  if (action) return {title: actLabels[actionType(action)] || actionType(action), detail: briefAct(action), icon: '↗', kind: 'action'};
  if (event.type === 'memory') return {title: 'Update page memory', detail: `${p.pages ?? 0} pages · ${p.characters ?? 0} characters${p.evicted_pages ? ` · ${p.evicted_pages} older pages removed` : ''}${p.clipped_pages ? ` · ${p.clipped_pages} shortened pages` : ''}`, icon: '▣', kind: 'memory'};
  if (event.type === 'recovery') return {title: 'Review page feedback', detail: recText(p), icon: '↻', kind: 'recovery'};
  if (event.type === 'error') return {title: 'Execution error', detail: p.message || p.error?.message || p.kind || 'Review the saved error.', icon: '!', kind: 'error'};
  if (event.type === 'model_request') return {title: 'Request next action', detail: 'Send the page observation and available tools.', icon: '◇', kind: 'model'};
  if (event.type === 'model_response') return {title: 'Model action received', detail: 'Check the action and element reference before execution.', icon: '◇', kind: 'model'};
  if (event.type === 'policy_action') return {title: 'Script action', detail: 'Execute the preset action.', icon: '◇', kind: 'policy'};
  if (event.type === 'procurement_audit') return {title: 'Review cart operations', detail: `${p.metrics?.cart_count ?? '—'} items · ${p.metrics?.duplicates ?? '—'} duplicates · ${p.metrics?.cart_checks ?? '—'} cart reads`, icon: '✓', kind: 'evaluation'};
  if (event.type === 'evaluation') return {title: 'Check task result', detail: readReason(p.result?.reason, state.run), icon: '✓', kind: 'evaluation'};
  const label = p.status || p.termination_reason || p.reason;
  return {title: event.type === 'status' ? 'Run status' : event.type, detail: statLabels[label] || label || 'Event saved', icon: '·', kind: 'status'};
}
function drawEvents() {
  const list = empty($('event-list'));
  $('event-count').textContent = `${state.events.length} events`;
  if (!state.events.length) list.append(el('p', 'Observations, actions, and errors appear in order.', 'timeline-empty'));
  for (const event of state.events) {
    const desc = eventDesc(event), button = el('button', null, `event-row ${desc.kind}`); button.type = 'button'; button.dataset.sequence = event.sequence;
    const body = el('span', null, 'event-body'), line = el('span', null, 'event-line');
    line.append(el('strong', desc.title), el('span', event.created_at ? new Date(event.created_at).toLocaleTimeString('en-US', {hour12: false}) : 'Saved'));
    body.append(line, el('p', String(desc.detail).slice(0, 240)));
    const icon = el('span', desc.icon, 'event-icon'); icon.setAttribute('aria-hidden', 'true');
    button.append(icon, body, el('span', `#${event.sequence}`, 'event-index'));
    button.onclick = () => pickEvent(event.sequence);
    list.append(button);
  }
  markEvent();
  if ($('follow-latest').checked && active()) list.scrollTop = list.scrollHeight;
}
function markEvent() {
  for (const button of $('event-list').querySelectorAll('button')) {
    const selected = Number(button.dataset.sequence) === Number(state.selectedSequence);
    button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected));
  }
}
function pickEvent(sequence) {
  stopPlay(); $('follow-latest').checked = false;
  state.selectedSequence = Number(sequence);
  const selected = state.events.find(event => Number(event.sequence) === Number(sequence));
  let index;
  if (['memory', 'recovery', 'model_request', 'model_response'].includes(selected?.type)) {
    index = state.frames.findLastIndex(frame => Number(frame.event.sequence) <= Number(sequence));
  } else index = state.frames.findIndex(frame => Number(frame.event.sequence) >= Number(sequence));
  if (index < 0) index = state.frames.length - 1;
  state.selectedFrame = Math.max(0, index); drawFrame();
}
function pickFrame(index, manual = true) {
  if (manual) { stopPlay(); $('follow-latest').checked = false; }
  state.selectedFrame = Math.max(0, Math.min(Number(index), state.frames.length - 1));
  state.selectedSequence = state.frames[state.selectedFrame]?.event.sequence ?? null;
  drawFrame();
}
function drawFrame() {
  const frames = state.frames, frame = frames[state.selectedFrame], image = $('page-screenshot');
  $('frame-slider').max = String(Math.max(0, frames.length - 1)); $('frame-slider').value = String(state.selectedFrame);
  $('frame-slider').disabled = frames.length < 2;
  $('previous-frame').disabled = !frame || state.selectedFrame <= 0;
  $('next-frame').disabled = !frame || state.selectedFrame >= frames.length - 1;
  $('play-button').disabled = frames.length < 2 || active();
  $('enlarge-button').disabled = !frame?.screenshot;
  $('frame-counter').textContent = frame ? `${state.selectedFrame + 1} / ${frames.length} observations` : '0 observations';
  $('frame-caption').textContent = frame ? `Screenshot · observation ${frame.observation.observation_id || state.selectedFrame + 1} · step ${frame.step}` : 'Review screenshots by step.';
  loadShot(frame);
  const obs = frame?.observation;
  $('page-url').querySelector('span:last-child').textContent = obs?.url || 'Browser not started';
  $('page-url').title = obs?.url || 'Current page URL';
  $('observation-step').textContent = frame ? `Step ${frame.step}` : 'No observation';
  $('frame-slider').setAttribute('aria-valuetext', frame ? `Observation ${state.selectedFrame + 1} of ${frames.length} · step ${frame.step}` : 'No observation');
  drawObs(obs);
  const selected = state.events.find(event => Number(event.sequence) === Number(state.selectedSequence));
  const actEvent = getAction(selected) ? selected : [...state.events].reverse().find(event => Number(event.sequence) < Number(frame?.event.sequence) && getAction(event));
  const action = getAction(actEvent);
  $('current-action').hidden = !action;
  if (action) { $('action-name').textContent = actLabels[actionType(action)] || actionType(action); $('action-detail').textContent = JSON.stringify(action, null, 2); }
  drawHints(frame);
  drawLarge();
  markEvent();
}
function drawHints(frame) {
  const start = Number(frame?.event.sequence || 0);
  const next = state.frames.find(item => Number(item.event.sequence) > start);
  const end = Number(next?.event.sequence || Infinity);
  const events = state.events.filter(event => Number(event.sequence) >= start && Number(event.sequence) < end);
  const memory = [...events].reverse().find(event => event.type === 'memory')?.payload;
  const recovery = [...events].reverse().find(event => event.type === 'recovery')?.payload;
  $('run-insight').hidden = !memory && !recovery;
  $('memory-note').textContent = memory ? `${memory.pages ?? 0} remembered pages · ${memory.characters ?? 0} characters${memory.evicted_pages ? ` · ${memory.evicted_pages} older pages removed` : ''}${memory.clipped_pages ? ` · ${memory.clipped_pages} shortened pages` : ''}` : '';
  $('recovery-note').textContent = recovery ? recText(recovery) : '';
}
function loadShot(frame, force = false) {
  const url = frame?.screenshot || '', image = $('page-screenshot');
  image.alt = frame ? `Step ${frame.step}, ${frame.observation.title || 'Task page'} screenshot` : 'Current page screenshot';
  if (url === state.shot && !force) return;
  state.shot = url; state.shotDone = false;
  const key = ++state.shotKey;
  image.hidden = true; image.removeAttribute('src');
  $('snapshot-empty').hidden = false; $('retry-shot').hidden = true;
  if (!url) { renderRun(); return; }
  $('empty-title').textContent = 'Loading screenshot…';
  $('empty-copy').textContent = 'The observation and action details are available below.';
  const img = new Image();
  img.onload = async () => {
    try { await img.decode(); } catch { /* The loaded image remains usable. */ }
    if (key !== state.shotKey || state.shot !== url) return;
    image.src = url; image.hidden = false; state.shotDone = true;
    $('snapshot-empty').hidden = true; $('retry-shot').hidden = true;
    drawLarge();
  };
  img.onerror = () => {
    if (key !== state.shotKey || state.shot !== url) return;
    image.hidden = true; $('snapshot-empty').hidden = false;
    $('empty-title').textContent = 'This screenshot could not be loaded.';
    $('empty-copy').textContent = 'Reload the image or inspect the saved page text.';
    $('retry-shot').hidden = false; drawLarge();
  };
  img.src = url;
}
function drawLarge() {
  if (!$('screenshot-dialog').open) return;
  const frame = state.frames[state.selectedFrame], img = $('large-screenshot');
  $('large-caption').textContent = frame ? `Observation ${state.selectedFrame + 1} of ${state.frames.length} · step ${frame.step}` : 'No screenshot';
  $('large-prev').disabled = state.selectedFrame <= 0;
  $('large-next').disabled = state.selectedFrame >= state.frames.length - 1;
  const ready = !!frame?.screenshot && state.shotDone && state.shot === frame.screenshot;
  img.hidden = !ready; $('large-state').hidden = ready;
  $('large-state').textContent = $('retry-shot').hidden ? 'Loading screenshot…' : 'The image could not be loaded. Close this view to reload it.';
  if (ready) {
    if (img.src !== frame.screenshot) {img.src = frame.screenshot; $('large-stage').scrollTo(0, 0);}
    img.alt = $('page-screenshot').alt;
    scaleShot();
  }
}
function scaleShot() {
  const img = $('large-screenshot'), stage = $('large-stage');
  const width = $('page-screenshot').naturalWidth || img.naturalWidth;
  if (!width) return;
  const ratio = state.zoom ?? Math.min(1, (stage.clientWidth - 24) / width);
  img.style.width = `${Math.max(1, width * ratio)}px`;
  $('zoom-label').textContent = state.zoom === null ? 'Fit' : `${Math.round(ratio * 100)}%`;
  $('zoom-fit').setAttribute('aria-pressed', String(state.zoom === null));
  $('zoom-real').setAttribute('aria-pressed', String(state.zoom === 1));
  $('zoom-out').disabled = state.zoom !== null && state.zoom <= .25;
  $('zoom-in').disabled = state.zoom !== null && state.zoom >= 3;
  stage.classList.toggle('pannable', width * ratio > stage.clientWidth || img.getBoundingClientRect().height > stage.clientHeight);
}
function setZoom(value) {
  const stage = $('large-stage');
  const x = (stage.scrollLeft + stage.clientWidth / 2) / Math.max(1, stage.scrollWidth);
  const y = (stage.scrollTop + stage.clientHeight / 2) / Math.max(1, stage.scrollHeight);
  state.zoom = value === null ? null : Math.max(.25, Math.min(3, value));
  scaleShot();
  stage.scrollLeft = x * stage.scrollWidth - stage.clientWidth / 2;
  stage.scrollTop = y * stage.scrollHeight - stage.clientHeight / 2;
}
function nudgeZoom(delta) {
  const img = $('page-screenshot'), stage = $('large-stage');
  const base = state.zoom ?? Math.min(1, (stage.clientWidth - 24) / (img.naturalWidth || 1));
  setZoom(Math.round((base + delta) * 100) / 100);
}
function drawObs(obs) {
  const elements = Array.isArray(obs?.elements) ? obs.elements : [];
  $('element-count').textContent = String(elements.length);
  const list = empty($('element-list'));
  if (!elements.length) list.append(el('p', obs ? 'No actionable elements in this observation.' : 'Elements appear when the browser opens a page.', 'inspector-empty'));
  for (const item of elements) {
    const row = el('div', null, 'element-item'), line = el('div', null, 'element-topline');
    line.append(el('span', item.ref || 'No reference', 'element-ref'), el('span', item.role || item.tag || 'element', 'element-role'));
    if (item.disabled) line.append(el('span', 'Disabled', 'element-disabled'));
    row.append(line, el('p', item.name || item.text || item.label || 'Unnamed element', 'element-name'));
    if (item.value !== undefined && item.value !== '') row.append(el('p', `Value: ${String(item.value)}`, 'element-value'));
    if (Array.isArray(item.options) && item.options.length) row.append(el('p', `Options: ${item.options.map(option => typeof option === 'string' ? option : option.label || option.text || option.value).join(' / ')}`, 'element-value'));
    list.append(row);
  }
  $('observation-text').textContent = obs?.text || 'No page text.';
  $('text-truncated').hidden = !obs?.truncated;
}

function updateRuns(run) {
  const index = state.runs.findIndex(item => item.run_id === run.run_id);
  if (index < 0) state.runs.unshift(run); else state.runs[index] = run;
  $('run-count').textContent = String(state.runs.length);
}
function applyRun(run, events, fresh = false) {
  state.run = run; updateRuns(run);
  if (taskFor(run.task_id)) {
    state.selectedTask = String(run.task_id);
    if (fresh) {
      const task = pickedTask(), cfg = run.runtime || {}, meta = run.benchmark_case || {};
      state.group = taskGroup(task); state.split = task.split || meta.split || 'development';
      setBudget(task);
      for (const [key, id] of [['max_steps', 'max-steps'], ['max_seconds', 'max-seconds'], ['max_output_tokens', 'max-tokens'], ['context_mode', 'context-mode'], ['recovery', 'run-recovery']]) if (cfg[key] !== undefined) $(id).value = String(cfg[key]);
      if (meta.seed !== undefined) $('run-seed').value = String(meta.seed);
      if (meta.fault) $('run-fault').value = meta.fault;
      const input = document.querySelector(`input[name=provider][value=${isModel(run) ? 'api' : 'browser_smoke'}]`); input.checked = true;
      drawTasks();
    }
  }
  const signature = `${run.run_id}:${events.length}:${events.at(-1)?.sequence || 0}`;
  if (fresh || signature !== state.signature) {
    state.signature = signature; state.events = events; state.frames = events.map(getObs).filter(Boolean);
    if (fresh || $('follow-latest').checked) {
      state.selectedFrame = fresh && preview ? 0 : Math.max(0, state.frames.length - 1);
      state.selectedSequence = state.frames[state.selectedFrame]?.event.sequence ?? null;
    }
    state.selectedFrame = Math.min(state.selectedFrame, Math.max(0, state.frames.length - 1));
    drawEvents(); drawFrame();
  }
  markTask(); renderRun();
}
async function loadRun(id, {focus = false} = {}) {
  stopPoll(); stopPlay();
  const token = ++state.token;
  $('follow-latest').checked = !preview;
  if (preview) {
    const run = state.runs.find(item => item.run_id === id);
    if (!run) throw new Error('This saved run was not found.');
    applyRun(run, state.previewEvents.get(id) || [], true);
  } else {
    const results = await Promise.allSettled([jsonFetch(`/api/runs/${encodeURIComponent(id)}`), jsonFetch(`/api/runs/${encodeURIComponent(id)}/events`)]);
    if (token !== state.token) return;
    for (const result of results) if (result.status === 'rejected') throw result.reason;
    applyRun(results[0].value, results[1].value.events || [], true);
    if (active()) queuePoll(token);
  }
  if (focus) { $('browser-heading').focus({preventScroll: true}); $('browser-section').scrollIntoView({behavior: lessMotion.matches ? 'auto' : 'smooth', block: 'start'}); }
}
function queuePoll(token) {
  stopPoll();
  state.poll = setTimeout(async () => {
    if (preview || token !== state.token || !state.run) return;
    const id = state.run.run_id;
    try {
      const results = await Promise.allSettled([jsonFetch(`/api/runs/${encodeURIComponent(id)}`), jsonFetch(`/api/runs/${encodeURIComponent(id)}/events`)]);
      if (token !== state.token || id !== state.run?.run_id) return;
      for (const result of results) if (result.status === 'rejected') throw result.reason;
      const wasActive = active(); applyRun(results[0].value, results[1].value.events || []);
      if (wasActive && !active()) await loadEval();
    } catch (error) { if (token === state.token) showError2(error); }
    if (token === state.token && active()) queuePoll(token);
  }, 1000);
}

async function startRun() {
  if (preview || $('start-button').disabled) return;
  const task = pickedTask(); if (!task) return;
  const limits = [['max_steps', 'max-steps'], ['max_seconds', 'max-seconds'], ['max_output_tokens', 'max-tokens']];
  const body = {task_id: task.id, provider: provider(), context_mode: $('context-mode').value};
  for (const [key, id] of limits) {
    const input = $(id); if (!input.checkValidity()) { input.reportValidity(); return; }
    body[key] = Number(input.value);
  }
  if (isProc(task)) {
    const input = $('run-seed'); if (!input.checkValidity()) {input.reportValidity(); return;}
    Object.assign(body, {seed: Number(input.value), fault: $('run-fault').value, recovery: $('run-recovery').value});
  }
  showError(''); state.creating = true; drawCtrls();
  try {
    const run = await jsonFetch('/api/runs', {method: 'POST', body: JSON.stringify(body)});
    updateRuns(run); await loadRun(run.run_id, {focus: true});
  } finally { state.creating = false; drawCtrls(); }
}
async function cancelRun() {
  if (preview || !active()) return;
  $('cancel-button').disabled = true;
  try { await jsonFetch(`/api/runs/${encodeURIComponent(state.run.run_id)}/cancel`, {method: 'POST', body: '{}'}); $('run-source').textContent = 'Stop requested. Waiting for the browser to close.'; }
  finally { $('cancel-button').disabled = false; }
}
function togglePlay() {
  if (state.playing) { stopPlay(); return; }
  if (state.frames.length < 2 || active()) return;
  $('follow-latest').checked = false;
  if (state.selectedFrame >= state.frames.length - 1) pickFrame(0, false);
  $('play-button').textContent = 'Ⅱ Pause';
  state.playing = setInterval(() => {
    if (state.selectedFrame >= state.frames.length - 1) { stopPlay(); return; }
    pickFrame(state.selectedFrame + 1, false);
  }, (lessMotion.matches ? 1800 : 1200) / Number($('play-speed').value));
}
function drawHist() {
  const list = empty($('history-list'));
  if (!state.runs.length) list.append(el('p', 'No runs yet. Select a task to try the script test.', 'dialog-note'));
  for (const run of state.runs) {
    const card = el('button', null, 'history-card'); card.type = 'button'; card.dataset.runId = run.run_id; card.classList.toggle('selected', run.run_id === state.run?.run_id);
    card.append(el('strong', taskTitle(taskFor(run.task_id) || {id: run.task_id})));
    const line = el('div'); line.append(el('span', isModel(run) ? 'Model' : 'Script test', `history-source ${isModel(run) ? '' : 'smoke'}`), el('span', statLabels[run.status] || run.status, `status-badge ${run.status}`));
    card.append(line, el('small', runInfo(run)), el('small', `${run.created_at ? new Date(run.created_at).toLocaleString('en-US') : 'Saved record'} · ${run.steps ?? 0} actions · ${run.model_calls ?? 0} model calls`));
    card.onclick = guard(async () => { $('history-dialog').close(); await loadRun(run.run_id, {focus: true}); });
    list.append(card);
  }
}
async function showHist() {
  drawHist(); $('history-dialog').showModal();
  if (!preview) {
    try { const data = await jsonFetch('/api/runs'); state.runs = data.runs || []; drawCtrls(); drawHist(); }
    catch (error) { $('history-list').prepend(el('p', `Could not refresh history: ${errMsg(error)}`, 'dialog-note')); }
  }
}
async function exportRun() {
  if (!state.run) return;
  const data = preview ? {schema_version: 'shoptrace-webagent-recorded-preview-v1', run: state.run, events: state.events, note: 'Trace replay; keep the screenshot files together.'} : await jsonFetch(`/api/runs/${encodeURIComponent(state.run.run_id)}/export`);
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json;charset=utf-8'}));
  const link = el('a'); link.href = url; link.download = `shoptrace-browser-${state.run.run_id}.json`; document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1500);
}
async function loadEval(data) {
  if (!data && !preview) data = await jsonFetch('/api/evaluation');
  if (!data) return;
  const modelRuns = data.model_runs ?? data.model_run_count ?? 0, smokeRuns = data.smoke_runs ?? data.smoke_run_count ?? 0;
  const rate = data.model_success_rate;
  if (data.mixed_configuration) {
    $('evaluation-note').textContent = `Model runs: ${modelRuns}; known outcomes ${data.known_results ?? '—'} / ${data.finished_model_runs ?? '—'}; ${data.configuration_groups?.length ?? 'multiple'} settings reported separately. Script runs: ${smokeRuns}.`;
    return;
  }
  $('evaluation-note').textContent = typeof rate === 'number'
    ? `Model runs: ${modelRuns}; known outcomes ${data.known_results ?? '—'} / ${data.finished_model_runs ?? '—'}; known-result pass rate ${(rate * 100).toFixed(1)}%. Script runs: ${smokeRuns}.`
    : `Model runs: ${modelRuns}, ${modelRuns ? 'not enough known outcomes to show a rate' : 'no pass rate yet'}. Script runs: ${smokeRuns}.`;
}

function pickPane(tab) {
  const elements = tab === 'elements';
  for (const name of ['elements', 'text']) { const selected = name === tab; $(`${name}-tab`).setAttribute('aria-selected', String(selected)); $(`${name}-tab`).tabIndex = selected ? 0 : -1; }
  $('elements-pane').hidden = !elements; $('text-pane').hidden = elements;
}
async function initPrev() {
  document.body.classList.add('preview-mode'); $('preview-badge').hidden = false;
  document.title = 'ShopTrace · Trace replay';
  $('browser-heading').textContent = 'Trace replay';
  $('task-heading').textContent = 'Browse saved tasks';
  const data = await jsonFetch('./preview-data.json');
  state.runs = data.runs || (data.run ? [data.run] : []);
  if (!state.runs.length) throw new Error('No saved traces.');
  for (const run of state.runs) state.previewEvents.set(run.run_id, Array.isArray(data.events) ? data.events : data.events?.[run.run_id] || []);
  state.tasks = data.tasks || state.runs.map(run => ({id: run.task_id, title: run.title || run.task_id, query: run.query, difficulty: 'Saved browser task', start_path: '/sandbox/'})).filter((task, index, tasks) => tasks.findIndex(item => item.id === task.id) === index);
  state.status = {application: 'shoptrace-webagent', model_name: null, capabilities: {browser_available: true, llm_configured: false}};
  $('scope-notice').querySelector('strong').textContent = 'Browser trace replay';
  $('scope-notice').querySelector('p > span').textContent = 'Replay saved browser runs. Each run is labeled Model or Script.';
  state.selectedTask = String(state.tasks[0]?.id); drawTasks(); drawStatus();
  await loadRun(state.runs[0].run_id);
  await loadEval(data.evaluation || {model_runs: state.runs.filter(isModel).length, smoke_runs: state.runs.filter(run => !isModel(run)).length, model_success_rate: null});
}
async function initLive() {
  const urls = ['/api/status', '/api/tasks', '/api/runs', '/api/evaluation', '/api/cases?split=development', '/api/cases?split=heldout', '/api/procurement?split=development', '/api/procurement?split=heldout'];
  const results = await Promise.allSettled(urls.map(url => jsonFetch(url)));
  if (results[0].status === 'fulfilled') state.status = results[0].value; else showError(errMsg(results[0].reason));
  if (results[1].status === 'fulfilled') state.tasks = (results[1].value.tasks || []).map(task => ({...task, group: 'original'})); else showError(errMsg(results[1].reason));
  if (results[2].status === 'fulfilled') state.runs = results[2].value.runs || []; else showError(errMsg(results[2].reason));
  for (const [index, group, split] of [[4, 'expanded', 'development'], [5, 'expanded', 'heldout'], [6, 'procure', 'development'], [7, 'procure', 'heldout']]) {
    const item = results[index];
    if (item.status === 'fulfilled') state.tasks.push(...(item.value.cases || item.value.tasks || []).map(task => ({...task, group, split})));
    else showError(`Some task sets could not be loaded. ${errMsg(item.reason)}`);
  }
  state.selectedTask = String(state.tasks[0]?.id || '');
  if (state.status?.capabilities?.llm_configured) document.querySelector('input[name=provider][value=api]').checked = true;
  drawTasks(); drawStatus(); renderRun();
  if (results[3].status === 'fulfilled') await loadEval(results[3].value);
  if (state.runs.length) await loadRun(state.runs[0].run_id);
}

$('start-button').onclick = guard(startRun); $('cancel-button').onclick = guard(cancelRun);
$('history-button').onclick = guard(showHist); $('export-button').onclick = guard(exportRun);
$('export-inline').onclick = guard(exportRun);
$('previous-frame').onclick = () => pickFrame(state.selectedFrame - 1); $('next-frame').onclick = () => pickFrame(state.selectedFrame + 1);
$('frame-slider').oninput = event => pickFrame(Number(event.target.value)); $('play-button').onclick = togglePlay;
$('follow-latest').onchange = event => { if (event.target.checked) { stopPlay(); pickFrame(state.frames.length - 1, false); } };
for (const input of document.querySelectorAll('input[name=provider]')) input.onchange = drawCtrls;
for (const name of ['elements', 'text']) {
  $(`${name}-tab`).onclick = () => pickPane(name);
  $(`${name}-tab`).onkeydown = event => { if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); const next = name === 'elements' ? 'text' : 'elements'; pickPane(next); $(`${next}-tab`).focus(); } };
}
$('enlarge-button').onclick = () => {
  const frame = state.frames[state.selectedFrame]; if (!frame?.screenshot) return;
  state.zoom = null; $('screenshot-dialog').showModal(); drawLarge();
};
$('retry-shot').onclick = () => loadShot(state.frames[state.selectedFrame], true);
$('large-screenshot').onload = scaleShot;
$('large-prev').onclick = () => pickFrame(state.selectedFrame - 1);
$('large-next').onclick = () => pickFrame(state.selectedFrame + 1);
$('zoom-fit').onclick = () => setZoom(null); $('zoom-real').onclick = () => setZoom(1);
$('zoom-in').onclick = () => nudgeZoom(.25); $('zoom-out').onclick = () => nudgeZoom(-.25);
$('full-screen').onclick = guard(async () => {
  const dialog = $('shot-view');
  if (document.fullscreenElement) await document.exitFullscreen();
  else if (dialog.requestFullscreen) await dialog.requestFullscreen();
  else $('screenshot-dialog').classList.toggle('maximized');
  scaleShot();
});
document.addEventListener('fullscreenchange', () => {$('full-screen').textContent = document.fullscreenElement ? 'Exit full screen' : 'Full screen'; scaleShot();});
$('screenshot-dialog').addEventListener('close', () => {
  state.drag = null; $('large-stage').classList.remove('dragging');
  $('screenshot-dialog').classList.remove('maximized');
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
});
$('large-stage').addEventListener('pointerdown', event => {
  if (event.button !== 0 || !$('large-stage').classList.contains('pannable')) return;
  const stage = $('large-stage');
  state.drag = {id: event.pointerId, x: event.clientX, y: event.clientY, left: stage.scrollLeft, top: stage.scrollTop};
  stage.setPointerCapture(event.pointerId); stage.classList.add('dragging');
});
$('large-stage').addEventListener('pointermove', event => {
  const drag = state.drag; if (!drag || drag.id !== event.pointerId) return;
  const stage = $('large-stage'); stage.scrollLeft = drag.left - event.clientX + drag.x; stage.scrollTop = drag.top - event.clientY + drag.y;
});
for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) $('large-stage').addEventListener(type, () => {state.drag = null; $('large-stage').classList.remove('dragging');});
for (const button of $('task-groups').querySelectorAll('button')) button.onclick = guard(() => pickGroup(button.dataset.group));
$('task-split').onchange = guard(event => pickGroup(state.group, event.target.value));
$('context-mode').onchange = drawCtrls;
$('play-speed').onchange = () => {if (state.playing) {stopPlay(); togglePlay();}};
window.addEventListener('resize', () => {if ($('screenshot-dialog').open) scaleShot();});
document.addEventListener('keydown', event => {
  if (event.altKey || event.ctrlKey || event.metaKey || ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON'].includes(event.target.tagName)) return;
  if (!$('screenshot-dialog').open && !event.target.closest('#browser-section')) return;
  if (event.key === 'ArrowLeft') {event.preventDefault(); pickFrame(state.selectedFrame - 1);}
  if (event.key === 'ArrowRight') {event.preventDefault(); pickFrame(state.selectedFrame + 1);}
});
for (const button of document.querySelectorAll('.dialog-close')) button.onclick = () => button.closest('dialog').close();
for (const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', event => { if (event.target !== dialog) return; const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); });
window.addEventListener('pagehide', () => { stopPoll(); stopPlay(); state.token++; });
guard(preview ? initPrev : initLive)();
