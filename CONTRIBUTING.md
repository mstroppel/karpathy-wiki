# Contributing

Issues and pull requests are welcome. Contributors without repository write
access should create pull requests from a fork. Repository collaborators may use
branches in this repository.

Use Conventional Commits and keep changes focused. Before opening a pull request,
run the validation commands documented in `README.md`. Include behavior changes,
security implications, migration impact, and test evidence in the pull request.
Pull requests from branches in this repository publish GHCR preview images with
the tag `pr-<number>` for integration testing. Preview tags are updated whenever
the pull request branch changes and are not stable release artifacts.

Do not include real source documents, wiki data, session exports, credentials,
tokens, redaction values, hostnames, or private URLs in issues, fixtures, logs, or
commits.

## Code review

Reviews in this repository are grounded in
`.github/skills/code-review/SKILL.md`, the project agent skill that GitHub
Copilot code review loads for changes here. That agent skill is the confirmed
review integration; MCP servers are not configured because a review needs no
context beyond this repository and its GitHub issues.

To request a review for a pull request, use **Ask Copilot to review** in the
pull request, or from the command line:

```bash
gh pr edit <number> --add-reviewer @copilot
```

With a GitHub CLI older than 2.88.0, request the reviewer through the API:

```bash
gh api --method POST repos/<owner>/<repo>/pulls/<number>/requested_reviewers \
  -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
```

If the request does not show up under `requested_reviewers`, it was silently
dropped: Copilot code review is not enabled for the repository (Settings →
Copilot). Enable it, then request the review again.

The review covers repository standards and the originating issue's acceptance
criteria and reports actionable findings with `file/line` references plus
missing validation evidence. Treat its findings as input to, not a replacement
for, human judgement, especially for pull requests from forks. Keep the
private material listed above out of the review context, pull request
descriptions, and review comments.
