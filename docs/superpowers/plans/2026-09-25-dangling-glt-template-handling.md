# Dangling GLT Template Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only tool that reports which GLT templates are provably dangling, and a provisional, singular, heavily-gated tool that deletes one of them so the open question about channel-SKU rows can be settled on a single SKU.

**Architecture:** Two new `@mcp.tool()` functions in `server.py`, plus three small module-level private helpers they share. `unpublish_channel_listing` is not modified — a regression test pins that. Proof that a listing is dead is `Info.Status == "Not deleted"` and nothing else; every other status refuses. The deleter is singular by construction (one SKU, one template id, no lists) and forces a staged manifest on every live run.

**Tech Stack:** Python 3.14, FastMCP (`@mcp.tool()`), pytest with `unittest.mock.patch`. Pure stdlib plus the existing Linnworks HTTP helpers. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md`

## Global Constraints

- **Proof of death is `Info.Status == "Not deleted"` only.** Every other status, including `"Listed"` and `"Errors while updating"`, refuses. (Spec D1)
- **Nothing may report a template as healthy.** The verdict vocabulary is exactly `dangling_proven` and `not_proven_dangling` — no third value, and the string `healthy` must not appear in any response. (Spec §2)
- **`unpublish_channel_listing` is not modified.** (Spec D2)
- **No Shopify Admin API call, in either tool.** Shopify is out of scope by design; `_shopify_listings_exist` / `_glt_listing_existence` are not used here. (Spec D1, CLAUDE.md SCOPE section)
- **Trust `TemplatesInfo`, never `TotalEntries`** — `TotalEntries` echoes the input count. (Spec, tool (a) required behaviour 1)
- **`ChannelName` is the uppercase source string** (`"SHOPIFY"`), taken from `_resolve_glt_channel`, never hardcoded. (required behaviour 2)
- **`Info` fields are `{"Type","Value"}`-wrapped** — always unwrap via `_glt_field`. (required behaviour 3)
- **`RateLimitError` does not subclass `RuntimeError`** (v1.40.0, issue #37) — it must be caught *before* `RuntimeError` at every call site, in the `create_order` order.
- **`dry_run: bool = True`** is the default on the write tool.
- All four version strings (`pyproject.toml`, `server.__version__`, README badge, CLAUDE.md header) must agree — `tests/test_docs_consistency.py` enforces it.
- Tool count goes 98 → 100.

## Review Focus

Five input classes the spec implies but whose tests it does not specify. Each line's test is added to the task that owns the code, in that task's own step style.

1. **A SKU with no GLT templates at all on the channel** (never listed via GLT, not a variation child). Must report zero templates explicitly and must not read as a clean bill of health. → Task 2.
2. **`template_id` supplied as a string** (`"38539"`) rather than an int. MCP callers routinely stringify numbers; a naive `==` against `t.get("Id")` would return `template_not_on_item` for a template that is right there, and the caller would reasonably conclude the id was wrong. → Task 3.
3. **`Info.Status` with case or whitespace variance** (`"not deleted"`, `" Not deleted "`). Exact-match is the safe default, but it must be a *decided* behaviour with a test, not an accident of using `==`. → Task 1.
4. **An item whose templates are all dangling** — gate 3 passes because there is more than one template, but there is no *live* sibling to protect. The response must not claim a live sibling survived when none was live. → Task 4.
5. **`RateLimitError` during the AFTER read-back on a live run**, when the delete has already been sent. Must report that the write was already fired and may have succeeded — never as unchanged or failed. This is the `set_order_status` precedent from issue #88. → Task 4.

---

## Deviation from the spec, decided during planning

The spec says `WRITE_THRESHOLDS["delete_dangling_glt_template"] = 1`. **That would never fire.** `_write_guard` returns `None` when `count <= threshold` (`server.py:643`), so a one-item list against a threshold of 1 proceeds unstaged.

**This plan uses `0`**, which makes every live run stage a manifest and require `confirmed_count=1` echoed back. For a destructive experiment against a live listing, two deliberate acts (`dry_run=False` *and* the echo) is the intent the spec described. Task 6 records the deviation in the spec file.

**Two smaller reconciliations with the spec, both deliberate:**

- **Commit count.** The spec's sequencing says "one PR, two commits". This plan keeps one PR and `Closes #115`, but splits into **six** commits so each carries its own test cycle and can be rejected independently — which is what the task-granularity rule requires. The two *features* still land in the order the spec gives.
- **D4 turned out cheaper than feared.** The spec expected to duplicate `unpublish_channel_listing`'s variation whole-group gate. In practice the subtle part of that gate — `_group_of_child` — is a memoising closure, and memoisation exists solely to amortise one `SearchVariationGroups` sweep across *many* SKUs. A singular tool has exactly one SKU, so Task 3's gate 4 calls the module-level `_resolve_variation` directly and does the liveness check inline in ~15 lines. Nothing is extracted from the 827-line function, and the duplicated surface is far smaller than D4 anticipated.

---

## File Structure

| File | Responsibility |
|---|---|
| `server.py` (modify) | Three private helpers + two `@mcp.tool()` functions, added in the GLT section after `unpublish_channel_listing` |
| `tests/test_dangling_template_report.py` (create) | Tool (a): verdicts, variation shape, `TotalEntries` trap, rows filtering, rate limits |
| `tests/test_dangling_template_delete.py` (create) | Tool (b): four gates, write-payload isolation, four outcomes, rate limits |
| `tests/test_delist_readback.py` (modify) | One regression test pinning `unpublish_channel_listing` unchanged |
| `CLAUDE.md`, `README.md`, `pyproject.toml` (modify) | Tools tables, threshold table, version, tool count, release note |

`server.py` is a 22k-line single module. That is the established pattern here and this plan follows it rather than unilaterally restructuring.

---

## Task 1: Verdict helper and template reader

**Files:**
- Modify: `server.py` — add after `_fetch_channel_skus_for_ids` (ends ~`server.py:16505`)
- Test: `tests/test_dangling_template_report.py` (create)

**Interfaces:**
- Consumes: `_glt_field(info, key)`, `_ZERO_GUID`, `call_linnworks`, `RateLimitError`
- Produces:
  - `_DANGLING_STATUS: str`
  - `_dangling_verdict(status: str | None) -> str` → `"dangling_proven"` | `"not_proven_dangling"`
  - `_open_item_templates(ch: dict, channel_id: int, stock_item_id: str) -> list[dict]` → raw `TemplatesInfo` list
  - `_template_row(t: dict) -> dict` → `{template_id, configurator_id, active_listing_id, status, verdict}`

- [ ] **Step 1: Write the failing tests**

```python
"""
Tests for the dangling-GLT-template reporter (issue #115).

Proof that a listing is dead is `Info.Status == "Not deleted"` and nothing
else — see docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md.

That status is Linnworks recording that it tried to remove a listing which was
already gone (docs/dangling-templates-swh-shopify.md). It is a PRECISE signal,
not a complete one: 141/141 and 42/42 "Not deleted" templates were dangling in
two independent sweeps, but ~4 of the census's 145 dangling templates carry some
other status, and "Listed" templates have never been swept.

So there is deliberately no `healthy` verdict. The vocabulary is
`dangling_proven` vs `not_proven_dangling`, and the second one means
"unproven", never "fine".
"""
import pytest

import server


@pytest.mark.parametrize("status", ["Not deleted"])
def test_exact_status_is_the_only_proof_of_death(status):
    assert server._dangling_verdict(status) == "dangling_proven"


@pytest.mark.parametrize("status", [
    "Listed",
    "Errors while updating",
    "not deleted",      # case variance is NOT accepted (Review Focus 3)
    "",
    None,
    123,
])
def test_everything_else_is_unproven_not_healthy(status):
    assert server._dangling_verdict(status) == "not_proven_dangling"


def test_surrounding_whitespace_is_tolerated_but_case_is_not():
    # Whitespace is a transport artefact; case is a different string from the
    # API and must not be guessed at. Decided behaviour, not an accident of ==.
    assert server._dangling_verdict("  Not deleted  ") == "dangling_proven"
    assert server._dangling_verdict("NOT DELETED") == "not_proven_dangling"


def test_template_row_unwraps_the_type_value_envelope():
    t = {
        "Id": 38539,
        "ConfiguratorId": 81,
        "Info": {
            "ActiveListingId": {"Type": "Value", "Value": "9277472506102"},
            "Status": {"Type": "Value", "Value": "Not deleted"},
        },
    }
    row = server._template_row(t)
    assert row == {
        "template_id": 38539,
        "configurator_id": 81,
        "active_listing_id": "9277472506102",
        "status": "Not deleted",
        "verdict": "dangling_proven",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_report.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute '_dangling_verdict'`

- [ ] **Step 3: Write the minimal implementation**

Add to `server.py`, immediately after `_fetch_channel_skus_for_ids`:

```python
# ---------- Dangling GLT templates (issue #115) ----------
#
# A template is DANGLING when its stored ActiveListingId points at a listing
# that no longer exists on the channel. "Does this listing still exist?" is a
# CHANNEL fact, so no Linnworks endpoint answers it directly — but Linnworks
# carries a free proxy for it, and this repo is Linnworks-centric by design
# (see the SCOPE section in CLAUDE.md): Info.Status == "Not deleted", which is
# Linnworks recording that it tried to remove a listing that was already gone.
#
# PRECISION, NOT RECALL. 141/141 (9 Sep 2026 census) and 42/42 (24 Sep 2026
# re-verification, fresh 5,000-item sample against a live control) "Not deleted"
# templates were dangling. But ~4 of the census's 145 dangling templates carry
# some OTHER status, and "Listed" templates have never been swept. So this
# detector can prove a template IS dangling and can never prove one is fine —
# which is why there is no `healthy` verdict anywhere below.

_DANGLING_STATUS = "Not deleted"


def _dangling_verdict(status) -> str:
    """Proof-of-death verdict for one GLT template status.

    Surrounding whitespace is tolerated (a transport artefact); case is NOT
    (a different string from the API, which must not be guessed at).
    """
    if isinstance(status, str) and status.strip() == _DANGLING_STATUS:
        return "dangling_proven"
    return "not_proven_dangling"


def _template_row(t: dict) -> dict:
    """Flatten one raw TemplatesInfo entry into our reporting shape."""
    info = t.get("Info") if isinstance(t.get("Info"), dict) else {}
    status = _glt_field(info, "Status")
    return {
        "template_id":       t.get("Id"),
        "configurator_id":   t.get("ConfiguratorId"),
        "active_listing_id": _glt_field(info, "ActiveListingId"),
        "status":            status,
        "verdict":           _dangling_verdict(status),
    }


def _open_item_templates(ch: dict, channel_id: int, stock_item_id: str) -> list[dict]:
    """Open one item's GLT templates on one channel. Read-only.

    ⚠️ Returns `TemplatesInfo` ONLY. `TotalEntries` echoes the input count and
    will claim a template exists for a variation child that has none — reading
    it would invent templates. Confirmed trap, see the 24 Sep 2026 commit notes.
    """
    resp = call_linnworks(
        "GenericListings/OpenTemplatesByInventory",
        {"request": {
            "ChannelType": ch["channel_type"],
            "ChannelName": ch["channel_name"],
            "Parameters": {
                "SelectedRegions": [],
                "Token": _ZERO_GUID,
                "InventoryItemIds": [stock_item_id],
                "ChannelId": channel_id,
            },
            "PaginationParameters": {"PageNumber": 1, "EntriesPerPage": 20},
        }},
    )
    return (resp.get("TemplatesInfo") if isinstance(resp, dict) else None) or []
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_report.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_dangling_template_report.py
git commit -m "feat: dangling-template verdict helper and template reader (#115)"
```

---

## Task 2: `find_dangling_glt_templates`

**Files:**
- Modify: `server.py` — add after the Task 1 helpers
- Test: `tests/test_dangling_template_report.py` (extend)

**Interfaces:**
- Consumes: `_dangling_verdict`, `_template_row`, `_open_item_templates` (Task 1); `_resolve_glt_target`, `_norm_conf_name`, `_format_channel_sku_row`, `_fetch_channel_skus_for_ids`, `_resolve_variation`, `_check_injection`, `call_linnworks`, `RateLimitError`
- Produces: `find_dangling_glt_templates(skus, sub_source="SWH Shopify", channel="Shopify") -> dict` with keys `items`, `unresolved`, `rate_limited`, `dangling_proven_count`, `not_proven_dangling_count`, `items_examined`, `complete`, `detector_note`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dangling_template_report.py`:

```python
from unittest.mock import patch

SID = "aaaaaaaa-0000-0000-0000-000000000001"
PSID = "aaaaaaaa-0000-0000-0000-00000000000p"
STORE = "SWH Shopify"
CHANNEL_ID = 18


def _target(*a, **k):
    return {"ok": True, "channel_id": CHANNEL_ID, "resolution": "exact",
            "available_sub_sources": [STORE],
            "channel": {"channel_type": "Shopify", "channel_name": "SHOPIFY",
                        "source": "SHOPIFY"}}


def _tpl(tid, status="Listed", listing="listing-x"):
    return {"Id": tid, "ConfiguratorId": 81,
            "Info": {"ActiveListingId": {"Value": listing},
                     "Status": {"Value": status}}}


def _row(ref="9276972859638:1:1", source="SHOPIFY", sub=STORE):
    return {"Source": source, "SubSource": sub, "SKU": "SKU-A",
            "ChannelReferenceId": ref, "UpdateStatus": "Confirmed",
            "ListedQuantity": 3, "MaxListedQuantity": 0, "LastUpdate": None,
            "IgnoreSync": False, "IsMultiLocation": False,
            "ChannelSKURowId": "row-1"}


def test_reports_dangling_and_unproven_siblings_with_rows():
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates",
                      lambda ch, cid, sid: [_tpl(38539, "Not deleted"), _tpl(54649, "Listed")]), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()]}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["dangling_proven_count"] == 1
    assert out["not_proven_dangling_count"] == 1
    assert out["complete"] is True
    item = out["items"][0]
    assert {t["template_id"]: t["verdict"] for t in item["templates"]} == {
        38539: "dangling_proven", 54649: "not_proven_dangling"}
    assert item["channel_sku_rows"]["count"] == 1
    assert item["channel_sku_rows"]["channel_reference_ids"] == ["9276972859638:1:1"]
    assert "healthy" not in repr(out).lower()


def test_rows_from_another_store_are_not_counted():
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", lambda ch, cid, sid: [_tpl(1, "Not deleted")]), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row(sub="Other Store"),
                                                 _row(source="EBAY", sub="EBAY0")]}):
        out = server.find_dangling_glt_templates(["SKU-A"])
    assert out["items"][0]["channel_sku_rows"]["count"] == 0


def test_variation_child_reports_the_parents_template_not_no_templates():
    """A Shopify variation child holds rows but no template of its own (#26).
    Reporting "no templates" would read as a clean bill of health for a SKU
    that was never examined."""
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(77000, "Not deleted")]

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID,
                                        "group_name": "G", "siblings": []}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    item = out["items"][0]
    assert item["template_source"] == "variation_parent"
    assert item["parent_sku"] == "PARENT-A"
    assert item["templates"][0]["template_id"] == 77000
    assert item["no_templates"] is False


def test_item_with_no_templates_says_so_explicitly():
    """Review Focus 1 — never listed via GLT, and not a variation child."""
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", lambda ep, p=None: {"StockItemId": SID, "ItemTitle": "T"}), \
         patch.object(server, "_open_item_templates", lambda ch, cid, sid: []), \
         patch.object(server, "_fetch_channel_skus_for_ids", lambda ids: {SID.lower(): []}), \
         patch.object(server, "_resolve_variation", lambda sku, sid: {"role": "standalone"}):
        out = server.find_dangling_glt_templates(["SKU-A"])

    item = out["items"][0]
    assert item["no_templates"] is True
    assert item["templates"] == []
    assert item["template_source"] == "none"
    assert "no GLT template" in item["note"]


def test_rate_limit_is_never_reported_as_dangling():
    def _boom(ep, p=None):
        raise server.RateLimitError("429 throttled")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _boom):
        out = server.find_dangling_glt_templates(["SKU-A"])

    assert out["rate_limited"] and out["rate_limited"][0]["sku"] == "SKU-A"
    assert out["items"] == []
    assert out["dangling_proven_count"] == 0
    assert out["complete"] is False


def test_unknown_sku_lands_in_unresolved():
    def _missing(ep, p=None):
        raise RuntimeError("no such item")

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _missing):
        out = server.find_dangling_glt_templates(["NOPE"])

    assert out["unresolved"][0]["blocked_reason"] == "not_found"
    assert out["complete"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_report.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'find_dangling_glt_templates'`

- [ ] **Step 3: Write the minimal implementation**

```python
@mcp.tool()
def find_dangling_glt_templates(
    skus: list[str],
    sub_source: str = "SWH Shopify",
    channel: str = "Shopify",
) -> dict:
    """
    Report which of a SKU's GLT templates are PROVABLY dangling — pointing at a
    listing that no longer exists on the channel. Read-only; writes nothing.

    A dangling template is invisible to `get_channel_listings`: the channel-SKU
    mapping can look perfectly healthy while the template's stored
    ActiveListingId points at a deleted product. It breaks pushes in a nasty way
    (Linnworks batches listing ids into one Shopify `nodes(ids:)` call and throws
    on the null slot, failing every healthy template batched with it — issue #52).

    ⚠️  THIS CAN PROVE A TEMPLATE IS DANGLING. IT CAN NEVER PROVE ONE IS FINE.
    Proof of death is `Info.Status == "Not deleted"`, which is Linnworks
    recording that it tried to remove a listing already gone. That signal is
    precise (141/141 and 42/42 across two sweeps) but not complete (~4 of 145
    dangling templates carry another status; "Listed" templates have never been
    swept). So the verdicts are `dangling_proven` and `not_proven_dangling` —
    the second means UNPROVEN, not healthy. There is no `healthy` verdict.

    This is a VERIFICATION tool, not a discovery tool: scope is per-SKU, so you
    must already know which SKU to ask about. Discovery is the catalogue sweep
    whose output is `docs/dangling-templates-swh-shopify.md`.

    Remediation for a dangling template is a HUMAN step in the Linnworks GLT UI
    (open the listing so the template re-points at the surviving product). See
    `delete_dangling_glt_template` for the provisional, experimental API route
    and why it may not work.

    Args:
        skus:       SKUs to examine.
        sub_source: Store/account name, e.g. "SWH Shopify".
        channel:    "Shopify" (default), "Amazon", "TikTok", Magento, Walmart.

    Returns:
        items[] (per SKU: templates with verdicts, channel-SKU rows, sibling
        summary, remediation), unresolved[], rate_limited[], the two counts,
        items_examined, complete, and detector_note.
    """
    target = _resolve_glt_target(channel, sub_source)
    if not target.get("ok"):
        return {"success": False, "error": target.get("error"),
                "available_sub_sources": target.get("available_sub_sources")}

    ch = target["channel"]
    channel_id = target["channel_id"]
    channel_source = ch["source"]

    def _rows_on_store(rows) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    items: list[dict] = []
    unresolved: list[dict] = []
    rate_limited: list[dict] = []

    for raw in skus:
        sku = (raw or "").strip()
        if not sku:
            unresolved.append({"sku": raw, "blocked_reason": "empty_sku", "error": "empty SKU"})
            continue
        _check_injection("skus", sku)
        try:
            item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": f"not found: {exc}"})
            continue

        sid = item.get("StockItemId")
        if not sid:
            unresolved.append({"sku": sku, "blocked_reason": "not_found",
                               "error": "item found but StockItemId was missing"})
            continue

        try:
            raw_templates = _open_item_templates(ch, channel_id, sid)
            rows = _rows_on_store(_fetch_channel_skus_for_ids([sid]).get(sid.lower(), []))
        except RateLimitError as exc:
            rate_limited.append({"sku": sku, "error": str(exc)})
            continue
        except RuntimeError as exc:
            unresolved.append({"sku": sku, "blocked_reason": "channel_read_failed",
                               "error": str(exc)})
            continue

        template_source = "item"
        parent_sku = None
        note = None

        # A Shopify variation CHILD holds channel-SKU rows but no template of its
        # own — the template hangs off the PARENT and serves every variant (#26).
        # Reporting "no templates" here would read as a clean bill of health for
        # a SKU that was never actually examined.
        if not raw_templates:
            try:
                rel = _resolve_variation(sku, sid)
            except RuntimeError:
                rel = {}
            if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
                parent_sku = rel.get("parent_sku")
                try:
                    raw_templates = _open_item_templates(
                        ch, channel_id, rel["parent_stock_item_id"])
                except RateLimitError as exc:
                    rate_limited.append({"sku": sku, "error": str(exc)})
                    continue
                except RuntimeError:
                    raw_templates = []
                template_source = "variation_parent"
                note = (f"variation child — the group's template(s) hang off parent "
                        f"'{parent_sku}' and serve every member")

        rows_out = [_format_channel_sku_row(r) for r in rows]
        template_rows = [_template_row(t) for t in raw_templates]
        if not template_rows:
            template_source = "none"
            note = note or (
                f"no GLT template on {ch['channel_type']} / {sub_source} for this SKU — "
                "nothing to report either way; this is NOT a statement that the SKU is healthy")

        items.append({
            "sku": sku,
            "stock_item_id": sid,
            "title": item.get("ItemTitle"),
            "channel": ch["channel_type"],
            "sub_source": sub_source,
            "channel_id": channel_id,
            "template_source": template_source,
            "parent_sku": parent_sku,
            "no_templates": not template_rows,
            "templates": template_rows,
            "dangling_proven": [t["template_id"] for t in template_rows
                                if t["verdict"] == "dangling_proven"],
            "channel_sku_rows": {
                "count": len(rows_out),
                "channel_reference_ids": [r["channel_reference_id"] for r in rows_out],
                "rows": rows_out,
            },
            "remediation": (
                "Rebuild or remove the template in the Linnworks GLT UI — opening the "
                "listing re-points the template at the surviving product. This is the "
                "human step v1.50.0 recommended; the API route is provisional, see "
                "delete_dangling_glt_template."
            ),
            "note": note,
        })

    proven = sum(len(i["dangling_proven"]) for i in items)
    total = sum(len(i["templates"]) for i in items)
    return {
        "success": True,
        "channel": ch["channel_type"],
        "sub_source": sub_source,
        "sub_source_resolution": target.get("resolution"),
        "items": items,
        "unresolved": unresolved,
        "rate_limited": rate_limited,
        "items_examined": len(items),
        "dangling_proven_count": proven,
        "not_proven_dangling_count": total - proven,
        "complete": not unresolved and not rate_limited,
        "detector_note": (
            "PRECISION, NOT RECALL. `Not deleted` proves a template is dangling "
            "(141/141 and 42/42 across two sweeps). It does NOT prove the others are "
            "fine — ~4 of 145 known dangling templates carry another status, and "
            "`Listed` templates have never been swept. `not_proven_dangling` means "
            "UNPROVEN, never healthy."
        ),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_report.py -v`
Expected: PASS (13 passed)

- [ ] **Step 5: Document the new tool in the same commit**

`tests/test_docs_consistency.py` asserts the tool count in three places and that every
registered tool appears in BOTH docs. Registering a tool without documenting it leaves
the suite red, so the docs move with the code.

1. `CLAUDE.md:3` — `**Current version: 1.63.1** — 99 tools.`
2. `CLAUDE.md` Tools section — the line reading `98 tools.` becomes `99 tools.`
3. `README.md:4` — `![Tools](https://img.shields.io/badge/tools-99-blue)`
4. Add this row to the tools table in **both** `CLAUDE.md` and `README.md`:

```markdown
| `find_dangling_glt_templates(skus, sub_source="SWH Shopify", channel="Shopify")` | `GenericListings/GetConfiguratorsInfoPaged` + `GenericListings/OpenTemplatesByInventory` + `Inventory/BatchGetInventoryItemChannelSKUs` (all reads) | — | **Read-only.** Reports which of a SKU's GLT templates are PROVABLY dangling — pointing at a listing that no longer exists on the channel, which is invisible to `get_channel_listings` and breaks pushes (#52). Proof of death is `Info.Status == "Not deleted"` and nothing else, per the SCOPE corollary: no Shopify credentials are used or needed. ⚠️ **PRECISION, NOT RECALL — it can prove a template IS dangling and can never prove one is fine.** Verdicts are `dangling_proven` / `not_proven_dangling`; there is deliberately no `healthy` verdict, because ~4 of the census's 145 dangling templates carry another status and `Listed` templates have never been swept. A VERIFICATION tool, not a discovery tool: per-SKU scope, so discovery remains the catalogue sweep (`docs/dangling-templates-swh-shopify.md`). Variation-aware — a Shopify child holds rows but no template of its own, so it reports the parent's template rather than "no templates". Trusts `TemplatesInfo`, never `TotalEntries`. |
```

- [ ] **Step 6: Run the docs suite to verify it stayed green**

Run: `.venv/bin/python -m pytest tests/test_docs_consistency.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_dangling_template_report.py CLAUDE.md README.md
git commit -m "feat: find_dangling_glt_templates, read-only (#115)"
```

---

## Task 3: `delete_dangling_glt_template` — gates and dry run

Everything up to the write. No `ProcessTemplates` call exists in this task.

**Files:**
- Modify: `server.py` — after `find_dangling_glt_templates`; `WRITE_THRESHOLDS` (`server.py:560`)
- Test: `tests/test_dangling_template_delete.py` (create)

**Interfaces:**
- Consumes: everything from Tasks 1–2, plus `_write_guard`
- Produces: `delete_dangling_glt_template(sku, template_id, sub_source="SWH Shopify", channel="Shopify", allow_unproven_delete=False, confirmed_count=None, dry_run=True) -> dict`. Refusals return `{"success": False, "blocked_reason": <code>, ...}` with `blocked_reason` one of `template_not_on_item`, `dangling_not_proven`, `no_sibling_use_unpublish`, `variation_child_live_siblings`.

- [ ] **Step 1: Write the failing tests**

```python
"""
Tests for the PROVISIONAL targeted dangling-template delete (issue #115).

This tool exists to answer ONE question on ONE SKU, and two of its three
plausible outcomes end with its own removal. See
docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md.

The danger it guards against: channel-SKU rows belong to the ITEM, not the
template. `unpublish_channel_listing`'s issue-#36 read-back records that "the
first successful delete empties that table for the whole item", and CLAUDE.md's
v1.50.0 note records that this tool's predecessor was deliberately NOT used on
the Echo orphan for exactly that reason. So deleting an orphan template may take
the LIVE sibling's mapping with it — the listing stays up but loses stock and
price sync, and nothing flags it.

Near-circularity, stated plainly: the proof of death (`Not deleted`) is also the
signal of a PRIOR delete failure. This tool is therefore engineered to fire on
templates that may well refuse to delete.
"""
from unittest.mock import patch

import pytest

import server

SID = "aaaaaaaa-0000-0000-0000-000000000001"
PSID = "aaaaaaaa-0000-0000-0000-00000000000p"
STORE = "SWH Shopify"
CHANNEL_ID = 18
ORPHAN = 38539
LIVE = 54649


def _target(*a, **k):
    return {"ok": True, "channel_id": CHANNEL_ID, "resolution": "exact",
            "available_sub_sources": [STORE],
            "channel": {"channel_type": "Shopify", "channel_name": "SHOPIFY",
                        "source": "SHOPIFY"}}


def _tpl(tid, status="Listed", listing="listing-x"):
    return {"Id": tid, "ConfiguratorId": 81,
            "Info": {"ActiveListingId": {"Value": listing},
                     "Status": {"Value": status}}}


def _row(ref="9276972859638:1:1"):
    return {"Source": "SHOPIFY", "SubSource": STORE, "SKU": "SKU-A",
            "ChannelReferenceId": ref, "UpdateStatus": "Confirmed",
            "ListedQuantity": 3, "MaxListedQuantity": 0, "LastUpdate": None,
            "IgnoreSync": False, "IsMultiLocation": False,
            "ChannelSKURowId": "row-1"}


def _item(ep, p=None):
    return {"StockItemId": SID, "ItemTitle": "T"}


def _both_templates(ch, cid, sid):
    return [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]


def _run(**kw):
    """Call the tool with the happy-path fixtures, overriding as needed."""
    opts = {"templates": _both_templates, "rows": lambda ids: {SID.lower(): [_row()]}}
    opts.update(kw.pop("_fixtures", {}))
    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _item), \
         patch.object(server, "_open_item_templates", opts["templates"]), \
         patch.object(server, "_fetch_channel_skus_for_ids", opts["rows"]):
        return server.delete_dangling_glt_template(
            kw.pop("sku", "SKU-A"), kw.pop("template_id", ORPHAN), **kw)


def test_dry_run_is_the_default_and_plans_only_the_named_template():
    out = _run()
    assert out["dry_run"] is True
    assert out["plan"]["template_id"] == ORPHAN
    assert out["plan"]["siblings_untouched"] == [
        {"template_id": LIVE, "configurator_id": 81, "active_listing_id": "listing-x",
         "status": "Listed", "verdict": "not_proven_dangling"}]


def test_template_id_as_a_string_still_resolves():
    """Review Focus 2 — MCP callers routinely stringify numbers. Returning
    `template_not_on_item` for a template that is right there would send the
    caller hunting for a wrong id."""
    out = _run(template_id="38539")
    assert out.get("blocked_reason") != "template_not_on_item"
    assert out["plan"]["template_id"] == ORPHAN


def test_template_not_on_this_item_is_refused():
    out = _run(template_id=99999)
    assert out["blocked_reason"] == "template_not_on_item"
    assert out["success"] is False


@pytest.mark.parametrize("status", ["Listed", "Errors while updating", "", None])
def test_only_not_deleted_may_be_deleted(status):
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [
        _tpl(ORPHAN, status), _tpl(LIVE, "Listed")]})
    assert out["blocked_reason"] == "dangling_not_proven"


def test_single_template_item_is_redirected_to_unpublish():
    out = _run(_fixtures={"templates": lambda ch, cid, sid: [_tpl(ORPHAN, "Not deleted")]})
    assert out["blocked_reason"] == "no_sibling_use_unpublish"
    assert "unpublish_channel_listing" in out["error"]


def test_variation_child_with_live_siblings_is_blocked():
    def _open(ch, cid, sid):
        return [] if sid == SID else [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")]

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _item), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()], "sib-sid": [_row()]}), \
         patch.object(server, "_resolve_variation",
                      lambda sku, sid: {"role": "child", "parent_sku": "PARENT-A",
                                        "parent_stock_item_id": PSID, "group_name": "G",
                                        "siblings": [{"sku": "SIB-1",
                                                      "stock_item_id": "sib-sid"}]}):
        out = server.delete_dangling_glt_template("SKU-A", ORPHAN)

    assert out["blocked_reason"] == "variation_child_live_siblings"
    assert out["live_siblings"] == ["SIB-1"]


def test_override_permits_an_unproven_delete_and_warns():
    out = _run(template_id=LIVE, allow_unproven_delete=True)
    assert out.get("blocked_reason") is None
    assert "listing-x" in out["warning"]
    assert "NOT proven dead" in out["warning"]


def test_live_run_stages_a_manifest_before_it_will_fire():
    out = _run(dry_run=False)
    assert out["staged"] is True
    assert out["success"] is False
    assert out["confirmed_count"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_delete.py -v`
Expected: FAIL with `AttributeError: module 'server' has no attribute 'delete_dangling_glt_template'`

- [ ] **Step 3: Add the threshold**

In `WRITE_THRESHOLDS` (`server.py:560`), after the `unpublish_channel_listing` line:

```python
    "delete_dangling_glt_template":    0,   # PROVISIONAL — 0 forces a staged manifest on EVERY live run
```

- [ ] **Step 4: Write the minimal implementation**

```python
@mcp.tool()
def delete_dangling_glt_template(
    sku: str,
    template_id: int,
    sub_source: str = "SWH Shopify",
    channel: str = "Shopify",
    allow_unproven_delete: bool = False,
    confirmed_count: int | None = None,
    dry_run: bool = True,
) -> dict:
    """
    ⚠️  PROVISIONAL AND EXPERIMENTAL. Delete ONE named GLT template while leaving
    its siblings alone — for retiring an orphan whose listing is already gone,
    without touching the live listing beside it.

    ⚠️  THIS TOOL EXISTS TO ANSWER A QUESTION, NOT TO CLEAN UP AT SCALE, AND TWO
    OF ITS THREE PLAUSIBLE OUTCOMES END WITH IT BEING REMOVED FROM THIS SERVER.

    Two questions, in sequence:
      A. Does ProcessTemplates Delete land AT ALL on a template reading
         `Not deleted`? Unknown. That status means Linnworks already tried to
         remove the listing and it did not happen, so such a template has
         arguably already resisted deletion once — and re-firing Delete on one
         "returned a clean 2xx and changed nothing" (issue #36, 6 Aug 2026).
      B. If it lands, does it take the ITEM's channel-SKU rows with it, and so
         the LIVE sibling's Linnworks mapping? Channel-SKU rows belong to the
         item, not the template, and the #36 read-back records that "the first
         successful delete empties that table for the whole item". If that
         happens the sibling listing stays up but loses stock and price sync,
         and NOTHING flags it.

    B is reachable only if A is yes, which is the less likely branch. Run this
    on ONE low-value SKU, read the result, and record it — do not batch it.

    Gates (each refuses and sends no write):
      template_not_on_item          — id is not a template of this SKU on this store
      dangling_not_proven           — status is not exactly "Not deleted"
      no_sibling_use_unpublish      — only one template; use unpublish_channel_listing
      variation_child_live_siblings — the group's template serves live members

    Args:
        sku:                   ONE SKU. No lists — the signature is the safety limit.
        template_id:           ONE template id, which must belong to that SKU here.
        sub_source:            Store/account, e.g. "SWH Shopify".
        channel:               "Shopify" (default), "Amazon", "TikTok", …
        allow_unproven_delete: Bypass the dangling gate. Fires a delete at a
                               template NOT proven dead, on an item with a live
                               sibling — the exact risk this tool guards. Recorded
                               in the response as a warning.
        confirmed_count:       Echo back 1 to execute a live run.
        dry_run:               True (default) plans and proves, and writes nothing.

    Returns:
        plan (target + siblings_untouched), before/after snapshots, outcome,
        and on a refusal: success=False with blocked_reason.
    """
    _check_injection("sku", sku or "")
    target = _resolve_glt_target(channel, sub_source)
    if not target.get("ok"):
        return {"success": False, "error": target.get("error"),
                "available_sub_sources": target.get("available_sub_sources")}

    ch = target["channel"]
    channel_id = target["channel_id"]
    channel_source = ch["source"]

    def _rows_on_store(rows) -> list:
        return [
            r for r in (rows if isinstance(rows, list) else [])
            if _norm_conf_name(r.get("Source")) == _norm_conf_name(channel_source)
            and _norm_conf_name(r.get("SubSource")) == _norm_conf_name(sub_source)
        ]

    base = {"success": False, "sku": sku, "template_id": template_id,
            "channel": ch["channel_type"], "sub_source": sub_source,
            "dry_run": dry_run}

    try:
        item = call_linnworks("Inventory/GetInventoryItem", {"sku": sku})
    except RateLimitError as exc:
        return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {**base, "blocked_reason": "not_found", "error": f"not found: {exc}"}

    sid = item.get("StockItemId")
    if not sid:
        return {**base, "blocked_reason": "not_found",
                "error": "item found but StockItemId was missing"}

    # ── Open the item's templates; fall back to the variation parent ───────────
    template_sid, via_parent, group = sid, False, None
    try:
        raw_templates = _open_item_templates(ch, channel_id, sid)
        if not raw_templates:
            try:
                rel = _resolve_variation(sku, sid)
            except RuntimeError:
                rel = {}
            if rel.get("role") == "child" and rel.get("parent_stock_item_id"):
                group, via_parent = rel, True
                template_sid = rel["parent_stock_item_id"]
                raw_templates = _open_item_templates(ch, channel_id, template_sid)
    except RateLimitError as exc:
        return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
    except RuntimeError as exc:
        return {**base, "blocked_reason": "channel_read_failed", "error": str(exc)}

    rows = [_template_row(t) for t in raw_templates]

    # ── Gate 1: the id must be a template of THIS sku on THIS store ────────────
    # int(): MCP callers routinely stringify numbers, and refusing a template
    # that is right there would send the caller hunting for a wrong id.
    try:
        wanted = int(template_id)
    except (TypeError, ValueError):
        wanted = None
    target_row = next((r for r in rows if r["template_id"] == wanted), None)
    if target_row is None:
        return {**base, "blocked_reason": "template_not_on_item",
                "templates_on_item": [r["template_id"] for r in rows],
                "error": (f"template {template_id} is not among this SKU's templates on "
                          f"{ch['channel_type']} / {sub_source}. A template id from another "
                          "SKU or store is never deleted by this tool.")}

    siblings = [r for r in rows if r["template_id"] != wanted]

    # ── Gate 2: proof of death ────────────────────────────────────────────────
    warning = None
    if target_row["verdict"] != "dangling_proven":
        if not allow_unproven_delete:
            return {**base, "blocked_reason": "dangling_not_proven",
                    "status": target_row["status"],
                    "error": (f"template {wanted} reads status {target_row['status']!r}, not "
                              f"{_DANGLING_STATUS!r}. Only a template PROVEN dangling may be "
                              "deleted. Pass allow_unproven_delete=True to override — that "
                              "fires a delete at a listing that may still be live.")}
        warning = (f"OVERRIDE: template {wanted} is NOT proven dead (status "
                   f"{target_row['status']!r}). If listing {target_row['active_listing_id']} "
                   "is still live on the channel, this will end it.")

    # ── Gate 3: scope, not safety — nothing to protect means wrong tool ───────
    if not siblings:
        return {**base, "blocked_reason": "no_sibling_use_unpublish",
                "error": ("this item has only one template, so there is no sibling for this "
                          "tool to protect and its whole purpose does not apply. Use "
                          "unpublish_channel_listing, which is live-proven for that shape.")}

    # ── Gate 4: the variation whole-group gate (issue #35) ────────────────────
    if via_parent and group:
        members = [{"sku": group.get("parent_sku"), "stock_item_id": group.get("parent_stock_item_id")}]
        members += [{"sku": s.get("sku"), "stock_item_id": s.get("stock_item_id")}
                    for s in (group.get("siblings") or []) if s.get("sku")]
        outside = [m for m in members
                   if (m["sku"] or "").strip().lower() != (sku or "").strip().lower()]
        try:
            sib_map = _fetch_channel_skus_for_ids(
                [m["stock_item_id"] for m in outside if m.get("stock_item_id")]) if outside else {}
        except RateLimitError as exc:
            return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}
        live_outside = [m["sku"] for m in outside
                        if _rows_on_store(sib_map.get((m.get("stock_item_id") or "").lower(), []))]
        if live_outside:
            return {**base, "blocked_reason": "variation_child_live_siblings",
                    "parent_sku": group.get("parent_sku"),
                    "group_name": group.get("group_name"),
                    "live_siblings": live_outside,
                    "error": ("this template hangs off the variation parent and serves every "
                              f"member; {len(live_outside)} other member(s) are still live on "
                              "this store, so deleting it would end their listing too.")}

    # ── Before snapshot (captured on dry runs too — it is the evidence) ───────
    try:
        before_rows = [_format_channel_sku_row(r) for r in
                       _rows_on_store(_fetch_channel_skus_for_ids([sid]).get(sid.lower(), []))]
    except RateLimitError as exc:
        return {**base, "blocked_reason": "rate_limited", "complete": False, "error": str(exc)}

    before = {
        "channel_sku_row_count": len(before_rows),
        "channel_reference_ids": [r["channel_reference_id"] for r in before_rows],
        "templates": {r["template_id"]: r["status"] for r in rows},
    }
    plan = {
        "template_id": wanted,
        "active_listing_id": target_row["active_listing_id"],
        "status": target_row["status"],
        "action": "Delete",
        "via_variation_parent": via_parent,
        "siblings_untouched": siblings,
    }
    out = {**base, "success": True, "template_stock_item_id": template_sid,
           "plan": plan, "before": before, "warning": warning}

    if dry_run:
        out["message"] = (
            f"DRY RUN — nothing sent. Would delete template {wanted} and leave "
            f"{len(siblings)} sibling template(s) in place. Re-run with dry_run=False "
            "and confirmed_count=1 to execute.")
        return out

    guard = _write_guard("delete_dangling_glt_template", [plan], confirmed_count, dry_run)
    if guard:
        return {**out, **guard, "plan": plan}

    return out  # live execution lands in Task 4
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_delete.py -v`
Expected: PASS (11 passed)

- [ ] **Step 6: Document the new tool and its threshold in the same commit**

This task registers tool #100 **and** adds a `WRITE_THRESHOLDS` key.
`tests/test_docs_consistency.py` asserts both: every registered tool must appear in both
docs, and every threshold key must have a matching table row in both docs. So both move
with the code.

1. `CLAUDE.md:3` — `**Current version: 1.63.1** — 100 tools.`
2. `CLAUDE.md` Tools section — `99 tools.` becomes `100 tools.`
3. `README.md:4` — `![Tools](https://img.shields.io/badge/tools-100-blue)`
4. Add this row to the threshold table in **both** `CLAUDE.md` and `README.md`. The row
   format the test parses is `` | `name` | N | `` — the number must be exactly `0`:

```markdown
| `delete_dangling_glt_template` | 0 |
```

5. Add this row to the tools table in **both** `CLAUDE.md` and `README.md`:

```markdown
| `delete_dangling_glt_template(sku, template_id, sub_source="SWH Shopify", channel="Shopify", allow_unproven_delete=False, confirmed_count, dry_run=True)` | `GenericListings/OpenTemplatesByInventory` (read) → `GenericListings/ProcessTemplates` Action=`Delete` (write) → read-back | 0 | ⚠️ **PROVISIONAL AND EXPERIMENTAL — this tool exists to answer a question on ONE SKU, and two of its three plausible outcomes end with it being REMOVED from this server.** Deletes one named GLT template while leaving its siblings alone. Singular by construction: one SKU, one template id, no lists. **Two questions in sequence: (A) does `ProcessTemplates` Delete land at all on a template reading `Not deleted`** — unproven, and that status means Linnworks already tried and did not succeed, so re-firing one "returned a clean 2xx and changed nothing" (#36); **(B) if it lands, does it take the ITEM's channel-SKU rows and so the LIVE sibling's mapping with it** — the #36 read-back records that "the first successful delete empties that table for the whole item", and v1.50.0 declined to use `unpublish_channel_listing` on the Echo orphan for exactly this reason. B is reachable only if A is yes. Four gates, each refusing with no write sent: `template_not_on_item`, `dangling_not_proven`, `no_sibling_use_unpublish` (scope, not safety — use `unpublish_channel_listing`), `variation_child_live_siblings`. Captures channel-SKU rows before and after; outcomes are `orphan_removed_siblings_intact`, `orphan_removed_sibling_mapping_lost` (failure, loud warning — restore via the UI), `delete_refused_by_linnworks`, `unconfirmed`. A threshold of **0** means every live run stages a manifest and must echo `confirmed_count=1`. **Do not batch it. Run it once, on one low-value SKU, and record the result.** |
```

- [ ] **Step 7: Run the docs suite to verify it stayed green**

Run: `.venv/bin/python -m pytest tests/test_docs_consistency.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_dangling_template_delete.py CLAUDE.md README.md
git commit -m "feat: delete_dangling_glt_template gates and dry run (#115)"
```

---

## Task 4: Live execution, evidence capture and outcome classification

**Files:**
- Modify: `server.py` — replace the `return out  # live execution lands in Task 4` line
- Test: `tests/test_dangling_template_delete.py` (extend)

**Interfaces:**
- Consumes: everything from Task 3
- Produces: on a live run, `out["outcome"]` ∈ `{orphan_removed_siblings_intact, orphan_removed_sibling_mapping_lost, delete_refused_by_linnworks, unconfirmed}`, plus `out["after"]` mirroring `out["before"]`, and `out["settles"]` ∈ `{"A and B", "A only", "neither"}`

- [ ] **Step 1: Write the failing tests**

```python
def _live(after_templates, after_rows, delete_raises=None, readback_raises=None):
    """Drive one live run. Returns (result, write_payloads)."""
    payloads = []
    state = {"phase": "before"}

    def _open(ch, cid, sid):
        if state["phase"] == "before":
            return _both_templates(ch, cid, sid)
        if readback_raises:
            raise readback_raises
        return after_templates

    def _rows(ids):
        return {SID.lower(): [_row()] if state["phase"] == "before" else after_rows}

    def _call(ep, p=None):
        if ep == "GenericListings/ProcessTemplates":
            payloads.append(p)
            state["phase"] = "after"
            if delete_raises:
                raise delete_raises
            return {}
        return _item(ep, p)

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _call), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids", _rows):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)
    return res, payloads


def test_the_sibling_template_id_never_appears_in_a_write_payload():
    """The single most important test here. If the sibling id reaches
    ProcessTemplates, a live listing ends."""
    _, payloads = _live([_tpl(LIVE, "Listed")], [_row()])
    assert len(payloads) == 1
    requests = payloads[0]["request"]["TemplateRequests"]
    assert requests == [{"TemplateId": ORPHAN, "Action": "Delete"}]
    assert str(LIVE) not in repr(payloads)


def test_ideal_outcome_target_gone_siblings_present_rows_intact():
    res, _ = _live([_tpl(LIVE, "Listed")], [_row()])
    assert res["outcome"] == "orphan_removed_siblings_intact"
    assert res["settles"] == "A and B"
    assert res["after"]["channel_sku_row_count"] == 1


def test_rows_lost_is_a_failure_with_a_loud_warning():
    res, _ = _live([_tpl(LIVE, "Listed")], [])
    assert res["outcome"] == "orphan_removed_sibling_mapping_lost"
    assert res["success"] is False
    assert res["settles"] == "A and B"
    assert "lost its Linnworks mapping" in res["warning"]


def test_target_surviving_is_delete_refused_and_settles_only_A():
    res, _ = _live([_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Listed")], [_row()])
    assert res["outcome"] == "delete_refused_by_linnworks"
    assert res["settles"] == "A only"


def test_all_siblings_dangling_does_not_claim_a_live_sibling_survived():
    """Review Focus 4 — gate 3 passes on template count, but a dangling sibling
    is not a live listing, so nothing may imply one was protected."""
    payloads = []
    state = {"phase": "before"}

    def _open(ch, cid, sid):
        if state["phase"] == "before":
            return [_tpl(ORPHAN, "Not deleted"), _tpl(LIVE, "Not deleted")]
        return [_tpl(LIVE, "Not deleted")]

    def _call(ep, p=None):
        if ep == "GenericListings/ProcessTemplates":
            payloads.append(p)
            state["phase"] = "after"
            return {}
        return _item(ep, p)

    with patch.object(server, "_resolve_glt_target", _target), \
         patch.object(server, "call_linnworks", _call), \
         patch.object(server, "_open_item_templates", _open), \
         patch.object(server, "_fetch_channel_skus_for_ids",
                      lambda ids: {SID.lower(): [_row()]}):
        res = server.delete_dangling_glt_template(
            "SKU-A", ORPHAN, dry_run=False, confirmed_count=1)

    assert res["live_siblings_present"] is False
    assert "no sibling on this item was proven live" in res["note"]


def test_rate_limit_during_readback_says_the_write_already_fired():
    """Review Focus 5 — the delete has been sent. Reporting this as unchanged
    or failed would invite a re-run of a write that may have succeeded."""
    res, payloads = _live([], [], readback_raises=server.RateLimitError("429"))
    assert len(payloads) == 1
    assert res["outcome"] == "unconfirmed"
    assert res["settles"] == "neither"
    assert res["complete"] is False
    assert "already sent" in res["warning"].lower()


def test_a_failed_delete_call_is_reported_not_raised():
    res, _ = _live([], [], delete_raises=RuntimeError("500 boom"))
    assert res["outcome"] == "unconfirmed"
    assert "500 boom" in res["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_delete.py -v -k live or outcome or sibling_template_id`
Expected: FAIL — `KeyError: 'outcome'`, because Task 3 returns before executing

- [ ] **Step 3: Write the minimal implementation**

Replace `return out  # live execution lands in Task 4` with:

```python
    # ── Live execution: ONE template id, and only the one that was gated ──────
    out["dry_run"] = False
    try:
        call_linnworks(
            "GenericListings/ProcessTemplates",
            {"request": {
                "ChannelType": ch["channel_type"],
                "ChannelName": ch["channel_name"],
                "TemplateRequests": [{"TemplateId": wanted, "Action": "Delete"}],
                "ClientContext": {"Activity": "delete_dangling_glt_template",
                                  "Source": "linnworks-mcp"},
            }},
        )
        out["processed"] = True
    except (RateLimitError, RuntimeError) as exc:
        return {**out, "success": False, "processed": False, "outcome": "unconfirmed",
                "settles": "neither", "complete": False,
                "error": f"ProcessTemplates (Delete) failed: {exc}",
                "warning": ("the delete may or may not have been sent — re-read the item's "
                            "templates before trying again.")}

    # ── Read-back: the whole point of this tool ──────────────────────────────
    try:
        after_rows_raw = _rows_on_store(
            _fetch_channel_skus_for_ids([sid]).get(sid.lower(), []))
        after_templates = [_template_row(t) for t in
                           _open_item_templates(ch, channel_id, template_sid)]
    except (RateLimitError, RuntimeError) as exc:
        # The write HAS been sent. Saying "unchanged" or "failed" here would
        # invite a re-run of a delete that may well have succeeded (the #88
        # precedent in set_order_status).
        return {**out, "success": False, "outcome": "unconfirmed", "settles": "neither",
                "complete": False, "error": str(exc),
                "warning": ("THE DELETE WAS ALREADY SENT and may have succeeded — the "
                            "read-back could not complete. Do NOT re-run on the assumption "
                            "it failed. Re-open this item's templates and read its "
                            "channel-SKU rows by hand before doing anything else.")}

    after_rows = [_format_channel_sku_row(r) for r in after_rows_raw]
    out["after"] = {
        "channel_sku_row_count": len(after_rows),
        "channel_reference_ids": [r["channel_reference_id"] for r in after_rows],
        "templates": {r["template_id"]: r["status"] for r in after_templates},
    }

    target_gone = wanted not in out["after"]["templates"]
    siblings_present = [r["template_id"] for r in siblings
                        if r["template_id"] in out["after"]["templates"]]
    # An item that had NO rows to begin with cannot have lost any — scoring that
    # as `mapping_lost` would raise a false alarm on the one signal that must
    # stay trustworthy.
    before_count = before["channel_sku_row_count"]
    rows_kept = before_count == 0 or len(after_rows) >= before_count
    live_siblings_present = any(r["verdict"] == "not_proven_dangling" for r in siblings)
    out["live_siblings_present"] = live_siblings_present
    out["siblings_still_present"] = siblings_present

    if not target_gone:
        out.update({
            "success": False, "outcome": "delete_refused_by_linnworks", "settles": "A only",
            "message": (
                f"Template {wanted} is STILL PRESENT, reading "
                f"{out['after']['templates'].get(wanted)!r}. The delete did not land. This "
                "answers question A (does Delete work on a dangling template) with NO, and "
                "leaves question B (does it take the sibling's channel-SKU rows) untouched. "
                "The GLT UI is the remediation route."),
        })
    elif not rows_kept:
        out.update({
            "success": False, "outcome": "orphan_removed_sibling_mapping_lost",
            "settles": "A and B",
            "warning": (
                f"⛔ THE LIVE LISTING HAS lost its Linnworks mapping. Template {wanted} was "
                f"deleted and the item's channel-SKU rows went from "
                f"{before_count} to {len(after_rows)}. The sibling listing "
                "is probably still up on the channel but no longer syncs stock or price, so "
                "it can oversell with nothing flagging it. Restore the mapping via the "
                "Linnworks UI and do NOT run this tool on any other SKU."),
        })
    else:
        out.update({
            "success": True, "outcome": "orphan_removed_siblings_intact", "settles": "A and B",
            "message": (
                f"Template {wanted} deleted; sibling template(s) {siblings_present} still "
                f"present and the item's {len(after_rows)} channel-SKU row(s) intact. Verify "
                "the sibling's product page on the channel, and check again after the next "
                "stock sync that quantity still updates."),
        })

    if not live_siblings_present:
        out["note"] = (
            "no sibling on this item was proven live — every sibling template read as "
            "dangling too, so this run does not demonstrate that a LIVE listing survives.")
    return out
```

- [ ] **Step 4: Run the full file to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_dangling_template_delete.py -v`
Expected: PASS (18 passed)

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_dangling_template_delete.py
git commit -m "feat: targeted delete read-back and outcome classification (#115)"
```

---

## Task 5: Pin `unpublish_channel_listing` unchanged

Spec D2 says the 827-line destructive tool is not touched. That is a claim about the whole change, so it gets its own test rather than a code review note.

**Files:**
- Modify: `tests/test_delist_readback.py`

**Interfaces:**
- Consumes: the existing fixtures in that file (`_configurators`, `_tpl`)
- Produces: nothing new

- [ ] **Step 1: Write the test**

Append to `tests/test_delist_readback.py`:

```python
def test_unpublish_signature_is_unchanged_by_issue_115():
    """#115 added two new tools rather than a `template_ids` parameter here,
    precisely so this 827-line live-proven destructive path stayed still.
    A new parameter appearing on it means that decision was reversed by
    accident — see the spec's D2."""
    import inspect
    params = list(inspect.signature(server.unpublish_channel_listing).parameters)
    assert params == [
        "skus", "sub_source", "channel", "allow_variation_parent_takedown",
        "also_retiring_skus", "confirmed_count", "dry_run",
    ]
```

- [ ] **Step 2: Run it to verify it passes now (it pins current behaviour)**

Run: `.venv/bin/python -m pytest tests/test_delist_readback.py -v`
Expected: PASS — this test guards a decision rather than driving new code, so it is green from the start. Verify it *can* fail by adding a dummy parameter to `unpublish_channel_listing`, re-running (expect FAIL), then removing it.

- [ ] **Step 3: Commit**

```bash
git add tests/test_delist_readback.py
git commit -m "test: pin unpublish_channel_listing's signature against #115 drift"
```

---

## Task 6: Documentation and version

`tests/test_docs_consistency.py` enforces every item here, so this task is mechanical but not optional — a missing row fails the suite.

**Files:**
- Modify: `pyproject.toml:3`, `server.py:13`, `README.md:3`, `README.md` tools + thresholds tables, `CLAUDE.md:3`, `CLAUDE.md` tools table + release notes
- Modify: `docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md` (record the threshold deviation)

**Interfaces:**
- Consumes: the final tool names from Tasks 2 and 4
- Produces: nothing code depends on

- [ ] **Step 1: Confirm the tool count the tests will assert**

Run: `.venv/bin/python server.py --list-tools | tail -3`
Expected: a count of **100**

- [ ] **Step 2: Bump the version in all four places**

Tool counts and both tools-table rows are already correct — Tasks 2 and 3 added them
alongside the code they document, so the docs suite has been green throughout. Only the
version moves here. Set `1.64.0` (a feature release — two new tools) in:

```bash
# pyproject.toml:3      version = "1.64.0"
# server.py:13          __version__ = "1.64.0"
# README.md:3           ![Version](https://img.shields.io/badge/version-1.64.0-blue)
# CLAUDE.md:3           **Current version: 1.64.0** — 100 tools. …
```

- [ ] **Step 3: Add the CLAUDE.md release note**

Insert directly above the `v1.63.1` entry, matching house style:

```markdown
> **v1.64.0 (issue #115) — targeted dangling-GLT-template handling: a read-only reporter, and a PROVISIONAL delete whose likeliest outcome is its own removal (25 Sep 2026):** Two new tools (98 → 100); `unpublish_channel_listing` was deliberately NOT modified and a signature test pins that. **(1) `find_dangling_glt_templates` is read-only** and reports which of a SKU's templates are provably dangling, using `Info.Status == "Not deleted"` as the sole proof of death per the v1.63.1 SCOPE corollary — no Shopify credentials, none needed. ⚠️ **It can prove a template IS dangling and can NEVER prove one is fine**, so the verdicts are `dangling_proven` / `not_proven_dangling` with no `healthy` value anywhere: the detector is precise (141/141, then 42/42 across two independent sweeps) but not complete (~4 of 145 known dangling templates carry another status; `Listed` templates have never been swept). Its value is packaging and vocabulary rather than new capability — `refresh_channel_listing`'s dry run already exposed the same `status` field — and it is a VERIFICATION tool, not a discovery one. **(2) `delete_dangling_glt_template` is provisional and exists to settle an open question, not to clean up at scale.** Singular by construction, four refusal gates, a staging threshold of **0** so every live run stages a manifest and echoes `confirmed_count=1`. ⚠️ **THE NEAR-CIRCULARITY IS THE POINT AND IS RECORDED HONESTLY: the proof of death (`Not deleted`) is ALSO the signal of a prior delete failure**, so this tool is engineered to fire on templates that may well refuse to delete. Hence two questions in sequence — **(A)** does Delete land at all, **(B)** if it lands, does it take the item's channel-SKU rows and so the live sibling's Linnworks mapping (the #36 read-back records that "the first successful delete empties that table for the whole item", and v1.50.0 declined to use `unpublish_channel_listing` on the Echo orphan for precisely this reason). **B is reachable only if A is yes, and A = no is the likelier branch** — inconclusive about the danger, but decisive about the decision, since it would mean the GLT UI is the answer regardless. **Two of three plausible endings finish with this tool being deleted from the server.** ⚠️ **What fraction of dangling templates even sit on an item with a live sibling is UNKNOWN** — the 9 Sep census records 145 dangling templates across 145 distinct SKUs but not how many of those items hold a live template, and the 23 Sep Group 1/Group 2 sweep is not in this repo. If Group 2 dominates, this tool addresses a minority of cases. Design: `docs/superpowers/specs/2026-09-25-dangling-glt-template-handling-design.md`. 25 new tests, 1218 → 1243 total.
```

- [ ] **Step 4: Record the threshold deviation in the spec**

In the spec's tool (b) section, replace `WRITE_THRESHOLDS["delete_dangling_glt_template"] = 1` with:

```markdown
`WRITE_THRESHOLDS["delete_dangling_glt_template"] = 0`. **Corrected during planning:**
a threshold of 1 would never fire — `_write_guard` returns `None` when
`count <= threshold` (`server.py:643`), so a one-item list against a threshold of 1
proceeds unstaged. `0` makes every live run stage a manifest and echo `confirmed_count=1`,
which is the two-deliberate-acts behaviour this spec intended.
```

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 1243 passed

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "docs: tools tables, thresholds and v1.64.0 release note for #115"
```

---

## Final verification

- [ ] `.venv/bin/python -m pytest -q` → 1243 passed
- [ ] `.venv/bin/python server.py --list-tools | tail -3` → 100
- [ ] `git log --oneline origin/main..HEAD` → six commits, each independently testable
- [ ] Open the PR with `Closes #115` in the body, and paste the spec's "What the experiment can and cannot settle" table into the description so the reviewer knows what result to expect
