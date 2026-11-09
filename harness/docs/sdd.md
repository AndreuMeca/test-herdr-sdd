# Spec-driven development rules

The only rules file. `AGENTS.md` is the map, `harness/roles/*.md` give each agent its job, and the launcher
(`harness/harness.py`) enforces what a script can enforce. When a role file and this file disagree, this file
wins. Commands: `python3 harness/harness.py --help`.

## 1. Lifecycle

One feature = one slug = one folder `harness/features/<slug>/` = one branch = one worktree. The orchestrator
(the agent the human talks to, on the base checkout) starts a feature and hands it to the driver: a launcher
process that runs the roles one at a time in the feature's worktree, commits what each role wrote, sends what
is missing back, runs the gates set to `auto`, and stops when a human is needed. One writer per checkout,
always.

A feature's state is never written anywhere: the launcher reads it from the files and refs on disk, in this
order of evidence.

| state | evidence | next |
|---|---|---|
| `done` | the folder is on the base checkout: the feature has no worktree on its branch | — |
| `blocked` | `questions.md` asks something | the human answers: `drive <slug> "<answer>" --tab` |
| `spec` | no approval yet; rules 2–4 red, or the spec review of HEAD says CHANGES_REQUESTED | spec_author |
| `spec_review` | no approval yet; rules 2–4 green; no spec review of HEAD | spec_reviewer |
| `spec_ready` | no approval yet; rules 2–4 green; the spec review of HEAD says APPROVED | `approve` |
| `implementing` | approved; rules 2–5 (without the tests) find a gap, or the review of HEAD says CHANGES_REQUESTED | implementer |
| `review` | approved; rules 2–5 hold; no review of HEAD | base merged in, tests, then reviewer |
| `reviewed` | approved; rules 2–5 hold; the review of HEAD says APPROVED | `merge` |

A worktree whose branch is not its slug (detached, re-branched by hand) is an error `status` names, not a
state. Approval is the ref `refs/harness/<slug>/approved`, set by `approve`. There are two reviews, of the spec
(`spec_review.md`, a round starts at `refs/harness/<slug>/spec-review-start`) and of the code (`review.md`,
`refs/harness/<slug>/review-start`); each is "of HEAD" when the launcher started the round and no commit since
then, other than a merge commit, touched anything but its file or `questions.md`.

The gates are `approve` and `merge`; `gates` in `harness/config.json` sets each to `human` (the driver stops
and prompts the orchestrator) or `auto` (the driver runs it). Only `merge` writes to the base branch. The
launcher blocks a feature itself, with a line in `questions.md`, at the caps: `max_review_rounds` reviews of
one kind ending in CHANGES_REQUESTED (`harness/help/config.md`), or the same role running three times in a row
within one driver run without the feature moving on. `approve <slug> --undo` withdraws an approval when a
requirement must change after all: the spec was frozen meanwhile, so the spec review that stood at the approval
stands again and the feature is back at `spec_ready`; `drive <slug> "<what changes>" --tab` runs the spec
author, whose change starts a new spec review round; ticked tasks stay.

## 2. Feature files

| file | written by | format |
|---|---|---|
| `spec.md` | spec_author; frozen once approved | `# Title`, `## Intent`, `## Requirements`, `## Design`, `## Verification`, in that order. |
| `tasks.md` | spec_author writes, implementer ticks | `# Tasks`, then `- [ ] T<n>: <text> (R1, R2)` per task, ids unique, ordered by dependency, each citing at least one R. |
| `spec_review.md` | the launcher seeds it, the spec_reviewer fills it, one per round | The form and verdict rules of `review.md`, with one `- [ ]` per §5 checkpoint; a finding names the R or T id. APPROVED makes the spec `spec_ready`; CHANGES_REQUESTED sends it back to the spec_author. |
| `log.md` | implementer, append-only | `# Log`, then one `## implementer` section per run: what was done and decided, and why. |
| `review.md` | the launcher seeds it, the reviewer fills it, one per round | `# Review`; `## Findings` (empty when seeded; the reviewer adds one line per finding with the R id and `file:line`); `## Checked` (one `- [ ]` per §6 checkpoint, seeded; the reviewer ticks what it verified). Every box ticked and nothing under `## Findings` is APPROVED; anything written there is CHANGES_REQUESTED, whatever it says; anything else decides nothing and goes back to the reviewer. |
| `questions.md` | any role, or the launcher | `# Questions`, then one line per question the human must answer. Anything written after the heading blocks the feature until the human answers (`drive <slug> "<answer>" --tab`): the launcher clears the file and hands the questions and the answer to the role the state names next, so a role whose prompt carries the answer finds the file already cleared. |

`## Design` lists the files to touch, interfaces and signatures, data flow, decisions, one discarded
alternative and why, and what is out of scope. `## Verification` has one row per active R naming its test
file(s) in backticks, relative paths outside `harness/`, no folder, no glob, no `..`
(``| R1 | `tests/test_login.py::test_x` |``; anything after `::` is ignored). The launcher replaces a
review file with the empty form before each of its rounds, so a review always belongs to one round.

## 3. Requirements (EARS)

- One per line, `- R<n>: <text>`, ids from R1 and **stable**: never renumber; a dropped one stays as
  `- R<n>: withdrawn`.
- Each active R has exactly one `SHALL` and one pattern:
  `THE SYSTEM SHALL <response>.` · `WHEN <trigger>, THE SYSTEM SHALL ...` · `WHILE <state>, THE SYSTEM SHALL ...` ·
  `WHERE <feature is enabled>, THE SYSTEM SHALL ...` · `IF <condition>, THEN THE SYSTEM SHALL ...`
  `THE SYSTEM` may name the component instead (`THE API SHALL`); `WHILE`,
  `WHEN` and `WHERE` clauses may combine before it (`WHILE offline, WHEN a write arrives, THE SYSTEM SHALL ...`).
- Observable behaviour with concrete values (`returns 404`, `within 2 s`), *what* not *how*, no soft
  verbs (`should`, `may`, `try to`).
- A question the intent and the code cannot answer goes to `questions.md`, not into a requirement.

## 4. Traceability

Every active R has a test carrying the token `<slug>:R<n>` (for example `login:R3`) anywhere in a file its
`## Verification` row names (the test's name, its docstring, a comment); the launcher verifies this with
`git grep` over the whole file, so the token is the contract. It is matched whole: `login:R10`,
`user-login:R1` or `login:R1x` do not cover `login:R1`. The token proves a test for the R exists and runs
green with the rest; whether it asserts the R is the reviewer's judgement (§6, checkpoint 1).

## 5. Spec reviewer checkpoints

Recorded one by one in `## Checked` of `spec_review.md`. Before the round starts the launcher has verified the
form of the spec (rules 2–4: the sections, EARS, every R cited by a task and given a test file), so the spec
reviewer judges what a script cannot: whether this spec, built as written, gives the human what `## Intent`
asks for and nothing else. It reads the code the spec touches and writes none. A finding is a change the spec
author can make, one line naming the R or T id; what only the human can decide goes to `questions.md`; wording
and style are not findings.

1. Every active R is testable as written: concrete values and observable outcomes, one behaviour per R, no wording two readers would implement differently.
2. The requirements cover `## Intent` and no more: nothing it asks for is missing, nothing it does not ask for is specified.
3. Error paths and edge values the intent implies (failures, limits, empty or invalid inputs) each have an R with the response spelled out, or `## Design` names them as out of scope.
4. `## Design` is the simplest that satisfies the requirements and fits the existing code: real files and interfaces, the project's layout and conventions (`AGENTS.md` §1).
5. The tasks are coherent: ordered by dependency, each a step an implementer can finish and tick on its own, together doing every active R (citing it is rule 3; doing it is this).
6. `## Verification` is plausible: each row names a file where the project keeps such tests and that can assert its R.

## 6. Reviewer checkpoints

Recorded one by one in `## Checked` of `review.md`. Before the round starts the launcher has merged the base
branch in, verified every task ticked and every token present (rule 5) and run the tests green, so the reviewer
reads tests rather than runs them, and judges what a script cannot. Formatting, lint and the conventions of `AGENTS.md` §1
that a tool can check are `test_cmd`'s to enforce (`harness/help/config.md`): no checkpoint repeats a script's job.

1. Every active R's test (the one carrying its token) asserts the R's behaviour: no test that only checks "does not raise", no mocking of the filesystem when a temporary directory works.
2. No behaviour that no R describes (scope creep); no R silently reinterpreted.
3. The diff matches the tasks; nothing undocumented.
4. `log.md` explains the non-obvious decisions and matches the code.
5. Error paths and edge values named in the requirements are handled.
6. No secrets, ownerless TODOs, debug leftovers or stray files.

## 7. Git and worktree rules

- A role never runs a git command that writes (commit, merge, rebase, reset, push, branch, worktree, refs):
  it edits files and stops; a commit it makes anyway is folded into the launcher's `<slug>: <role>` commit of
  the run (rule 1).
- The launcher merges the base branch in before every review of the code and at `merge`. A conflict git marks
  in the file is committed as it is, markers included, and the implementer resolves it in the files; the
  launcher commits the resolution. A conflict git cannot mark (a binary, a file deleted on one side, a rename) aborts the merge
  and stops the driver for the human, naming the path. At `merge`, a conflict or a failed check also clears
  the review: what was reviewed is not what would land.

## 8. What the launcher verifies

| # | rule | when |
|---|---|---|
| 1 | a run changes only its role's files: spec_author `spec.md`, `tasks.md`, `questions.md`; spec_reviewer `spec_review.md`, `questions.md`; implementer the project's files (not `harness/`, nor the files that steer agents, at any depth: `AGENTS.md`, `CLAUDE.md`, `.claude/`, `.codex/`, `.mcp.json`, `GEMINI.md`, `.gemini/`, `.opencode/`, `opencode.json`) plus `tasks.md`, `log.md`, `questions.md` in its folder; reviewer `review.md`, `questions.md`. The launcher commits those and restores the rest. | after every run |
| 2 | `## Intent`, `## Requirements`, `## Design`, `## Verification` present in that order (§2), Intent and Design not empty; at least one active R, ids unique, each with exactly one `SHALL` in an EARS pattern (§3) | always |
| 3 | every task cites an existing R, task ids unique; every active R is cited by a task | always |
| 4 | every active R has one `## Verification` row naming a test file: a relative path outside `harness/`, no folder, no glob, no `..` | always |
| 5 | complete: every task ticked, `log.md` exists, no conflict markers, every active R's token in a named file (§4), `test_cmd` exits 0 | before a review, at `merge` |
| 6 | a review of HEAD, of the spec or of the code, decides a verdict (§2): all checkpoints ticked and nothing under `## Findings`, or something written there | after a spec_reviewer or reviewer run, at `approve` and `merge` |

`check [slug] [--tests|--no-tests]` prints the state and what rules 2–6 find at it (rule 5's gaps from
`implementing` on, the tests from `review` on or with `--tests`); without a slug, every feature of the current
checkout. A role run is done when the state moved on: spec_author to `spec_review` or `blocked`, spec_reviewer to
`spec_ready`, `spec` or `blocked`, implementer to `review` or `blocked`, reviewer to `reviewed`, `implementing`
or `blocked`; otherwise the driver sends what
`check` finds back to the same role, three runs in a row at most (§1), then asks the human in `questions.md`;
a run that leaves nothing to send back stops for the human at once. `check` in a worktree runs the branch's
launcher and rules with the main checkout's `config.json`; what `test_cmd` runs is the branch's.

## 9. Launcher semantics not in `--help`

- A role run is judged from the files on disk after the launcher committed them, never from the agent's
  state. Uncommitted changes found when the driver starts a step are a run the launcher did not finish
  (`max_role_minutes`, a driver that died): they are committed under the role they belong to first.
- `drive <slug> "msg"`: the message goes to the first role run, on one line (it is typed into the agent's
  input box, whatever its size). At a gate, `auto` or not, to the role that
  produced what is judged (spec → spec_author, merge → implementer); when blocked, to the role the state names
  next (§2, `questions.md`). `--tab` runs the driver in the feature's workspace and returns; when it stops, it
  prompts the pane it was started from (when an agent runs in it) with `harness: <slug> ...`. After an `auto`
  merge, `merge` itself prompts that pane with the outcome (merged at `<sha>`, or why it failed).
- Every role run is a fresh agent session in the role's tab of the feature's workspace, started with
  `config.json` as it is then; the previous one is closed first. Memory between runs is the feature's files
  and the driver's message. The launcher answers the startup dialogs its CLI is known to show (`CLIS` in
  `harness.py`) and logs each one; when the first prompt stalls with the agent idle it sends Enter once;
  anything else stops the driver for the human. The tab is closed once the run is judged; it stays when the
  driver stops because of the agent (a dialog it does not know, stopped, still working, changed nothing), so
  they can look at the screen.
- herdr is two programs that share no agents, and the launcher runs on either, nothing to configure:
  `HERDR_ENV=1` is a pane of the terminal app (`herdr`), else `HC_TERMINAL_ID` is a terminal of herdr-core, under
  the desktop app; outside both, the one installed, the terminal app when both are. What these rules call a
  workspace, tab or pane is a space, tab or terminal in the desktop app. There the launcher drives terminals with
  `hc`: labels put each one in the feature's space, whether a window is open or not, without taking the human's
  focus; the worktree goes to `~/.herdr/worktrees/<repo>/<slug>`, where the app's own worktree menu puts them; and
  `herdr-app` only names tabs and spaces while a window is open. An agent must report its status there (OSC 7501);
  one that does not is stopped at startup for the human. A feature is plain git, so the other program can `drive`
  it, in a workspace of its own; a driver is one program's, and it is the lock (§1) that keeps a feature to one.
- While a driver runs a feature, `approve` and `merge` refuse it; one `merge` runs at a time.
  `max_role_minutes`, and what to do when it stops the driver: `harness/help/config.md`.

## 10. Trust model

The launcher keeps cooperative agents on the lifecycle; it catches mistakes, it is not a security boundary.
A role agent runs as you, in your worktree, with the permissions `config.json` gives it. `full` can run any
command: move a ref, edit the main checkout, read what `copy_files` brought in (`.env`), push. `guarded` limits
what the CLI itself allows (`doctor` names the mechanism per CLI); neither stops a determined agent, and the
launcher does not notice a push.

What holds against honest mistakes: the launcher is the only committer, commits and checks come from the main
checkout, `merge` is local, and the orchestrator runs with the permissions you started it with. What `test_cmd`
runs is the branch's own files, `conftest.py` and manifests included: a reviewer reads them. With both gates
`auto`, no human reads the spec or the code before it lands on the base branch; that is a per-project choice in
`config.json`. The boundary, when you need one, is a disposable machine or container.
