"""Tests for run_readonly_query (issue #128): SELECT-only Query Data access."""
import pytest
from unittest.mock import patch, MagicMock

import server

QUOTA_BODY = '{"Message":"API calls quota exceeded! Maximum admitted 150 per Minute."}'


def _resp(status=200, json_body=None, text=None, headers=None):
    import json as _json
    r = MagicMock()
    r.status_code = status
    r.text = text if text is not None else _json.dumps(json_body if json_body is not None else {})
    r.ok = 200 <= status < 300
    r.headers = headers or {}
    r.json.return_value = json_body if json_body is not None else {}
    return r


def _ok(rows=None, total=None, cols=("ItemNumber",)):
    rows = rows if rows is not None else [{"ItemNumber": "A"}]
    return _resp(200, {
        "IsError": False, "ErrorMessage": None,
        "TotalResults": len(rows) if total is None else total,
        "Columns": [{"Name": c} for c in cols],
        "Results": rows,
    })


@pytest.fixture(autouse=True)
def _env():
    with patch.object(server.time, "sleep"), \
         patch.object(server, "ensure_auth", return_value=("tok", "https://eu.linnworks.net")):
        yield


@pytest.fixture
def post():
    with patch.object(server._session, "post") as p:
        p.return_value = _ok()
        yield p


# AC1
def test_tool_registered():
    import asyncio
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "run_readonly_query" in names


# AC2
def test_sends_one_form_post_with_script_field(post):
    server.run_readonly_query("SELECT TOP 1 ItemNumber FROM StockItem")
    assert post.call_count == 1
    args, kwargs = post.call_args
    assert args[0] == "https://eu.linnworks.net/api/Dashboards/ExecuteCustomScriptQuery"
    assert kwargs["data"] == {"script": "SELECT TOP 1 ItemNumber FROM StockItem"}
    assert "json" not in kwargs
    assert kwargs["headers"] == {"Authorization": "tok"}


# AC3
@pytest.mark.parametrize("sql", ["", "   ", "UPDATE StockItem SET x=1", "hello", "(SELECT 1)", "SHOW TABLES"])
def test_non_select_refused_without_http(post, sql):
    r = server.run_readonly_query(sql)
    assert r["refused"] is True and r["error"]
    post.assert_not_called()


def test_non_string_refused(post):
    assert server.run_readonly_query(None)["refused"] is True
    post.assert_not_called()


# AC4
@pytest.mark.parametrize("word", [
    "INSERT", "UPDATE", "DELETE", "MERGE", "EXEC", "EXECUTE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "INTO", "DECLARE", "WAITFOR", "OPENROWSET", "OPENQUERY",
    "OPENDATASOURCE", "BULK", "SET", "USE", "REVOKE", "DENY",
])
@pytest.mark.parametrize("case", [str.upper, str.lower, str.title])
def test_denied_word_refused_anywhere(post, word, case):
    r = server.run_readonly_query(f"SELECT 1 FROM StockItem WHERE x = 1 {case(word)} y")
    assert r["refused"] is True
    assert word in r["error"]
    post.assert_not_called()


@pytest.mark.parametrize("sql", ["SELECT sp_who FROM StockItem", "SELECT 1 FROM xp_cmdshell"])
def test_sp_xp_prefixes_refused(post, sql):
    assert server.run_readonly_query(sql)["refused"] is True
    post.assert_not_called()


def test_string_literal_scanned_and_error_explains(post):
    r = server.run_readonly_query("SELECT 1 FROM StockItem WHERE ItemTitle LIKE '%update%'")
    assert r["refused"] is True
    assert "string literals" in r["error"]
    post.assert_not_called()


# AC5
def test_select_into_refused(post):
    r = server.run_readonly_query("SELECT * INTO dbo.X FROM StockItem")
    assert r["refused"] is True and "INTO" in r["error"]
    post.assert_not_called()


# AC6
def test_trailing_semicolon_is_stripped(post):
    server.run_readonly_query("SELECT 1 FROM StockItem;  ")
    assert post.call_args.kwargs["data"] == {"script": "SELECT 1 FROM StockItem"}


@pytest.mark.parametrize("sql", ["SELECT 1; SELECT 2", ";SELECT 1", "SELECT 1;;", "SELECT 'a;b'"])
def test_other_semicolons_refused(post, sql):
    assert server.run_readonly_query(sql)["refused"] is True
    post.assert_not_called()


# AC7
@pytest.mark.parametrize("sql", [
    "SELECT 1 -- x\nDELETE FROM StockItem",
    "SELECT 1 /* */ ; DROP TABLE StockItem",
    "SELECT 1 /* hi */",
    "SELECT 1 -- note",
])
def test_comments_refused(post, sql):
    assert server.run_readonly_query(sql)["refused"] is True
    post.assert_not_called()


def test_newline_hidden_statement_refused(post):
    assert server.run_readonly_query("SELECT 1\nDELETE FROM StockItem")["refused"] is True
    post.assert_not_called()


@pytest.mark.parametrize("sql", [
    "SELECT 1 DISABLE TRIGGER ALL ON StockItem",
    "SELECT 1 ENABLE TRIGGER ALL ON StockItem",
    "SELECT 1 UPDATETEXT StockItem.ItemTitle 0x00 0 NULL 'x'",
    "SELECT 1 WRITETEXT StockItem.ItemTitle 0x00 'x'",
    "SELECT 1 SHUTDOWN WITH NOWAIT",
    "SELECT 1 DBCC SHRINKDATABASE(0)",
    "SELECT 1 BACKUP DATABASE x TO DISK='a'",
    "SELECT 1 RESTORE DATABASE x",
    "SELECT 1 KILL 55",
    "SELECT 1 RECONFIGURE",
    "SELECT 1 CHECKPOINT",
    "SELECT 1 BEGIN TRAN SELECT * FROM StockItem WITH (TABLOCKX, HOLDLOCK)",
    "SELECT 1 COMMIT", "SELECT 1 ROLLBACK", "SELECT 1 SAVE TRAN x",
    "SELECT 1 SETUSER 'x'", "SELECT 1 REVERT", "SELECT 1 SEND ON CONVERSATION 1",
    "SELECT 1 RECEIVE * FROM q", "SELECT 1 MOVE CONVERSATION 1 TO 2",
    "SELECT 1 OPEN c", "SELECT 1 CLOSE c", "SELECT 1 FETCH NEXT FROM c",
    "SELECT 1 DEALLOCATE c",
])
def test_unlisted_tsql_batch_verbs_refused(post, sql):
    assert server.run_readonly_query(sql)["refused"] is True
    post.assert_not_called()


# AC8
def test_identifiers_containing_denied_words_accepted(post):
    r = server.run_readonly_query("SELECT LastUpdateDate, CreatedBy, Settings, Reuse FROM StockItem")
    assert "refused" not in r
    assert post.call_count == 1


# AC9
def test_cte_accepted(post):
    sql = "WITH x AS (SELECT ItemNumber FROM StockItem) SELECT * FROM x"
    r = server.run_readonly_query(sql)
    assert "refused" not in r
    assert post.call_args.kwargs["data"] == {"script": sql}


# AC10
def test_is_error_returned_verbatim_without_rows(post):
    post.return_value = _resp(200, {"IsError": True, "ErrorMessage": "Invalid column name 'nope'.",
                                    "TotalResults": 0, "Columns": [], "Results": []})
    r = server.run_readonly_query("SELECT nope FROM StockItem")
    assert r["is_error"] is True
    assert r["error"] == "Invalid column name 'nope'."
    assert "rows" not in r


# AC11
def test_success_shape(post):
    post.return_value = _ok([{"a": 1, "b": 2}, {"a": 3, "b": 4}], cols=("a", "b"))
    r = server.run_readonly_query("SELECT a, b FROM T")
    assert r == {"columns": ["a", "b"], "rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}],
                 "total_results": 2, "row_count": 2, "truncated": False}


def test_truncated_when_cut_to_max_rows(post):
    post.return_value = _ok([{"a": i} for i in range(5)], cols=("a",))
    r = server.run_readonly_query("SELECT a FROM T", max_rows=3)
    assert r["row_count"] == 3 and len(r["rows"]) == 3
    assert r["truncated"] is True and r["total_results"] == 5


def test_truncated_when_total_exceeds_returned(post):
    post.return_value = _ok([{"a": 1}], total=9, cols=("a",))
    r = server.run_readonly_query("SELECT a FROM T")
    assert r["row_count"] == 1 and r["truncated"] is True


# AC12
@pytest.mark.parametrize("bad", [0, -1, 1.5, "10", None, True])
def test_bad_max_rows_refused_before_http(post, bad):
    r = server.run_readonly_query("SELECT 1 FROM T", max_rows=bad)
    assert r["refused"] is True
    post.assert_not_called()


def test_default_max_rows_is_5000():
    import inspect
    assert inspect.signature(server.run_readonly_query).parameters["max_rows"].default == 5000


# AC13
def test_429_retried_then_succeeds(post):
    post.side_effect = [_resp(429, text=QUOTA_BODY), _resp(429, text=QUOTA_BODY), _ok()]
    r = server.run_readonly_query("SELECT 1 FROM T")
    assert post.call_count == 3
    assert r["row_count"] == 1
    assert [c.args[0] for c in server.time.sleep.call_args_list] == [5, 10]


def test_quota_body_on_200_is_treated_as_rate_limit(post):
    post.side_effect = [_resp(200, text=QUOTA_BODY), _ok()]
    assert server.run_readonly_query("SELECT 1 FROM T")["row_count"] == 1
    assert post.call_count == 2


def test_rate_limit_exhausted_raises_not_sql_error(post):
    post.return_value = _resp(429, text=QUOTA_BODY)
    with pytest.raises(server.RateLimitError):
        server.run_readonly_query("SELECT 1 FROM T")
    assert post.call_count == 1 + len(server._RATE_LIMIT_BACKOFF)


# AC14
def test_no_dry_run_confirmed_count_or_threshold():
    import inspect
    params = inspect.signature(server.run_readonly_query).parameters
    assert set(params) == {"sql", "max_rows"}
    assert "run_readonly_query" not in server.WRITE_THRESHOLDS
    assert "read-only" in server.run_readonly_query.__doc__.lower()


# AC15
def test_docstring_content():
    d = server.run_readonly_query.__doc__
    assert "StockItem_ExtendedProperties" in d and "fkStockItemId" in d
    assert "ProperyName = 'Taxable'" in d
    assert "ProperyName" in d and "ProperyValue" in d and "ProperyType" in d
    assert d.count("COLLATE Latin1_General_CS_AS") >= 2
    assert "GROUP BY ProperyValue COLLATE Latin1_General_CS_AS" in d
    assert "bLogicalDelete = 1" in d
    assert "string literals" in d


# AC17: shared helper
def test_form_helper_default_does_not_retry(post):
    post.return_value = _resp(429, text=QUOTA_BODY)
    with pytest.raises(RuntimeError):
        server.call_linnworks_form("PurchaseOrder/Deliver_PurchaseItemAll", {"pkPurchaseId": "x"})
    assert post.call_count == 1


def test_form_helper_opt_in_retry(post):
    post.side_effect = [_resp(429, text=QUOTA_BODY), _ok()]
    out = server.call_linnworks_form("X/Y", {"a": 1}, retry_rate_limit=True)
    assert out["TotalResults"] == 1 and post.call_count == 2


def test_form_helper_reauths_on_401(post):
    post.side_effect = [_resp(401, text="no"), _ok()]
    with patch.object(server, "ensure_auth", return_value=("tok2", "https://eu.linnworks.net")):
        out = server.call_linnworks_form("X/Y", {"a": 1})
    assert out["IsError"] is False and post.call_count == 2
