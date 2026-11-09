# harness/config.json

The one file you edit to set the harness up for your project; this page explains every key, so that the
README, `config.json` and this page are all you need. JSON cannot hold comments, so the explanations live
here. The shipped `config.json` is the safe default: Claude Code writes the spec and the code, Codex reviews
both, each CLI's default model, `guarded` permissions, both gates `human` ("Fully automatic" below is the
other end). Unknown keys such as `_help` are ignored. The launcher reads the main checkout's copy on every command,
validates it and names the first problem; a feature branch cannot change what it is checked with, and a role
run started earlier keeps the flags it was started with. `python3 harness/harness.py doctor` prints the setup
in a few lines, then checks the tools, the config and `test_cmd` on the main checkout.

## Setting up a new repo

What to change, top to bottom; run `python3 harness/harness.py doctor` after editing: it names what is wrong
and the fix.

1. `base_branch`: only when your default branch is not `main`.
2. `test_cmd` and `setup_cmd` for your stack (the table; what `test_cmd` decides: "test_cmd" below). A repo with
   no code yet keeps them: neither runs until the project has code, and the first feature sets the project up so
   that they pass.
3. `copy_files`: the gitignored files a worktree needs, such as `.env`.
4. `agents`: the CLI, model, effort and permissions of each role ("agents.<role>" below). `doctor --models`
   checks each model id with one tiny prompt.
5. `gates`: leave both `human` until you trust the roles with your project ("Fully automatic" below).

| stack | `setup_cmd` | `test_cmd` |
|---|---|---|
| Python with uv (shipped) | `uv sync` | `uv run pytest -q`; with lint and format: `uv run ruff check && uv run ruff format --check && uv run pytest -q` |
| Node | `npm ci` | `npm run lint && npm test` |
| Go | `""` | `test -z "$(gofmt -l .)" && go vet ./... && go test ./...` |
| Rust | `""` | `cargo fmt --check && cargo clippy -- -D warnings && cargo test` |

## Keys

| key | required | values | what it does |
|---|---|---|---|
| `base_branch` | yes | branch name | `start` forks from it; `merge` merges back into it. |
| `test_cmd` | yes | shell command | The one check the launcher runs on a feature's code: the tests, and lint and format with them (next section). Exit 0 = green. |
| `setup_cmd` | no | shell command or `""` | Run once by `start` in the new worktree, after `copy_files`: `uv sync`, `npm ci`. A worktree starts with only what git tracks, so it has no `.venv` or `node_modules` until this runs. Skipped while the repo has no project code (the first feature sets it up). When it fails, `start` says so and goes on: fix it in the worktree. |
| `copy_files` | no | paths or globs relative to the repo root, default `[]` | Gitignored files `start` copies from the main checkout into the worktree: `[".env"]`. Tracked paths are skipped; copies, not links. Not `.venv` or `node_modules`: `setup_cmd` rebuilds them. `doctor` notes a pattern that matches nothing in the main checkout. |
| `max_review_rounds` | no | integer, default `3`, `0` = no limit | After this many `CHANGES_REQUESTED` reviews of one kind (spec or code) the launcher asks the human in `questions.md` (and again after every further one). |
| `max_role_minutes` | no | integer, default `180`, `0` = no limit | When one role run (prompt to settled) takes longer, the driver stops and prompts the orchestrator ("still working after N min"). The agent keeps running: wait until it finishes (its screen), then `drive <slug> --tab` commits what it left and judges it; a `drive` while it still works starts a fresh agent in its place. |
| `gates.approve`, `gates.merge` | no | `human` or `auto`; a missing key means `human` | `human`: the driver stops and prompts the orchestrator. `auto`: the driver runs the command itself. The shipped file sets both to `human`. |
| `agents.<role>` | yes, all four | object, below | Which agent CLI and model run `spec_author`, `spec_reviewer`, `implementer` and `reviewer`. |

## test_cmd

`test_cmd` is the only check the launcher runs on a feature's code, so it decides what "green" means for your
project.

- When it runs: by the driver before every review round of the code, with the base branch merged in; by
  `merge`; by `check` from state `review` on (or with `--tests`); by `doctor`, on the main checkout, so that a
  red baseline is not blamed on the first feature's implementer. Not at `start`.
- How it runs: as one shell line at the worktree root, with no terminal and stdin closed, so it must not ask
  anything. Exit 0 is green, anything else is red; the launcher keeps the last 1500 characters of its output.
- What red does: before a review, the feature goes back to the implementer with the output; at `merge`, the
  merge is refused and the review is cleared (the feature goes through implementer, tests and reviewer again);
  `doctor` reports a problem.
- Lint and format belong in it. The reviewers' checkpoints are judgement calls (whether a requirement is
  testable, whether a test asserts it, scope creep, the log, error paths, leftovers) and they are told not to
  check what a script can; the launcher runs nothing but `test_cmd`. So what is not in this command nobody
  checks: add the linter, the formatter in check mode, a type checker if you have one, and any other
  `AGENTS.md` §1 convention a tool can verify.
- What it runs on is the branch's own files, test setup and manifests included; the command itself always comes
  from the main checkout's `config.json`.
- Its tools must exist in the worktree: `setup_cmd` installs them there (a worktree does not share the main
  checkout's `.venv` or `node_modules`).

## `agents.<role>`

Four roles, `spec_author`, `spec_reviewer`, `implementer` and `reviewer`, one entry each; the agent you talk
to (the orchestrator) is not in this file: you chose its model and permissions when you started it. You never
write a CLI's own flag for effort or permissions: the file has its own words, the launcher translates them per
CLI (tables below), and `doctor` prints one `flags` line per role with the exact flags the CLI gets.

| key | required | values | what it does |
|---|---|---|---|
| `harness` | yes | the agent CLI, by its herdr agent kind: `claude`, `codex`, `gemini`, `opencode`, ... (`herdr agent start --help` lists them) | The CLI herdr starts for the role, installed and logged in. In the desktop app it is the command itself, and it must report its status (OSC 7501: Claude Code and Codex do). It picks the model provider; `opencode` takes the provider in the model string. |
| `model` | no | the provider's model id, or `""` | Passed as `--model`; empty = the CLI's default. Kept as the provider names it, so you always see what you pay for; `doctor --models` checks it (Claude Code and Codex). |
| `effort` | no | `low`, `medium`, `high`, `extra`, `max`, or `""` | How hard the model thinks, by meaning, translated per CLI (table below). Empty = the CLI's default. |
| `permissions` | for `claude`, `codex`, `pi` | `guarded`, `full` | What the agent may do without asking (table below). Roles run unattended, so there is no level that asks. For any other CLI leave it out and put the CLI's own flags in `args`. |
| `args` | no | list of strings | Any other flag, verbatim; `doctor`'s `flags` line shows the result. |

**effort**, `low` < `medium` < `high` < `extra` < `max`, becomes:

| `effort` | `claude` | `codex` | `pi` |
|---|---|---|---|
| `low`, `medium`, `high` | `--effort <same>` | `-c model_reasoning_effort=<same>` | `--thinking <same>` |
| `extra` | `--effort xhigh` | `-c model_reasoning_effort=xhigh` | `--thinking xhigh` |
| `max` | `--effort max` | `-c model_reasoning_effort=xhigh` (its highest) | `--thinking max` |
| `""` | no flag: the CLI's default | same | same |

**permissions** is by what the agent does: `guarded` never asks and the CLI blocks risky actions by its own
mechanism; `full` never asks and has no limits. A level a CLI does not have is a config error that names the one
it does have.

| `permissions` | `claude` | `codex` | `pi` |
|---|---|---|---|
| `guarded` | `--permission-mode auto`: auto mode's classifier blocks risky actions | `--sandbox workspace-write --ask-for-approval never`: writes only inside the worktree, which is enough since the launcher commits for it | not available: pi never asks and has no limits |
| `full` | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | no flag |

What that means for you: a role agent runs as your user in the worktree. `full` can run any command: push, edit
the main checkout, read what `copy_files` brought in (`.env`), move a ref. `guarded` limits what the CLI itself
allows. Neither stops a determined agent: the launcher is the only committer and `merge` is local, which holds
against honest mistakes, not against an adversary. When you need a boundary, it is a disposable machine or
container. The whole trust model: `harness/docs/sdd.md` §10.

A Codex `guarded` role cannot run `uv` (its cache is outside the sandbox). The launcher itself needs only
`python3`, and it runs `test_cmd` for the reviewer; a Codex `guarded` implementer that runs a `uv` test command
itself needs `"args": ["--add-dir", "<your uv cache dir>"]`, or another test runner.

For a CLI not in the tables (`gemini`, `opencode`, ...), `effort` is not translated and `permissions` may be
left out: `doctor` says so, and the CLI's own flags go in `args` (Gemini CLI: `--yolo`). To translate for
another CLI, add one `Cli(...)` entry to `CLIS` in `harness/harness.py` (its flags, the `doctor --models` probe,
the startup dialogs it may show) and a column to the tables above.

herdr only passes model and flags through; every role run is a fresh agent with the flags as they are then. The
startup dialogs the launcher answers are `startup_dialogs` in `CLIS` (seen live with Claude Code 2.1.317 and
Codex 0.164.0; both CLIs record trust for the repo root, so a trust dialog shows once per repo, on the first
role run in it, and no flag suppresses Claude Code's). Any other dialog stops the driver and the orchestrator
shows you the screen; Claude Code's one-time auto-mode notice on a fresh machine is one such.

Ready-to-copy entries:
```json
{ "harness": "claude", "model": "claude-sonnet-5-5", "effort": "high",  "permissions": "full" }
{ "harness": "claude", "model": "claude-opus-5-5",   "effort": "extra", "permissions": "guarded" }
{ "harness": "codex",  "model": "",                  "effort": "extra", "permissions": "guarded" }
```

## Fully automatic
No gate stops for you: the driver approves the spec and merges by itself, and the roles never ask (a question
a role puts in `questions.md`, or a cap, still stops the feature for you). No human reads the spec or the code
before it lands on the base branch, and `full` roles can do anything your user can ("permissions" above). Use
model ids your account can run (`doctor --models` tells). Replace `gates` and `agents` with:
```json
"gates": { "approve": "auto", "merge": "auto" },
"agents": {
  "spec_author":   { "harness": "claude", "model": "claude-opus-5-5",  "effort": "extra", "permissions": "full" },
  "spec_reviewer": { "harness": "codex",  "model": "gpt-6-astra",      "effort": "extra", "permissions": "guarded" },
  "implementer":   { "harness": "claude", "model": "claude-sonnet-5-5", "effort": "high", "permissions": "full" },
  "reviewer":      { "harness": "codex",  "model": "gpt-6-astra",      "effort": "extra", "permissions": "guarded" }
}
```

## herdr's sidebar

The launcher reports each feature's state (`spec ready · needs you: approve`, `implementer · working`,
...). In the desktop app the space's name carries it, nothing to set up, whenever a window is open. In the
terminal app it is the Space token `$harness` of the workspace, and herdr shows custom tokens only when the layout
names them: add once to `~/.config/herdr/config.toml` and run `herdr server reload-config` (`doctor` reminds you):
```toml
[ui.sidebar.spaces]
rows = [["state_icon", "workspace"], ["branch", "git_status"], ["$harness"]]
```
