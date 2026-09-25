# Dangling GLT template handling — design

**Date:** 2026-09-25
**Issue:** [#115](https://github.com/josephgurney/linnworks-mcp/issues/115)
**Brief:** `~/twg-dev-agents/briefs/linnworks-mcp-115/brief-v1.md` (recommendation: `needs-decision`)
**Status:** design agreed, not yet implemented

---

## Why this document exists

The #115 brief was written on 23 Sep and recommended `needs-decision`, listing four
open questions. Three of them are now answered — one by a decision that landed in this
repo the day after the brief was written, two in the design session on 25 Sep. The
fourth cannot be answered by reading anything; it needs one live API call.

This spec records the answers, and the design that follows from them.

---

## What changed since the brief

### 1. Shopify Admin credentials are out of scope (answers open question 1)

Commit `6f7cd3d` ("state the Linnworks-centric scope, and re-verify the free dangling
detector so #92 can close", v1.62.3) added a scope section to CLAUDE.md:

> A design that needs a second platform's credentials to work is, by default, the wrong
> design for this repo. **Pair, don't absorb.**

and, directly addressing this work:

> ⚠️ **A corollary for dangling-listing work.** "Does this listing still exist on the
> channel?" is a *channel* fact, so no Linnworks endpoint can answer it directly — but
> Linnworks carries its own proxy for it, and the proxy is free:
> `Info.Status == "Not deleted"` on a GLT template. Reach for that before reaching for
> channel credentials.

That rules out the brief's option (a) — require #92 first and use the Shopify probe —
and promotes option (b) to house policy.

**Ordering dependency — resolved 2026-09-25.** That commit was still unmerged when this
design was written. PR #121 has since been merged (`main` at 1.63.1): the four conflicts
PR #120 created were mechanical version collisions, the scope entry was renumbered
v1.62.3 → v1.63.1 because 1.62.3 never shipped on `main`, and #92 closed on merge. The
scope section is now in `main`, so this work builds on it directly.

### 2. The detector's strength, and its honest limit

From `docs/dangling-templates-swh-shopify.md` (sweep of 9 Sep 2026) and the
re-verification in `6f7cd3d` (fresh 5,000-item sample, 15 days later):

- 141/141, then 42/42, templates reading `Status: "Not deleted"` were dangling —
  their stored `ActiveListingId` probed null on Shopify, against a live control.
- The converse does **not** hold: of 745 templates reading `"Errors while updating"`,
  only 4 are dangling. Most point at products that still exist, commonly ARCHIVED or
  DRAFT.

So the detector gives **precision, not recall**. ~4 of the census's 145 dangling
templates carry some other status, and `"Listed"` templates have never been swept.

**Consequence for the design: nothing may ever report a template as healthy.** The
vocabulary is `dangling_proven` versus `not_proven_dangling`. A tool that said
`healthy` would be wrong in roughly 3% of cases, on precisely the question where being
wrong costs a live listing.

### 3. `"Not deleted"` has one meaning, not two (a reconciliation worth recording)

The same string appears in two places that look contradictory:

- `docs/dangling-templates-swh-shopify.md` uses it as proof a **listing** is gone.
- `unpublish_channel_listing`'s issue-#36 read-back uses it as evidence a **delete
  attempt failed** — "one of each survived with Linnworks returning `Status: "Not
  deleted"`".

The dangling-templates doc states the resolution plainly:

> That status is Linnworks recording that it tried to remove a listing which was
> already gone.

One fact, two readings. A template reading `"Not deleted"` is one where a delete was
attempted and did not land, which is exactly what happens when the product is already
gone on the channel.

**This has a sharp consequence, and it is the strongest criticism of this design.** A
template reading `"Not deleted"` is one where a removal was attempted and did not land.
`tests/test_delist_readback.py` records that re-firing Delete on such a template
"returned a clean 2xx and changed nothing."

So the signal this design uses as **proof of death** is substantially the same signal as
**prior delete failure**. Gate 2 below permits deletion only on templates carrying it —
which means the tool is engineered to fire precisely on the templates most likely to
resist deletion.

This is not a strict circularity: the status describes the *listing*, not the template,
so it can predate any template-level delete attempt. But it is close enough that it must
be stated rather than discovered later, and it is why the experiment is framed in two
stages (see "What the experiment can and cannot settle").

On this tenant there is no escape from it. An alternative proof of death would need
either Shopify credentials (out of scope, D1) or the brief's unverified option (c).

### 4. The feared side effect has supporting live evidence (open question 2)

The brief's central trap — that deleting an orphan template may remove the channel-SKU
rows the *live sibling* depends on — is not merely speculative:

- CLAUDE.md's v1.50.0 note records that `unpublish_channel_listing` was deliberately
  **not** used on the Echo orphan (template 38827) "because a Delete against a template
  whose product is already gone risks removing the 12 channel-SKU rows that correctly
  serve the *surviving* 3-wheel listing."
- `unpublish_channel_listing`'s read-back comment records the mechanism live (issue
  #36, 6 Aug 2026): the channel-SKU check is a per-ITEM signal and **"the first
  successful delete empties that table for the whole item"** — observed on two items
  that each had two templates.

**What that does and does not establish.** Channel-SKU rows belong to the item, not the
template, and one delete was observed to empty them for the whole item. That is the
feared mechanism. But in the 6 Aug run *both* templates were targeted, so rows emptying
was the intended outcome; and the surviving template read `"Not deleted"`, meaning it
was itself dangling rather than live. Nobody has tested the #115 shape, where a
genuinely live sibling must survive.

The prior is bad. It is not proof.

---

## Decisions taken

| # | Decision | Rationale |
|---|---|---|
| D1 | Proof of death is `Info.Status == "Not deleted"` **only**. Every other status refuses. | The 24 Sep scope corollary. Fails in the safe direction: it refuses what it cannot prove. |
| D2 | New tools; `unpublish_channel_listing` is **not** modified. | It is 827 lines, live-proven and destructive, with a defect history (#35, #36). Refactoring it to serve an unproven feature points risk the wrong way. |
| D3 | Split into a read-only reporter and a provisional experiment tool. | Only deletion rests on an unproven premise. Separating them means the safe half is not held hostage to the experiment's result — and if the experiment fails, only the tool that failed is removed. |
| D4 | Minimal shared surface; deliberate, time-boxed duplication. | Extracting `_rows_on_store` / `_group_of_child` from `unpublish_channel_listing` means unpicking nested closures that capture local state. If the experiment fails, that refactor bought nothing. Extract *after* the experiment proves the tool is staying. |
| D5 | The experiment tool ships registered and documented, with its docs rows stating it is provisional. | Discoverable and covered by `test_docs_consistency.py`. The cost, if the experiment fails, is a removal commit. |

Superseded from the brief: criteria 4 and 5 collapse into one refusal (D1 makes "still
live" and "cannot tell" the same answer); criterion 10's threshold-of-10 staging is
dropped in favour of a signature that cannot batch; criterion 1 is strengthened from
"identical when `template_ids=None`" to "`unpublish_channel_listing` is untouched";
criterion 12's tool count becomes 98 → 100.

---

## Tool (a) — `find_dangling_glt_templates`

Read-only, and carries no unproven premise.

**Its value is packaging, not new capability — state this honestly.** The raw signal is
already reachable today: `refresh_channel_listing`'s dry-run plan rows carry
`"status": _glt_field(info, "Status")` per template (`server.py:18557`), so a dry run
already surfaces `"Not deleted"` for anyone who knows to look for it. What this tool adds
is a purpose-built name, an explicit verdict vocabulary, the channel-SKU rows and sibling
templates reported alongside, and a path that never touches the Shopify probe. That is
worth having — but it is not a capability the server lacks.

**It is also a verification tool, not a discovery tool.** Scope is per-SKU, so you must
already know which SKU to ask about. Discovery is the census sweep, whose output is
`docs/dangling-templates-swh-shopify.md`. The intended use is confirming one SKU's state
before acting on it.

```python
find_dangling_glt_templates(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    channel: str = "Shopify",
) -> dict
```

**Scope is per-SKU.** A tenant-wide sweep is the census method
(31,841 items → 11,351 templates, ~10 minutes) and belongs in a script, not in a tool
call that quietly makes 160+ API requests.

**Reads:** `_resolve_glt_target` → `GenericListings/OpenTemplatesByInventory` (templates
and `Info.Status`) → `Inventory/BatchGetInventoryItemChannelSKUs` (mapping rows). There
is no write path in this tool.

### Per-template verdict

| Verdict | Condition |
|---|---|
| `dangling_proven` | `Info.Status == "Not deleted"` |
| `not_proven_dangling` | any other status, including `Listed` and `Errors while updating` |

No third value. Nothing reports a template as healthy — see §2 above.

### Per-item output

- `sku`, `stock_item_id`, `title`, `channel`, `sub_source`, `channel_id`
- `templates[]` — `template_id`, `configurator_id`, `active_listing_id`, `status`, `verdict`
- `channel_sku_rows` — count, and each row's `channel_reference_id`, filtered to this
  `Source` + `SubSource`
- `remediation` — the GLT UI step (open the listing so the template re-points), which
  v1.50.0 already ruled is the human route
- `detector_note` — states the precision-not-recall limit on every response

### Top level

- `items[]` (the per-item blocks above), plus `unresolved[]` for SKUs that could not be
  examined, each carrying the existing `blocked_reason` vocabulary — `empty_sku`,
  `not_found`, `channel_read_failed`, `not_listed` — to match
  `unpublish_channel_listing` and `refresh_channel_listing`
- `dangling_proven_count`, `not_proven_dangling_count`, `items_examined`
- `complete: false` when any read failed, so a partial sweep is never mistaken for a
  clean one

### Required behaviours

1. Trust `TemplatesInfo`; **never `TotalEntries`**, which echoes the input count and
   would invent a template for a variation child that has none.
2. `ChannelName` is the uppercase source string (`"SHOPIFY"`).
3. `Info` fields are `{"Type","Value"}`-wrapped; unwrap via `_glt_field`.
4. **Variation shape.** On Shopify the template hangs off the parent while the child
   holds the channel-SKU rows. A child SKU is reported as *"no template of its own; the
   group's template is N on parent SKU X"* — never as "no templates", which would read
   as a clean bill of health for a SKU that was never examined.
5. A `RateLimitError` surfaces as itself and is never classified as dangling.

No write threshold; it is a read.

---

## Tool (b) — `delete_dangling_glt_template` (provisional)

Its purpose is **not** orphan cleanup at scale. It is to answer open question 2 on one
low-value SKU, with the evidence captured in the tool's own output.

```python
delete_dangling_glt_template(
    sku: str,
    template_id: int,
    sub_source: str = "SWH Shopify",
    channel: str = "Shopify",
    allow_unproven_delete: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict
```

Singular by construction — one SKU, one template id, no lists. The signature *is* the
safety limit. `WRITE_THRESHOLDS["delete_dangling_glt_template"] = 0`. **Corrected during planning:**
a threshold of 1 would never fire — `_write_guard` returns `None` when
`count <= threshold` (`server.py:643`), so a one-item list against a threshold of 1
proceeds unstaged. `0` makes every live run stage a manifest and echo `confirmed_count=1`,
which is the two-deliberate-acts behaviour this spec intended.

### Refusal gates

Evaluated in order; each returns its own `blocked_reason` and makes **no**
`ProcessTemplates` call.

| Gate | `blocked_reason` | Why |
|---|---|---|
| `template_id` is not among templates opened for this SKU on the resolved `ChannelId` | `template_not_on_item` | A raw id could belong to another SKU or another store; deleting by it bypasses both SKU and store scoping. |
| `Info.Status != "Not deleted"` | `dangling_not_proven` | D1. Covers "still live" and "cannot tell" alike. |
| The item has only this one template | `no_sibling_use_unpublish` | **A scope gate, not a safety gate** — see below. |
| Variation child whose group has live siblings | `variation_child_live_siblings` | The whole-group gate still applies — the template serves every member, so a single-template delete could end a group listing. |

`allow_unproven_delete=True` is the only bypass, and only of gate 2. The response then
carries a warning naming the listing that would end.

**On gate 3, and an unknown that could not be resolved.** This gate is about *purpose*,
not danger. With no sibling there is nothing for this tool to protect, its entire outcome
vocabulary ("siblings intact") is vacuous, and `unpublish_channel_listing` — live-proven
for exactly that shape — is the correct tool. So it refuses rather than warns.

⚠️ **How much of the population that excludes is unknown.**
`docs/dangling-templates-swh-shopify.md` records 145 dangling templates across 145
distinct SKUs — no SKU carries two *dangling* templates — but it does not record how many
of those items also hold a *live* template. The brief cites a 23 Sep sweep splitting
Group 1 (161, something live) from Group 2 (195, nothing live); **that sweep is not in
this repo and was not available when this spec was written.** If Group 2 dominates, this
tool applies to a minority of cases. That is acceptable for an experiment whose subject
*is* the sibling, but it should not be discovered later and mistaken for a defect.

### Evidence capture

Before (on dry runs too) and after (live only):

- the item's channel-SKU rows on this store, with each `channel_reference_id`
- every template on the item, with its status

Both snapshots and their diff are returned, so the answer to open question 2 lives in
the tool output rather than in someone's recollection of a session.

### Outcome classification

Inverted from `unpublish_channel_listing`, whose success means the listing ended. Here
success means the *sibling survives*.

| Outcome | Condition | Reading | Settles |
|---|---|---|---|
| `orphan_removed_siblings_intact` | target gone, siblings present, rows unchanged | The ideal. Unlocks the brief's full tool. | A **and** B |
| `orphan_removed_sibling_mapping_lost` | target gone, siblings present, rows gone or reduced | **Failure, with a loud warning.** The live listing has lost its Linnworks mapping — restore via the UI and stop. | A **and** B |
| `orphan_removed_sibling_template_lost` | target gone, and the sibling's own TEMPLATE (not just its channel-SKU rows) is gone from the after read-back | **Worse than `orphan_removed_sibling_mapping_lost`.** A missing mapping row can potentially be restored; a missing template cannot be re-opened the same way. Distinct remediation, so it earns its own value rather than folding into the mapping-lost case. | A **and** B |
| `delete_refused_by_linnworks` | target still present | The delete did not land. Per §3, the most likely result. | A only |
| `unconfirmed` | read-back failed | Prove by hand. | neither |

**Added during implementation:** the table above originally listed four outcomes.
`orphan_removed_sibling_template_lost` was added as a fifth because the after read-back
can lose a sibling's own template entry outright, not just empty its channel-SKU rows —
a different failure from `orphan_removed_sibling_mapping_lost`, with a different
remediation path, so it was given its own value rather than being folded into the
mapping-lost case above.

A `RateLimitError` at any point yields `rate_limited` with `complete: false`, and is
never reported as `template_not_on_item` or as dangling.

### What the experiment can and cannot settle

Because of the near-circularity in §3, this is **two questions in sequence**, not one:

> **A. Does `ProcessTemplates` Delete land at all on a template reading `"Not deleted"`?**
> **B. If it lands, does it take the item's channel-SKU rows — and so the live sibling's mapping — with it?**

**B is only reachable if A answers yes.** Since gate 2 fires only on templates that have
already shown a removal did not land, the most likely single result is A = no, which
leaves B untouched.

That is inconclusive about the *danger*, but it is **decisive about the decision**: if
the API route does not work on this shape, the GLT UI is the answer regardless of what
would have happened to the rows, and this tool gets removed. So a run that never reaches
B is still worth having made — it just must not be written up as evidence that deleting
orphans is safe.

**Expected endings**

| Ending | Then |
|---|---|
| A yes, B rows survive | Build the brief's full tool |
| A yes, B rows die | Remove this tool; record the UI as the answer; restore the mapping |
| A no (most likely) | Remove this tool; record that the API route does not exist for this shape |

Two of three end in removal. The docstring must say so, and must not imply a working
cleanup path.

**Choosing the SKU to maximise information.** An informative run wants a template where a
delete has *not* already failed — but on this tenant that is unreachable, because
`"Not deleted"` is the only available proof of death and it is also the failure signal.
Reaching B on the first attempt would therefore require `allow_unproven_delete=True` on a
template believed dangling by some other evidence, which on a tenant with no Shopify
credentials does not exist. **The recommendation is to accept A-first**: run it gated,
expect A = no, and treat that as the answer. Overriding the gate to chase B means firing
a delete at a template that has not been proven dead, on an item with a live sibling —
precisely the risk the whole design exists to avoid. That trade is the owner's to make,
and this spec recommends against it.

---

## Testing

House convention: a module docstring stating the incident, `patch` on `server`
internals, small `_tpl(tid, status)` builders.

**`tests/test_dangling_template_report.py`**

1. `"Not deleted"` → `dangling_proven`
2. `"Listed"` → `not_proven_dangling`; asserts the string `healthy` appears nowhere in the response
3. `"Errors while updating"` → `not_proven_dangling`, pinning the v1.50.1 correction
4. Variation child with no template of its own → reported as *template on parent*
5. `TotalEntries` echoes the input count while `TemplatesInfo` is empty → no phantom template
6. Channel-SKU rows filtered to the correct `Source` + `SubSource` only
7. `RateLimitError` surfaces; never classified as dangling

**`tests/test_dangling_template_delete.py`**

8–11. Each refusal gate, each asserting no `ProcessTemplates` call is made
12. Every non-`"Not deleted"` status refused (parametrised)
13. `allow_unproven_delete=True` bypasses gate 2; response names the listing that would end
14. `dry_run=True` by default; a live run requires `confirmed_count=1` against the threshold-0 staging gate
15. **`ProcessTemplates` receives only the named `TemplateId`; the sibling id appears in
    no write payload.** The brief's criterion 7, and the most important test here.
16–20. Each of the five outcomes, including the warnings on `orphan_removed_sibling_mapping_lost`
    and `orphan_removed_sibling_template_lost`
21. `RateLimitError` mid-run → `rate_limited`, `complete: false`

**`tests/test_delist_readback.py`** gains one regression test: `unpublish_channel_listing`'s
result on a two-template fixture is unchanged, pinning D2.

---

## Docs

Enforced by `tests/test_docs_consistency.py`, so this is mechanical:

- CLAUDE.md and README tools-table rows for both tools; the experiment tool's rows state
  plainly that it is provisional pending one live result
- README `WRITE_THRESHOLDS` table row for the deleter
- version bump in `pyproject.toml` and `server.__version__`
- tool count 98 → 100
- a CLAUDE.md version-note entry in house style recording D1, the `"Not deleted"`
  reconciliation from §3, and the evidence in §4

---

## Sequencing

One PR, two commits, `Closes #115`:

1. `feat: find_dangling_glt_templates` — read-only
2. `feat: delete_dangling_glt_template (provisional)` — the experiment

Then the brief's post-merge verification runs on one low-value Group 1 SKU (two
templates, one live, not a variation). Those steps are kept as the brief wrote them —
notably *stop if the sibling appears in the plan* — and their result decides whether the
full tool gets built or this one gets removed.

---

## Out of scope

Unchanged from the brief:

- The 195 Group 2 templates (a merchandising decision)
- Re-listing, repointing or rewriting a template's `ActiveListingId`
- Configuring Shopify Admin credentials (#92) — now settled as out of scope by design
- Adding `template_ids` to `delist_all_channel_listings` / `delist_all_shopify_listings`
- Amazon or TikTok existence probing
- A tenant-wide sweep tool (the census method stays a script)

## Open questions that remain

1. **Does `ProcessTemplates` Delete land at all on a template reading `"Not deleted"`?**
   (Question A.) Tool (b) answers this on its first run.
2. **If it lands, does it remove the channel-SKU rows the live sibling relies on?**
   (Question B.) Reachable only if A is yes, which §3 argues is the less likely branch.
3. **What fraction of dangling templates sit on an item with a live sibling?** The 23 Sep
   Group 1 / Group 2 sweep the brief cites is not in this repo. It determines how much of
   the population tool (b) can address at all. If that sweep is recovered, add it to
   `docs/` — it is cheap to re-run by the census method and would settle this in minutes.

None of the three is settled by reading. The first two need one live call; the third
needs a sweep.
