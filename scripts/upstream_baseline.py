#!/usr/bin/env python3
"""Prepare, inspect and run a pinned upstream development baseline."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "shoptrace-upstream-development-baseline-v1"
REQUIRED = ("scripts/evaluate_shop_benchmark.py",
            "src/shopping_grpo/evaluation/rollout.py",
            "src/shopping_grpo/evaluation/summary.py")
SPLITS = {"train": "data/grpo/train.jsonl", "validation": "data/grpo/validation.jsonl",
          "test": "data/evaluation/tasks.jsonl"}
PROTOCOL = {"max_steps": 35, "temperature": 0.0, "top_p": 1.0, "timeout": 180,
            "max_tokens": 512, "context_window": 24576, "context_safety_margin": 512,
            "context_compaction": False, "observation_token_budget": 1536,
            "observation_detail_token_budget": 4096,
            "observation_generic_token_budget": 768, "observation_search_top_k": 20}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def load_lock():
    return json.loads((ROOT / "upstream.lock.json").read_text(encoding="utf-8"))


def _git(root: Path, *args, binary=False):
    # Offline checks require the pinned Git objects to be present.
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(["git", "-c", "protocol.allow=never", "-C", str(root), *args], env=env,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Required upstream Git objects are unavailable; fetch the pinned source first.")
    return result.stdout if binary else result.stdout.decode("utf-8").strip()


def _identity(root: Path):
    expected = load_lock()["reproduction"]["commit"]
    if _git(root, "rev-parse", "HEAD") != expected:
        raise ValueError("Upstream HEAD differs from upstream.lock.json.")
    if _git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("Upstream checkout has local changes; use a clean pinned checkout.")
    return expected


def _protocol(name):
    if name not in {"upstream-vllm", "api-no-tokenizer"}:
        raise ValueError("Unknown baseline protocol.")
    result = {"id": name, **PROTOCOL}
    if name == "api-no-tokenizer":
        result.update(context_window=0, observation_token_budget=0)
    return result


def _split_evidence(root: Path, task_ids):
    if not 1 <= len(task_ids) <= 20 or any(type(t) is not int or t < 0 for t in task_ids):
        raise ValueError("Select 1 to 20 nonnegative integer development task IDs.")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Development task IDs must not repeat.")
    evidence, pools = {}, {}
    for split, path in SPLITS.items():
        raw = _git(root, "show", "HEAD:" + path, binary=True)
        try:
            rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            ids = [int(row["task_id"]) for row in rows]
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Upstream task manifests are invalid.") from exc
        if len(ids) != len(set(ids)):
            raise ValueError("Upstream task manifests contain duplicate IDs.")
        evidence[split] = {"path": path, "sha256": _sha(raw), "tasks": len(ids)}
        pools[split] = set(ids)
    expected = load_lock()["reproduction"]["evaluation"]["sha256"]
    if evidence["test"]["sha256"] != expected:
        raise ValueError("Held-out evaluation manifest does not match the pinned lock.")
    if pools["train"] & (pools["validation"] | pools["test"]):
        raise ValueError("Upstream training task IDs overlap held-out tasks.")
    if not set(task_ids) <= pools["train"]:
        raise ValueError("Development tasks must come only from the upstream GRPO training pool.")
    if set(task_ids) & (pools["validation"] | pools["test"]):
        raise ValueError("Development tasks overlap validation/test data.")
    return evidence


def _sources(root: Path):
    sources = {}
    for line in _git(root, "ls-tree", "-r", "HEAD", "src/shopping_grpo", REQUIRED[0]).splitlines():
        header, path = line.split("\t", 1)
        mode, kind, blob = header.split()
        if kind == "blob" and (path.endswith((".py", ".json")) or path in REQUIRED):
            if mode not in {"100644", "100755"}:
                raise ValueError("Upstream source symlinks are not supported.")
            sources[path] = blob
    if not set(REQUIRED) <= sources.keys():
        raise ValueError("Required upstream baseline modules are absent from this commit.")
    return sources


def _source_problems(root: Path, sources):
    problems = []
    for name, expected in sources.items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            problems.append("missing_source:" + name)
            continue
        raw = path.read_bytes()
        actual = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if actual != expected:
            problems.append("modified_source:" + name)
    return problems


def create_plan(upstream: Path, output_dir: Path, task_ids: list[int], *,
                protocol_name="upstream-vllm", model_name="shopping-agent",
                declared_checkpoint="Qwen/Qwen3.5-2B"):
    upstream, output_dir = Path(upstream).resolve(), Path(output_dir).resolve()
    if output_dir.exists():
        raise ValueError("Refusing to overwrite an existing run directory.")
    if output_dir == upstream or upstream in output_dir.parents:
        raise ValueError("Keep development outputs outside the pinned upstream checkout.")
    if not model_name.strip() or not declared_checkpoint.strip():
        raise ValueError("Model alias and declared checkpoint must be nonempty.")
    commit = _identity(upstream)
    splits = _split_evidence(upstream, task_ids)
    sources = _sources(upstream)
    protocol = _protocol(protocol_name)
    task_text = "".join(json.dumps({"task_id": t}) + "\n" for t in task_ids)
    plan = {"schema_version": SCHEMA, "purpose": "development_baseline", "created_at": _now(),
            "upstream": {"path": str(upstream), "repository": load_lock()["reproduction"]["repository"],
                         "commit": commit, "source_git_blobs": sources},
            "tasks": {"file": "dev-tasks.jsonl", "ids": task_ids, "sha256": _sha(task_text.encode()),
                      "source_split": "grpo_train", "split_evidence": splits,
                      "reserved_for_development": True, "remove_from_future_training": True},
            "model": {"served_alias": model_name, "declared_checkpoint": declared_checkpoint,
                      "checkpoint_verified": False}, "protocol": protocol,
            "scope": {"formal_test_evaluated": False, "training_started": False,
                      "environment": "shopsimulator-environment-v2.1", "reward": "shopsimulator-reward-v3",
                      "environment_source_attested": False, "judge_called": False},
            "bridge_sha256": _sha(Path(__file__).read_bytes()),
            "artifacts": ["trajectories.jsonl", "summary.json", "run.json"]}
    output_dir.mkdir(parents=True, mode=0o700)
    (output_dir / "dev-tasks.jsonl").write_text(task_text, encoding="utf-8")
    encoded = _json(plan).encode()
    (output_dir / "plan.json").write_bytes(encoded)
    (output_dir / "plan.sha256").write_text(_sha(encoded) + "\n", encoding="ascii")
    return plan


def validate_plan(run_dir: Path):
    run_dir = Path(run_dir).resolve()
    try:
        raw = (run_dir / "plan.json").read_bytes()
        if _sha(raw) != (run_dir / "plan.sha256").read_text().strip():
            raise ValueError("Plan fingerprint changed; prepare a new run directory.")
        plan = json.loads(raw)
        if plan["schema_version"] != SCHEMA or plan["purpose"] != "development_baseline":
            raise ValueError("Unsupported baseline plan.")
        if plan["bridge_sha256"] != _sha(Path(__file__).read_bytes()):
            raise ValueError("Baseline bridge changed; prepare a new plan.")
        if plan["protocol"] != _protocol(plan["protocol"]["id"]):
            raise ValueError("Baseline protocol changed.")
        root = Path(plan["upstream"]["path"])
        if _identity(root) != plan["upstream"]["commit"]:
            raise ValueError("Upstream identity changed.")
        if _sources(root) != plan["upstream"]["source_git_blobs"]:
            raise ValueError("Upstream source inventory changed.")
        if any(p.startswith("modified_source:") for p in _source_problems(root, plan["upstream"]["source_git_blobs"])):
            raise ValueError("Materialized upstream sources differ from their committed Git blobs.")
        if _split_evidence(root, plan["tasks"]["ids"]) != plan["tasks"]["split_evidence"]:
            raise ValueError("Task split evidence changed.")
        if plan["tasks"]["file"] != "dev-tasks.jsonl":
            raise ValueError("Unexpected development task filename.")
        tasks = (run_dir / "dev-tasks.jsonl").read_bytes()
        if _sha(tasks) != plan["tasks"]["sha256"]:
            raise ValueError("Development task file changed.")
        if [r["task_id"] for r in map(json.loads, tasks.decode().splitlines())] != plan["tasks"]["ids"]:
            raise ValueError("Task IDs differ from the plan.")
        if not plan["model"]["served_alias"].strip():
            raise ValueError("Model alias is missing.")
        return plan
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Baseline plan or required local artifacts are invalid.") from exc


def _service_url(name, blockers):
    value = os.environ.get(name, "").strip().rstrip("/")
    if not value:
        blockers.append("not_configured:" + name)
        return None
    try:
        p = urlsplit(value)
        _ = p.port
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError()
        if p.scheme == "http" and p.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
    except ValueError:
        blockers.append("invalid_service_url:" + name)
        return None
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe(url, key=None, model_alias=None):
    try:
        request = Request(url + ("/models" if model_alias else "/"),
                          headers={"Authorization": "Bearer " + key} if key else {})
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            if model_alias:
                data = json.loads(response.read(1024 * 1024))
                return any(row.get("id") == model_alias for row in data.get("data", []) if isinstance(row, dict))
            return response.status < 500
    except HTTPError as exc:
        # A root 404/405 still confirms HTTP reachability.
        return not model_alias and exc.code in {404, 405}
    except (OSError, URLError, ValueError, TypeError, AttributeError):
        return False


def _native_env(root: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src")
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _python_path(value):
    # Use an absolute path while preserving the venv interpreter symlink.
    value = os.fspath(value)
    path = os.path.expanduser(value)
    if os.path.dirname(path):
        return os.path.abspath(path)
    found = shutil.which(path)
    if not found:
        raise ValueError("Requested Python interpreter is unavailable.")
    return os.path.abspath(found)


def preflight(run_dir: Path, python_executable=sys.executable, probe_services=False):
    plan = validate_plan(run_dir)
    root = Path(plan["upstream"]["path"])
    blockers = _source_problems(root, plan["upstream"]["source_git_blobs"])
    model = _service_url("LLM_BASE_URL", blockers)
    shop = _service_url("SHOPSIM_BASE_URL", blockers)
    key = os.environ.get("LLM_API_KEY", "").strip()
    if model and urlsplit(model).hostname not in {"localhost", "127.0.0.1", "::1"} and (not key or key == "EMPTY"):
        blockers.append("not_configured:LLM_API_KEY")
    deps = {"checked": False, "importable": False}
    if not any(x.startswith(("missing_source:", "modified_source:")) for x in blockers):
        try:
            py_bin = _python_path(python_executable)
            result = subprocess.run([py_bin, "-c",
                                     "import json,sys,tqdm; from shopping_grpo.evaluation.rollout import OpenAIChatClient,collect_tasks,load_tasks; from shopping_grpo.evaluation.summary import summarize_trajectories; print(json.dumps({'python':sys.version.split()[0],'tqdm':tqdm.__version__}))"],
                                    cwd=root, env=_native_env(root), capture_output=True, text=True, timeout=30)
            deps = {"checked": True, "importable": result.returncode == 0}
            if result.returncode == 0:
                deps.update(json.loads(result.stdout.strip()))
                deps["executable"] = py_bin
            else:
                blockers.append("upstream_python_import_failed")
        except (OSError, subprocess.SubprocessError, ValueError):
            deps = {"checked": True, "importable": False}
            blockers.append("upstream_python_unavailable")
    services = {"probed": False, "api_protocol_validated": False, "tool_call_validated": False}
    if probe_services and not blockers:
        services["probed"] = True
        services["model_alias_advertised"] = _probe(model, key, plan["model"]["served_alias"])
        if not services["model_alias_advertised"]:
            blockers.append("model_alias_not_advertised_or_service_unreachable")
        parsed = urlsplit(shop)
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            try:
                with socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=2):
                    services["environment_reachable"] = True
            except OSError:
                services["environment_reachable"] = False
        else:
            services["environment_reachable"] = _probe(shop)
        if not services["environment_reachable"]:
            blockers.append("environment_service_unreachable")
    return {"schema_version": SCHEMA, "checked_at": _now(), "purpose": "development_baseline",
            "status": "blocked" if blockers else ("ready_to_attempt" if probe_services else "static_ready"),
            "blockers": blockers, "dependencies": deps, "services": services,
            "tasks": plan["tasks"]["ids"], "protocol": plan["protocol"]["id"],
            "model_called": False, "environment_mutated": False, "training_started": False,
            "checkpoint_verified": False, "formal_test_evaluated": False}


def _run_native_process(run_dir, python_executable):
    plan = validate_plan(run_dir)
    root = Path(plan["upstream"]["path"])
    env = _native_env(root)
    env["SHOPTRACE_BASELINE_CHILD"] = "1"
    return subprocess.run([_python_path(python_executable), str(Path(__file__).resolve()), "_native", "--run-dir", str(Path(run_dir).resolve())],
                          cwd=root, env=env, capture_output=True, text=True)


def _collect_native(run_dir):
    # Use native APIs to keep the key out of process arguments.
    plan = validate_plan(run_dir)
    root, run_dir = Path(plan["upstream"]["path"]), Path(run_dir).resolve()
    state = json.loads((run_dir / "run.json").read_text())
    if (os.environ.get("SHOPTRACE_BASELINE_CHILD") != "1" or state.get("status") != "running"
            or state.get("plan_sha256") != _sha((run_dir / "plan.json").read_bytes())
            or any((run_dir / name).exists() for name in ("trajectories.jsonl", "summary.json"))):
        raise ValueError("The native collector is only launched by a preflighted run.")
    sys.path.insert(0, str(root / "src"))
    from shopping_grpo.evaluation.rollout import OpenAIChatClient, collect_tasks, load_tasks
    from shopping_grpo.evaluation.summary import summarize_trajectories
    p = plan["protocol"]
    client = OpenAIChatClient(model=plan["model"]["served_alias"],
        base_url=os.environ["LLM_BASE_URL"].rstrip("/"), api_key=os.environ.get("LLM_API_KEY") or "EMPTY",
        temperature=p["temperature"], top_p=p["top_p"], timeout=p["timeout"], max_tokens=p["max_tokens"],
        context_window=p["context_window"], context_safety_margin=p["context_safety_margin"],
        context_compaction_enable=p["context_compaction"], observation_token_budget=p["observation_token_budget"],
        observation_detail_token_budget=p["observation_detail_token_budget"],
        observation_generic_token_budget=p["observation_generic_token_budget"], observation_search_top_k=p["observation_search_top_k"])
    tasks = load_tasks(run_dir / "dev-tasks.jsonl")
    collect_tasks(tasks, client=client, output_path=run_dir / "trajectories.jsonl",
                  base_url=os.environ["SHOPSIM_BASE_URL"].rstrip("/"), max_steps=p["max_steps"])
    rows = [json.loads(line) for line in (run_dir / "trajectories.jsonl").read_text().splitlines() if line.strip()]
    summary = summarize_trajectories([task["task_id"] for task in tasks], rows)
    summary["protocol"] = {"benchmark": str(run_dir / "dev-tasks.jsonl"), "model": plan["model"]["served_alias"],
                           "reward_contract": "shopsimulator-reward-v3", **{k: v for k, v in p.items() if k != "id"}}
    (run_dir / "summary.json").write_text(_json(summary), encoding="utf-8")


def execute(run_dir: Path, python_executable=sys.executable):
    run_dir = Path(run_dir).resolve()
    plan = validate_plan(run_dir)
    if any((run_dir / name).exists() for name in plan["artifacts"]):
        raise ValueError("Native outputs already exist; prepare a new run directory.")
    check = preflight(run_dir, python_executable, probe_services=True)
    if check["status"] != "ready_to_attempt":
        return check
    record = {"schema_version": SCHEMA, "purpose": "development_baseline", "status": "running",
              "started_at": _now(), "plan_sha256": _sha((run_dir / "plan.json").read_bytes()),
              "preflight": check, "execution_attempted": True, "model_called": None,
              "training_started": False, "formal_test_evaluated": False,
              "task_success_inferred_from_exit_code": False, "native_artifacts": {}}
    with (run_dir / "run.json").open("x", encoding="utf-8") as handle:
        handle.write(_json(record))
    try:
        child = _run_native_process(run_dir, python_executable)
        record["child_exit_code"] = child.returncode
        record["status"] = "failed" if child.returncode else "finished"
        for name in ("trajectories.jsonl", "summary.json"):
            path = run_dir / name
            if not path.is_file():
                record["status"] = "failed"
                continue
            # Redact credentials from provider errors.
            text = path.read_text(encoding="utf-8")
            key = os.environ.get("LLM_API_KEY", "")
            if key and key != "EMPTY" and key in text:
                text = text.replace(key, "[REDACTED]")
                path.write_text(text, encoding="utf-8")
                record["provider_secret_redacted"] = True
            record["native_artifacts"][name] = {"sha256": _sha(path.read_bytes()), "bytes": path.stat().st_size}
        if record["status"] == "finished":
            rows = [json.loads(line) for line in (run_dir / "trajectories.jsonl").read_text().splitlines() if line.strip()]
            ids = [int(row["task_id"]) for row in rows]
            if len(ids) != len(plan["tasks"]["ids"]) or set(ids) != set(plan["tasks"]["ids"]):
                raise ValueError("Native trajectory coverage does not match the development plan.")
            if not isinstance(json.loads((run_dir / "summary.json").read_text()), dict):
                raise ValueError("Native summary is invalid.")
    except KeyboardInterrupt:
        record.update(status="interrupted", child_exit_code=130)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        record.update(status="failed", error="native_execution_or_artifact_validation_failed")
    finally:
        record["finished_at"] = _now()
        # Discard provider stdout/stderr.
        (run_dir / "run.json").write_text(_json(record), encoding="utf-8")
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Freeze a small development plan without calling services")
    prepare.add_argument("--upstream", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--task-id", type=int, action="append", required=True)
    prepare.add_argument("--protocol", choices=["upstream-vllm", "api-no-tokenizer"], default="upstream-vllm")
    prepare.add_argument("--model", default="shopping-agent")
    prepare.add_argument("--checkpoint-label", default="Qwen/Qwen3.5-2B")
    for name in ("check", "run", "_native"):
        child = commands.add_parser(name)
        child.add_argument("--run-dir", type=Path, required=True)
        if name != "_native":
            child.add_argument("--python", default=sys.executable)
        if name == "check":
            child.add_argument("--probe-services", action="store_true")
            child.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            plan = create_plan(args.upstream, args.output_dir, args.task_id, protocol_name=args.protocol,
                               model_name=args.model, declared_checkpoint=args.checkpoint_label)
            print(_json({"status": "prepared", "run_dir": str(args.output_dir.resolve()), "tasks": plan["tasks"]["ids"],
                         "protocol": plan["protocol"]["id"], "model_called": False, "training_started": False}), end="")
            return 0
        if args.command == "_native":
            _collect_native(args.run_dir)
            return 0
        report = preflight(args.run_dir, args.python, args.probe_services) if args.command == "check" else execute(args.run_dir, args.python)
        if args.command == "check" and args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(_json(report))
        print(_json(report), end="")
        return 0 if report["status"] in {"static_ready", "ready_to_attempt", "finished"} else 1
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        # Expose error types only.
        print(_json({"status": "error", "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}), file=sys.stderr, end="")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
