# Report portal

`dist/` is a static report viewer. Open `dist/index.html` locally or serve it with any static host. It displays saved runs; the live workbench is a separate service.

Build the report from completed batches:

```bash
python portal/build_report.py \
  --suite stability=work/stability \
  --suite development:full_history=CONTROL_DIR \
  --suite development:latest_observation=CANDIDATE_DIR \
  --suite heldout:full_history=HELDOUT_DIR
```

Inputs contain `summary.json` or `batch.json` and per-attempt `trajectory.json`, `result.json`, and `screenshots/`. Repeat a suite ID to combine its settings. Running batches are rejected unless `--allow-incomplete` is set for local development.

The builder stages new files before replacing `dist/evidence/`. It copies the saved records, creates `report.json` and `report-data.js`, and writes a SHA-256 manifest. A failed build preserves the previous report. Original batch directories are unchanged.

Screenshots copied by the builder are full-resolution PNGs. Before committing a rebuilt portal, compress them and rewrite the report paths:

```bash
python scripts/optimize_portal.py --gzip-trajectories
```

The optimizer keeps every observation, converts it to WebP, gzips raw traces, and regenerates `manifest.json`. It needs Pillow (`requirements-dev.txt`).

For HTTP preview:

```bash
python -m http.server 8790 --bind 127.0.0.1 --directory portal/dist
```

`--live-url https://YOUR_HOST` adds a link to an existing workbench; it does not deploy or verify it. Without a URL, the portal shows **Not deployed**. See [the deployment guide](../docs/DEPLOYMENT.md).

`--archive-url https://.../results.zip` adds the result-package download.

## Data format

The `shoptrace.portal.v1` report contains `generated_at`, `model`, `source_commit`, optional `live_url` and `archive_url`, and `suites`. Each suite has `{id,title,description,attempts}` and may override the download link with `archive_url`. An attempt contains:

- Identity: `id`, `task_id`, `task_title`, `repeat`, `arm`, `source`, `status`.
- Outcome: `success` (`true`, `false`, or `null`), `result_reason`, `error`, `finish`.
- Usage: `elapsed_seconds`, `model_calls`, `input_tokens`, `output_tokens`; missing values stay `null`.
- Conditions: `fault`, `seed`, and `split` in `conditions`, plus `recovery`, `context_mode`, and result `metrics`.
- Records: `trajectory_url`, `result_url`, and `steps` with `step`, `url`, `text`, `screenshot`, `action`, `error`, and memory/recovery `notes`.

Metrics use finished `browser_model` attempts. The pass rate is passed / known outcomes; coverage is known / finished; passed / all finished is shown separately. Script and mock API records are excluded from model scores.

Different `arm`, `protocol`, or `conditions` values receive separate rate cards, even when their setting names match. The default mixed view shows **By setting** instead of a pooled rate.

Historical page text, screenshots, actions, and raw JSON remain unchanged. Display labels summarize task titles and outcomes. See the [evaluation report](../docs/EVALUATION.md) for methods and results.

## Append results

Append a completed experiment without rebuilding historical batches:

```bash
python build_report.py --preserve \
  --suite procurement-dev=../work/procurement-development
```

Run the command from `portal/`, or use the repository-root paths shown in the batch guide. Existing attempts and their evidence paths remain unchanged. Duplicate run IDs, unsupported saved schemas, and symlinked evidence stop the append before it replaces the report.

Procurement views add fault and seed filters. Rate cards keep conditions separate, while the action inspector includes the page-memory counters and recovery guidance at that observation. Opening the report never calls a model or runs a browser task.
