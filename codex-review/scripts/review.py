#!/usr/bin/env python3
"""Run one native Codex review; retain rounds and Claude's finding dispositions."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import uuid

MAX_ROUNDS = 5
DISPOSITIONS = ("fixed", "not_a_problem", "accepted_tradeoff", "deferred", "needs_decision")
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


class ReviewError(Exception):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode:
        raise ReviewError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def snapshot(repo):
    """Fingerprint refs, index, working changes, and nonignored untracked files."""
    parts = [
        git(repo, "rev-parse", "HEAD"),
        git(repo, "show-ref"),
        git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all"),
        git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD"),
        git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--cached"),
    ]
    digest = hashlib.sha256()
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    untracked = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    for raw_name in sorted(filter(None, untracked.split(b"\0"))):
        path = Path(repo) / os.fsdecode(raw_name)
        digest.update(raw_name + b"\0")
        if path.is_symlink():
            digest.update(b"symlink\0" + os.fsencode(os.readlink(path)))
        elif path.is_file():
            digest.update(str(path.stat().st_mode).encode() + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
    return {
        "head": parts[0].decode().strip(),
        "fingerprint": digest.hexdigest(),
        "status": parts[2].decode(errors="replace").replace("\0", "\n"),
        "untracked": [os.fsdecode(p) for p in untracked.split(b"\0") if p],
    }


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def validate_review(output):
    """Codex falls back to an unstructured explanation when its review JSON fails to parse."""
    if not isinstance(output, dict) or not isinstance(output.get("findings"), list):
        raise ReviewError("Codex review output did not contain a findings array")
    if not str(output.get("overall_correctness") or "").strip():
        raise ReviewError("Codex returned an unstructured review (no overall_correctness); inspect review.txt")
    for finding in output["findings"]:
        if not isinstance(finding, dict):
            raise ReviewError("Invalid finding object")
        for key in ("title", "body"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                raise ReviewError(f"Finding is missing a nonempty {key}")
        location = finding.get("code_location")
        if not isinstance(location, dict) or not str(location.get("absolute_file_path") or "").strip():
            raise ReviewError("Finding is missing code_location.absolute_file_path")
        if "priority" in finding and finding["priority"] is not None and finding["priority"] not in (0, 1, 2, 3):
            raise ReviewError("Finding priority must be 0 through 3")
    return output


def parse_events(path):
    """Read `codex exec --json` output; only a completed turn counts."""
    thread_id, text_parts, warnings = None, [], []
    completed, failure, usage = False, None, None
    with Path(path).open() as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise ReviewError(f"Invalid JSON in events line {number}") from exc
            kind = event.get("type")
            if kind == "thread.started":
                thread_id = event.get("thread_id")
            elif kind == "item.completed" and event.get("item", {}).get("type") == "agent_message":
                text_parts.append(event["item"].get("text", ""))
            elif kind == "turn.completed":
                completed, usage = True, event.get("usage")
            elif kind == "turn.failed":
                failure = (event.get("error") or {}).get("message") or "turn failed"
            elif kind == "error":
                warnings.append(event.get("message") or json.dumps(event))
    if failure:
        raise ReviewError(f"Codex review failed: {failure}")
    if not thread_id:
        raise ReviewError("Codex did not report a thread ID; review is incomplete")
    if not completed:
        detail = f" ({warnings[-1]})" if warnings else ""
        raise ReviewError(f"Codex exited without completing its turn; review is incomplete{detail}")
    return {"thread_id": thread_id, "text": "\n\n".join(text_parts), "usage": usage, "warnings": warnings}


def started_thread(path):
    """Thread ID from a partial event log, so failed reviews can still be archived."""
    with contextlib.suppress(OSError, ValueError):
        with Path(path).open() as stream:
            for line in stream:
                event = json.loads(line) if line.strip() else {}
                if event.get("type") == "thread.started":
                    return event.get("thread_id")
    return None


def find_rollout(home, thread_id):
    matches = sorted((home / "sessions").glob(f"*/*/*/rollout-*-{thread_id}.jsonl"))
    if not matches:
        raise ReviewError(f"No Codex rollout found for thread {thread_id} under {home / 'sessions'}")
    return matches[-1]


def parse_rollout(path, thread_id):
    """Extract the structured review and the review sub-thread IDs from the parent rollout."""
    output, children = None, []
    with Path(path).open() as stream:
        for line in stream:
            if not line.strip():
                continue
            payload = json.loads(line).get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "item_completed":
                continue
            child = payload.get("thread_id")
            if child and child != thread_id and child not in children:
                children.append(child)
            item = payload.get("item") or {}
            if item.get("type") == "ExitedReviewMode":
                output = item.get("review_output")
    if output is None:
        raise ReviewError("Codex rollout has no review output; the review did not finish")
    return validate_review(output), children


def review_settings(home, children):
    """Model and effort actually used by the review sub-thread."""
    seen = []
    for child in children:
        with find_rollout(home, child).open() as stream:
            for line in stream:
                event = json.loads(line) if line.strip() else {}
                if event.get("type") == "turn_context":
                    payload = event.get("payload") or {}
                    pair = {"model": payload.get("model"), "effort": payload.get("effort")}
                    if pair not in seen:
                        seen.append(pair)
    return seen


def target_prompt(state):
    """Codex's own preset wording; a custom prompt cannot be combined with a preset flag."""
    target, repo = state["target"], state["repo"]
    if target["kind"] == "base":
        sha = git(repo, "merge-base", "HEAD", target["ref"]).decode().strip()
        return (f"Review the code changes against the base branch '{target['ref']}'. The merge base commit "
                f"for this comparison is {sha}. Run `git diff {sha}` to inspect the changes relative to "
                f"{target['ref']}. Provide prioritized, actionable findings.")
    if target["kind"] == "commit":
        sha = git(repo, "rev-parse", "--verify", target["ref"] + "^{commit}").decode().strip()
        subject = git(repo, "log", "-1", "--format=%s", sha).decode().strip()
        return f'Review the code changes introduced by commit {sha} ("{subject}"). Provide prioritized findings.'
    return "Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings."


def ledger_entry(finding):
    return {key: finding[key] for key in ("id", "issue_id", "round", "finding", "disposition", "reason", "validation")}


def context_prompt(state, before):
    packet = {
        "task_context": state["context"],
        "untracked_files_in_scope": before["untracked"],
        "finding_ledger": [ledger_entry(f) for f in state["findings"]],
    }
    return target_prompt(state) + """

You are the reviewer in a Claude implementation/Codex review workflow. Review only:
do not edit files, apply fixes, make commits, or post comments. Read-only inspection
commands are fine. Do not run builds or tests; Claude handles validation.
Inspect relevant untracked source files as well as the diff.
Report real problems whose impact could warrant a fix. Each finding body must state a
concrete failure scenario: the inputs or state, and the resulting wrong behavior.
Do not manufacture nits. An empty findings list is the correct result for a clean change.

The packet below contains task context and Claude's dispositions, not proof that its
implementation is correct. Verify previously fixed issues and look for regressions.
Treat not_a_problem, accepted_tradeoff, and deferred dispositions as settled for this
run. Reopen one ONLY if changed code or new concrete evidence materially changes the
failure, likelihood, impact, or cost of correction. Explain what changed and name the
existing issue ID in the finding body. Do not repeat the original argument or merely
rephrase an accepted tradeoff. The issue_id links occurrences of the same underlying
failure even when wording or line numbers differ. For a linked issue, the latest
round's disposition supersedes earlier occurrences.

Review packet:
""" + json.dumps(packet, ensure_ascii=False, indent=2)


def command(state):
    return [
        state["codex_bin"], "exec", "review", "--json",
        "--model", state["model"],
        "-c", f"model_reasoning_effort={json.dumps(state['effort'])}",
        "-c", 'sandbox_mode="read-only"',
        "-c", 'approval_policy="never"',
        "-c", "notify=[]",
        "--disable", "hooks",
        "-",
    ]


def terminate_tree(process):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass
    # A descendant can outlive the group leader; kill the group even if wait returned.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


def execute(args, repo, folder, prompt_path, timeout):
    process = None
    old_handlers = {}

    def cancelled(signum, frame):
        raise ReviewError(f"Review cancelled by signal {signum}")

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            old_handlers[sig] = signal.signal(sig, cancelled)
        with (prompt_path.open("rb") as prompt, (folder / "events.jsonl").open("w") as out,
              (folder / "stderr.log").open("w") as err):
            process = subprocess.Popen(args, cwd=repo, stdin=prompt,
                                       stdout=out, stderr=err, start_new_session=True)
            try:
                code = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                raise ReviewError(f"Review timed out after {timeout:g} seconds") from exc
            return code
    finally:
        if process is not None:
            terminate_tree(process)
        for sig, previous in old_handlers.items():
            signal.signal(sig, previous)


def archive(state, thread_id):
    """Keep headless review threads out of Codex history and Monitor; the rollout stays on disk."""
    result = subprocess.run([state["codex_bin"], "archive", thread_id], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=60)
    if result.returncode:
        return (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
    return None


@contextlib.contextmanager
def locked(folder):
    with (folder / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError(f"Another operation is using {folder}") from exc
        yield


def location(repo, finding):
    place = finding["code_location"]
    path = Path(place["absolute_file_path"])
    with contextlib.suppress(ValueError):
        path = path.relative_to(repo)
    lines = place.get("line_range") or {}
    start, end = lines.get("start"), lines.get("end")
    if start is None:
        return str(path)
    return f"{path}:{start}" + (f"-{end}" if end not in (None, start) else "")


def summary(folder, state):
    latest = len(state["rounds"])
    last = state["rounds"][-1] if state["rounds"] else None
    return {
        "run_dir": str(folder), "status": state["status"],
        "model": state["model"], "effort": state["effort"], "mode": state["mode"],
        "target": state["target"],
        "rounds_used": latest, "max_rounds": state["max_rounds"],
        "rounds_remaining": state["max_rounds"] - latest,
        "last_round": {k: v for k, v in last.items() if k not in ("before", "after")} if last else None,
        "findings": [f for f in state["findings"] if f["round"] == latest],
        "ledger_path": str(folder / "state.json"),
        "finish_reason": state.get("finish_reason"),
    }


def initialize(args):
    repo = Path(git(Path(args.repo).expanduser().resolve(), "rev-parse", "--show-toplevel").decode().strip())
    before = snapshot(repo)
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))) / "codex-review" / "runs"
    folder = Path(args.run_dir).expanduser().resolve() if args.run_dir else root / (dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
    if folder.is_relative_to(repo):
        raise ReviewError("Use a run directory outside the reviewed repository")
    if args.base and args.commit:
        raise ReviewError("Choose at most one of --base and --commit")
    ref = args.base or args.commit
    if ref is not None and (not ref.strip() or ref.startswith("-") or any(c in ref for c in "\r\n")):
        raise ReviewError("--base/--commit must be a Git ref")
    target = {"kind": "base", "ref": ref} if args.base else {"kind": "commit", "ref": ref} if args.commit else {"kind": "uncommitted"}
    model = args.model or "gpt-6-astra"
    if not model.strip() or model.startswith("-"):
        raise ReviewError("Invalid model")
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        "created_at": now(), "repo": str(repo), "target": target,
        "model": model, "effort": args.effort or "xhigh",
        "mode": args.mode or "standard",
        "max_rounds": args.rounds or (5 if args.mode == "max" else 2),
        "timeout": args.timeout or 1800,
        "codex_bin": args.codex_bin or "codex",
        "context": Path(args.context_file).read_text() if args.context_file else "",
        "initial_snapshot": before, "status": "ready", "rounds": [], "findings": [],
    }
    with locked(folder):
        if (folder / "state.json").exists():
            raise ReviewError("Run already exists; resume with only --run-dir")
        target_prompt(state)  # Fail fast on a bad ref.
        write_json(folder / "state.json", state)
    return folder


def run(args):
    existing = args.run_dir and (Path(args.run_dir).expanduser() / "state.json").exists()
    if existing:
        if any(getattr(args, key) is not None for key in ("repo", "model", "effort", "mode", "rounds", "base", "commit", "context_file", "timeout", "codex_bin")):
            raise ReviewError("Resume with only --run-dir; model, scope, and round budget are fixed for the run")
        folder = Path(args.run_dir).expanduser().resolve()
    else:
        if not args.repo:
            raise ReviewError("A new run requires --repo")
        folder = initialize(args)
    with locked(folder):
        state = read_json(folder / "state.json")
        if state["status"] == "complete":
            raise ReviewError("This run is complete; start a new run explicitly")
        if len(state["rounds"]) >= state["max_rounds"]:
            raise ReviewError(f"Round limit reached ({state['max_rounds']}); do not create a new run to bypass it")
        if any(f["disposition"] in ("pending", "needs_decision") for f in state["findings"]):
            raise ReviewError("Resolve pending findings and required decisions before the next round")
        before = snapshot(state["repo"])
        number = len(state["rounds"]) + 1
        output = folder / f"round-{number:02d}"
        output.mkdir()
        round_state = {"number": number, "started_at": now(), "status": "running", "before": before}
        state["rounds"].append(round_state)
        state["status"] = "running"
        write_json(folder / "state.json", state)
        print(f"Codex {state['model']} ({state['effort']}) review {number}/{state['max_rounds']}: {folder}", file=sys.stderr, flush=True)
        thread_id = None
        try:
            prompt_path = output / "context.txt"
            prompt_path.write_text(context_prompt(state, before))
            argv = command(state)
            write_json(output / "command.json", argv)
            code = execute(argv, state["repo"], output, prompt_path, state["timeout"])
            round_state["exit_code"] = code
            after = snapshot(state["repo"])
            round_state["after"] = after
            events = parse_events(output / "events.jsonl")
            thread_id = events["thread_id"]
            round_state["thread_id"] = thread_id
            (output / "review.txt").write_text(events["text"])
            if code:
                raise ReviewError(f"Codex exited with status {code}; see stderr.log")
            home = codex_home()
            rollout = find_rollout(home, thread_id)
            report, children = parse_rollout(rollout, thread_id)
            settings = review_settings(home, children)
            write_json(output / "result.json", {"thread_id": thread_id, "rollout": str(rollout),
                                                "review_threads": children, "settings": settings,
                                                "review_output": report, "usage": events["usage"],
                                                "warnings": events["warnings"]})
            if before["fingerprint"] != after["fingerprint"]:
                raise ReviewError("Repository changed during review; findings are stale. No changes were reverted")
            if not settings:
                raise ReviewError("Could not confirm the review model from the Codex rollout; inspect result.json")
            if any(s != {"model": state["model"], "effort": state["effort"]} for s in settings):
                raise ReviewError("Codex reviewed with an unexpected model or effort; inspect result.json")
            round_state.update({"status": "completed", "settings": settings, "usage": events["usage"],
                                "overall": {key: report.get(key) for key in ("overall_correctness", "overall_explanation", "overall_confidence_score")}})
            for index, finding in enumerate(report["findings"], 1):
                identity = f"R{number}F{index}"
                state["findings"].append({"id": identity, "issue_id": identity, "round": number,
                                          "location": location(state["repo"], finding),
                                          "finding": finding, "disposition": "pending", "reason": "", "validation": "", "history": []})
            state["status"] = "awaiting_dispositions" if report["findings"] else "no_new_findings"
        except (ReviewError, OSError, ValueError) as exc:
            round_state.update({"status": "failed", "error": str(exc)})
            state["status"] = "failed"
            raise ReviewError(f"{exc}\nRun artifacts: {folder}") from exc
        finally:
            thread_id = thread_id or started_thread(output / "events.jsonl")
            if thread_id:
                round_state["thread_id"] = thread_id
                try:
                    problem = archive(state, thread_id)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    problem = str(exc)
                round_state["archived"] = problem is None
                if problem:
                    round_state["archive_error"] = problem
            round_state["finished_at"] = now()
            write_json(folder / "state.json", state)
        return summary(folder, state)


def record(args):
    folder = Path(args.run_dir).expanduser().resolve()
    with locked(folder):
        state = read_json(folder / "state.json")
        if state["status"] == "complete":
            raise ReviewError("Cannot change a completed run")
        entries = {f["id"]: f for f in state["findings"]}
        if args.id not in entries:
            raise ReviewError(f"Unknown finding {args.id}")
        reason = Path(args.reason_file).read_text() if args.reason_file else args.reason
        if not reason or not reason.strip():
            raise ReviewError("A disposition requires a reason")
        entry = entries[args.id]
        same_as = entries.get(args.same_as) if args.same_as else None
        if args.same_as and (same_as is None or same_as["round"] >= entry["round"]):
            raise ReviewError("--same-as must name a finding from an earlier round")
        if args.status == "fixed" and not args.validation:
            raise ReviewError("A fixed finding requires --validation (describe checks or explain why none were run)")
        if same_as:
            entry["issue_id"] = same_as["issue_id"]
        update = {"disposition": args.status, "reason": reason.strip(), "validation": args.validation or "", "recorded_at": now()}
        entry["history"].append(update.copy())
        entry.update(update)
        if not any(f["disposition"] == "pending" for f in state["findings"]):
            state["status"] = "needs_decision" if any(f["disposition"] == "needs_decision" for f in state["findings"]) else "ready"
        write_json(folder / "state.json", state)
        return {"run_dir": str(folder), "finding": entry, "status": state["status"]}


def finish(args):
    folder = Path(args.run_dir).expanduser().resolve()
    with locked(folder):
        state = read_json(folder / "state.json")
        if args.reason == "round_limit" and len(state["rounds"]) < state["max_rounds"]:
            raise ReviewError("The round limit has not been reached")
        if args.reason == "no_worthwhile_fixes" and any(f["disposition"] in ("pending", "needs_decision") for f in state["findings"]):
            raise ReviewError("Cannot declare no worthwhile fixes with untriaged findings or required decisions")
        if args.reason == "no_worthwhile_fixes" and (not state["rounds"] or state["rounds"][-1]["status"] != "completed"):
            raise ReviewError("A failed or missing review cannot count as a clean result")
        state.update({"status": "complete", "finish_reason": args.reason, "finished_at": now(), "final_note": args.note})
        write_json(folder / "state.json", state)
        return summary(folder, state)


def positive(value):
    result = float(value)
    if not 0 < result < float("inf"):
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    review = commands.add_parser("run", help="Run one review, or resume a saved run")
    review.add_argument("--repo")
    review.add_argument("--run-dir")
    review.add_argument("--model", help="Codex model slug; default gpt-6-astra")
    review.add_argument("--effort", choices=EFFORTS, help="Reasoning effort; default xhigh")
    review.add_argument("--mode", choices=("standard", "max"))
    review.add_argument("--rounds", type=int, choices=range(1, MAX_ROUNDS + 1))
    review.add_argument("--base", help="Review changes against this base branch")
    review.add_argument("--commit", help="Review the changes introduced by this commit")
    review.add_argument("--context-file")
    review.add_argument("--timeout", type=positive, help="Per-round seconds; default 1800")
    review.add_argument("--codex-bin", help="Codex executable; default codex on PATH")
    disposition = commands.add_parser("record", help="Record Claude's disposition for a finding")
    disposition.add_argument("--run-dir", required=True)
    disposition.add_argument("--id", required=True)
    disposition.add_argument("--status", choices=DISPOSITIONS, required=True)
    reasons = disposition.add_mutually_exclusive_group(required=True)
    reasons.add_argument("--reason")
    reasons.add_argument("--reason-file")
    disposition.add_argument("--validation")
    disposition.add_argument("--same-as", help="Prior occurrence of the same underlying issue")
    status = commands.add_parser("status", help="Show the saved run and latest findings")
    status.add_argument("--run-dir", required=True)
    complete = commands.add_parser("finish", help="Record why the loop stopped")
    complete.add_argument("--run-dir", required=True)
    complete.add_argument("--reason", required=True, choices=("no_worthwhile_fixes", "round_limit", "needs_decision", "review_only", "failed", "user_stopped"))
    complete.add_argument("--note", default="")
    return cli


def main():
    args = parser().parse_args()
    try:
        if args.action == "run":
            value = run(args)
        elif args.action == "record":
            value = record(args)
        elif args.action == "finish":
            value = finish(args)
        else:
            folder = Path(args.run_dir).expanduser().resolve()
            value = summary(folder, read_json(folder / "state.json"))
        print(json.dumps(value, indent=2, ensure_ascii=False))
        return 0
    except (ReviewError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
