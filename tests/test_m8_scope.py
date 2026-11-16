"""M8 判定范围：表级隔离、待复核原因、受影响下游、落盘/回滚、幂等、页面导出一致。"""

from __future__ import annotations

import csv
import io
import json

import pytest

from app import store
from app.modules.m8_scope import propagate, revision, scope, service
from tests.fixtures import expected


# ---------- 纯规则 ----------

def _consumers():
    run = store.latest_run()
    return run["refs"]["consumers"]


def test_blocking_rows_isolate_only_their_table(run):
    """坏行所在表进待复核，证据完整的其他表仍能出正式结论。"""
    payload = run["scope"]
    assert payload["review_tables"] == expected.SCOPE_REVIEW_TABLES
    assert payload["conclusive_tables"] == expected.SCOPE_CONCLUSIVE_TABLES
    assert payload["table_count"] == expected.SCOPE_TABLE_COUNT
    assert payload["review_count"] == 1 and payload["conclusive_count"] == 2


def test_review_table_keeps_blocking_row_records(run):
    """隔离坏行不能丢记录：每条坏行的行号、原因、原文都挂在待复核表上。"""
    table = next(item for item in run["scope"]["tables"] if item["table"] == "dim_order")
    assert table["status"] == "REVIEW"
    assert table["blocked_count"] == expected.SCOPE_BLOCKED_TOTAL
    reasons = {row["reason"] for row in table["blocked_rows"]}
    assert reasons == {"LINE_MALFORMED", "DUPLICATE_COLUMN"}
    raw_joined = " ".join(row.get("raw") or "" for row in table["blocked_rows"])
    assert "order_amt" in raw_joined and "ORDER_NO" in raw_joined
    # 阻断计数与隔离区总账一致
    assert run["scope"]["blocked_total"] == expected.SCOPE_BLOCKED_TOTAL


def test_warning_level_diagnostics_do_not_block(run):
    """提示级（派生身份、未知类型、编码回退）不挡结论：dim_user 有派生身份仍是正式。"""
    warn_tables = {
        item["table"] for item in run["scope"]["tables"]
        if any(not row.get("blocking") for row in item.get("blocked_rows", []))
    }
    # blocked_rows 只收阻断级，提示级不应出现在这里
    assert warn_tables == set()
    statuses = {item["table"]: item["status"] for item in run["scope"]["tables"]}
    assert statuses["dim_user"] == "CONCLUSIVE"


def test_diff_rows_are_annotated_but_not_dropped(run):
    """逐字段行一条不丢，只是待复核表的行被标记为范围外。"""
    rows = run["diff"]
    # 全量行数 = 旧侧桶 + 新增，与字段账总量对齐
    expected_total = expected.OLD_TOTAL + expected.BUCKETS["added"]
    assert len(rows) == expected_total
    review = [row for row in rows if not row["in_scope"]]
    formal = [row for row in rows if row["in_scope"]]
    assert review and all(row["table"] == "dim_order" for row in review)
    assert formal and all(row["table"] != "dim_order" for row in formal)
    assert all(row["scope_reason"] == "SCOPE_REVIEW" for row in review)
    # 全量字段账仍然守恒：范围标记不影响账目
    assert run["ledger"]["balanced"]["all"] is True


def test_formal_breaking_excludes_review_table(run):
    """正式破坏只来自干净表；待复核表的破坏项被扣留，不进正式条款。"""
    assert run["scope"]["formal_breaking"] == ["2003", "2004", "2005"]
    assert set(run["scope"]["withheld_breaking"]) == {"auto:df826cfe948a", "1007", "1011"}
    assert run["ledger"]["counts"]["formal_breaking"] == expected.SCOPE_FORMAL_BREAKING
    assert run["ledger"]["counts"]["withheld_breaking"] == expected.SCOPE_WITHHELD_BREAKING
    # 正式范围口径自身守恒
    formal = run["ledger"]["formal"]
    assert formal["balanced"]["fields_old"] and formal["balanced"]["fields_new"]
    assert formal["old_gap"] == 0 and formal["new_gap"] == 0


def test_affected_downstream_closure(run):
    """受影响下游 = 直接引用待复核表 + 反向闭包，深度取最短跳数。"""
    payload = run["scope"]
    assert payload["affected_consumers"] == expected.SCOPE_AFFECTED_CONSUMERS
    assert payload["affected_direct"] == expected.SCOPE_AFFECTED_DIRECT
    closure_rows = payload["downstream_closure"]["rows"]
    depths = {row["name"]: row["depth"] for row in closure_rows}
    assert depths["dw.mv_order_daily"] == 1
    assert depths["dw.task_settle"] == 2
    assert depths["ads.rpt_pay_channel"] == 3
    # 末梢节点也能追溯到来源待复核表
    far = next(row for row in closure_rows if row["name"] == "ads.rpt_pay_channel")
    assert far["sources"] == ["dim_order"]


def test_scope_pure_rules_unit():
    fields_old = [
        {"table": "t_a", "name": "x", "field_id": "1"},
        {"table": "t_b", "name": "y", "field_id": "2"},
    ]
    fields_new = [
        {"table": "t_a", "name": "x", "field_id": "1"},
        {"table": "t_b", "name": "y2", "field_id": "2"},
    ]
    quarantine_old = [
        {"table": "t_b", "line": 4, "reason": "LINE_MALFORMED", "blocking": True,
         "column": "y", "raw": "y broken"},
        {"table": "t_b", "line": 5, "reason": "FIELD_ID_DERIVED", "blocking": False,
         "column": "z", "raw": "t_b.z"},
    ]
    consumers = [
        {"name": "c1", "kind": "view", "refs": ["t_b.y"]},
        {"name": "c2", "kind": "task", "refs": ["t_a.x"]},
    ]
    result = scope.build_scope(fields_old, fields_new, quarantine_old, [], consumers)
    by_name = {item["table"]: item for item in result["tables"]}
    assert by_name["t_b"]["status"] == "REVIEW"
    assert by_name["t_a"]["status"] == "CONCLUSIVE"
    # 提示级坏行不计入阻断
    assert by_name["t_b"]["blocked_count"] == 1
    assert [c["name"] for c in by_name["t_b"]["affected_consumers"]] == ["c1"]


def test_propagate_direct_and_transitive():
    consumers = [
        {"name": "d1", "kind": "view", "refs": ["rv.col"], "upstream": [], "downstream": ["d2"]},
        {"name": "d2", "kind": "view", "refs": [], "upstream": ["d1"], "downstream": []},
        {"name": "clean", "kind": "view", "refs": ["ok.col"], "upstream": [], "downstream": []},
    ]
    review_tables = [{"table_key": "rv", "table": "rv", "affected_consumers": [
        {"name": "d1", "kind": "view", "ref_count": 1}]}]
    out = propagate.propagate(review_tables, consumers)
    names = {row["name"]: row for row in out["rows"]}
    assert set(names) == {"d1", "d2"}
    assert names["d1"]["depth"] == 1 and names["d2"]["depth"] == 2
    assert "clean" not in names


# ---------- 接口 ----------

def test_scope_api_matches_run_payload(client, run):
    body = client.get("/api/scope").json()
    assert body["run_id"] == run["run_id"]
    assert body["scope"]["review_tables"] == run["scope"]["review_tables"]
    assert body["summary"]["review_count"] == 1


def test_scope_page_renders(client):
    response = client.get("/scope")
    assert response.status_code == 200
    page = response.text
    assert "判定范围" in page
    assert "dim_order" in page and "待复核" in page
    assert "fct_pay" in page and "正式" in page
    # 坏行原文出现在证据小节（页面显示原因码的中文标签）
    assert "列定义不完整" in page
    assert "order_amt" in page


def test_scope_csv_export_matches_page_scope(client, run):
    """范围 CSV 与页面/接口共用同一份范围、原因、受影响下游。"""
    text = client.get("/api/export/scope.csv").text
    rows = {row["table"]: row for row in csv.DictReader(io.StringIO(text))}
    assert rows["dim_order"]["scope_status"] == "REVIEW"
    assert rows["dim_order"]["affected_count"] == "3"
    assert rows["fct_pay"]["scope_status"] == "CONCLUSIVE"
    # M8 自有导出端点与 M7 导出端点字节一致
    other = client.get("/api/scope/export.csv").text
    assert other == text


def test_fields_csv_carries_scope_column(client):
    text = client.get("/api/export/fields.csv").text
    rows = list(csv.DictReader(io.StringIO(text)))
    review = [row for row in rows if row["scope_status"] == "REVIEW"]
    assert review and all(row["table"] == "dim_order" for row in review)
    assert any(row["scope_status"] == "CONCLUSIVE" for row in rows)


def test_manifest_carries_scope(client):
    manifest = json.loads(client.get("/api/export/manifest.json").text)
    assert manifest["scope"]["review_tables"] == ["dim_order"]
    assert manifest["scope"]["formal_breaking"] == 3
    assert manifest["scope"]["withheld_breaking"] == 3


def test_spec_page_withholds_review_clauses(client):
    page = client.get("/spec").text
    # 正式条款里只有 fct_pay，dim_order 出现在扣留小节
    assert "待复核扣留项" in page
    # 页面把原因码渲染成中文标签
    assert "停在待复核" in page


# ---------- 落盘、备份、回滚 ----------

def test_scope_revision_persisted_on_seed(run):
    revisions = revision.list_scope_revisions(run["run_id"])
    assert len(revisions) == 1
    assert revisions[0]["status"] == "ACTIVE"
    stored = revision.current_revision(run["run_id"])
    assert stored["scope"]["review_tables"] == ["dim_order"]
    # 文件快照存在且能解析
    path = revision.scope_path(run["run_id"])
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["scope"]["review_tables"] == ["dim_order"]


def test_revision_is_idempotent(run):
    """相同范围内容重算命中同一 revision_id，不重复生成修订记录。"""
    before = len(revision.list_scope_revisions(run["run_id"]))
    saved = revision.persist_revision(run["run_id"], run["scope"], note="初次范围")
    again = revision.persist_revision(run["run_id"], run["scope"], note="初次范围")
    assert saved["revision_id"] == again["revision_id"]
    assert again.get("reused") is True
    assert len(revision.list_scope_revisions(run["run_id"])) == before


def test_distinct_revision_creates_new_seq_and_keeps_backup(run, tmp_path, monkeypatch):
    monkeypatch.setenv("FIELDMAP_SCOPE_DIR", str(tmp_path / "scope"))
    first = revision.persist_revision(run["run_id"], run["scope"], note="v1")
    # 改一份内容（模拟复核后调整），落新版并生成 .bak 备份
    tweaked = json.loads(json.dumps(run["scope"]))
    tweaked["note_marker"] = "v2"
    second = revision.persist_revision(run["run_id"], tweaked, note="v2")
    assert second["revision_seq"] == first["revision_seq"] + 1
    assert revision.backup_path(run["run_id"]).exists()
    history_rows = revision.list_scope_revisions(run["run_id"])
    assert [item["revision_seq"] for item in history_rows][-2:] == [
        first["revision_seq"], second["revision_seq"]]


def test_rollback_restores_previous_revision(run, tmp_path, monkeypatch):
    monkeypatch.setenv("FIELDMAP_SCOPE_DIR", str(tmp_path / "scope"))
    first = revision.persist_revision(run["run_id"], run["scope"], note="v1")
    tweaked = json.loads(json.dumps(run["scope"]))
    tweaked["note_marker"] = "v2"
    revision.persist_revision(run["run_id"], tweaked, note="v2")
    restored = revision.rollback(run["run_id"])
    assert restored["status"] == "ROLLED_BACK"
    # 回滚后磁盘上的范围内容等于第一版
    on_disk = json.loads(revision.scope_path(run["run_id"]).read_text(encoding="utf-8"))
    assert "note_marker" not in on_disk["scope"]
    assert on_disk["scope"]["review_tables"] == first["scope"]["review_tables"]


def test_rollback_without_backup_raises(run, tmp_path, monkeypatch):
    monkeypatch.setenv("FIELDMAP_SCOPE_DIR", str(tmp_path / "fresh"))
    from app import errors

    with pytest.raises(errors.NotFound):
        revision.rollback(run["run_id"])


def test_persist_failure_keeps_previous_revision(run, tmp_path, monkeypatch):
    """落盘失败时旧版文件原封不动，并显式报错，可回滚恢复。"""
    scope_dir = tmp_path / "scope"
    monkeypatch.setenv("FIELDMAP_SCOPE_DIR", str(scope_dir))
    good = revision.persist_revision(run["run_id"], run["scope"], note="v1")
    original_text = revision.scope_path(run["run_id"]).read_text(encoding="utf-8")

    real_atomic = revision._atomic_write

    def boom(path, text):
        raise revision.ScopePersistError("注入失败", detail={"path": str(path)})

    monkeypatch.setattr(revision, "_atomic_write", boom)
    with pytest.raises(revision.ScopePersistError):
        revision.persist_revision(run["run_id"], {**run["scope"], "x": 1}, note="v2")
    # 旧文件内容不变
    assert revision.scope_path(run["run_id"]).read_text(encoding="utf-8") == original_text
    monkeypatch.setattr(revision, "_atomic_write", real_atomic)
    # 库内权威历史没有多出失败的修订
    assert all(item["note"] != "v2" for item in revision.list_scope_revisions(run["run_id"]))
    assert good["revision_id"]


# ---------- 批次幂等：同批重跑不重复判定/范围记录 ----------

def test_rerun_does_not_duplicate_scope_revision(client, run):
    from app.modules.m6_batches import service as batch_service

    before_runs = store.count_runs()
    before_rev = len(revision.list_scope_revisions(run["run_id"]))
    again = batch_service.execute()
    assert again["reused"] is True
    assert store.count_runs() == before_runs
    assert len(revision.list_scope_revisions(run["run_id"])) == before_rev


def test_create_revision_endpoint_idempotent(client, run):
    r1 = client.post("/api/scope/revisions", json={"note": "核对"})
    r2 = client.post("/api/scope/revisions", json={"note": "核对"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["revision_id"] == r2.json()["revision_id"]
    assert r2.json()["reused"] is True


def test_rollback_endpoint(client, run):
    client.post("/api/scope/revisions", json={"note": "第二版"})
    resp = client.post("/api/scope/rollback")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ROLLED_BACK"


def test_scope_endpoints_404_without_run(empty_client):
    assert empty_client.get("/api/scope").status_code == 404
    assert empty_client.get("/api/export/scope.csv").status_code == 404
