/* Browser preview with fixed scenarios. */
const SCHEMA = 'shopping-lab.trajectory.v1';
const STORE_KEY = 'shoptrace.preview.v1';
const COMMIT = '9140f0d699b2feef7d11b8cbbcadbf612877096b';
const INTERVAL = 400;
const FINAL = new Set(['completed', 'failed', 'cancelled', 'budget_exceeded']);
const CATEGORIES = new Set(['retrieval', 'evidence', 'memory', 'specification', 'tool', 'environment', 'scoring', 'other']);
const clone = value => JSON.parse(JSON.stringify(value));
const now = () => new Date().toISOString();
const id = () => crypto.randomUUID().replaceAll('-', '');
const isObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const number = (value, fallback = 0) => typeof value === 'number' && Number.isFinite(value) ? value : fallback;
const text = (value, fallback = '') => typeof value === 'string' ? value : fallback;
const knownBool = value => typeof value === 'boolean' ? value : null;
const emptyRes = () => ({reward: null, reward_valid: false, strict_success: null, purchase_success: null});

const TASKS = [
  {
    id: 'fixture-001', title: 'Headphones: budget and color', source: 'fixture',
    query: '[Fixture] Find black wireless headphones costing no more than CNY 300.',
    search: 'black wireless headphones', product: 'DEMO-HEADPHONE', options: ['black'],
    products: [
      {asin: 'DEMO-HEADPHONE', title: 'Demo Wireless Headphones', brand: 'Demo', category: 'headphones', price: 249, key_attributes: ['wireless', 'black']},
      {asin: 'DEMO-EXPENSIVE', title: 'Demo Premium Headphones', brand: 'Demo', category: 'headphones', price: 499, key_attributes: ['wireless', 'black']},
    ],
  },
  {
    id: 'fixture-002', title: 'Thermos: choose capacity', source: 'fixture',
    query: '[Fixture] Find a blue 500ml thermos costing no more than CNY 100.',
    search: 'blue 500ml thermos', product: 'DEMO-CUP', options: ['blue', '500ml'],
    products: [
      {asin: 'DEMO-CUP', title: 'Demo Thermos', brand: 'Demo', category: 'thermos', price: 69, key_attributes: ['blue', '500ml']},
      {asin: 'DEMO-CUP-SMALL', title: 'Demo Small Thermos', brand: 'Demo', category: 'thermos', price: 49, key_attributes: ['blue', '300ml']},
    ],
  },
  {
    id: 'fixture-003', title: 'No match: stop without buying', source: 'fixture',
    query: '[Fixture] Find a mechanical keyboard costing no more than CNY 50; stop if none match.',
    search: 'mechanical keyboard', product: 'DEMO-KEYBOARD', options: [], abstain: true,
    products: [{asin: 'DEMO-KEYBOARD', title: 'Demo Mechanical Keyboard', brand: 'Demo', category: 'keyboards', price: 199, key_attributes: ['mechanical']}],
  },
];

let hasStore = true;
let database = {runs: [], events: {}, annotations: {}};
try {
  const loaded = JSON.parse(localStorage.getItem(STORE_KEY) || 'null');
  if (loaded && Array.isArray(loaded.runs) && isObject(loaded.events) && isObject(loaded.annotations)) database = loaded;
} catch { hasStore = false; }
const timers = new Map();
function persist() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(database)); }
  catch { hasStore = false; }
}
function getRun(runId) {
  const run = database.runs.find(item => item.run_id === runId);
  if (!run) throw new Error('Run not found. Browser preview records may have been cleared.');
  return run;
}
function addEvent(runId, type, payload) {
  const events = database.events[runId] ||= [];
  const sequence = events.length + 1;
  const event = {schema_version: SCHEMA, run_id: runId, event_id: `e${String(sequence).padStart(6, '0')}`, sequence, type, payload: {...clone(payload), synthetic_preview: true}, created_at: now()};
  events.push(event);
  return event;
}
// Mark interrupted runs when the page reloads.
for (const run of database.runs) {
  if (!FINAL.has(run.status)) {
    run.status = 'cancelled'; run.completed_at = now(); run.termination_reason = 'preview_page_reloaded';
    addEvent(run.run_id, 'status', {status: 'cancelled', reason: 'Page reloaded; demo stopped.'});
  }
}
persist();

function stateFor(pageType, extra = {}) {
  return {observation_version: 'shopping-observation-v2', page_type: pageType, search_available: pageType.startsWith('search_'), actions: [], ...clone(extra)};
}
function obs(state) { return '[Fixture demo · no model calls]\n' + JSON.stringify(state, null, 2); }
function planFor(task) {
  return [
    ['search_products', {query: task.search}, `search[${task.search}]`],
    ['open_product', {asin: task.product}, `click[${task.product}]`],
    ['view_features', {}, 'click[Features]'],
    ...task.options.map(value => ['select_option', {value}, `click[${value}]`]),
    task.abstain ? ['finish_without_purchase', {reason: 'no_suitable_product'}, 'finish[no_suitable_product]'] : ['buy_now', {}, 'click[Buy Now]'],
  ];
}
function readBudget(value, fallback, minimum, maximum) {
  if (value === undefined) return fallback;
  if (!Number.isInteger(value) || value < minimum || value > maximum) throw new Error('Run budget is outside the supported preview range.');
  return value;
}
function createRun(spec) {
  if (!isObject(spec) || spec.mode !== 'fixture' || spec.model?.provider !== 'scripted') throw new Error('Preview supports fixture scripts. Use the local app for other providers.');
  const task = TASKS.find(item => item.id === spec.task_id);
  if (!task) throw new Error('Select a built-in preview task.');
  if (database.runs.filter(run => !FINAL.has(run.status)).length >= 3) throw new Error('At most 3 demos can run at once.');
  const requested = spec.runtime || {};
  const runtime = {
    max_steps: readBudget(requested.max_steps, 35, 1, 100),
    max_seconds: readBudget(requested.max_seconds, 120, 1, 3600),
    max_calls: readBudget(requested.max_calls, 35, 1, 200),
    max_output_tokens: readBudget(requested.max_output_tokens, 512, 32, 8192),
  };
  const run = {
    schema_version: SCHEMA, run_id: id(), task_id: task.id, query: task.query,
    mode: 'fixture', source: 'synthetic_fixture', synthetic_preview: true,
    model: {provider: 'scripted', name: 'fixture-script', stage: ['base', 'sft', 'rl', 'other'].includes(spec.model.stage) ? spec.model.stage : 'other'},
    runtime, strategy: 'default', environment_version: 'browser-preview-fixture-v1', upstream_commit: COMMIT,
    created_at: now(), completed_at: null, status: 'running', termination_reason: null,
    steps: 0, model_calls: 0, scripted_steps: 0, elapsed_seconds: 0,
    usage: {input_tokens: null, output_tokens: null}, result: emptyRes(), error: null,
    timing_note: 'Time measures script execution; model calls are zero.',
  };
  database.runs.unshift(run); database.events[run.run_id] = []; database.annotations[run.run_id] = [];
  let current = stateFor('search_home');
  addEvent(run.run_id, 'status', {status: 'running', message: 'Fixture demo started.'});
  addEvent(run.run_id, 'observation', {step: 0, state: current, observation: obs(current)});
  const plan = planFor(task), selected = [], started = Date.now();
  let cursor = 0;
  function finish(status, reason, result = emptyRes()) {
    clearInterval(timers.get(run.run_id)); timers.delete(run.run_id);
    run.status = status; run.termination_reason = reason; run.completed_at = now();
    run.elapsed_seconds = Math.round((Date.now() - started) / 10) / 100; run.result = result;
    addEvent(run.run_id, 'status', {status, reason}); persist();
  }
  const timer = setInterval(() => {
    if (run.status !== 'running') {clearInterval(timer); timers.delete(run.run_id); return;}
    run.elapsed_seconds = Math.round((Date.now() - started) / 10) / 100;
    if (run.steps >= runtime.max_steps || cursor >= runtime.max_calls || run.elapsed_seconds >= runtime.max_seconds) {
      finish('budget_exceeded', 'preview_budget_exceeded'); return;
    }
    const [toolName, args, envAction] = plan[cursor];
    const step = run.steps;
    const call = {id: `synthetic-${cursor + 1}`, type: 'function', function: {name: toolName, arguments: JSON.stringify(args)}};
    addEvent(run.run_id, 'model_request', {step, synthetic: true, messages: [{role: 'system', content: 'Fixture script input.'}, {role: 'user', content: task.query}, {role: 'tool', content: obs(current)}], generation_config: {source: 'browser_script', model_not_called: true}});
    addEvent(run.run_id, 'model_response', {step, synthetic: true, message: {role: 'assistant', content: '[Scripted action]', tool_calls: [call]}, usage: null});
    addEvent(run.run_id, 'action', {step, tool_name: toolName, arguments: args, env_action: envAction, synthetic: true});
    let result = null, done = false;
    if (toolName === 'search_products') {
      current = stateFor('search_results', {query: task.search, products: task.products, actions: task.products.map(product => product.asin)});
    } else if (toolName === 'open_product') {
      current = stateFor('product_detail', {product: task.products[0], selected_options: {}, available_options: {'Options': task.options}, selected_price: task.products[0].price, actions: ['Features', 'Description', 'Reviews', 'Attributes', 'Buy Now', 'Back to Search', ...task.options]});
    } else if (toolName === 'view_features') {
      current = {...current, page_type: 'information_subpage', subpage: 'Features', content: 'Fixture product features: ' + task.products[0].key_attributes.join(', ')};
    } else if (toolName === 'select_option') {
      selected.push(args.value); current = {...current, selected_options: Object.fromEntries(selected.map((value, index) => [String(index + 1), value]))};
    } else {
      done = true;
      result = {...emptyRes(), fixture_success: true, fixture_purchase: !task.abstain, source: 'synthetic_fixture', termination_reason: task.abstain ? 'fixture_abstain' : 'fixture_purchase'};
      current = stateFor('terminal');
    }
    cursor++; run.steps++; run.scripted_steps++;
    addEvent(run.run_id, 'environment_result', {step: run.steps, observation: obs(current), state: current, done, result, synthetic: true});
    if (done) finish('completed', result.termination_reason, result); else persist();
  }, INTERVAL);
  timers.set(run.run_id, timer); persist();
  return run;
}

function safeProd(product) {
  if (!isObject(product)) return null;
  return {asin: text(product.asin), title: text(product.title), brand: text(product.brand), category: text(product.category), price: typeof product.price === 'number' && Number.isFinite(product.price) ? product.price : null, key_attributes: Array.isArray(product.key_attributes) ? product.key_attributes.filter(value => typeof value === 'string').slice(0, 100) : []};
}
function safeData(payload) {
  const result = clone(payload);
  if (result.state) {
    if (!isObject(result.state)) delete result.state;
    else {
      const raw = result.state, state = {page_type: text(raw.page_type, 'unknown')};
      if (Array.isArray(raw.products)) state.products = raw.products.map(safeProd).filter(Boolean);
      if (isObject(raw.product)) state.product = safeProd(raw.product);
      if (isObject(raw.selected_options)) state.selected_options = Object.fromEntries(Object.entries(raw.selected_options).map(([key, value]) => [key, String(value)]));
      if (isObject(raw.available_options)) state.available_options = Object.fromEntries(Object.entries(raw.available_options).map(([key, value]) => [key, Array.isArray(value) ? value.map(String) : String(value)]));
      if (typeof raw.content === 'string') state.content = raw.content;
      result.state = state;
    }
  }
  return result;
}
function importRun(data) {
  if (!isObject(data) || data.schema_version !== SCHEMA || !isObject(data.run) || !Array.isArray(data.events) || data.events.length > 10000) throw new Error('Preview accepts single shopping-lab.trajectory.v1 JSON exports. Convert other formats with the local app.');
  const original = data.run;
  if (!['fixture', 'shopsimulator'].includes(original.mode) || !isObject(original.model) || typeof original.task_id !== 'string') throw new Error('Imported trace is missing task, mode or model information.');
  const seen = new Set(); let previous = 0;
  for (const event of data.events) {
    if (!isObject(event) || !isObject(event.payload) || typeof event.type !== 'string' || typeof event.event_id !== 'string' || seen.has(event.event_id) || !Number.isInteger(event.sequence) || event.sequence <= previous) throw new Error('Imported events require valid payloads, unique event_id and increasing sequence.');
    seen.add(event.event_id); previous = event.sequence;
  }
  const runId = id();
  const run = {
    schema_version: SCHEMA, run_id: runId, task_id: original.task_id.slice(0, 100), query: text(original.query), mode: original.mode, source: 'imported',
    model: {provider: original.model.provider === 'api' ? 'api' : 'scripted', name: text(original.model.name, 'imported-model').slice(0, 200), stage: ['base', 'sft', 'rl'].includes(original.model.stage) ? original.model.stage : 'other'},
    runtime: isObject(original.runtime) ? clone(original.runtime) : {}, strategy: 'default',
    environment_version: text(original.environment_version, 'imported-unverified'), upstream_commit: text(original.upstream_commit),
    created_at: now(), completed_at: now(), status: FINAL.has(original.status) ? original.status : 'failed', termination_reason: text(original.termination_reason, 'imported_history'),
    steps: number(original.steps, null), model_calls: number(original.model_calls, null), elapsed_seconds: number(original.elapsed_seconds, null),
    usage: {input_tokens: null, output_tokens: null}, error: null,
    result: {...emptyRes(), reward: original.result?.reward_valid === true ? number(original.result.reward, null) : null, reward_valid: original.result?.reward_valid === true, strict_success: knownBool(original.result?.strict_success), purchase_success: knownBool(original.result?.purchase_success), fixture_success: knownBool(original.result?.fixture_success)},
    provenance: {original_run_id: text(original.run_id), original_source: text(original.source), verified: false, import_note: 'Imported into this browser; results were not rerun.'},
  };
  database.runs.unshift(run); database.events[runId] = []; database.annotations[runId] = [];
  const map = new Map();
  for (const event of data.events) {
    const saved = addEvent(runId, event.type, {...safeData(event.payload), imported_event_id: event.event_id});
    // Keep imported event provenance.
    saved.payload.synthetic_preview = false;
    map.set(event.event_id, saved.event_id);
  }
  for (const annotation of Array.isArray(data.annotations) ? data.annotations : []) {
    if (isObject(annotation) && map.has(annotation.event_id) && CATEGORIES.has(annotation.category) && typeof annotation.note === 'string') database.annotations[runId].push({annotation_id: id(), run_id: runId, event_id: map.get(annotation.event_id), category: annotation.category, note: annotation.note.slice(0, 2000), created_at: now()});
  }
  persist(); return run;
}
function evaluation(runs) {
  const fixture = runs.filter(run => run.mode === 'fixture').length;
  const imported = runs.filter(run => run.source === 'imported').length;
  const statCounts = {};
  for (const run of runs) statCounts[run.status] = (statCounts[run.status] || 0) + 1;
  return {schema_version: 'shopping-lab.evaluation.v1', status: 'not_available', total_runs: runs.length, formal_run_count: 0, excluded: {fixture: runs.filter(run => run.mode === 'fixture' && run.source !== 'imported').length, imported}, groups: [], diagnostics: {fixture_runs: fixture, imported_runs: imported, scripted_runs: runs.filter(run => run.model.provider === 'scripted').length, status_counts: statCounts}, notes: ['This preview runs fixture scripts.', 'Fixtures and imports are counted separately.', 'Time measures script execution.']};
}
function compare(left, right) {
  const diffs = [];
  for (const field of ['task_id', 'mode', 'source', 'model', 'runtime', 'strategy', 'environment_version']) if (JSON.stringify(left[field]) !== JSON.stringify(right[field])) diffs.push({field, left: clone(left[field] ?? null), right: clone(right[field] ?? null)});
  const pair = field => ({left: left[field] ?? null, right: right[field] ?? null, right_minus_left: typeof left[field] === 'number' && typeof right[field] === 'number' ? right[field] - left[field] : null});
  return {schema_version: 'shopping-lab.comparison.v1', comparable: false, same_task: left.task_id === right.task_id && left.mode === right.mode, warnings: ['Compare saved settings and records.'], differences:diffs, outcomes: {left: {status: left.status, result: clone(left.result), source: left.source}, right: {status: right.status, result: clone(right.result), source: right.source}}, efficiency: {steps: pair('steps'), elapsed_seconds: pair('elapsed_seconds'), model_calls: pair('model_calls')}, statistical_significance: null};
}

/* Export a copy. */
export function getExport(runId) {
  return clone({schema_version: SCHEMA, run: getRun(runId), events: database.events[runId] || [], annotations: database.annotations[runId] || []});
}
export async function request(path, options = {}) {
  const url = new URL(path, globalThis.location?.origin || 'https://preview.local');
  const route = url.pathname, method = (options.method || 'GET').toUpperCase();
  let body = {};
  if (options.body) {
    try { body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body; }
    catch { throw new Error('Request must contain valid JSON.'); }
    if (!isObject(body)) throw new Error('Request must be a JSON object.');
  }
  if (method === 'GET' && route === '/api/status') return {application: 'shopping-agent-lab', display_name: 'ShopTrace', version: '0.1.0-preview', platform: 'Browser preview', model_name: null, preview: true, upstream: {commit: COMMIT, license_status: 'Standalone fixture preview; upstream code and data are not bundled'}, capabilities: {fixture: true, shopsimulator_configured: false, llm_configured: false}, limitations: ['Interactive preview · fixture scripts', ...(hasStore ? ['Records stay in this browser and are removed when site data is cleared.'] : ['Browser storage is unavailable; records last only for this page session.'])]};
  if (method === 'GET' && route === '/api/tasks') return {tasks: TASKS.map(({id, title, query, source}) => ({id, title, query, source}))};
  if (route === '/api/runs') {
    if (method === 'GET') return clone({runs: database.runs});
    if (method === 'POST') return clone(createRun(body));
  }
  if (method === 'POST' && route === '/api/import') return clone(importRun(body.trajectory || body));
  if (route === '/api/evaluation' && ['GET', 'POST'].includes(method)) {
    if (method === 'POST' && (!Array.isArray(body.run_ids) || body.run_ids.some(value => typeof value !== 'string'))) throw new Error('Select valid run records.');
    return evaluation(method === 'GET' ? database.runs : [...new Set(body.run_ids)].map(getRun));
  }
  if (method === 'GET' && route === '/api/compare') return compare(getRun(url.searchParams.get('left')), getRun(url.searchParams.get('right')));
  const match = route.match(/^\/api\/runs\/([^/]+)(?:\/(events|annotations|cancel|export))?$/);
  if (match) {
    const runId = decodeURIComponent(match[1]), action = match[2], run = getRun(runId);
    if (!action && method === 'GET') return clone(run);
    if (action === 'events' && method === 'GET') {
      const after = Number(url.searchParams.get('after') || 0);
      if (!Number.isInteger(after) || after < 0) throw new Error('Invalid event cursor.');
      const events = (database.events[runId] || []).filter(event => event.sequence > after);
      return clone({events, next_sequence: events.length ? events.at(-1).sequence : after});
    }
    if (action === 'export' && method === 'GET') return getExport(runId);
    if (action === 'cancel' && method === 'POST') {
      if (!FINAL.has(run.status)) {
        clearInterval(timers.get(runId)); timers.delete(runId); run.status = 'cancelled'; run.completed_at = now(); run.termination_reason = 'preview_user_cancelled';
        addEvent(runId, 'status', {status: 'cancelled', reason: 'Demo stopped by user.'}); persist();
      }
      return clone(run);
    }
    if (action === 'annotations') {
      if (method === 'GET') return clone({annotations: database.annotations[runId] || []});
      if (method === 'POST') {
        if (!CATEGORIES.has(body.category) || typeof body.note !== 'string' || !body.note.trim() || body.note.length > 2000 || !(database.events[runId] || []).some(event => event.event_id === body.event_id)) throw new Error('Select a valid event and issue type, and enter a 1-2000 character note.');
        (database.annotations[runId] ||= []).push({annotation_id: id(), run_id: runId, event_id: body.event_id, category: body.category, note: body.note.trim(), created_at: now()}); persist();
        return clone({annotations: database.annotations[runId]});
      }
    }
  }
  throw new Error('Use the local app for this operation.');
}
