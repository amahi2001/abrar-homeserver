---
name: github-pr-workflow
description: Inspect, prepare, or monitor GitHub pull requests and CI while following BuildFleet's repository and publishing rules.
---

Read the repository instructions and inspect Git status before touching a checkout. Use the repo's supported dev environment for checks, and stage only task files. Use `gh pr view`, `gh pr checks`, and `gh run view --log-failed` to verify state and diagnose failures.

Follow the user's standing commit and push rules. Create a PR when requested or needed for the requested workflow. Get explicit instruction before merging, force-pushing, changing repository visibility, or publishing packages. Keep unrelated work and local configuration out of commits. Report the PR link and the check results you actually observed.
