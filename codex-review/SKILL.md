---
name: codex-review
description: Ask Codex to review a change, then verify and fix worthwhile findings in the current Claude session. Use for requests for Codex review, GPT review, or a Codex review-and-fix loop, including max mode with up to five total review rounds. Preserve accepted tradeoffs across follow-ups.
argument-hint: "[model=gpt-6-astra] [effort=xhigh] [mode=max] [base=BRANCH | commit=SHA]"
---

# Codex review

Keep implementation and judgment in the current Claude session. Use Codex's native
review mode (`codex exec review`) as an independent reviewer. Verify that every
finding is a real problem; fix it **only when the expected benefit warrants the
complexity and regression risk**. A reviewer suggestion is not an obligation to
change code, and Codex's `[P0]`–`[P3]` priority is input to that judgment, not a verdict.

## Settings

Resolve natural-language choices and explicit parameters using this table:

| Setting | Default | Choices |
| --- | --- | --- |
| `model` | `gpt-6-astra` | Any Codex model slug, e.g. `gpt-6-astra`, `gpt-5.6-terra`, or `gpt-5.6-luna` for inexpensive tests; keep the user's choice throughout the run |
| `effort` | `xhigh` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra`; Codex reasoning effort |
| `mode` | `standard` | `standard`: at most 2 rounds; `max`: at most **5 total rounds** |
| `rounds` | From mode | An explicit ceiling from 1 through 5 |
| `target` | uncommitted | Staged, unstaged, and untracked changes; or `--base BRANCH` for branch changes against a base; or `--commit SHA` for one commit |

`mode=max` controls loop persistence, not model or effort. A round is one Codex
invocation; unsuccessful attempts consume the budget too. Five is the hard cap,
including follow-ups. Never silently start a new run to reset the budget. Stop early
when no worthwhile fixes remain. Follow an explicit review-only request without
editing code; otherwise the requested review-and-fix loop includes fixes and
appropriate validation.

Examples: `/codex-review mode=max`, `/codex-review base=main effort=high`, or "Have
Codex review this at medium effort, then fix the worthwhile issues."

## Start a review

The runner is `scripts/review.py` in this skill's base directory; use its absolute
path. It needs Python 3.10+ and an authenticated `codex` CLI. Use the existing login;
if Codex reports it is not logged in, tell the user to run `codex login` rather than
attempting it. Do not use Codex's approval- or sandbox-bypass flags.

Finish current edits and applicable checks, then identify the review scope. The
uncommitted target covers staged, unstaged, and untracked changes. With `--base`,
the runner lists untracked files for Codex too. Do not create commits just to run a
review. Keep source edits paused until the runner finishes. Existing unrelated
working changes remain the user's; identify them in the context so Codex does not
expand the task.

When useful, write a short context file outside the reviewed repository (the
scratchpad directory is ideal) containing the user's requirements, constraints,
resolved ambiguities, scope exclusions, and validation already run. Distinguish
requirements from Claude's own assumptions. Use `--context-file` to supply it; don't
copy the whole conversation.

Run one round:

```bash
python3 /absolute/path/to/codex-review/scripts/review.py run --repo /absolute/path/to/project --effort xhigh --mode max
```

**A round can take longer than the Bash tool's 10-minute limit.** Launch it with
`run_in_background: true` and wait for the completion notification; do not poll,
sleep, or start a second reviewer on the same run. Codex needs network access and
write access to `~/.codex` (its session log). If the Bash sandbox blocks either,
rerun outside the sandbox; the user approves that prompt.

The command prints a JSON summary with the `run_dir`, Codex's overall verdict,
findings with IDs (`R1F1`, …), repo-relative locations, native titles, bodies,
priorities, and confidence scores. Default records live under
`~/.local/state/codex-review/runs` (or `$XDG_STATE_HOME/codex-review/runs`). Keep
the run directory in context. An explicit `--run-dir` must be outside the reviewed
repository. Each round saves the prompt (`context.txt`), the command, raw JSONL
events, stderr, Codex's rendered review (`review.txt`), and the structured result.
`state.json` is the finding ledger and round record. `--timeout` sets a per-round
wall-clock limit in seconds (default 1800).

The runner runs Codex with a read-only sandbox, `approval_policy=never`, hooks and
`notify` disabled, and explicit model and effort, which it then confirms from the
review thread. It reads the structured review from Codex's rollout and archives the
review thread afterward so it stays out of Codex history and Monitor's queue (an
archive failure is recorded, not fatal). It checks for source changes during
review; that detects changed inputs, it is not an isolation boundary.

A completed round with an empty findings array is a clean review. Missing output, an
unstructured review, authentication or usage-limit errors, timeouts, an unexpected
model or effort, or changed source are incomplete reviews, never evidence that no
issues exist. Inspect the saved error, fix the specific cause where authorized, and
retry within the same budget if warranted.

## Adjudicate findings

Read each complete finding body, trace its failure scenario through the current code,
and consider reachability, impact, fix complexity, and regression risk. Do not treat
style preferences or speculative edge cases as automatic work. A small fix can still
be worthwhile; do not impose a blanket priority cutoff. Support both fixes and
dismissals with code evidence, concrete reasoning, or targeted checks.

Record every finding using `record`:

| Disposition | Meaning |
| --- | --- |
| `fixed` | Changed and checked; describe validation or explain why none was warranted |
| `not_a_problem` | The claimed failure does not occur; explain why |
| `accepted_tradeoff` | Real issue, expected benefit does not warrant added complexity/risk |
| `deferred` | Worth doing, outside the authorized task or current change |
| `needs_decision` | A real user decision or missing fact prevents resolution |

```bash
python3 /absolute/path/to/codex-review/scripts/review.py record --run-dir /absolute/run --id R1F2 --status accepted_tradeoff --reason-file /absolute/reason.txt
```

For `fixed`, include `--validation` describing the actual checks. A disposition like
"too much of a pain" is insufficient: name the impact, reachability, and complexity
tradeoff. Do not infer authority for unrelated changes from a finding. Preserve
unresolved substantive risk in the final report.

Compare new findings to earlier failure scenarios in `state.json`, not just titles
or line numbers. For a repeat or reopening, add `--same-as R1F2` to link it to the
original issue. Record whether Codex supplied material new evidence. An unchanged
repeat retains its prior disposition and does not justify another cycle.

## Follow up and stop

When fixes were made, or a substantive evidence-backed disagreement warrants another
review, resume using **only** the saved directory (again in the background):

```bash
python3 /absolute/path/to/codex-review/scripts/review.py run --run-dir /absolute/run
```

Codex review mode cannot resume a thread, so each round is a fresh review with the
original model, effort, and target plus the complete disposition ledger. Codex is
asked to verify fixes and find remaining worthwhile issues. Accepted tradeoffs,
rejected findings, and deferred items are settled unless changed code or new
concrete evidence materially alters likelihood, impact, or fix cost. Do not ask
Codex to forget the earlier dispositions.

Stop when no new worthwhile fixes remain, findings are only settled repeats without
new evidence, the round budget is exhausted, the user stops, or progress needs a user
decision. Do not spend remaining rounds just because they exist. Apply and validate
justified final-round fixes, but accurately label them as not subsequently reviewed
by Codex. No sixth "final verification" review in max mode.

Record completion with `finish --run-dir ... --reason ... --note ...`. Reasons are
`no_worthwhile_fixes`, `round_limit`, `needs_decision`, `review_only`, `failed`, or
`user_stopped`. This closes the loop record, not a claim that all bugs are gone. Use
`status --run-dir ...` to recover after interruption; it never launches Codex.

Report model and effort, rounds used, what was fixed and validated, accepted
tradeoffs/deferred issues, unresolved decisions, and the run directory. Clearly
distinguish successful review, validation performed, and unreviewed final edits.
Keep routine details in the saved record.
