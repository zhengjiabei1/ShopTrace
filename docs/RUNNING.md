# Running ShopTrace

ShopTrace needs Python 3.11+ and Chromium. The model is a standard OpenAI-compatible chat-completions endpoint with tool-call support.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-webagent.txt
python -m playwright install chromium
python -m webagent doctor
```

`doctor` checks package versions, browser paths, and configuration without network access, browser startup, or secret output. To use an installed Chromium instead of Playwright's build:

```bash
export WEBAGENT_CHROMIUM_PATH="/path/to/chrome"
```

## Start the workbench

```bash
export LLM_BASE_URL="https://your-model-provider.example/v1"
export LLM_MODEL="your-served-model"
export LLM_API_KEY="..."
python -m webagent serve --port 8775
```

Open <http://127.0.0.1:8775>. The service listens on loopback and stops when its terminal closes. The key stays on the server and never enters the browser or the trajectory. The app does not load `.env` automatically; restart it after changing settings. Remote APIs require HTTPS; loopback HTTP is allowed for local services or SSH tunnels.

The default data directory is `artifacts/webagent`. Choose another with `--data-dir` or `WEBAGENT_DATA_DIR`. Only one service should manage each directory.

## Run a task

```bash
python -m webagent tasks
python -m webagent run --task-id browser-headphones-commute --provider api \
  --max-steps 35 --max-seconds 180 --max-output-tokens 512
```

A fixed-action run makes no model calls and is useful for checking the browser:

```bash
python -m webagent run --task-id browser-headphones-commute --provider browser_smoke
```

`completed` means the execution loop ended. Check `result.success`, the termination reason, and errors for the task outcome. Failed, budget-exceeded, cancelled, and unknown attempts are saved.

Use `finish.reason` as follows: `completed` when required actions and checks are complete, `blocked` when information is missing or progress is impossible, and `no_suitable_product` when filtering finds no match. A task score of `result.success=null` means the outcome is unknown.

## Procurement

```bash
python -m webagent procurement --split development
python -m webagent run --task-id procure-dev-01 --provider api \
  --seed 7 --fault ack_lost --recovery verify \
  --context-mode page_memory --max-steps 70 --max-seconds 180
```

See [PROCUREMENT.md](PROCUREMENT.md) for the store, faults, context, and recovery settings.

## Batch evaluation

The batch runner connects to a running workbench and runs each task in a new browser context:

```bash
python -m webagent.benchmark \
  --base-url http://127.0.0.1:8775 \
  --repeats 5 \
  --max-steps 35 \
  --max-seconds 180 \
  --output work/browser-batch-001
```

Output directories must be new. The plan is saved before execution, and failures do not add attempts. Add `--provider browser_smoke` for a zero-model-call regression. `--task-id` can be repeated to choose tasks.

For fixed procurement plans:

```bash
python -m webagent.experiment run \
  --plan experiments/procurement-pilot.json \
  --output work/procurement-pilot

python -m webagent.experiment report --output work/procurement-pilot --archive
```

A resume requires the saved plan and a compatible source and model identity. Completed cases, including failures, are skipped. An unknown submission is kept for inspection and is not sent again automatically.

## Tests

```bash
WEBAGENT_CHROMIUM_PATH="/path/to/chrome" python -m unittest discover -s tests
python -m unittest discover -s portal -p 'test_*.py'
```

Browser tests skip when Chromium is not configured. The suite covers the browser boundary, stale references, origin and path rules, budgets, cancellation, storage, task grading, procurement faults, model protocol errors, the portal builder, and deployment templates.

## Static replay

The committed report is in `portal/dist/`:

```bash
python -m http.server 8790 --bind 127.0.0.1 --directory portal/dist
```

Raw traces under `portal/dist/evidence/` are gzip-compressed; the report itself is not. Rebuilding the portal does not call a model or a browser.
