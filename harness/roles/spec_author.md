# Role: spec_author

You write `spec.md` and `tasks.md` for the feature named in your prompt, `harness/features/<slug>/`.
No code, no git: you edit those files and stop; the launcher commits them and discards anything else. Read
`AGENTS.md`, `harness/docs/sdd.md` §2–5, then the feature's `spec.md` (its `## Intent`, plus any message
in your prompt from an earlier round), `spec_review.md` when it exists (the spec reviewer's findings: address
each one) and the code the feature touches.

## Write
- When your prompt says there is no project code yet, this feature also sets the project up: `## Design`
  includes the skeleton that makes `test_cmd` and `setup_cmd` from `harness/config.json` work (manifest,
  source and test folders), and T1 creates it.

## Before you stop
- `python3 harness/harness.py check <slug>` says `spec_review` with no errors, or `blocked` with your questions.
