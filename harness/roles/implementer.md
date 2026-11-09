# Role: implementer

You implement the feature named in your prompt, `harness/features/<slug>/`, exactly as specified. Read
`AGENTS.md`, `harness/docs/sdd.md` §2, §4, §7–8, then `spec.md`, `tasks.md`, `log.md` and `review.md`
(those that exist). A message in your prompt says what is special about this run (a failed check, a
conflict to resolve, the human's answer to a question): that comes first. Findings are in `review.md`.

## Work
- Tasks in order. For each: code, tests in the file its `## Verification` row names (the token: sdd §4),
  run the tests, tick the box.
- `log.md`: append a `## implementer` section as you go: what you did and decided, and why.

## Before you stop
- All boxes ticked and `python3 harness/harness.py check <slug> --tests` says `review` with no errors; or
  your question is in `questions.md`.
