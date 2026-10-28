import { getScenario } from './scenarios.js';

// Separate storage for the experience page.
export const localStorageKey = 'shoptrace.experience.v1';
const STORE_VER = 'shoptrace.experience.v1';
const SCHEMA_VER = 'human-feedback.v1';
const JUDGMENTS = new Set(['met', 'unmet', 'unknown']);
const PREFS = new Set(['a', 'b', 'neither', 'unsure']);
// Match saved v1 requests without rewriting their original text.
const oldNeeds = {
  "commute-headphones": "\u6211\u60f3\u4e70\u4e00\u526f\u901a\u52e4\u7528\u7684\u8033\u673a\uff0c\u9884\u7b97\u4e0d\u8d85\u8fc7300\u5143\uff0c\u8981\u9ed1\u8272\u3001\u65e0\u7ebf\u7684\u3002",
  "daily-thermos": "\u6211\u60f3\u4e70\u4e00\u4e2a\u4fdd\u6e29\u676f\uff0c\u4ef7\u683c\u4e0d\u8d85\u8fc7100\u5143\uff0c\u8981\u84dd\u8272\uff0c\u5bb9\u91cf500ml\u3002",
  "travel-power-bank": "\u6211\u51fa\u5dee\u60f3\u5e26\u4e00\u4e2a\u79fb\u52a8\u7535\u6e90\uff0c\u9884\u7b97\u4e0d\u8d85\u8fc7150\u5143\uff0c\u5bb9\u91cf\u898110000mAh\uff0c\u5546\u54c1\u9875\u9762\u8981\u660e\u786e\u5c55\u793a\u98de\u673a\u643a\u5e26\u6807\u8bc6\u4fe1\u606f\u3002"
};

const judgeNames = { met: 'Meets', unmet: 'Does not meet', unknown: 'Not enough information' };
const prefNames = { a: 'Prefer candidate A', b: 'Prefer candidate B', neither: 'Choose neither', unsure: 'Not sure yet' };

function createId(prefix) {
  const uuid = globalThis.crypto?.randomUUID?.();
  return `${prefix}-${uuid || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`}`;
}

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function makeOrigin() {
  return {
    mode: 'synthetic_participation_demo',
    model_called: false,
    purchase_attempted: false,
    submitted_to_team: false,
  };
}

function cleanReply(record) {
  return {
    schema_version: SCHEMA_VER,
    id: record.id,
    scenario_id: record.scenario_id,
    scenario_version: record.scenario_version,
    need: record.need,
    requirement_judgments: record.requirement_judgments.map((row) => ({
      candidate_id: row.candidate_id,
      requirement_id: row.requirement_id,
      judgment: row.judgment,
      evidence_ids: [...row.evidence_ids],
    })),
    preference: record.preference,
    comment: (record.comment ?? record.note ?? '').trim(),
    created_at: record.created_at,
    provenance: makeOrigin(),
  };
}

function cleanIdea(record) {
  return {
    schema_version: 'scenario-suggestion.v1',
    id: record.id,
    need: record.need,
    created_at: record.created_at,
    provenance: makeOrigin(),
  };
}

function shownRefs(candidate, need2) {
  return candidate.evidence
    .filter((evidence) => evidence.requirement_ids?.includes(need2.id))
    .map((evidence) => evidence.id);
}

function readJudges(draft, scenario, errors) {
  const rows = [];
  const sourceRows = Array.isArray(draft.requirement_judgments) ? draft.requirement_judgments : null;
  if (sourceRows) {
    const expectN = scenario.candidates.length * scenario.requirements.length;
    if (sourceRows.length !== expectN) errors.push('Review each condition once for both candidates.');
    for (const row of sourceRows) {
      if (!isObject(row) || !scenario.candidates.some((c) => c.id === row.candidate_id)
        || !scenario.requirements.some((r) => r.id === row.requirement_id)) {
        errors.push('The review contains an unrecognized candidate or condition.');
        break;
      }
    }
  }
  for (const candidate of scenario.candidates) {
    for (const need2 of scenario.requirements) {
      const matches = sourceRows?.filter((row) => row?.candidate_id === candidate.id && row?.requirement_id === need2.id);
      const judgment = sourceRows
        ? (matches.length === 1 ? matches[0].judgment : undefined)
        : draft.judgments?.[candidate.id]?.[need2.id];
      if (!JUDGMENTS.has(judgment)) {
        errors.push(`For candidate ${candidate.id.toUpperCase()}, choose meets, does not meet or not enough information for "${need2.text}".`);
      }
      rows.push({
        candidate_id: candidate.id,
        requirement_id: need2.id,
        judgment,
        // Bind each judgment to its displayed evidence.
        evidence_ids: shownRefs(candidate, need2),
      });
    }
  }
  return rows;
}

export function validateFeedback(draft, scenario) {
  const errors = [];
  if (!isObject(draft)) return { valid: false, errors: ['Complete your review first.'] };
  const fixedScene = getScenario(scenario?.id);
  if (!fixedScene || fixedScene.version !== scenario?.version) {
    return { valid: false, errors: ['This scenario is unavailable. Choose another scenario.'] };
  }
  scenario = fixedScene;
  const rows = readJudges(draft, scenario, errors);
  if (!PREFS.has(draft.preference)) errors.push('Choose an overall preference, including not sure if needed.');
  const comment = draft.comment ?? draft.note ?? '';
  if (typeof comment !== 'string') errors.push('The comment must be text.');
  else if (comment.length > 1000) errors.push('Keep your comment within 1,000 characters.');
  if (errors.length) return { valid: false, errors };
  return {
    valid: true,
    errors: [],
    record: {
      schema_version: SCHEMA_VER,
      id: createId('feedback'),
      scenario_id: scenario.id,
      scenario_version: scenario.version,
      need: scenario.need,
      requirement_judgments: rows,
      preference: draft.preference,
      comment: comment.trim(),
      created_at: new Date().toISOString(),
      provenance: makeOrigin(),
    },
  };
}

function emptyStore() {
  return { schema_version: STORE_VER, feedback: [], suggestions: [] };
}

function storage() {
  try {
    return globalThis.localStorage;
  } catch {
    return undefined;
  }
}

function isSaved(record) {
  if (!isObject(record) || record.schema_version !== SCHEMA_VER || typeof record.id !== 'string') return false;
  const scenario = getScenario(record.scenario_id);
  if (!scenario) return false;
  const current = record.scenario_version === scenario.version && record.need === scenario.need;
  const legacy = record.scenario_version === '0.9' && record.need === oldNeeds[scenario.id];
  if (!current && !legacy) return false;
  if (typeof record.created_at !== 'string' || !Number.isFinite(Date.parse(record.created_at))) return false;
  const result = validateFeedback(record, scenario);
  if (!result.valid) return false;
  const actual = record.requirement_judgments;
  if (!actual.every((row) => {
    const candidate = scenario.candidates.find((c) => c.id === row.candidate_id);
    const need2 = scenario.requirements.find((r) => r.id === row.requirement_id);
    const expected = shownRefs(candidate, need2);
    return Array.isArray(row.evidence_ids) && row.evidence_ids.length === expected.length
      && expected.every((id) => row.evidence_ids.includes(id));
  })) return false;
  return record.provenance?.mode === 'synthetic_participation_demo'
    && record.provenance.model_called === false
    && record.provenance.purchase_attempted === false
    && record.provenance.submitted_to_team === false;
}

function isIdea(record) {
  return isObject(record) && record.schema_version === 'scenario-suggestion.v1'
    && typeof record.id === 'string' && typeof record.need === 'string'
    && record.need.trim().length > 0 && record.need.length <= 1500
    && typeof record.created_at === 'string' && Number.isFinite(Date.parse(record.created_at))
    && record.provenance?.mode === 'synthetic_participation_demo'
    && record.provenance.model_called === false
    && record.provenance.purchase_attempted === false
    && record.provenance.submitted_to_team === false;
}

function readStore() {
  try {
    const raw = storage()?.getItem(localStorageKey);
    if (!raw) return emptyStore();
    const value = JSON.parse(raw);
    if (!isObject(value) || value.schema_version !== STORE_VER) return emptyStore();
    return {
      schema_version: STORE_VER,
      feedback: Array.isArray(value.feedback) ? value.feedback.filter(isSaved).map(cleanReply) : [],
      suggestions: Array.isArray(value.suggestions) ? value.suggestions.filter(isIdea).map(cleanIdea) : [],
    };
  } catch {
    return emptyStore();
  }
}

function writeStore(value) {
  try {
    const target = storage();
    if (!target) throw new Error('unavailable');
    target.setItem(localStorageKey, JSON.stringify(value));
  } catch {
    throw new Error('The browser could not save your feedback. Check its storage settings and try again.');
  }
}

export function saveFeedback(record) {
  if (!isSaved(record)) throw new Error('The review is incomplete. Check each condition before saving.');
  const next = readStore();
  const saved = cleanReply(record);
  const existing = next.feedback.findIndex((item) => item.id === saved.id);
  if (existing < 0) next.feedback.push(saved);
  else next.feedback[existing] = saved;
  writeStore(next);
  return clone(saved);
}

export function listFeedback() {
  return clone(readStore().feedback);
}

export function formatFeedbackSummary(record, scenario = getScenario(record?.scenario_id)) {
  if (!isSaved(record) || scenario?.id !== record.scenario_id) return 'This review cannot be displayed. Please try the scenario again.';
  scenario = getScenario(record.scenario_id);
  const lines = [
    'Your feedback is saved in this browser. It has not been sent to the team.',
    '',
    `Scenario: ${scenario.title}`,
    `Saved scenario version: ${record.scenario_version}`,
    `Shopping request: ${scenario.need}`,
    '',
  ];
  for (const candidate of scenario.candidates) {
    lines.push(`Candidate ${candidate.id.toUpperCase()}: ${candidate.name}`);
    for (const need2 of scenario.requirements) {
      const row = record.requirement_judgments.find((item) => item.candidate_id === candidate.id && item.requirement_id === need2.id);
      lines.push(`• ${need2.text}: ${judgeNames[row.judgment]}`);
    }
    lines.push('');
  }
  lines.push(`Overall preference: ${prefNames[record.preference]}`);
  if (record.comment) lines.push(`Comment: ${record.comment}`);
  lines.push('', 'These are your judgments, saved on this device.');
  return lines.join('\n');
}

export function exportFeedback(records = listFeedback()) {
  if (!Array.isArray(records) || !records.every(isSaved)) throw new Error('The export includes an invalid review. Save the review again and retry.');
  return JSON.stringify({
    schema_version: SCHEMA_VER,
    exported_at: new Date().toISOString(),
    notice: 'Feedback exported from this device; not sent to the team.',
    records: records.map(cleanReply),
  }, null, 2);
}

export function saveSuggestion(need) {
  if (typeof need !== 'string' || !need.trim()) throw new Error('Describe the shopping request you would like to try.');
  if (need.length > 1500) throw new Error('Keep your shopping request within 1,500 characters.');
  const record = {
    schema_version: 'scenario-suggestion.v1',
    id: createId('suggestion'),
    need: need.trim(),
    created_at: new Date().toISOString(),
    provenance: makeOrigin(),
  };
  const next = readStore();
  next.suggestions.push(record);
  writeStore(next);
  return clone(record);
}

export function listSuggestions() {
  return clone(readStore().suggestions);
}
