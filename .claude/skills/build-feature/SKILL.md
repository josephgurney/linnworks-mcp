# build-feature — issue-driven feature pipeline

---
name: build-feature
description: Fetch the next approved feature request from GitHub, build it, test it, version it, and ship it. Trigger with "/build-feature", "build the next feature", "build issue N", or "work on the approved features".
---

This skill is the single, repeatable pipeline that turns an `approved` GitHub issue
into a shipped version of the Linnworks MCP. Follow every step in order. Do not skip
the claim step, the test step, or the version/docs step.

## Label lifecycle (already set up on the repo)

```
feature-request → approved → in-progress → built   (or wont-do / needs-info)
```

Only issues labelled **`approved`** are buildable. A `feature-request` without
`approved` is a proposal — never build it, no matter how good it looks.

## Step 0 — Environment detection

Determine which environment you are in — it changes the test and ship policy:

- **Local machine** (`.env` exists in repo root, `.venv/` exists): full pipeline.
  Live tenant testing allowed. Commit to `main` and push directly.
- **Cloud session** (Claude Code web/mobile — no `.env`, no `.brain/`): build +
  pytest only. NO live testing possible. Ship as a **branch + pull request**, never
  direct to main, and say clearly in the PR body and issue comment that live
  verification is still required on the local machine.

## Step 1 — Select the issue

- If the user named an issue ("build issue 22"), use that one — but verify it
  carries the `approved` label. If it doesn't, stop and tell the user; do not build.
- Otherwise: `gh issue list --repo josephgurney/linnworks-mcp --label approved --state open`
  and take the **lowest-numbered** one. Skip any that already carry `in-progress`
  (something else is building it).
- If none: report "no approved issues waiting" and stop. List any open
  `feature-request` issues awaiting a decision so the user can approve from
  the GitHub app if they want.

## Step 2 — Claim it

```
gh issue edit N --remove-label approved --add-label in-progress
gh issue comment N --body "🔨 Build started (Claude Code, <local|cloud> session)."
```

## Step 3 — Load context (in this order)

1. `CLAUDE.md` — the version-log entries at the top ARE the house style; the
   conventions section is binding (auth header raw token, `{"request": ...}`
   wrapper for OpenOrders, read-before-write, `dry_run=True` default,
   `_write_guard`, `_check_injection`, docstrings-as-UX, surface Linnworks
   errors verbatim).
2. `.brain/HOOK_LOG.md` + `.brain/BRAIN.md` — **if present** (local only; the
   brain is private and never pushed). In a cloud session note their absence
   and continue; the CLAUDE.md version log carries most of the same knowledge.
3. `.claude/skills/linnworks-api/SKILL.md` — endpoint selection, failure
   patterns, auth model.
4. The full issue thread: `gh issue view N --comments`. Requirements in
   comments are as binding as the issue body.

## Step 4 — Pre-flight

- `git pull --rebase` so you build on the latest main.
- Note any pre-existing uncommitted changes (`git status --short`). They belong
  to the user — the feature commit must contain ONLY files this feature touched.

## Step 5 — Implement

Work in `server.py` following the loaded conventions. Rules that are always true:

- New write tools: read-before-write manifest, `dry_run=True` default,
  `confirmed_count` handshake where the house pattern uses it, `_write_guard`
  threshold, `_check_injection` on free-text fields.
- Tool docstrings are the UX — write them as carefully as the code.
- Check CLAUDE.md's broken-endpoints notes before trusting any public-schema
  endpoint. Prefer endpoint families already proven in this tenant.

## Step 6 — Test

- **Always**: `.venv/bin/python3 -m pytest tests/ -v --tb=short` (local) or the
  session's python + pytest (cloud). All tests must pass — a red suite never ships.
- Add/extend a test file for the new tool where the existing pattern does
  (see `tests/test_write_protection.py` for the write-guard pattern).
- **Live testing (local only)** — follow the established tenant policy:
  - Read paths: live-test against real data; record what was verified.
  - Write paths: live-test ONLY against isolated fixtures — `ZZZ-MCP-TEST-*`
    SKUs or a disposable test PO that you create and delete inside the run.
  - Customer-facing side effects (live listings, real orders, real customer
    data): stay **spec-based** unless the issue explicitly authorises a live
    run. Document as "spec-based, NOT yet live-run" in the changelog, exactly
    like `list_to_shopify` / `refresh_channel_listing` before it.
  - **Cloud session**: skip live testing entirely; flag it (Step 8).

## Step 7 — Version + docs

1. Bump the **minor** version in `pyproject.toml` (1.X.0 pattern).
2. Add a version-log blockquote at the TOP of CLAUDE.md's log, in the exact
   house style of the existing entries: what was added, endpoints + payload
   shapes discovered, gotchas, what was live-tested vs spec-based.
3. Update the tool count in CLAUDE.md's header line.
4. **Local only**: append a HOOK_LOG.md entry and run the rollup per
   `.brain/rollup.md` (it is gitignored — never commit it).

## Step 8 — Ship

- Stage ONLY the files the feature touched (`git add <files>` — never `git add -A`).
- Commit message in house style: `Add <capability> (closes #N)` — the `closes #N`
  auto-closes the issue on push to main.
- **Local**: push to `main`.
- **Cloud**: push a branch `feature/issue-N-<slug>` and open a PR titled the same
  as the commit; body = summary + "⚠️ pytest-only build: live verification against
  the tenant still required on the local machine before merge."

## Step 9 — Close the loop

```
gh issue edit N --remove-label in-progress --add-label built
gh issue comment N --body "<summary>"
```

The summary comment must state: new version number, tool names added/changed,
what was live-tested vs spec-based, and any follow-ups deferred (file these as
new `feature-request` issues rather than losing them).

**Local builds**: remind the user to restart Claude Desktop so the live MCP
picks up the new tools.

## Failure / blocked path

If the build cannot be completed (endpoint doesn't exist, spec ambiguity, needs
a product decision):

1. Revert or stash the incomplete feature changes — leave the tree as you found it.
2. `gh issue edit N --remove-label in-progress --add-label needs-info` (if a
   question blocks it) or re-add `approved` (if it's simply unfinished).
3. Comment on the issue with exactly what's blocking and what was learned —
   discovered endpoint behaviour is valuable even when the build fails.
