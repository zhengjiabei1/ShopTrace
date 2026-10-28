# External read-only slice

This is a 30-task subset of WebVoyager used to test the browser loop on live sites without writes. It is not a leaderboard entry.

## Selection

- The task asks for information, not a write. No cart, booking, login, upload, purchase, subscription, or comment.
- The answer can be checked against page state.
- No account is required, and obvious anti-bot pages are excluded.
- Tasks are spread across sites so one layout does not dominate.
- Stable tasks should remain correct; snapshot tasks (prices, ratings, current lists) need re-checking before a run.

The manifest is `benchmarks/external/webvoyager-readonly-30.json`: Cambridge Dictionary 5, Wolfram Alpha 5, GitHub 4, Hugging Face 4, arXiv 3, Allrecipes 3, Apple 3, Coursera 3. The question text comes from [WebVoyager](https://github.com/MinorJerry/WebVoyager) (Apache-2.0); the selection, read-only constraint, and manual grading are additions by this project.

## Protocol

1. Start the workbench with the slice origins:

```bash
export WEBAGENT_ALLOWED_ORIGINS="https://dictionary.cambridge.org,https://www.wolframalpha.com,https://github.com,https://huggingface.co,https://arxiv.org,https://www.allrecipes.com,https://www.apple.com,https://support.apple.com,https://www.coursera.org"
export LLM_BASE_URL="https://api.deepseek.com/v1"
export LLM_MODEL="deepseek-flash"
export LLM_API_KEY="..."
python -m webagent serve --port 8775
```

2. Run the slice:

```bash
python scripts/external_benchmark.py --run --context-mode latest_observation
```

Defaults: 50 steps, 240 seconds, 512 output tokens, temperature 0. Each invocation writes a new `work/external-benchmark/runs-*.jsonl`; earlier runs are not overwritten.

3. Grade each row in `benchmarks/external/grades-template.csv`:

| Value | Meaning |
|---|---|
| `true` | The finish evidence contains the requested facts, and the saved page state does not contradict it. |
| `false` | The answer is wrong, invented, or the run stopped before checking the relevant page. |
| `unknown` | The site blocked the run, the budget expired, or the page was too unstable to grade. |

4. Summarize:

```bash
python scripts/external_benchmark.py --summary work/external-benchmark/grades.csv
```

The summary reports attempted / passed / failed / unknown, coverage, known-result success, a Wilson interval, missing grades, and per-site counts.

## Results

| Group | Attempted | Passed | Failed | Unknown |
|---|---:|---:|---:|---:|
| Clean-site pilot | 15 | 10 | 5 | 0 |
| Remaining 15 | 15 | 2 | 9 | 4 |
| All | 30 | 12 | 14 | 4 |

By site: Cambridge 5/5, Hugging Face 3/4, Apple 2/3, GitHub 2/4, arXiv 0/2, Wolfram Alpha 0/1, Coursera 0/3, Allrecipes 0/3. The clean-site subset passed 10/15.

## Failure notes

- Allrecipes returned an access-issue page; three runs are graded unknown.
- GitHub and Coursera render without their CDN assets under the origin allowlist. Text and links remain, but the pages are degraded.
- Wolfram's result area did not render for one task; the run finished `blocked`.
- Most failures are repeated-action or no-progress stops on Wolfram, GitHub, and Coursera, plus one arXiv invalid-decision stop and one Coursera step-budget stop.

## Limits

- The slice is small and selected from a public task set; it is not a random sample of the web.
- Grading is manual and read-only; GET requests can still trigger side effects, so results need review.
- The environment policy differs from the WebVoyager leaderboard: DOM-only observations, origin allowlists, no writes, and no multimodal input.
- Do not compare the 12/30 rate with published WebVoyager numbers.
