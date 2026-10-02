# Codex Review for Claude Code

A Claude Code skill that runs Codex's native review mode as a reviewer, then keeps
the verification and implementation work in the original Claude session.

For every finding, Claude verifies that it is a real problem and fixes it only when
the benefit warrants the complexity and regression risk. Accepted tradeoffs and
rejected findings go back to Codex on follow-up rounds, so the loop can stop when
there are no worthwhile fixes left.

Companion to [claude-review](https://github.com/JustinTervala/claude-review), which
does the reverse (Codex implements, Claude reviews).

## Install

Requirements: Python 3.10+, Git, macOS or Linux, Claude Code, and an authenticated
Codex CLI with `codex exec review` and `codex archive`.

```bash
git clone https://github.com/JustinTervala/codex-review.git
cd codex-review
mkdir -p ~/.claude/skills
ln -s "$PWD/codex-review" ~/.claude/skills/codex-review
```

If the CLI is not already authenticated, run `codex login` in your terminal. The
runner uses that login; it does not store credentials.

## Use in Claude Code

```text
/codex-review mode=max
/codex-review base=main effort=high
/codex-review model=gpt-5.6-luna effort=low rounds=1
```

Natural-language requests work too: "Have Codex review this change, then fix the
issues worth addressing." Ask for review only when you want findings without edits.

| Setting | Default | Options |
| --- | --- | --- |
| Model | `gpt-6-astra` | Any Codex model slug; passed directly to Codex |
| Effort | `xhigh` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |
| Target | Uncommitted | Staged, unstaged, and untracked changes; `base=BRANCH`; or `commit=SHA` |
| Loop mode | `standard` | `standard`: up to 2 rounds; `max`: up to 5 rounds |
| Round ceiling | From mode | Explicit `rounds=1` through `rounds=5` |

Max mode controls the number of review rounds independently of the model and
effort. Five is a hard cap, including failed attempts and follow-up reviews.
Claude stops early when further rounds would only repeat settled findings.

## How the loop works

1. Claude finishes its edits and relevant checks, then pauses source changes.
2. The Python runner invokes `codex exec review` with Codex's own target wording and
   reads the structured review (`[P0]`–`[P3]` findings with file and line ranges)
   from the Codex session log.
3. Claude traces each failure scenario and records a disposition: `fixed`,
   `not_a_problem`, `accepted_tradeoff`, `deferred`, or `needs_decision`.
4. When another round is useful, the runner starts a fresh Codex review (review
   mode cannot resume a thread) and supplies the full finding ledger. Related
   findings can be linked across rounds even when their wording or line numbers
   change.
5. Codex checks fixes and remaining issues. It should reopen a settled tradeoff
   only when new evidence materially changes the likelihood, impact, or fix cost.

The runner performs one review per invocation. The [skill](codex-review/SKILL.md)
directs Claude's judgment, edits, validation, and stopping decisions. It does not
automatically publish review comments.

## Local records and runner

Run records live outside the reviewed repository, under
`$XDG_STATE_HOME/codex-review/runs` or `~/.local/state/codex-review/runs`.
They contain the finding ledger, review prompt, raw events, stderr, and extracted
results. These files can include source code and review content; they are not part
of this repository.

```bash
python3 codex-review/scripts/review.py --help
python3 codex-review/scripts/review.py run --repo /path/to/project --effort xhigh --mode max
python3 codex-review/scripts/review.py status --run-dir /path/to/saved/run
```

See [SKILL.md](codex-review/SKILL.md) for disposition, resume, and completion commands.
The reviewed project must be a Git repository with an existing commit. Codex needs
network access and write access to `~/.codex`; a round can outlast Claude Code's
10-minute Bash limit, so the skill runs it in the background.

Codex runs with a read-only sandbox, `approval_policy=never`, hooks and `notify`
disabled, and an explicit model and effort, which the runner confirms from the
review thread. Each review thread is archived afterward so it stays out of Codex
history. The runner detects changes to review inputs; that is not a filesystem
sandbox. Missing or unstructured reviews, failed turns, unexpected models,
timeouts, and changed source are reported as incomplete reviews rather than clean
results.

This is a personal tool targeting the current Codex CLI review format. There is
no compatibility layer for older output formats.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The suite uses a fake Codex executable and synthetic findings. It makes no API
calls and uses no Codex quota. Tests cover rollout parsing, unstructured-output
detection, target prompts, model and effort verification, disposition feedback,
round limits, failed runs, archiving, source changes, timeouts, cancellation, and
concurrent access to a run.
