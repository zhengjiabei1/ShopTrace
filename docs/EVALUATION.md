# Evaluation

ShopTrace is evaluated on a synthetic store, a cross-page procurement benchmark, and a read-only external slice. The measurements cover the browser loop, task grading, and trace retention.

## Setup

- Model: DeepSeek `deepseek-flash`, temperature 0, 512 output tokens per call.
- Browser: Playwright with Chromium, one context per run.
- Synthetic tasks: 35 steps and 180 seconds. Procurement: 70 steps and 180 seconds. External: 50 steps and 240 seconds.
- Grading: server-side for synthetic tasks; manual for external tasks.

## Base tasks

Six tasks on a 12-product store: variant price, specification matching, no-match, and missing information. Five runs per task:

| Outcome | Count |
|---|---:|
| Passed | 30 |
| Known failures | 0 |
| Unknown | 0 |

The tasks are small. The result checks that the browser boundary, task pages, and grading are stable; it does not measure general web competence.

## Procurement

Ten tasks on a 24-product store. The agent must select one item from each requested category under a total budget, open detail pages, and check the cart. Four fault settings cover normal addition, lost acknowledgement, silent no-op, and stock change. Development and held-out splits share the store and task format; the held-out split uses different constraint combinations.

| Split | Attempts | Passed | Unknown |
|---|---:|---:|---:|
| Held-out | 8 | 7 | 1 |

The unknown run was cancelled when the session paused and was not retried. All other held-out attempts passed. The split is not out-of-distribution: it shares products, vocabulary, and page structure with development.

## Context comparison

Two procurement tasks were run once per context mode:

| Context | Passed | Unknown |
|---|---:|---:|
| `full_history` | 1 | 1 |
| `page_memory` | 2 | 0 |

`full_history` reached the 96,000-character context limit on the three-item task after 19 steps. `page_memory` completed that task in 40 steps and the two-item task in 13 steps. With two attempts per arm, this is a descriptive observation, not a general result.

## External slice

Thirty read-only tasks from WebVoyager, across eight sites, run with the same browser loop and manually graded:

| Group | Attempted | Passed | Failed | Unknown |
|---|---:|---:|---:|---:|
| Clean-site pilot | 15 | 10 | 5 | 0 |
| Remaining 15 | 15 | 2 | 9 | 4 |
| All | 30 | 12 | 14 | 4 |

The clean-site subset is the better signal for agent capability. The full set includes Allrecipes, which returned an access-issue page, and GitHub/Coursera, which lost CDN assets under the origin allowlist. Four unknown results are environmental: three Allrecipes pages and one Wolfram result area that never rendered.

The slice is read-only, manually graded, and selected from the public WebVoyager task set. It is not leaderboard-compatible.

## Failure notes

- Cambridge Dictionary: 5/5 passed.
- Hugging Face: 3/4; one docs page ended in repeated clicks.
- Apple: 2/3; one compatibility check hit the step budget.
- arXiv: 0/2; one search flow looped, one run made three invalid actions.
- Wolfram Alpha: 0/1; repeated input attempts.
- GitHub: 2/4; two passes, two no-progress stops.
- Coursera: 0/3; degraded pages and one 50-step budget.
- Allrecipes: 0/3; access-issue page, graded unknown.

## Evidence

- `benchmarks/external/webvoyager-readonly-30.json`: task manifest.
- `benchmarks/external/grades-all-30.csv`: manual grades.
- `docs/verification/external-30.json`: run records and summary.
- `docs/verification/procurement-heldout.json`: held-out procurement records.
- `docs/verification/model-regression.json`: 30-run base-task regression.
- `docs/verification/unit-tests.txt`: test run.

## Limitations

- Synthetic stores are small and authored with the tasks; they do not model merchant catalogues, inventory contention, or checkout.
- Held-out cases share the store and vocabulary with development.
- Samples are small and there is no baseline agent.
- External results are read-only and manually graded; site changes and CDN policy affect them.
- A model ID does not pin provider weights, so a rerun may not reproduce the same outputs.
