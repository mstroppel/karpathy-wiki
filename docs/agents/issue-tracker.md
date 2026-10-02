# Issue tracker: GitHub

Issues and specs live in GitHub Issues for `mstroppel/karpathy-wiki`.
Use the `gh` CLI for all operations. Infer the repository from the Git
remote; `gh` does this automatically inside a clone.

## Conventions

- Create: `gh issue create --title "..." --body-file <file>`.
  Use a heredoc or body file for multi-line bodies.
- Read: `gh issue view <number> --comments`; fetch labels with
  `gh issue view <number> --json labels`.
- List: `gh issue list --state open --json number,title,body,labels,comments`.
  Apply appropriate `--label` and `--state` filters.
- Comment: `gh issue comment <number> --body "..."`.
- Apply or remove labels: `gh issue edit <number> --add-label "..."`
  or `--remove-label "..."`.
- Close: `gh issue close <number> --comment "..."`.

Follow `CONTRIBUTING.md` and `SECURITY.md`: never publish private source
material, credentials, redaction values, or other sensitive data.

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub shares one number space across issues and PRs. Resolve ambiguous
references with `gh pr view <number>`, falling back to `gh issue view <number>`.

## Skill instructions

- “Publish to the issue tracker”: create a GitHub issue.
- “Fetch the relevant ticket”: run `gh issue view <number> --comments`.

## Wayfinding operations

- Map: one issue labelled `wayfinder:map`, holding Notes, Decisions-so-far,
  and Fog.
- Child ticket: link as a GitHub sub-issue using `gh api`. If sub-issues
  are unavailable, use a task list in the map and `Part of #<map>` in the
  child. Label `wayfinder:<type>`: research, prototype, grilling, or task.
- Blocking: use native issue dependencies:
  `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`.
  Obtain the database ID with
  `gh api repos/<owner>/<repo>/issues/<number> --jq .id`.
  If unavailable, record `Blocked by: #<number>` in the child.
- Frontier: list open children in map order; exclude assigned tickets
  and tickets with open blockers.
- Claim: `gh issue edit <number> --add-assignee @me`, the session's first write.
- Resolve: comment with the result, close the child, and append a context
  pointer to the map's Decisions-so-far. Do not publish sensitive data.
