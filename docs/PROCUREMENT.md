# Cross-page procurement

Procurement extends the browser loop with multi-item shopping, page memory, and controlled add-to-cart faults. It uses the same workbench, browser tools, screenshots, and result storage as the base tasks.

## Store and tasks

The catalog has 24 synthetic products across keyboards, mice, and headsets, with four summaries per page. Summaries contain a name, category, and price; the agent must open a detail page to read attributes and stock. Returning to the catalog preserves its search and page. The cart supports quantity changes and removal.

Tasks require one item from each requested category within a total budget. Search, detail inspection, comparison, and the final cart check all consume browser actions. Product order and prices depend on the seed.

| Split | Task IDs | Purpose |
|---|---|---|
| Development | `procure-dev-01` to `procure-dev-06` | Implementation checks and configuration selection |
| Held-out | `procure-test-01` to `procure-test-04` | Check frozen settings on other constraint combinations |

Both splits share the storefront, product vocabulary, and task format. The held-out split tests combinations within this environment, not unseen websites. Task constraints and grading stay on the server; model inputs contain the public goal and browser observations.

Each run creates a separate server session and browser context. The browser receives an HttpOnly session cookie. Fault settings and grading criteria are absent from page observations.

## Faults

A selected fault applies to the first eligible add operation in that session.

| Setting | Server outcome | Page feedback |
|---|---|---|
| `none` | Item added normally | Addition confirmed |
| `ack_lost` | Item added | Result uncertain |
| `no_effect` | Item not added | The same uncertain feedback |
| `stockout` | Selected item becomes unavailable | Stock change reported |

The two uncertain cases require checking the cart to distinguish their state. Repeating an addition can create a duplicate. Stock changes require inspecting another candidate. The final check verifies product attributes, quantities, the total budget, and agreement between the visible cart and server state.

A fault may never trigger if a run stops before adding anything. Reports keep `fault_triggered` so these runs are not mistaken for successful recovery. A final server audit records add attempts, retries, peak duplicate quantities, cart reads, and inspected products, including runs that stop at a budget. The audit is saved after execution and is not sent to the model.

## Context settings

| Setting | Model input |
|---|---|
| `full_history` | Complete preceding model and tool messages, within the character budget |
| `latest_observation` | Current DOM and the executed-action journal |
| `page_memory` | Current DOM, recent actions, and text retained from previously observed pages |

Page memory stores at most 32 pages, 24,000 characters in total, and 1,800 characters per page. Entries retain their observation IDs and text hashes. A later observation updates the same page; a loading view does not replace previously read facts. Clipping and eviction counts are saved as memory events.

Only the current observation supplies actionable element references. Cached text has old references removed and is labeled as potentially stale. The cache uses observed page content; it does not read hidden catalog attributes, answers, or fault settings.

## Recovery settings

| Setting | Extra guidance supplied to the model |
|---|---|
| `baseline` | No added recovery instruction |
| `verify` | Inspect the cart after an uncertain addition, then decide whether to retry |
| `adaptive` | Use visible feedback to suggest waiting, checking the cart, or choosing another candidate |

The adaptive setting is a state-guided instruction rule. It does not execute actions itself or learn a policy. The model still chooses every browser tool call. Recovery events record the observed state, source observation, and supplied guidance. Use the saved browser actions and server audit to inspect behavior after feedback.

## Running a task

```bash
python -m webagent serve --port 8775
```

In another terminal:

```bash
python -m webagent procurement --split development
python -m webagent run \
  --task-id procure-dev-01 --provider api \
  --seed 7 --fault ack_lost --recovery verify \
  --context-mode page_memory \
  --max-steps 70 --max-seconds 180 --max-output-tokens 512
```

In the workbench, choose **Procurement**, select a task, and open the run settings to change the fault, recovery setting, seed, or context.

## Results

The held-out plan covers eight cases across `procure-test-01` and `procure-test-04`. Seven passed; one `stockout` case was cancelled when the session paused and remains unknown. The cancelled run was not retried. All cases share the same store and task format, so this is a transfer check inside the environment, not an out-of-distribution test.

Fixed plans save every attempt and support resume, offline report reconstruction, and complete-case regression checks. See [EVALUATION.md](EVALUATION.md) for the result table and [RUNNING.md](RUNNING.md) for the experiment commands.
