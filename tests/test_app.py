import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def put_bom(client, part_no, eff, items):
    return client.put("/boms", json={
        "part_no": part_no,
        "effective_date": eff,
        "items": [{"component": c, "qty_per": q} for c, q in items],
    })


def create_wo(client, order_no, date, product, qty):
    return client.post("/work-orders", json={
        "order_no": order_no, "assembly_date": date,
        "product": product, "quantity": qty,
    })


def usage(client, order_no):
    r = client.get(f"/work-orders/{order_no}/usage")
    assert r.status_code == 200
    return {u["part_no"]: u["total_qty"] for u in r.json()["usage"]}


def bom_items(client, part_no, eff):
    r = client.get(f"/boms/{part_no}/{eff}")
    assert r.status_code == 200
    return [(i["component"], i["qty_per"]) for i in r.json()["items"]]


def test_aggregate_across_paths_without_path_expansion(client):
    # 菱形结构：STEEL 经 WHEEL->HUB 与 AXLE 两条路径进入
    assert put_bom(client, "CAR", "2024-01-01", [("WHEEL", 4), ("AXLE", 2)]).status_code == 200
    assert put_bom(client, "WHEEL", "2024-01-01", [("HUB", 1), ("TIRE", 1)]).status_code == 200
    assert put_bom(client, "HUB", "2024-01-01", [("STEEL", 2)]).status_code == 200
    assert put_bom(client, "AXLE", "2024-01-01", [("STEEL", 3)]).status_code == 200

    r = create_wo(client, "WO-1", "2024-06-01", "CAR", 10)
    assert r.status_code == 201, r.text
    # STEEL = 10 * (4*1*2 + 2*3) = 140，TIRE = 40；HUB/AXLE/WHEEL 是中间件
    assert usage(client, "WO-1") == {"TIRE": 40.0, "STEEL": 140.0}


def test_no_bom_means_base_part(client):
    assert create_wo(client, "WO-1", "2024-06-01", "GIZMO", 5).status_code == 201
    assert usage(client, "WO-1") == {"GIZMO": 5.0}


def test_effective_bom_selected_by_assembly_date(client):
    put_bom(client, "P", "2024-01-01", [("A", 1)])
    put_bom(client, "P", "2024-03-01", [("A", 2)])
    create_wo(client, "WO-1", "2024-02-01", "P", 3)   # 用 01-01 版
    create_wo(client, "WO-2", "2024-04-01", "P", 3)   # 用 03-01 版
    create_wo(client, "WO-3", "2023-12-31", "P", 3)   # 装配日前无单，P 算基础件
    assert usage(client, "WO-1") == {"A": 3.0}
    assert usage(client, "WO-2") == {"A": 6.0}
    assert usage(client, "WO-3") == {"P": 3.0}


def test_backdated_bom_recomputes_orders_that_treated_part_as_base(client):
    put_bom(client, "X", "2024-01-01", [("M", 2), ("N", 1)])
    create_wo(client, "WO-1", "2024-02-01", "X", 4)   # M 暂无单，算基础件
    create_wo(client, "WO-2", "2024-01-10", "X", 4)   # 在新单生效日之前
    assert usage(client, "WO-1") == {"M": 8.0, "N": 4.0}

    # 补登 M 的用料单，生效日 01-15：只有 WO-1 落在本单有效期内
    r = put_bom(client, "M", "2024-01-15", [("K", 3)])
    assert r.status_code == 200, r.text
    assert r.json()["recomputed_orders"] == ["WO-1"]
    assert usage(client, "WO-1") == {"K": 24.0, "N": 4.0}  # 4*2*3
    assert usage(client, "WO-2") == {"M": 8.0, "N": 4.0}   # 不受影响


def test_correction_recomputes_affected_orders(client):
    put_bom(client, "X", "2024-01-01", [("M", 2)])
    create_wo(client, "WO-1", "2024-02-01", "X", 4)
    assert usage(client, "WO-1") == {"M": 8.0}

    r = put_bom(client, "X", "2024-01-01", [("M", 5)])  # 同料号同生效日 = 更正
    assert r.status_code == 200
    assert r.json()["recomputed_orders"] == ["WO-1"]
    assert usage(client, "WO-1") == {"M": 20.0}
    assert bom_items(client, "X", "2024-01-01") == [("M", 5.0)]


def test_effective_window_bounded_by_next_version(client):
    put_bom(client, "M", "2024-01-01", [("K", 1)])
    put_bom(client, "M", "2024-03-01", [("K", 2)])
    put_bom(client, "X", "2024-01-01", [("M", 1)])
    create_wo(client, "WO-1", "2024-02-01", "X", 1)   # 用 M 的 01-01 版
    create_wo(client, "WO-2", "2024-04-01", "X", 1)   # 用 M 的 03-01 版

    r = put_bom(client, "M", "2024-01-01", [("K", 10)])  # 更正旧版
    assert r.json()["recomputed_orders"] == ["WO-1"]
    assert usage(client, "WO-1") == {"K": 10.0}
    assert usage(client, "WO-2") == {"K": 2.0}           # 不在本单有效期内，不动


def test_cycle_rejects_work_order_and_lists_parts(client):
    put_bom(client, "A", "2024-01-01", [("B", 1)])
    put_bom(client, "B", "2024-01-01", [("A", 1)])
    r = create_wo(client, "WO-1", "2024-06-01", "A", 1)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert set(detail["cycle"]) == {"A", "B"}
    assert client.get("/work-orders/WO-1").status_code == 404  # 未落单


def test_self_cycle_rejected(client):
    put_bom(client, "A", "2024-01-01", [("A", 1)])
    r = create_wo(client, "WO-1", "2024-06-01", "A", 1)
    assert r.status_code == 422
    assert r.json()["detail"]["cycle"] == ["A"]


def test_failed_bom_save_rolls_back_and_keeps_original_results(client):
    put_bom(client, "P", "2024-01-01", [("Q", 1)])
    create_wo(client, "WO-1", "2024-02-01", "P", 2)
    assert usage(client, "WO-1") == {"Q": 2.0}

    # 新登 Q 的单会造成 P->Q->P 成环：保存失败，沿用原单和原结果
    r = put_bom(client, "Q", "2024-01-01", [("P", 1)])
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert set(detail["cycle"]) == {"P", "Q"}
    assert detail["order_no"] == "WO-1"
    assert client.get("/boms/Q").json()["versions"] == []      # 新单未保存
    assert bom_items(client, "P", "2024-01-01") == [("Q", 1.0)]
    assert usage(client, "WO-1") == {"Q": 2.0}                 # 原结果不变


def test_failed_correction_keeps_original_bom(client):
    put_bom(client, "P", "2024-01-01", [("Q", 1)])
    put_bom(client, "Q", "2024-01-01", [("R", 1)])
    create_wo(client, "WO-1", "2024-02-01", "P", 2)
    assert usage(client, "WO-1") == {"R": 2.0}

    r = put_bom(client, "Q", "2024-01-01", [("P", 1)])  # 更正成环
    assert r.status_code == 422
    assert bom_items(client, "Q", "2024-01-01") == [("R", 1.0)]  # 原明细还在
    assert usage(client, "WO-1") == {"R": 2.0}


def test_duplicate_order_no_rejected(client):
    assert create_wo(client, "WO-1", "2024-06-01", "X", 1).status_code == 201
    r = create_wo(client, "WO-1", "2024-06-02", "X", 2)
    assert r.status_code == 409


def test_unaffected_orders_not_recomputed(client):
    put_bom(client, "X", "2024-01-01", [("M", 1)])
    put_bom(client, "Y", "2024-01-01", [("N", 1)])
    create_wo(client, "WO-1", "2024-02-01", "X", 1)
    create_wo(client, "WO-2", "2024-02-01", "Y", 1)

    r = put_bom(client, "M", "2024-01-01", [("K", 7)])  # 只与 WO-1 相关
    assert r.json()["recomputed_orders"] == ["WO-1"]
    assert usage(client, "WO-1") == {"K": 7.0}
    assert usage(client, "WO-2") == {"N": 1.0}
