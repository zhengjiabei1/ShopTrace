import {scenarios, getScenario} from './scenarios.js';
import {validateFeedback, saveFeedback, listFeedback, formatFeedbackSummary, exportFeedback, saveSuggestion, listSuggestions} from './feedback.js';

const $ = id => document.getElementById(id);
const state = {scenario: null, revealed: 1, mobileCand: 'a', savedRec: null, savedHash: null};
const lessMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const svgNS = 'http://www.w3.org/2000/svg';
function el(tag, text, className) {const item = document.createElement(tag); if(text !== undefined) item.textContent = text; if(className) item.className = className; return item;}
function empty(item) {item.replaceChildren(); return item;}
function scrollTo(item) {item.scrollIntoView({behavior: lessMotion.matches ? 'auto' : 'smooth', block: 'start'});}
function message(text) {$('app-message').textContent = text; $('app-message').hidden = !text;}
function guard(fn) {return async (...args) => {try {await fn(...args);} catch (error) {message(error.message || 'This step could not finish. Please try again.');}};}
function drawIcon(scenario) {
  const svg = document.createElementNS(svgNS, 'svg'); svg.setAttribute('viewBox', '0 0 96 96'); svg.setAttribute('aria-hidden', 'true'); svg.setAttribute('fill', 'none');
  const shape = (type, attrs) => {const node = document.createElementNS(svgNS, type); for(const [key, value] of Object.entries(attrs)) node.setAttribute(key, value); svg.append(node);};
  const stroke = {stroke: 'currentColor', 'stroke-width': '3.4', 'stroke-linecap': 'round', 'stroke-linejoin': 'round'};
  if(scenario.id.includes('headphones')) {
    shape('path', {d: 'M23 53V43a25 25 0 0 1 50 0v10', ...stroke});
    shape('rect', {x: '17', y: '45', width: '17', height: '29', rx: '7', fill: 'currentColor', opacity: '.22'});
    shape('rect', {x: '62', y: '45', width: '17', height: '29', rx: '7', fill: 'currentColor', opacity: '.22'});
    shape('rect', {x: '17', y: '45', width: '17', height: '29', rx: '7', ...stroke});
    shape('rect', {x: '62', y: '45', width: '17', height: '29', rx: '7', ...stroke});
    shape('path', {d: 'M29 39a19 19 0 0 1 38 0', ...stroke, opacity: '.5'});
  } else if(scenario.id.includes('thermos')) {
    shape('path', {d: 'M28 29h40l-4 50H32L28 29Z', fill: 'currentColor', opacity: '.15'});
    shape('path', {d: 'M28 29h40l-4 50H32L28 29Z', ...stroke});
    shape('rect', {x: '26', y: '20', width: '44', height: '10', rx: '4', ...stroke});
    shape('path', {d: 'M39 13h18M34 44h28M37 66h22', ...stroke, opacity: '.6'});
  } else {
    shape('rect', {x: '26', y: '15', width: '44', height: '66', rx: '10', fill: 'currentColor', opacity: '.13'});
    shape('rect', {x: '26', y: '15', width: '44', height: '66', rx: '10', ...stroke});
    shape('path', {d: 'M39 25h18M50 39l-9 13h13l-9 13', ...stroke});
    for(const x of [37, 44, 51, 58]) shape('circle', {cx: String(x), cy: '72', r: '1.5', fill: 'currentColor'});
  }
  return svg;
}
function setJourney(stage) {
  for(const item of document.querySelectorAll('.journey li')) {
    const number = Number(item.dataset.stage); item.classList.toggle('current', number === stage); item.classList.toggle('complete', number < stage);
    if(number === stage) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
    item.querySelector('span').textContent = number < stage ? '✓' : String(number);
  }
}
function drawScenes() {
  const list = empty($('scenario-list'));
  for(const scenario of scenarios) {
    const button = el('button', undefined, 'scenario-card'); button.type = 'button'; button.dataset.scenarioId = scenario.id;
    const icon = el('span', undefined, 'scenario-icon'); icon.append(drawIcon(scenario));
    button.append(icon, el('h2', scenario.title), el('p', scenario.subtitle), el('span', 'Explore scenario  →', 'card-action'));
    button.onclick = () => pickScene(scenario.id); list.append(button);
  }
}
function pickScene(id) {
  const scenario = getScenario(id); if(!scenario) throw new Error('This scenario is unavailable. Please choose another.');
  state.scenario = scenario; state.revealed = 1; state.mobileCand = 'a'; state.savedRec = null; state.savedHash = null;
  $('selection-view').hidden = true; $('experience-view').hidden = false; $('review-section').hidden = true; $('saved-panel').hidden = true;
  $('review-form').reset(); $('review-error').hidden = true; $('copy-status').textContent = ''; $('summary-fallback').hidden = true;
  $('scenario-title').textContent = scenario.title; $('scenario-need').textContent = scenario.need;
  const pills = empty($('requirement-pills')); for(const need2 of scenario.requirements) pills.append(el('span', need2.text));
  setJourney(2); drawSteps(); drawCands(); drawJudges(); message(''); window.scrollTo({top: 0, behavior: 'auto'}); $('scenario-title').focus({preventScroll: true});
}
function drawSteps() {
  const scenario = state.scenario, steps = empty($('process-steps'));
  $('step-position').textContent = `Viewed ${state.revealed} / ${scenario.steps.length} steps`;
  scenario.steps.slice(0, state.revealed).forEach((step, index) => {
    const item = el('li', undefined, 'process-step'); item.dataset.step = String(index + 1); const content = el('div');
    content.append(el('h3', step.title), el('p', step.text));
    if(step.evidence_ids.length) {
      const refs = el('div', undefined, 'step-evidence');
      for(const candidate of scenario.candidates) {
        const matching = candidate.evidence.filter(evidence => step.evidence_ids.includes(evidence.id));
        if(matching.length) {const ref = el('button', `View candidate ${candidate.id.toUpperCase()} details`); ref.type = 'button'; ref.onclick = () => showDetail(candidate.id); refs.append(ref);}
      }
      content.append(refs);
    }
    item.append(el('span', String(index + 1)), content); steps.append(item);
  });
  const done = state.revealed >= scenario.steps.length;
  $('step-next').textContent = done ? 'Start my review  →' : 'Continue  →'; $('step-next').disabled = false;
  $('process-hint').textContent = done ? 'You have seen the process. Review the evidence in your own words.' : 'Take your time before drawing a conclusion.';
}
function nextStep() {
  if(state.revealed < state.scenario.steps.length) {state.revealed++; drawSteps();}
  else { $('review-section').hidden = false; $('step-next').textContent = 'Example complete ✓'; $('step-next').disabled = true; setJourney(3); scrollTo($('review-section')); $('review-heading').focus({preventScroll: true}); }
}
function specs(candidate) {
  const dl = el('dl', undefined, 'candidate-specs');
  for(const parameter of candidate.specs) {const row = el('div'); row.append(el('dt', parameter.label), el('dd', parameter.value)); dl.append(row);}
  return dl;
}
function evidence(candidate) {
  const block = el('div', undefined, 'candidate-evidence'); block.append(el('h4', 'Evidence shown on the page'));
  candidate.evidence.forEach((item, index) => {const row = el('div', undefined, 'evidence-item'); row.append(el('span', `Source ${index + 1}`), el('p', item.text)); block.append(row);});
  return block;
}
function drawCands() {
  const list = empty($('candidate-list'));
  for(const candidate of state.scenario.candidates) {
    const card = el('article', undefined, 'candidate-card'); card.id = `candidate-${candidate.id}`; card.setAttribute('aria-label', `Candidate ${candidate.id.toUpperCase()}`); card.setAttribute('role', 'tabpanel'); card.setAttribute('aria-labelledby', `candidate-tab-${candidate.id}`);
    const visual = el('div', undefined, 'candidate-visual'); visual.append(drawIcon(state.scenario), el('span', `Candidate ${candidate.id.toUpperCase()}`, 'candidate-letter'), el('span', 'Illustration', 'illustration-label'));
    const body = el('div', undefined, 'candidate-body'), title = el('div', undefined, 'candidate-title-row'), price = el('span', undefined, 'price');
    price.append(el('small', 'CNY'), document.createTextNode(String(candidate.price))); title.append(el('h3', candidate.name), price);
    const details = el('button', 'View all details  ↗', 'text-button'); details.type = 'button'; details.dataset.details = candidate.id; details.onclick = () => showDetail(candidate.id);
    body.append(title, specs(candidate), evidence(candidate), details); card.append(visual, body); list.append(card);
  }
  pickCand('a');
}
function pickCand(id) {
  state.mobileCand = id;
  for(const candidate of ['a', 'b']) {const selected = candidate === id; $(`candidate-${candidate}`)?.classList.toggle('mobile-active', selected); const tab = $(`candidate-tab-${candidate}`); tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;}
}
function showDetail(candId) {
  const candidate = state.scenario.candidates.find(item => item.id === candId); if(!candidate) return;
  $('details-title').textContent = `Candidate ${candidate.id.toUpperCase()} · ${candidate.name}`;
  empty($('details-content')).append(el('p', state.scenario.disclosure, 'dialog-note'), specs(candidate), evidence(candidate));
  $('details-dialog').showModal();
}
function drawJudges() {
  const target = empty($('judgment-list'));
  state.scenario.requirements.forEach((need2, index) => {
    const card = el('section', undefined, 'judgment-card'), heading = el('h3', `${index + 1}. ${need2.text}`); heading.id = `question-${need2.id}`; card.append(heading); const grid = el('div', undefined, 'judgment-candidates');
    for(const candidate of state.scenario.candidates) {
      const field = el('fieldset'); field.dataset.question = `${candidate.id}-${need2.id}`; field.setAttribute('aria-describedby', heading.id); field.append(el('legend', `Candidate ${candidate.id.toUpperCase()}`));
      const options = el('div', undefined, 'choice-options');
      for(const [value, label] of [['met', 'Meets'], ['unmet', 'Does not meet'], ['unknown', 'Not enough info']]) {
        const choice = el('label'), input = el('input'); input.type = 'radio'; input.name = `judgment-${candidate.id}-${need2.id}`; input.value = value; input.id = `${input.name}-${value}`;
        choice.append(input, el('span', label)); options.append(choice);
      }
      field.append(options);
      for(const item of candidate.evidence.filter(item => item.requirement_ids.includes(need2.id))) field.append(el('p', item.text, 'judgment-evidence'));
      grid.append(field);
    }
    card.append(grid); target.append(card);
  });
}
function readDraft() {
  const values = new FormData($('review-form')), judgments = {};
  for(const candidate of state.scenario.candidates) {judgments[candidate.id] = {}; for(const need2 of state.scenario.requirements) judgments[candidate.id][need2.id] = values.get(`judgment-${candidate.id}-${need2.id}`) || undefined;}
  return {judgments, preference: values.get('preference') || undefined, comment: $('feedback-comment').value};
}
function sendReview(event) {
  event.preventDefault(); const draft = readDraft(), validation = validateFeedback(draft, state.scenario);
  if(!validation.valid) {
    $('review-error').textContent = validation.errors[0]; $('review-error').hidden = false; scrollTo($('review-error'));
    for(const field of $('judgment-list').querySelectorAll('fieldset')) {const missing = !field.querySelector('input:checked'); field.classList.toggle('unanswered', missing);}
    return;
  }
  const digest = JSON.stringify(draft);
  try {if(state.savedHash !== digest) {state.savedRec = saveFeedback(validation.record); state.savedHash = digest;}} catch(error) {$('review-error').textContent = error.message; $('review-error').hidden = false; scrollTo($('review-error')); return;}
  $('review-error').hidden = true; for(const field of $('judgment-list').querySelectorAll('fieldset')) field.classList.remove('unanswered');
  $('saved-panel').hidden = false; $('copy-status').textContent = ''; $('summary-fallback').hidden = true; countSaved(); scrollTo($('saved-panel')); $('saved-panel').focus({preventScroll: true});
}
async function copyText(content, statTarget) {
  try {
    if(!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable'); await navigator.clipboard.writeText(content);
    statTarget.textContent = 'Copied. You can share it with the project owner yourself. Nothing was sent automatically.';
  } catch {
    let fallback;
    if(statTarget.closest('dialog')) {fallback = statTarget.parentElement.querySelector('textarea.summary-fallback'); if(!fallback) {fallback = el('textarea', undefined, 'summary-fallback'); fallback.readOnly = true; fallback.setAttribute('aria-label', 'Summary for manual copying'); statTarget.after(fallback);}}
    else fallback = $('summary-fallback');
    fallback.value = content; fallback.hidden = false; fallback.focus(); fallback.select(); statTarget.textContent = 'Automatic copying is unavailable. The summary below is selected for manual copying.';
  }
}
function copySum() {if(state.savedRec) return copyText(formatFeedbackSummary(state.savedRec, state.scenario), $('copy-status'));}
function showScenes() {
  $('selection-view').hidden = false; $('experience-view').hidden = true; state.scenario = null; setJourney(1); message(''); window.scrollTo({top: 0, behavior: 'auto'}); $('selection-view').focus({preventScroll: true});
}
function countSaved() {$('record-count').textContent = String(listFeedback().length);}
function drawRecord() {
  const list = empty($('records-list')), records = listFeedback();
  if(!records.length) list.append(el('p', 'No feedback saved yet. Try a scenario and save your review on this device.', 'dialog-note'));
  for(const record of records) {
    const scenario = getScenario(record.scenario_id), card = el('article', undefined, 'record-card');
    card.append(el('h3', scenario?.title || 'Shopping scenario review'), el('p', `${new Date(record.created_at).toLocaleString('en-US')} · Saved on this device only`));
    const copy = el('button', 'Copy this review', 'text-button'), status = el('p', undefined, 'copy-status'); copy.type = 'button'; copy.onclick = guard(() => copyText(formatFeedbackSummary(record, scenario), status));
    const details = el('details'); details.append(el('summary', 'View summary'), el('pre', formatFeedbackSummary(record, scenario))); card.append(copy, status, details); list.append(card);
  }
  const ideas = listSuggestions();
  if(ideas.length) {list.append(el('h3', 'My scenario ideas')); for(const suggestion of ideas){const card = el('article', undefined, 'record-card'); card.append(el('p', suggestion.need), el('p', 'Scenario idea saved on this device.')); const copy = el('button', 'Copy this idea', 'text-button'), status = el('p', undefined, 'copy-status'); copy.type = 'button'; copy.onclick = guard(() => copyText(`ShopTrace · Scenario idea\n${suggestion.need}\n\nSaved on this device. Not sent to the team.`, status)); card.append(copy, status); list.append(card);}}
}
function showRecord() {drawRecord(); $('records-dialog').showModal();}
function download2() {
  const data = exportFeedback(), blob = new Blob([data], {type: 'application/json;charset=utf-8'}), url = URL.createObjectURL(blob);
  const anchor = el('a'); anchor.href = url; anchor.download = `ShopTrace-device-feedback-${new Date().toISOString().slice(0, 10)}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function sendIdea(event) {
  event.preventDefault(); const value = $('suggestion-need').value.trim(); if(!value){$('suggestion-status').textContent = 'Describe the shopping scenario you would like to try.'; return;}
  try {saveSuggestion(value); $('suggestion-status').textContent = 'Suggestion saved on this device. Not sent to the team.'; $('suggestion-need').value = '';} catch(error) {$('suggestion-status').textContent = error.message;}
}
$('step-next').onclick = nextStep;
$('back-to-scenarios').onclick = showScenes; $('try-again').onclick = showScenes;
$('review-form').addEventListener('submit', guard(sendReview));
$('copy-summary').onclick = guard(copySum); $('records-button').onclick = guard(showRecord); $('export-feedback').onclick = guard(download2);
$('suggestion-button').onclick = () => {$('suggestion-status').textContent = ''; $('suggestion-dialog').showModal();};
$('suggestion-form').addEventListener('submit', guard(sendIdea));
for(const id of ['a', 'b']) {
  $(`candidate-tab-${id}`).onclick = () => pickCand(id);
  $(`candidate-tab-${id}`).onkeydown = event => {if(['ArrowLeft', 'ArrowRight'].includes(event.key)){event.preventDefault(); const next = id === 'a' ? 'b' : 'a'; pickCand(next); $(`candidate-tab-${next}`).focus();}};
}
for(const button of document.querySelectorAll('.dialog-close')) button.onclick = () => button.closest('dialog').close();
for(const dialog of document.querySelectorAll('dialog')) dialog.addEventListener('click', event => {if(event.target === dialog){const box = dialog.getBoundingClientRect(); if(event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();}});
$('feedback-comment').maxLength = 1000; $('suggestion-need').maxLength = 1500;
try {drawScenes(); countSaved();} catch(error) {message(error.message || 'The page could not load. Please refresh and try again.');}
