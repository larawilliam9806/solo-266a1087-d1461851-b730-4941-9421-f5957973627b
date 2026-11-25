"""工单标准耗料核对供数接口（FastAPI + SQLite）。"""

import os
import sqlite3
from datetime import date

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import service
from .db import connect, init_db


class BomItemIn(BaseModel):
    component: str = Field(min_length=1)   # 子件料号
    qty_per: float = Field(gt=0)           # 单套用量


class BomIn(BaseModel):
    part_no: str = Field(min_length=1)     # 料号
    effective_date: date                   # 生效日
    items: list[BomItemIn] = Field(min_length=1)  # 整份明细


class WorkOrderIn(BaseModel):
    order_no: str = Field(min_length=1)    # 单号
    assembly_date: date                    # 装配日
    product: str = Field(min_length=1)     # 成品料号
    quantity: float = Field(gt=0)          # 产量


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("APP_DB_PATH", "data/app.db")
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    init_db(db_path)

    app = FastAPI(title="工单标准耗料核对接口")

    def get_conn():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    # ---------- 工单 ----------

    @app.post("/work-orders", status_code=201)
    def create_work_order(body: WorkOrderIn, conn=Depends(get_conn)):
        """登记工单并按装配日核算各基础件总用量。用料成环则退回。"""
        try:
            wo_id = service.create_work_order(
                conn, body.order_no, body.assembly_date.isoformat(),
                body.product, body.quantity)
        except service.CycleError as exc:
            raise HTTPException(status_code=422, detail={
                "message": "工单用料成环，已退回", "cycle": exc.cycle})
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="单号已存在")
        return {"id": wo_id, "order_no": body.order_no}

    @app.get("/work-orders")
    def list_work_orders(conn=Depends(get_conn)):
        rows = conn.execute(
            "SELECT id, order_no, assembly_date, product, quantity "
            "FROM work_orders ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    @app.get("/work-orders/{order_no}")
    def get_work_order(order_no: str, conn=Depends(get_conn)):
        row = conn.execute(
            "SELECT id, order_no, assembly_date, product, quantity "
            "FROM work_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        return dict(row)

    @app.get("/work-orders/{order_no}/usage")
    def get_work_order_usage(order_no: str, conn=Depends(get_conn)):
        """按工单查各基础件总用量（已合计所有路径）。"""
        row = conn.execute(
            "SELECT id FROM work_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="工单不存在")
        items = conn.execute(
            "SELECT part_no, total_qty FROM work_order_usage "
            "WHERE work_order_id = ? ORDER BY part_no", (row["id"],)).fetchall()
        return {"order_no": order_no, "usage": [dict(r) for r in items]}

    # ---------- 用料单 ----------

    @app.put("/boms")
    def put_bom(body: BomIn, conn=Depends(get_conn)):
        """按料号+生效日整份登记或更正用料单，并重核受影响工单。

        重核失败（如用料成环）时整体回滚，沿用原单和原结果。
        """
        try:
            recomputed = service.upsert_bom(
                conn, body.part_no, body.effective_date.isoformat(),
                [(it.component, it.qty_per) for it in body.items])
        except service.CycleError as exc:
            raise HTTPException(status_code=422, detail={
                "message": "重核时用料成环，保存失败，已沿用原单和原结果",
                "cycle": exc.cycle, "order_no": exc.order_no})
        return {"part_no": body.part_no,
                "effective_date": body.effective_date.isoformat(),
                "recomputed_orders": recomputed}

    @app.get("/boms/{part_no}")
    def list_bom_versions(part_no: str, conn=Depends(get_conn)):
        rows = conn.execute(
            "SELECT effective_date FROM boms WHERE part_no = ? "
            "ORDER BY effective_date", (part_no,)).fetchall()
        return {"part_no": part_no,
                "versions": [r["effective_date"] for r in rows]}

    @app.get("/boms/{part_no}/{effective_date}")
    def get_bom(part_no: str, effective_date: date, conn=Depends(get_conn)):
        row = conn.execute(
            "SELECT id FROM boms WHERE part_no = ? AND effective_date = ?",
            (part_no, effective_date.isoformat())).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="用料单不存在")
        items = conn.execute(
            "SELECT component, qty_per FROM bom_items WHERE bom_id = ? "
            "ORDER BY component", (row["id"],)).fetchall()
        return {"part_no": part_no,
                "effective_date": effective_date.isoformat(),
                "items": [dict(r) for r in items]}

    return app


app = create_app()
