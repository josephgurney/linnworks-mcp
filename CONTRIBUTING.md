# Contributing

Thanks for your interest in improving this project. Here's how it works.

---

## Requesting a new feature

1. **Open a GitHub Issue** using the _Feature request_ template
2. Fill in the template — especially "What question do you want to ask Claude?" and "Why can't existing tools answer this?"
3. Wait for the maintainer to review it

The maintainer will add one of these labels:

| Label | Meaning |
|---|---|
| `approved` | Accepted — will be built |
| `wont-do` | Out of scope or declined — a comment will explain why |
| `needs-info` | More detail needed before a decision |

**Please don't open a pull request for a new feature until the corresponding issue has been labelled `approved`.** Unsolicited feature PRs will be closed.

---

## Reporting a bug

Open an Issue using the _Bug report_ template. Include the tool name, what you asked, and any error output. The more specific, the faster it gets fixed.

---

## Why this process?

This MCP server makes live API calls to your Linnworks account. New tools — especially anything that writes data — go through a deliberate review before being built. The issue-first process is how that review happens.

---

## Code style

If you do submit a PR (for a bug fix, or an approved feature):

- Python 3.10+, type hints throughout
- Tool docstrings are the UX — write them as if explaining the tool to a non-technical user
- Write tools must default to `dry_run=True` and include read-before-write + read-back-after logic
- No credentials, tokens, or tenant-specific data in committed code
- Run `python server.py --check-auth` before opening a PR to confirm auth still works

---

## Security

If you discover a security issue, please **do not** open a public issue. Contact the maintainer directly instead.
