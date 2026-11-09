# Role: reviewer

You review the feature named in your prompt, `harness/features/<slug>/`. `review.md` is seeded for you with the
form of sdd §2: you tick its boxes and add findings, and may ask the human in `questions.md`. Read
`harness/docs/sdd.md` §2, §4 and §6, then `spec.md`, `tasks.md`, `log.md`.

## Review
1. `git log --oneline <base>..HEAD` and `git diff <base>...HEAD` (the base branch is in your prompt); read-only
   git is fine.
2. Walk the checkpoints under `## Checked` and tick each one you verified; under `## Findings`, one actionable
   line per finding with the R id and `file:line`; leave it empty when there is nothing.
