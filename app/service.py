"""用料展开、成环检测与重核逻辑。

展开算法按"料号节点"聚合，不逐条展开路径：
先按装配日确定每个料号最近已生效的用料单（无单即基础件），
再对展开图做拓扑排序，把需求量自顶向下逐层汇总——同一料号在
不同路径下的用量在节点处直接相加。复杂度 O(料号数 + 明细行数)，
与路径条数无关。
"""

from collections import deque


class CycleError(Exception):
    """工单用料成环。cycle 为环上的料号（按环的顺序）。"""

    def __init__(self, cycle):
        self.cycle = list(cycle)
        self.order_no = None  # 重核场景下记录触发成环的工单号
        super().__init__("用料成环: " + " -> ".join(self.cycle + self.cycle[:1]))


def get_effective_bom(conn, part_no, date_str):
    """取装配日当天或之前最近生效的用料单；无单返回 None（按基础件计）。"""
    row = conn.execute(
        "SELECT id FROM boms WHERE part_no = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC LIMIT 1",
        (part_no, date_str),
    ).fetchone()
    if row is None:
        return None
    items = conn.execute(
        "SELECT component, qty_per FROM bom_items WHERE bom_id = ?",
        (row["id"],),
    ).fetchall()
    return [(r["component"], r["qty_per"]) for r in items]


def _find_cycle(parts, children):
    """迭代式 DFS 找环，返回环上料号列表；无环返回 None。"""
    color = {}  # 0/缺省=未访问 1=在栈上 2=已完成
    for start in parts:
        if color.get(start):
            continue
        color[start] = 1
        stack = [(start, iter(children.get(start) or []))]
        path = [start]
        while stack:
            node, it = stack[-1]
            pushed = False
            for comp, _per in it:
                comp_color = color.get(comp, 0)
                if comp_color == 0:
                    color[comp] = 1
                    stack.append((comp, iter(children.get(comp) or [])))
                    path.append(comp)
                    pushed = True
                    break
                if comp_color == 1:
                    return path[path.index(comp):]
            if not pushed:
                color[node] = 2
                stack.pop()
                path.pop()
    return None


def explode(conn, product, quantity, date_str):
    """展开一张工单，返回 (基础件总用量 dict, 涉及料号集合)。

    用量合计所有路径，但计算在料号节点上聚合，不逐条展开路径。
    用料成环时抛出 CycleError。
    """
    # 1) 自成品向外取各层用料单，建立展开图
    children = {}  # 料号 -> [(子件, 单套用量)]，None 表示基础件
    involved = {product}
    stack = [product]
    while stack:
        part = stack.pop()
        if part in children:
            continue
        bom = get_effective_bom(conn, part, date_str)
        children[part] = bom
        if bom:
            for comp, _ in bom:
                if comp not in involved:
                    involved.add(comp)
                    stack.append(comp)

    # 2) 成环检测
    cycle = _find_cycle(involved, children)
    if cycle:
        raise CycleError(cycle)

    # 3) 拓扑序上聚合需求量（Kahn），基础件落账
    indegree = {p: 0 for p in involved}
    for bom in children.values():
        if bom:
            for comp, _ in bom:
                indegree[comp] += 1
    demand = {product: float(quantity)}
    usage = {}
    queue = deque(p for p in involved if indegree[p] == 0)
    while queue:
        part = queue.popleft()
        qty = demand.get(part, 0.0)
        bom = children[part]
        if bom is None:
            if qty:
                usage[part] = usage.get(part, 0.0) + qty
            continue
        for comp, per in bom:
            demand[comp] = demand.get(comp, 0.0) + qty * per
            indegree[comp] -= 1
            if indegree[comp] == 0:
                queue.append(comp)
    return usage, involved


def _save_usage(conn, wo_id, usage, involved):
    conn.execute("DELETE FROM work_order_usage WHERE work_order_id = ?", (wo_id,))
    conn.execute("DELETE FROM work_order_parts WHERE work_order_id = ?", (wo_id,))
    conn.executemany(
        "INSERT INTO work_order_usage (work_order_id, part_no, total_qty) VALUES (?, ?, ?)",
        [(wo_id, p, q) for p, q in usage.items()],
    )
    conn.executemany(
        "INSERT INTO work_order_parts (work_order_id, part_no) VALUES (?, ?)",
        [(wo_id, p) for p in involved],
    )


def create_work_order(conn, order_no, assembly_date, product, quantity):
    """登记工单并核算。成环时抛 CycleError，不落任何数据。"""
    usage, involved = explode(conn, product, quantity, assembly_date)
    with conn:
        cur = conn.execute(
            "INSERT INTO work_orders (order_no, assembly_date, product, quantity) "
            "VALUES (?, ?, ?, ?)",
            (order_no, assembly_date, product, quantity),
        )
        _save_usage(conn, cur.lastrowid, usage, involved)
        return cur.lastrowid


def upsert_bom(conn, part_no, effective_date, items):
    """按料号+生效日整份登记或更正用料单，并重核受影响的工单。

    只读取并重核本单有效期 [生效日, 下一张同料号用料单生效日) 内、
    展开时涉及该料号的工单（包括原先把它算作基础件的）。
    任一工单重核失败（如用料成环）时整体回滚，沿用原单和原结果。
    返回实际重核的工单号列表。
    """
    with conn:
        row = conn.execute(
            "SELECT id FROM boms WHERE part_no = ? AND effective_date = ?",
            (part_no, effective_date),
        ).fetchone()
        if row is None:
            bom_id = conn.execute(
                "INSERT INTO boms (part_no, effective_date) VALUES (?, ?)",
                (part_no, effective_date),
            ).lastrowid
        else:  # 更正：整份替换明细
            bom_id = row["id"]
            conn.execute("DELETE FROM bom_items WHERE bom_id = ?", (bom_id,))
        conn.executemany(
            "INSERT INTO bom_items (bom_id, component, qty_per) VALUES (?, ?, ?)",
            [(bom_id, comp, per) for comp, per in items],
        )

        # 本单有效期上界：同料号下一张用料单的生效日
        nxt = conn.execute(
            "SELECT MIN(effective_date) AS d FROM boms "
            "WHERE part_no = ? AND effective_date > ?",
            (part_no, effective_date),
        ).fetchone()["d"]
        sql = (
            "SELECT w.id, w.order_no, w.assembly_date, w.product, w.quantity "
            "FROM work_orders w "
            "JOIN work_order_parts p ON p.work_order_id = w.id "
            "WHERE p.part_no = ? AND w.assembly_date >= ?"
        )
        params = [part_no, effective_date]
        if nxt is not None:
            sql += " AND w.assembly_date < ?"
            params.append(nxt)

        recomputed = []
        for wo in conn.execute(sql, params).fetchall():
            try:
                usage, involved = explode(
                    conn, wo["product"], wo["quantity"], wo["assembly_date"])
            except CycleError as exc:
                exc.order_no = wo["order_no"]
                raise
            _save_usage(conn, wo["id"], usage, involved)
            recomputed.append(wo["order_no"])
        return recomputed
