import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "codex-review" / "scripts" / "review.py"
spec = importlib.util.spec_from_file_location("review", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def finding(path="/repo/price.py"):
    return {"title": "[P1] Subtract the discount", "priority": 1, "confidence_score": 0.9,
            "body": "price(100, 20) returns 120 instead of 80 because the discount is added.",
            "code_location": {"absolute_file_path": path, "line_range": {"start": 2, "end": 2}}}


def output(findings, correctness="patch is incorrect"):
    return {"findings": findings, "overall_correctness": correctness,
            "overall_explanation": "Discount math is inverted.", "overall_confidence_score": 0.9}


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
mode = os.environ.get('CODEX_REVIEW_TEST_MODE', 'finding')
home = pathlib.Path(os.environ['CODEX_HOME'])
day = home / 'sessions' / '2026' / '10' / '01'
day.mkdir(parents=True, exist_ok=True)
if args[0] == 'archive':
    if mode == 'archive_fail':
        print('no such session', file=sys.stderr); sys.exit(1)
    (home / 'archived.txt').open('a').write(args[1] + '\n')
    sys.exit(0)
assert args[:3] == ['exec', 'review', '--json'] and args[-1] == '-', args
prompt = sys.stdin.read()
(home / 'last-prompt.txt').write_text(prompt)
model = args[args.index('--model') + 1]
effort = json.loads(args[args.index('-c') + 1].split('=', 1)[1])
count = len(list(day.glob('rollout-*-parent-*.jsonl'))) + 1
parent, child = f'parent-{count}', f'child-{count}'
def emit(obj):
    print(json.dumps(obj), flush=True)
emit({'type': 'thread.started', 'thread_id': parent})
emit({'type': 'turn.started'})
if mode == 'timeout':
    grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    pathlib.Path(os.environ['CODEX_REVIEW_TEST_PID']).write_text(str(grandchild.pid))
    time.sleep(60)
if mode == 'mutate':
    pathlib.Path('price.py').write_text('changed by reviewer')
if mode == 'turn_failed':
    emit({'type': 'turn.failed', 'error': {'message': 'usage limit reached'}}); sys.exit(1)
location = {'absolute_file_path': os.path.join(os.getcwd(), 'price.py'), 'line_range': {'start': 2, 'end': 2}}
finding = {'title': '[P1] Subtract the discount', 'body': 'price(100, 20) returns 120 instead of 80.',
           'confidence_score': 0.9, 'priority': 1, 'code_location': location}
review = {'findings': [] if mode in ('empty', 'unstructured') else [finding],
          'overall_correctness': '' if mode == 'unstructured' else 'patch is incorrect',
          'overall_explanation': 'Review done', 'overall_confidence_score': 0.9}
used = 'gpt-other' if mode == 'wrong_model' else model
(day / f'rollout-2026-10-01T00-00-00-{child}.jsonl').write_text(
    json.dumps({'type': 'turn_context', 'payload': {'model': used, 'effort': effort}}) + '\n')
lines = [{'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': child, 'item': {'type': 'Reasoning'}}}]
if mode != 'no_output':
    lines.append({'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': parent,
                  'item': {'type': 'ExitedReviewMode', 'review_output': review}}})
(day / f'rollout-2026-10-01T00-00-00-{parent}.jsonl').write_text('\n'.join(json.dumps(l) for l in lines) + '\n')
emit({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'agent_message', 'text': 'Review done'}})
emit({'type': 'turn.completed', 'usage': {'output_tokens': 1}})
if mode == 'nonzero': sys.exit(2)
'''


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def events(self, events):
        path = self.root / "events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events))
        return review.parse_events(path)

    def rollout(self, items, thread="parent"):
        path = self.root / "rollout.jsonl"
        path.write_text("\n".join(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "thread_id": t, "item": i}}) for t, i in items))
        return review.parse_rollout(path, thread)

    def test_completed_turn_is_required(self):
        start = {"type": "thread.started", "thread_id": "abc"}
        self.assertEqual(self.events([start, {"type": "turn.completed"}])["thread_id"], "abc")
        with self.assertRaisesRegex(review.ReviewError, "incomplete"):
            self.events([start, {"type": "turn.started"}])
        with self.assertRaisesRegex(review.ReviewError, "usage limit"):
            self.events([start, {"type": "turn.failed", "error": {"message": "usage limit"}}])

    def test_retry_errors_are_warnings_when_turn_completes(self):
        result = self.events([{"type": "thread.started", "thread_id": "abc"},
                              {"type": "error", "message": "Reconnecting... 1/5"}, {"type": "turn.completed"}])
        self.assertEqual(result["warnings"], ["Reconnecting... 1/5"])

    def test_extracts_native_review_and_review_threads(self):
        report, children = self.rollout([("child", {"type": "Reasoning"}),
                                         ("parent", {"type": "ExitedReviewMode", "review_output": output([finding()])})])
        self.assertEqual(report["findings"], [finding()])
        self.assertEqual(children, ["child"])

    def test_empty_structured_review_is_clean(self):
        report, _ = self.rollout([("parent", {"type": "ExitedReviewMode", "review_output": output([], "patch is correct")})])
        self.assertEqual(report["findings"], [])

    def test_unstructured_fallback_and_missing_output_are_failures(self):
        with self.assertRaisesRegex(review.ReviewError, "unstructured"):
            self.rollout([("parent", {"type": "ExitedReviewMode", "review_output": output([], "")})])
        with self.assertRaisesRegex(review.ReviewError, "did not finish"):
            self.rollout([("parent", {"type": "ExitedReviewMode", "review_output": None})])

    def test_invalid_findings_fail_with_useful_error(self):
        broken = finding()
        del broken["code_location"]
        with self.assertRaisesRegex(review.ReviewError, "absolute_file_path"):
            review.validate_review(output([broken]))
        with self.assertRaisesRegex(review.ReviewError, "priority"):
            review.validate_review(output([dict(finding(), priority=7)]))

    def test_location_is_repo_relative(self):
        self.assertEqual(review.location(Path("/repo"), finding()), "price.py:2")


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-review-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.fake = self.root / "fake codex"
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o755)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.directory = self.root / "run"
        self.env = dict(os.environ, CODEX_HOME=str(self.home), CODEX_REVIEW_TEST_MODE="finding")
        self.git("init", "-q", "-b", "main")
        (self.repo / "price.py").write_text("def price(p, d):\n    return p * (1 - d / 100)\n")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Initial")
        (self.repo / "price.py").write_text("def price(p, d):\n    return p * (1 + d / 100)\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def cli(self, *args, ok=True):
        p = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env, capture_output=True, text=True, timeout=15)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return p.stderr

    def start(self, *extra, ok=True):
        return self.cli("run", "--repo", str(self.repo), "--run-dir", str(self.directory),
                        "--codex-bin", str(self.fake), "--model", "gpt-test", "--effort", "low", *extra, ok=ok)

    def record(self, finding_id, status="accepted_tradeoff", *extra, ok=True):
        return self.cli("record", "--run-dir", str(self.directory), "--id", finding_id,
                        "--status", status, "--reason", "Rare fixture-only case; extra recovery state is not worthwhile", *extra, ok=ok)

    def state(self):
        return json.loads((self.directory / "state.json").read_text())

    def test_review_round_records_findings_and_archives_thread(self):
        result = self.start("--mode", "max")
        self.assertEqual(result["max_rounds"], 5)
        self.assertEqual(result["findings"][0]["location"], "price.py:2")
        self.assertTrue(result["last_round"]["archived"])
        self.assertEqual((self.home / "archived.txt").read_text(), "parent-1\n")
        args = json.loads((self.directory / "round-01" / "command.json").read_text())
        self.assertIn('sandbox_mode="read-only"', args)
        self.assertIn('model_reasoning_effort="low"', args)
        self.assertIn("Review the current code changes", (self.home / "last-prompt.txt").read_text())

    def test_follow_up_is_fresh_review_with_full_disposition_ledger(self):
        self.start("--mode", "max")
        self.assertIn("pending", self.cli("run", "--run-dir", str(self.directory), ok=False))
        self.record("R1F1")
        self.cli("run", "--run-dir", str(self.directory))
        prompt = (self.home / "last-prompt.txt").read_text()
        self.assertIn(self.state()["findings"][0]["reason"], prompt)
        self.assertIn("accepted_tradeoff", prompt)
        self.assertEqual(self.state()["rounds"][1]["thread_id"], "parent-2")
        self.record("R2F1", "accepted_tradeoff", "--same-as", "R1F1")
        self.assertEqual(self.state()["findings"][1]["issue_id"], "R1F1")

    def test_base_and_commit_targets_use_native_wording(self):
        self.git("checkout", "-q", "-b", "feature")
        self.start("--base", "main")
        self.assertIn("against the base branch 'main'", (self.home / "last-prompt.txt").read_text())
        self.directory = self.root / "run-commit"
        self.start("--commit", "HEAD")
        self.assertIn('("Initial")', (self.home / "last-prompt.txt").read_text())

    def test_bad_target_fails_before_any_review(self):
        self.assertIn("--base", self.start("--base", "main", "--commit", "HEAD", ok=False))
        self.assertIn("error", self.start("--commit", "nope", ok=False))
        self.assertFalse((self.home / "last-prompt.txt").exists())

    def test_five_total_attempts_and_no_sixth_invocation(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "empty"
        self.start("--mode", "max")
        for _ in range(4):
            self.cli("run", "--run-dir", str(self.directory))
        self.assertIn("Round limit reached (5)", self.cli("run", "--run-dir", str(self.directory), ok=False))
        self.assertFalse((self.directory / "round-06").exists())

    def test_standard_limit_is_two(self):
        self.assertEqual(self.start()["max_rounds"], 2)

    def test_cannot_change_model_or_budget_on_resume(self):
        self.start()
        self.assertIn("fixed for the run", self.cli("run", "--run-dir", str(self.directory), "--effort", "high", ok=False))

    def test_failed_attempt_consumes_budget_and_cannot_finish_clean(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "turn_failed"
        self.assertIn("usage limit reached", self.start("--rounds", "1", ok=False))
        self.assertEqual(self.state()["rounds"][0]["status"], "failed")
        self.assertTrue(self.state()["rounds"][0]["archived"])
        self.assertIn("Round limit", self.cli("run", "--run-dir", str(self.directory), ok=False))
        self.assertIn("clean result", self.cli("finish", "--run-dir", str(self.directory), "--reason", "no_worthwhile_fixes", ok=False))

    def test_unstructured_or_missing_review_is_never_clean(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "unstructured"
        self.assertIn("unstructured", self.start(ok=False))
        self.env["CODEX_REVIEW_TEST_MODE"] = "no_output"
        self.assertIn("did not finish", self.cli("run", "--run-dir", str(self.directory), ok=False))

    def test_source_mutation_is_detected_and_not_reverted(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "mutate"
        self.assertIn("Repository changed", self.start(ok=False))
        self.assertEqual((self.repo / "price.py").read_text(), "changed by reviewer")
        self.assertEqual(self.state()["findings"], [])

    def test_untracked_sources_affect_snapshot(self):
        before = review.snapshot(self.repo)
        (self.repo / "new.py").write_text("first")
        added = review.snapshot(self.repo)
        (self.repo / "new.py").write_text("other")
        changed = review.snapshot(self.repo)
        self.assertNotEqual(before["fingerprint"], added["fingerprint"])
        self.assertNotEqual(added["fingerprint"], changed["fingerprint"])
        self.assertIn("new.py", changed["untracked"])

    def test_fixed_requires_validation_and_decisions_block_next_round(self):
        self.start()
        self.assertIn("validation", self.record("R1F1", "fixed", ok=False))
        self.record("R1F1", "needs_decision")
        self.assertIn("required decisions", self.cli("run", "--run-dir", str(self.directory), ok=False))
        self.record("R1F1", "fixed", "--validation", "Focused regression test passed")
        self.assertEqual(len(self.state()["findings"][0]["history"]), 2)

    def test_wrong_model_and_nonzero_exit_never_pass(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "wrong_model"
        self.assertIn("unexpected model", self.start(ok=False))
        self.env["CODEX_REVIEW_TEST_MODE"] = "nonzero"
        self.assertIn("exited with status 2", self.cli("run", "--run-dir", str(self.directory), ok=False))

    def test_archive_failure_is_recorded_without_failing_review(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "archive_fail"
        result = self.start()
        self.assertEqual(result["last_round"]["status"], "completed")
        self.assertFalse(result["last_round"]["archived"])
        self.assertIn("no such session", result["last_round"]["archive_error"])

    def test_finish_preserves_tradeoffs_and_prevents_reopening_run(self):
        self.start()
        self.record("R1F1")
        self.cli("finish", "--run-dir", str(self.directory), "--reason", "no_worthwhile_fixes")
        self.assertIn("complete", self.cli("run", "--run-dir", str(self.directory), ok=False))
        self.assertEqual(self.state()["findings"][0]["disposition"], "accepted_tradeoff")

    def test_timeout_leaves_a_failed_round_and_stops_descendant(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "timeout"
        pidfile = self.root / "child.pid"
        self.env["CODEX_REVIEW_TEST_PID"] = str(pidfile)
        self.assertIn("timed out", self.start("--timeout", "3", ok=False))
        self.assertEqual(self.state()["rounds"][0]["status"], "failed")
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail(f"Reviewer descendant {pid} survived timeout cleanup")

    def test_concurrent_round_is_rejected_and_cancellation_is_recorded(self):
        self.env["CODEX_REVIEW_TEST_MODE"] = "timeout"
        pidfile = self.root / "child.pid"
        self.env["CODEX_REVIEW_TEST_PID"] = str(pidfile)
        process = subprocess.Popen([sys.executable, str(SCRIPT), "run", "--repo", str(self.repo),
                                    "--run-dir", str(self.directory), "--codex-bin", str(self.fake),
                                    "--model", "gpt-test", "--effort", "low"], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 8
            while not pidfile.exists() and time.monotonic() < deadline and process.poll() is None:
                time.sleep(0.05)
            self.assertTrue(pidfile.exists())
            self.assertIn("Another operation", self.cli("run", "--run-dir", str(self.directory), ok=False))
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=6)
            self.assertIn("cancelled", stderr)
            self.assertEqual(self.state()["rounds"][0]["status"], "failed")
            self.assertEqual(len(self.state()["rounds"]), 1)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
