/* Saved run report. */
(() => {
  'use strict';
  const report = window.SHOPTRACE_REPORT || {suites: []};
  const $ = id => document.getElementById(id);
  const state = {suite: 0, task: '', arm: '', fault: '', seed: '', outcome: '', attempt: null, step: 0};
  const defaults = [
    ...[
      {id: 'procurement-dev', title: 'Recovery comparison', description: 'Compare recovery settings under the same tasks and faults.'},
      {id: 'procurement-heldout', title: 'Procurement held-out', description: 'Check the selected setting on held-out constraint combinations.'},
      {id: 'procurement-memory', title: 'Page memory', description: 'Compare context settings on cross-page tasks.'},
      {id: 'procurement-pilot', title: 'Procurement pilot', description: 'Cross-page shopping and fault recovery checks.'},
      {id: 'release-regression', title: 'Release regression', description: 'Original browser tasks checked with the integrated release.'}
    ].filter(base => (report.suites || []).some(suite => suite.id === base.id)),
    ...((report.suites || []).some(suite => suite.id === 'english')
      ? [{id: 'english', title: 'Acceptance · 9ade785', description: 'Earlier browser task acceptance results.'}] : []),
    {id: 'stability', title: 'Stability', description: 'Repeated runs of a fixed version, with every outcome retained.'},
    {id: 'development', title: 'Development', description: 'Compare task outcomes and input cost across settings.'},
    {id: 'heldout', title: 'Held-out', description: 'Check the selected setting on held-out cases.'}
  ];
  const suites = defaults.map(base => ({...(report.suites || []).find(suite => suite.id === base.id), ...base,
    attempts: (report.suites || []).find(suite => suite.id === base.id)?.attempts || []}));
  for (const suite of (report.suites || [])) if (!suites.some(item => item.id === suite.id)) suites.push(suite);
  const taskLabels = {
    'browser-headphones-commute': 'Commuter headphones', 'browser-thermos-variant': 'Thermos options',
    'browser-powerbank-spec': 'Power bank specs', 'browser-lamp-adjustable': 'Reading lamp',
    'browser-thermos-budget': 'No suitable item', 'browser-headphones-unknown': 'Missing information'
  };
  const armLabels = {full_history: 'Full history', latest_observation: 'Latest observation', page_memory: 'Page memory', baseline: 'Baseline', verify: 'Check first', adaptive: 'State-guided', 'finish-reason-clarification': 'Finish-reason update'};
  const faultNames = {none: 'No fault', ack_lost: 'Lost feedback', no_effect: 'Add not applied', stockout: 'Stock changed'};
  const statLabels = {completed: 'Completed', failed: 'Failed', budget_exceeded: 'Budget reached', cancelled: 'Cancelled', pending: 'Pending', running: 'Running'};
  const srcLabels = {browser_model: 'Model', browser_smoke: 'Script', api_mock_browser_contract: 'Mock API', synthetic_fixture: 'Sample script'};
  const fmt = number => Number.isFinite(number) ? new Intl.NumberFormat('en-US', {maximumFractionDigits: 1}).format(number) : '—';
  const pct = (part, total) => total ? `${Math.round(part / total * 1000) / 10}%` : '—';
  const rawTitles = {
    '\u901a\u52e4\u8033\u673a': 'Commuter headphones', '\u4fdd\u6e29\u676f\u89c4\u683c': 'Thermos options', '\u79fb\u52a8\u7535\u6e90\u53c2\u6570': 'Power bank specs',
    '\u9605\u8bfb\u53f0\u706f': 'Reading lamp', '\u6ca1\u6709\u5408\u9002\u5546\u54c1': 'No suitable item', '\u4fe1\u606f\u4e0d\u8db3\u65f6\u505c\u6b62': 'Missing information',
    '\u6838\u5bf9\u8033\u673a\u8fde\u63a5\u4e0e\u9884\u7b97': 'Headphone connection and budget', '\u6838\u5bf9\u9009\u4e2d\u89c4\u683c\u540e\u7684\u676f\u4ef7': 'Thermos options and final price',
    '\u6bd4\u8f83\u79fb\u52a8\u7535\u6e90\u53c2\u6570': 'Power bank specifications', '\u6838\u5bf9\u53f0\u706f\u7684\u5fc5\u8981\u529f\u80fd': 'Lamp requirements',
    '\u4fdd\u7559\u6ca1\u6709\u5408\u9002\u5546\u54c1\u7684\u7ed3\u8bba': 'No suitable item', '\u6838\u5bf9\u672a\u5c55\u793a\u7684\u8fde\u63a5\u8d44\u6599': 'Missing connection details'
  };
  const taskName = item => taskLabels[item.task_id] || rawTitles[item.task_title]
    || (/[\u3400-\u9fff]/.test(item.task_title || '') ? `Task ${item.task_id}` : item.task_title)
    || item.task_id || 'Untitled task';
  function reasonText(item) {
    const reasons = {
      '\u8d2d\u7269\u6e05\u5355\u4e2d\u7684\u5b9e\u9645\u5546\u54c1、\u9009\u4e2d\u89c4\u683c\u548c\u6700\u7ec8\u4ef7\u683c\u6ee1\u8db3\u672c\u4efb\u52a1\u6761\u4ef6。': 'The cart item, selected options, and final price meet the task requirements.',
      '\u5df2\u5728\u5b9e\u9645\u8be6\u60c5\u9875\u4fdd\u7559\u7f3a\u5931\u7684\u8fde\u63a5\u4fe1\u606f，\u672a\u5c06\u672a\u77e5\u6761\u4ef6\u5f53\u4f5c\u7b26\u5408，\u4e5f\u672a\u52a0\u5165\u6e05\u5355。': 'Connection details are missing. The run kept that uncertainty and left the cart empty.',
      '\u672c\u5730\u76ee\u5f55\u6ca1\u6709\u6ee1\u8db3\u5168\u90e8\u6761\u4ef6\u7684\u5546\u54c1；\u6d4f\u89c8\u5668\u5c55\u793a\u4e86\u5e94\u7528\u9884\u7b97\u540e\u7684\u7a7a\u7ed3\u679c，\u5e76\u4fdd\u7559\u7a7a\u6e05\u5355。': 'The budget filter returned no matching items, and the cart remained empty.',
      '\u8bf7\u5728\u8d2d\u7269\u6e05\u5355\u9875\u6838\u5bf9\u6700\u7ec8\u5546\u54c1\u4e0e\u89c4\u683c\u540e\u7ed3\u675f。': 'The run ended before checking the final item and options on the cart page.',
      '\u9700\u5728\u7f3a\u5c11\u8fde\u63a5\u4fe1\u606f\u7684\u8033\u673a\u8be6\u60c5\u9875\u660e\u786e\u62a5\u544a\u963b\u585e，\u5e76\u4fdd\u7559\u7a7a\u6e05\u5355。': 'The run must report missing connection details as blocked and leave the cart empty.',
      not_finished: 'The run ended without a known outcome. Check the error and stop reason.',
      not_started: 'The task has not started.'
    };
    const raw = item?.result_reason;
    return reasons[raw] || (/[\u3400-\u9fff]/.test(raw || '') ? 'See the original result record for details.' : raw)
      || (item?.success === null ? 'The outcome is unknown. Check the error and stop reason.' : 'No result description.');
  }
  const armName = item => armLabels[item.arm] || item.arm || 'Default setting';
  const isFinished = item => !['pending', 'running', 'planned', 'not_started'].includes(item.status);
  const isLive = item => item.source === 'browser_model';
  const outcome = item => item.success === true ? 'passed' : item.success === false ? 'failed' : 'unknown';
  const outLabel = item => item.success === true ? 'Passed' : item.success === false ? 'Failed' : 'Unknown';
  const mean = rows => rows.length ? rows.reduce((sum, n) => sum + n, 0) / rows.length : null;
  function node(tag, value, cls) {const el = document.createElement(tag); if (value !== undefined) el.textContent = value; if (cls) el.className = cls; return el;}
  function safeUrl(value, allowExt = true) {
    if (typeof value !== 'string' || !value.trim() || value.startsWith('//')) return null;
    if (/^[a-z][a-z\d+.-]*:/i.test(value)) {
      try {const parsed = new URL(value); return allowExt && parsed.protocol === 'https:' && !parsed.username && !parsed.password ? value : null;} catch {return null;}
    }
    return value.startsWith('/') || value.split('/').includes('..') ? null : value;
  }
  function setLink(id, value) {const el = $(id), url = safeUrl(value); if (url) {el.href = url; el.hidden = false;} else {el.removeAttribute('href'); el.hidden = true;}}
  function curSuite() {return suites[state.suite];}
  function allRows() {return curSuite().attempts || [];}
  function filterRows() {return allRows().filter(item => (!state.task || item.task_id === state.task) && (!state.arm || (item.arm || '') === state.arm) && (!state.fault || item.conditions?.fault === state.fault) && (!state.seed || String(item.conditions?.seed) === state.seed) && (!state.outcome || outcome(item) === state.outcome));}
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])]));
    return value;
  }
  function cfgKey(item) {return JSON.stringify(canonical({model: item.model || report.model || null, arm: item.arm || '', protocol: item.protocol || null, conditions: item.conditions || null}));}
  function cfgName(item) {
    const cond = item.conditions || {}, parts = [armName(item)];
    if (item.recovery && item.context_mode) parts.push(armLabels[item.context_mode] || item.context_mode);
    if (cond.fault) parts.push(faultNames[cond.fault] || cond.fault);
    if (cond.seed !== null && cond.seed !== undefined) parts.push(`Seed ${cond.seed}`);
    return parts.join(' · ');
  }
  function cfgGroups(rows) {
    const groups = new Map();
    rows.filter(isLive).forEach(item => {const key = cfgKey(item); if (!groups.has(key)) groups.set(key, {key, rows: [], arm: item.arm || ''}); groups.get(key).rows.push(item);});
    const indices = new Map();
    groups.forEach(group => {const name = cfgName(group.rows[0]), index = (indices.get(name) || 0) + 1; indices.set(name, index); group.label = `${name}${index > 1 ? ` · protocol ${index}` : ''}`;});
    return [...groups.values()];
  }
  function statistics(rows) {
    const live = rows.filter(isLive), finished = live.filter(isFinished), known = finished.filter(item => typeof item.success === 'boolean');
    const passed = known.filter(item => item.success).length;
    const inputs = finished.map(item => item.input_tokens).filter(Number.isFinite), outputs = finished.map(item => item.output_tokens).filter(Number.isFinite);
    return {live: live.length, finished: finished.length, known: known.length, passed, excluded: rows.length - live.length,
      unknown: finished.length - known.length, calls: finished.map(item => item.model_calls).filter(Number.isFinite).reduce((a, b) => a + b, 0),
      input: inputs.length === finished.length && finished.length ? inputs.reduce((a, b) => a + b, 0) : null,
      output: outputs.length === finished.length && finished.length ? outputs.reduce((a, b) => a + b, 0) : null,
      elapsed: mean(finished.map(item => item.elapsed_seconds).filter(Number.isFinite)),
      timed: finished.filter(item => Number.isFinite(item.elapsed_seconds)).length};
  }
  function drawHeader() {
    $('model-name').textContent = report.model || 'Awaiting results';
    $('source-commit').textContent = report.source_commit ? `Source ${report.source_commit.slice(0, 8)}` : report.source_commits?.length > 1 ? 'Multiple versions' : 'Version unavailable';
    try {$('report-date').textContent = report.generated_at ? new Intl.DateTimeFormat('en-US', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}).format(new Date(report.generated_at)) : 'Report not generated';} catch {$('report-date').textContent = 'Date unavailable';}
    setLink('archive-link', report.archive_url);
    const live = safeUrl(report.live_url);
    if (live) {
      setLink('live-link', live); $('live-badge').textContent = 'Link configured'; $('live-badge').className = 'badge passed';
      $('live-description').textContent = 'Open the workbench to run tasks and review progress and usage.';
    }
    const hasData = suites.some(suite => (suite.attempts || []).length);
    document.querySelector('.report-workspace').hidden = !hasData; $('evidence').hidden = !hasData; $('no-data').hidden = hasData;
    $('footer-note').textContent = 'Browse by task. Replay each step.';
    return hasData;
  }
  function renderTabs() {
    $('suite-tabs').replaceChildren();
    suites.forEach((suite, index) => {
      const button = node('button', undefined, 'suite-tab'); button.type = 'button'; button.setAttribute('role', 'tab');
      button.setAttribute('aria-selected', String(index === state.suite)); button.setAttribute('tabindex', index === state.suite ? '0' : '-1');
      button.append(node('strong', suite.title), node('span', `${(suite.attempts || []).length} runs`));
      button.addEventListener('click', () => pickSuite(index));
      button.addEventListener('keydown', event => {if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {event.preventDefault(); pickSuite((index + (event.key === 'ArrowRight' ? 1 : suites.length - 1)) % suites.length); $('suite-tabs').children[state.suite].focus();}});
      $('suite-tabs').append(button);
    });
  }
  function pickSuite(index) {state.suite = index; state.task = ''; state.arm = ''; state.fault = ''; state.seed = ''; state.outcome = ''; state.attempt = null; state.step = 0; renderTabs(); fillFilter(); drawView();}
  function fillFilter() {
    const rows = allRows(), tasks = new Map(), arms = new Map(), faults = new Map(), seeds = new Map();
    rows.forEach(item => {tasks.set(item.task_id, taskName(item)); arms.set(item.arm || '', armName(item));
      const cond = item.conditions || {};
      if (cond.fault) faults.set(cond.fault, faultNames[cond.fault] || cond.fault);
      if (cond.seed !== null && cond.seed !== undefined) seeds.set(String(cond.seed), `Seed ${cond.seed}`);
    });
    for (const [id, values, label] of [['task-filter', tasks, 'All tasks'], ['arm-filter', arms, 'All settings']]) {
      $(id).replaceChildren(new Option(label, ''));
      values.forEach((name, value) => {if (value) $(id).append(new Option(name, value));});
      $(id).value = id === 'task-filter' ? state.task : state.arm;
    }
    for (const [id, values, key, label] of [['fault-filter', faults, 'fault', 'All faults'], ['seed-filter', seeds, 'seed', 'All seeds']]) {
      $(id).replaceChildren(new Option(label, ''));
      values.forEach((name, value) => $(id).append(new Option(name, value)));
      $(id).value = state[key]; $(id).parentElement.hidden = !values.size;
    }
    $('result-filter').value = state.outcome;
  }
  function metric(label, value, detail, accent) {const el = node('div', undefined, `metric${accent ? ' accent' : ''}`); el.append(node('span', label, 'metric-label'), node('strong', value, value === 'By setting' ? 'textual' : undefined), node('small', detail)); return el;}
  function drawStats(rows) {
    const stats = statistics(rows), groups = cfgGroups(rows), mixed = groups.length > 1;
    $('metrics').replaceChildren(
      metric('Known-result pass rate', mixed ? 'By setting' : pct(stats.passed, stats.known), mixed ? `${groups.length} settings; rates shown separately` : `${stats.passed} passed / ${stats.known} known outcomes`, true),
      metric('Result coverage', pct(stats.known, stats.finished), `${stats.unknown} finished runs have unknown outcomes`),
      metric('All finished runs', `${stats.passed} / ${stats.finished}`, 'Passed / all finished runs'),
      metric('Model calls / input tokens', fmt(stats.calls), `Input ${fmt(stats.input)} · output ${fmt(stats.output)}`),
      metric('Mean run time', stats.elapsed === null ? '—' : `${fmt(stats.elapsed)}s`, `${stats.timed} timed / ${stats.finished} finished · ${stats.excluded} non-model records excluded`)
    );
    const comparison = $('configuration-comparison'); comparison.hidden = !mixed; comparison.replaceChildren();
    if (mixed) {
      comparison.append(node('h3', 'Results by setting'));
      comparison.append(node('p', 'Settings are evaluated separately. Unknown outcomes remain visible.', 'configuration-note'));
      const cards = node('div', undefined, 'configuration-grid');
      groups.forEach(group => {
        const data = statistics(group.rows), card = node('div', undefined, 'configuration-card'); card.append(node('strong', group.label));
        const values = node('dl');
        for (const [label, value] of [['Known-result pass rate', `${pct(data.passed, data.known)} · ${data.passed}/${data.known}`], ['Result coverage', `${pct(data.known, data.finished)} · ${data.unknown} unknown`], ['Passed / all finished', `${data.passed} / ${data.finished}`]]) {
          const pair = node('div'); pair.append(node('dt', label), node('dd', value)); values.append(pair);
        }
        card.append(values); cards.append(card);
      });
      comparison.append(cards);
    }
  }
  function drawTasks() {
    const groups = new Map(), rowsToShow = allRows().filter(item => (!state.arm || item.arm === state.arm) && (!state.fault || item.conditions?.fault === state.fault) && (!state.seed || String(item.conditions?.seed) === state.seed));
    const configs = cfgGroups(rowsToShow), mixed = configs.length > 1;
    const labels = new Map(configs.map(group => [group.key, group.label]));
    rowsToShow.forEach(item => {const key = mixed ? `${item.task_id}|${cfgKey(item)}` : item.task_id; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(item);});
    $('task-bars').replaceChildren();
    groups.forEach(rows => {
      const taskId = rows[0].task_id;
      const stats = statistics(rows), button = node('button', undefined, `task-bar${state.task === taskId ? ' selected' : ''}`); button.type = 'button';
      const line = node('div', undefined, 'task-bar-top'); line.append(node('strong', taskName(rows[0])), node('span', `${stats.passed} / ${stats.known} / ${stats.finished}`, 'bar-count'));
      const progress = node('progress'); progress.max = Math.max(1, stats.finished); progress.value = stats.passed; progress.setAttribute('aria-label', `${taskName(rows[0])}: ${stats.passed} passed of ${stats.finished} finished`);
      button.append(line, progress, node('small', `${mixed ? `${labels.get(cfgKey(rows[0])) || armName(rows[0])} · ` : ''}${stats.unknown} unknown · ${pct(stats.known, stats.finished)} coverage`));
      button.addEventListener('click', () => {state.task = state.task === taskId ? '' : taskId; state.attempt = null; $('task-filter').value = state.task; drawView();});
      $('task-bars').append(button);
    });
    if (!groups.size) $('task-bars').append(node('p', 'No task records in this stage yet.', 'empty-list'));
  }
  function drawRuns(rows) {
    $('attempt-list').replaceChildren(); $('attempts-count').textContent = `${rows.length} records`;
    rows.forEach(item => {
      const button = node('button', undefined, 'attempt-row'); button.type = 'button'; button.setAttribute('aria-pressed', String(state.attempt === item));
      const name = node('span', undefined, 'attempt-name'); const detail = node('small', `Run ${item.repeat || 1} · ${(item.id || '').slice(0, 8)}`); detail.append(node('span', srcLabels[item.source] || item.source || 'Unknown source', 'source-label')); name.append(node('strong', taskName(item)), detail);
      const badge = node('span', outLabel(item), `badge ${outcome(item)}`);
      button.append(name, node('span', armName(item), 'attempt-arm'), badge, node('span', Number.isFinite(item.elapsed_seconds) ? `${fmt(item.elapsed_seconds)}s` : '—', 'attempt-time'), node('span', `${fmt(item.model_calls)} calls`, 'attempt-calls'));
      button.addEventListener('click', () => {state.attempt = item; state.step = Math.max(0, (item.steps || []).length - 1); drawRuns(rows); drawTrace();});
      $('attempt-list').append(button);
    });
    if (!rows.length) $('attempt-list').append(node('p', 'No matching runs. Change the task, setting, or outcome filter.', 'empty-list'));
  }
  function drawView() {
    const rows = filterRows();
    setLink('archive-link', curSuite().archive_url || report.archive_url);
    $('suite-intro').textContent = defaults.find(item => item.id === curSuite().id)?.description || curSuite().description || '';
    $('filter-count').textContent = `Showing ${rows.length} / ${allRows().length} saved records`;
    if (!rows.includes(state.attempt)) {state.attempt = rows[0] || null; state.step = state.attempt ? Math.max(0, (state.attempt.steps || []).length - 1) : 0;}
    drawStats(rows); drawTasks(); drawRuns(rows); drawTrace();
  }
  function setInspect(tab) {
    for (const key of ['dom', 'action']) {$(`${key}-tab`).setAttribute('aria-selected', String(key === tab)); $(`${key}-panel`).hidden = key !== tab;}
  }
  function drawTrace() {
    const item = state.attempt, steps = item?.steps || [], step = steps[state.step];
    $('selected-outcome').textContent = item ? outLabel(item) : 'No selection'; $('selected-outcome').className = `badge ${item ? outcome(item) : 'pending'}`;
    $('run-summary').replaceChildren();
    if (item) {
      const title = node('div'); title.append(node('strong', `${taskName(item)} · ${cfgName(item)} · run ${item.repeat || 1}`), node('p', `${srcLabels[item.source] || item.source || 'Unknown source'} · ${statLabels[item.status] || item.status || 'Unknown status'} · run_id ${item.id || 'Not provided'}`));
      const links = node('div', undefined, 'run-links');
      for (const [value, label] of [[item.trajectory_url, 'Trace JSON ↗'], [item.result_url, 'Result JSON ↗']]) {const url = safeUrl(value); if (url) {const a = node('a', label); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; links.append(a);}}
      $('run-summary').append(title, links);
    }
    $('page-address').textContent = step?.url || 'Page URL unavailable';
    $('dom-text').textContent = step?.text || 'No page text was saved for this observation.';
    $('action-text').textContent = step ? JSON.stringify({action: step.action ?? null, error: step.error ?? null}, null, 2) : 'No action recorded.';
    const notes = $('step-notes'); notes.replaceChildren(); notes.hidden = !(step?.notes || []).length;
    for (const note of step?.notes || []) {
      if (note.type === 'memory') {
        notes.append(node('strong', `Page memory · ${note.pages ?? '—'} pages`));
        notes.append(node('p', `${fmt(note.characters)} characters · ${note.clipped_pages || 0} clipped · ${note.evicted_pages || 0} evicted`));
        if (note.loading) notes.append(node('p', 'Loading page; prior facts retained.'));
      } else {
        notes.append(node('strong', `${armLabels[note.mode] || note.mode || 'Recovery'} · ${note.state || 'neutral'}`));
        notes.append(node('p', note.guidance || 'No extra recovery instruction.'));
      }
    }
    const screenshot = safeUrl(step?.screenshot, false); $('screenshot').hidden = !screenshot; $('screenshot-empty').hidden = Boolean(screenshot); $('expand-image').disabled = !screenshot;
    if (screenshot) {$('screenshot').src = screenshot; $('screenshot').alt = `${item ? taskName(item) : 'Browser'}: step ${step.step ?? state.step} screenshot`;} else {$('screenshot').removeAttribute('src');}
    $('screenshot-caption').textContent = step ? `Observation ${step.step ?? state.step} · ${screenshot ? 'Page screenshot' : 'No screenshot file'}` : 'No screenshot';
    $('step-count').textContent = steps.length ? `${state.step + 1} / ${steps.length}` : '0 / 0'; $('step-range').max = String(Math.max(0, steps.length - 1)); $('step-range').value = String(state.step); $('step-range').disabled = steps.length < 2;
    $('previous-step').disabled = !steps.length || state.step <= 0; $('next-step').disabled = !steps.length || state.step >= steps.length - 1;
    const result = $('result-detail'); result.className = `result-detail ${item ? outcome(item) : ''}`; result.replaceChildren();
    result.append(node('strong', item ? `${outLabel(item)} · task result` : 'Select a run to view its result'));
    result.append(node('p', item ? reasonText(item) : 'No result selected.'));
    if (item?.time_note) result.append(node('p', item.time_note));
    if (item?.conditions?.fault) {
      const data = item.metrics || {}, parts = [];
      if (Number.isFinite(data.duplicates)) parts.push(`Duplicate units: ${data.duplicates}`);
      if (typeof data.fault_triggered === 'boolean') parts.push(`Fault triggered: ${data.fault_triggered ? 'yes' : 'no'}`);
      if (parts.length) result.append(node('p', parts.join(' · ')));
    }
    if (item?.error) result.append(node('pre', `Error: ${typeof item.error === 'string' ? item.error : JSON.stringify(item.error, null, 2)}`));
    if (item?.finish) result.append(node('pre', `Original finish record: ${JSON.stringify(item.finish, null, 2)}`));
  }
  $('task-filter').addEventListener('change', event => {state.task = event.target.value; state.attempt = null; drawView();});
  $('arm-filter').addEventListener('change', event => {state.arm = event.target.value; state.attempt = null; drawView();});
  $('result-filter').addEventListener('change', event => {state.outcome = event.target.value; state.attempt = null; drawView();});
  for (const key of ['fault', 'seed']) $(`${key}-filter`).addEventListener('change', event => {state[key] = event.target.value; state.attempt = null; drawView();});
  $('step-range').addEventListener('input', event => {state.step = Number(event.target.value); drawTrace();});
  $('previous-step').addEventListener('click', () => {state.step = Math.max(0, state.step - 1); drawTrace();});
  $('next-step').addEventListener('click', () => {state.step = Math.min((state.attempt?.steps || []).length - 1, state.step + 1); drawTrace();});
  for (const tab of ['dom', 'action']) $(`${tab}-tab`).addEventListener('click', () => setInspect(tab));
  $('screenshot').addEventListener('error', () => {$('screenshot').hidden = true; $('screenshot-empty').hidden = false; $('screenshot-empty').querySelector('strong').textContent = 'Screenshot could not be loaded'; $('expand-image').disabled = true;});
  $('expand-image').addEventListener('click', () => {if (!$('screenshot').hidden) {$('dialog-image').src = $('screenshot').src; $('image-dialog').showModal();}});
  $('close-dialog').addEventListener('click', () => $('image-dialog').close());
  if (drawHeader()) {renderTabs(); fillFilter(); drawView();}
})();
