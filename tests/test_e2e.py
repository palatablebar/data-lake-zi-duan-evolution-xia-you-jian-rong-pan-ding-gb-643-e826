"""端到端：样例 → 判定 → 对账 → 导出，页面与接口一致。"""

from __future__ import annotations

import csv
import io
import json

from tests.fixtures import expected

PAGES = [
    ("/", "规格单"),
    ("/types", "类型与兼容"),
    ("/mapping", "字段映射"),
    ("/impact", "影响面"),
    ("/scope", "判定范围"),
    ("/batches", "版本历史"),
    ("/spec", "迁移单"),
]


def test_health_and_home(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["runs"] == 1
    assert body["window_semantics"] == "[start, end)"

    home = client.get("/")
    assert home.status_code == 200
    assert "规格单" in home.text


def test_all_pages_render(client):
    for path, marker in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert marker in response.text, path
        assert "/static/theme.css" in response.text
        assert "http://" not in response.text.split("<main")[0]


def test_full_flow_matches_ledger(client):
    reconcile = client.get("/api/reconcile").json()
    fields = reconcile["fields"]
    assert fields["old_total"] == expected.OLD_TOTAL
    assert fields["new_total"] == expected.NEW_TOTAL
    assert fields["old_gap"] == 0 and fields["new_gap"] == 0
    assert reconcile["refs"]["balanced"] is True
    assert reconcile["balanced"]["all"] is True

    manifest = json.loads(client.get("/api/export/manifest.json").text)
    assert manifest["run_id"] == reconcile["run_id"]
    assert manifest["counts"]["old_total"] == fields["old_total"]
    assert manifest["balanced"]["refs"] is True

    rows = list(csv.DictReader(io.StringIO(client.get("/api/export/fields.csv").text)))
    assert len(rows) == expected.OLD_TOTAL + 4
    assert sum(1 for row in rows if row["verdict"] == "BREAKING") == 6


def test_quarantine_csv_export(client):
    response = client.get("/api/schemas/quarantine.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == expected.QUARANTINE_BLOCKED + expected.QUARANTINE_WARNINGS
    reasons = {row["reason"] for row in rows}
    assert {"LINE_MALFORMED", "DUPLICATE_COLUMN", "BOM_STRIPPED"} <= reasons
    assert sum(1 for row in rows if row["blocking"] == "Y") == expected.QUARANTINE_BLOCKED


def test_home_renders_field_rows(client):
    home = client.get("/")
    assert "order_code" in home.text
    assert "pay_channel" in home.text
    assert "破坏" in home.text


def test_scope_is_consistent_across_page_and_exports(client):
    """页面、JSON、范围 CSV、字段 CSV、manifest 共用同一份范围/原因/下游。"""
    scope_json = client.get("/api/scope").json()["scope"]
    scope_csv = {row["table"]: row
                 for row in csv.DictReader(io.StringIO(
                     client.get("/api/export/scope.csv").text))}
    manifest = json.loads(client.get("/api/export/manifest.json").text)
    fields_csv = list(csv.DictReader(io.StringIO(
        client.get("/api/export/fields.csv").text)))

    # 同一份待复核表集合
    assert scope_json["review_tables"] == ["dim_order"]
    assert manifest["scope"]["review_tables"] == scope_json["review_tables"]
    assert scope_csv["dim_order"]["scope_status"] == "REVIEW"
    assert scope_csv["fct_pay"]["scope_status"] == "CONCLUSIVE"

    # 同一份受影响下游：CSV 里 dim_order 的下游数 == JSON 闭包总数口径
    assert int(scope_csv["dim_order"]["affected_count"]) == 3
    assert scope_json["affected_count"] == 5
    assert manifest["scope"]["affected_count"] == scope_json["affected_count"]

    # 字段 CSV 中待复核行的表全部落在待复核表集合内
    review_rows = [row for row in fields_csv if row["scope_status"] == "REVIEW"]
    assert review_rows
    assert {row["table"] for row in review_rows} == set(scope_json["review_tables"])

    # 两个范围导出端点字节一致
    assert client.get("/api/export/scope.csv").text == \
        client.get("/api/scope/export.csv").text

    # 范围页与迁移单页都渲染同一份待复核表
    assert "dim_order" in client.get("/scope").text
    spec = client.get("/spec").text
    assert "dim_order" in spec and "fct_pay" in spec


def test_spec_markdown_formal_clauses_exclude_review(client):
    text = client.get("/api/export/spec.md").text
    # 正式条款小节里不出现待复核表，扣留小节里出现
    formal = text.split("### 4.A")[0]
    assert "dim_order" not in formal.split("## 4")[1]
    assert "SCOPE_REVIEW" in text
    assert "[SCOPE_REVIEW] dim_order" in text
