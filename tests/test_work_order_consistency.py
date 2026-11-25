"""并发补登/更正用料单时 POST /work-orders 的一致性回归测试。

这些用例通过 monkeypatch ``service.get_effective_bom`` 在线程内
确定性地插入一次"已提交的用料单变更"，复现并锁定以下口径：

* 一次展开的各层用料单来自同一个已提交快照；
* 入账/成环退回前若选中的单或明细已变（含基础件新有生效单），按新数据重核；
* 不改变本工单取数的并发提交不会触发重新展开，也不会被展开阻塞；
* 成功时工单/耗料/涉及料号一并入账，失败不留数据。
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import service
from app.db import connect
from app.main import create_app


@pytest.fixture()
def env(tmp_path):
    db_path = tmp_path / "consistency.db"
    app = create_app(str(db_path))
    writer = connect(str(db_path))
    with TestClient(app) as client:
        yield client, writer
    writer.close()


def put_bom(client, part_no, effective_date, items):
    return client.put("/boms", json={
        "part_no": part_no,
        "effective_date": effective_date,
        "items": [{"component": c, "qty_per": q} for c, q in items],
    })


def create_wo(client, order_no="WO-1", date="2024-06-01", product="CAR", qty=10):
    return client.post("/work-orders", json={
        "order_no": order_no, "assembly_date": date,
        "product": product, "quantity": qty})


def usage_map(client, order_no):
    r = client.get(f"/work-orders/{order_no}/usage")
    assert r.status_code == 200, r.text
    return {u["part_no"]: u["total_qty"] for u in r.json()["usage"]}


def test_committed_change_right_after_layer_read_is_rechecked(env, monkeypatch):
    client, writer = env
    put_bom(client, "CAR", "2024-01-01", [("WHEEL", 4)])
    put_bom(client, "WHEEL", "2024-01-01", [("HUB", 1)])
    put_bom(client, "HUB", "2024-01-01", [("OLD", 1)])

    original = service.get_effective_bom
    fired = False

    def intercepted(conn, part_no, date_str):
        nonlocal fired
        rows = original(conn, part_no, date_str)  # 先按展开快照读到旧单
        if part_no == "HUB" and not fired:
            fired = True
            # 成本组在该层读完后提交更正
            service.upsert_bom(writer, "HUB", "2024-01-01", [("NEW", 2)])
        return rows

    monkeypatch.setattr(service, "get_effective_bom", intercepted)

    calls = 0
    original_explode = service.explode

    def counting_explode(conn, *args):
        nonlocal calls
        calls += 1
        return original_explode(conn, *args)

    monkeypatch.setattr(service, "explode", counting_explode)

    r = create_wo(client)
    assert r.status_code == 201, r.text
    assert usage_map(client, "WO-1") == {"NEW": 80.0}  # 10 * 4 * 2
    assert calls == 2  # 旧快照一次 + 按新数据重核一次

    # 新子件已在涉及料号中：后来给它补单不会再漏掉这张工单
    r = put_bom(client, "NEW", "2024-02-01", [("STEEL", 1)])
    assert r.status_code == 200
    assert r.json()["recomputed_orders"] == ["WO-1"]
    assert usage_map(client, "WO-1") == {"STEEL": 80.0}


def test_base_part_gaining_effective_bom_forces_recheck(env, monkeypatch):
    client, writer = env
    put_bom(client, "X", "2024-01-01", [("M", 2)])  # M 暂无单 = 基础件

    original = service.get_effective_bom
    fired = False

    def intercepted(conn, part_no, date_str):
        nonlocal fired
        rows = original(conn, part_no, date_str)
        if part_no == "M" and rows is None and not fired:
            fired = True
            service.upsert_bom(writer, "M", "2024-01-15", [("K", 3)])
        return rows

    monkeypatch.setattr(service, "get_effective_bom", intercepted)

    r = create_wo(client, product="X", qty=4)
    assert r.status_code == 201, r.text
    assert usage_map(client, "WO-1") == {"K": 24.0}


def test_unrelated_commit_does_not_reexplode_or_block(env, monkeypatch):
    client, writer = env
    put_bom(client, "CAR", "2024-01-01", [("WHEEL", 4)])
    put_bom(client, "WHEEL", "2024-01-01", [("TIRE", 1)])

    original = service.get_effective_bom

    def intercepted(conn, part_no, date_str):
        rows = original(conn, part_no, date_str)
        if part_no == "WHEEL":
            # 与本工单取数无关的补登：应照常提交且不使工单失效
            result = put_bom(client, "UNRELATED", "2024-01-01", [("Q", 1)])
            assert result.status_code == 200
        return rows

    monkeypatch.setattr(service, "get_effective_bom", intercepted)

    calls = 0
    original_explode = service.explode

    def counting_explode(conn, *args):
        nonlocal calls
        calls += 1
        return original_explode(conn, *args)

    monkeypatch.setattr(service, "explode", counting_explode)

    r = create_wo(client)
    assert r.status_code == 201, r.text
    assert usage_map(client, "WO-1") == {"TIRE": 40.0}
    assert calls == 1  # 取数未变，无需重算


def test_cycle_vanishing_after_committed_fix_is_rechecked(env, monkeypatch):
    client, writer = env
    put_bom(client, "A", "2024-01-01", [("B", 1)])
    put_bom(client, "B", "2024-01-01", [("A", 1)])  # A -> B -> A

    original = service.get_effective_bom
    fired = False

    def intercepted(conn, part_no, date_str):
        nonlocal fired
        rows = original(conn, part_no, date_str)
        if part_no == "B" and not fired:
            fired = True
            service.upsert_bom(writer, "B", "2024-01-01", [("C", 1)])  # 并发作废环
        return rows

    monkeypatch.setattr(service, "get_effective_bom", intercepted)

    r = create_wo(client, product="A", qty=2)
    assert r.status_code == 201, r.text
    assert usage_map(client, "WO-1") == {"C": 2.0}


def test_stable_cycle_still_listed_with_original_contract(env, monkeypatch):
    client, writer = env
    put_bom(client, "A", "2024-01-01", [("B", 1)])
    put_bom(client, "B", "2024-01-01", [("A", 1)])

    original = service.get_effective_bom

    def intercepted(conn, part_no, date_str):
        rows = original(conn, part_no, date_str)
        if part_no == "B":
            service.upsert_bom(writer, "UNRELATED", "2024-01-01", [("Q", 1)])
        return rows

    monkeypatch.setattr(service, "get_effective_bom", intercepted)

    r = create_wo(client, product="A")
    assert r.status_code == 422
    assert set(r.json()["detail"]["cycle"]) == {"A", "B"}
    assert client.get("/work-orders/WO-1").status_code == 404


def test_failed_registration_leaves_no_rows(tmp_path):
    db_path = tmp_path / "atomic.db"
    app = create_app(str(db_path))
    with TestClient(app) as client:
        put_bom(client, "A", "2024-01-01", [("A", 1)])
        r = create_wo(client, product="A")
        assert r.status_code == 422

    conn = sqlite3.connect(str(db_path))
    for table in ("work_orders", "work_order_usage", "work_order_parts"):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    conn.close()
