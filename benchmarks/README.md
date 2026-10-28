# Benchmarks

The synthetic tasks live in `webagent/` and `experiments/`. This directory holds **external** checks that are not part of the recorded synthetic results.

## WebVoyager read-only slice

- Manifest: [external/webvoyager-readonly-30.json](external/webvoyager-readonly-30.json)
- Pilot subset: [external/pilot-15.json](external/pilot-15.json)
- Protocol: [../docs/EXTERNAL_BENCHMARK.md](../docs/EXTERNAL_BENCHMARK.md)
- Grades template: [external/grades-template.csv](external/grades-template.csv)
- Pilot grades: [external/grades-pilot-15.csv](external/grades-pilot-15.csv)
- Remaining manifest: [external/remaining-15.json](external/remaining-15.json)
- Remaining grades: [external/grades-remaining-15.csv](external/grades-remaining-15.csv)
- All-30 grades: [external/grades-all-30.csv](external/grades-all-30.csv)
- Preflight (no model): `python scripts/external_preflight.py`
- Runner: `python scripts/external_benchmark.py --plan`
- Upstream: [MinorJerry/WebVoyager](https://github.com/MinorJerry/WebVoyager), Apache-2.0

The slice is read-only and manually graded. It is not a WebVoyager leaderboard entry. Keep it separate from the synthetic numbers in the portal.
