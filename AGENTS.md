# AGENTS.md

<!-- The map, not a rulebook. §1 is per project; everything else points to its one source. -->

## 1. Project
<!-- Filled in by the orchestrator with the human before the first feature. -->
- What it is: <one paragraph: what this software does and for whom>
- Stack and layout: <language, framework; where code and tests live>
- Conventions: <style, naming, commit style, anything non-obvious an agent would get wrong>

Tests run with `test_cmd` in `harness/config.json`.

## 2. Two lanes
- Trivial change (typo, one-line fix, config tweak): just do it, with a test when behaviour changes.
- Feature (anything that needs requirements): through the harness in `harness/`, never outside it.

## 3. Your role
- Your prompt names a file in `harness/roles/`: that file is your job. Read it, then `harness/docs/sdd.md`.
- Otherwise you are the orchestrator, the agent the human talks to: follow `harness/roles/orchestrator.md`.
  While §1 above still holds the template's placeholders (the first feature in this repo), before your
  first `start`: run `python3 harness/harness.py doctor` (no tokens: the setup, then the checks and
  `test_cmd` on the main checkout), show the setup together with the model and permission mode you are
  running with, plus any `doctor` problem with its fix, fill in §1 with the human in the same step (same
  file), and wait for their yes (`harness/roles/orchestrator.md` says how). Afterwards `start` runs
  `doctor`'s checks itself and refuses on a problem; show the setup again only when `harness/config.json`
  changed or the human asks.
- Your own model and permissions are not in `harness/config.json`: the human chose them by starting you.
  To change them they use your CLI's own commands (Claude Code: `/model`, `/permissions`) or start a new
  session; never change them yourself.

## 4. Where things are
- Rules, lifecycle, what `check` enforces: `harness/docs/sdd.md`
- Commands: `python3 harness/harness.py --help`
- Config (agent per role, gates, commands, files copied into worktrees): `harness/config.json`, explained in `harness/help/config.md`
- Features: `harness/features/<slug>/{spec,tasks,spec_review,log,review,questions}.md`
- herdr beyond `harness.py`: terminal app (`HERDR_ENV=1`) `herdr --skill`; desktop app (`HC_TERMINAL_ID` set)
  `hc --skill` for terminals and agents, `herdr-app --skill` for the window
- The launcher's own tests, `python3 harness/selftest.py`: only for changes to `harness/`
