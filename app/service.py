"""用料展开、成环检测、一致性校验与重核逻辑。

展开算法按"料号节点"聚合，不逐条展开路径：
先按装配日确定每个料号最近已生效的用料单（无单即基础件），
再对展开图做拓扑排序，把需求量自顶向下逐层汇总——同一料号在
不同路径下的用量在节点处直接相加。复杂度 O(料号数 + 明细行数)，
与路径条数无关。

并发一致性
----------
一张工单展开时，各层用料单必须来自同一个已提交的数据库快照：
``create_work_order`` 先在一个只读事务里完成整棵展开，并记录每个
料号"装配日实际选中的用料单/明细（含无单这一事实）"作为取数指纹；
随后在 ``BEGIN IMMEDIATE`` 写事务里校验指纹是否仍是最新已提交状态。

* 指纹不变：期间补登/更正的用料单不改变本工单取数，直接入账，
  无需重新展开；
* 指纹已变（包括原来无单的基础件在装配日有了生效单）：按新数据
  重新展开、重新核环，直到取数稳定后再入账；
* 成环时同样先校验指纹：若环来自过期快照则按新数据重核，只有在
  持锁事务里仍确认成环时才按原接口列出环上料号退回。

校验与工单/耗料/涉及料号的写入在同一个 IMMEDIATE 事务内完成，
因此成功时一并入账，任何失败整体回滚，不留下本次登记的数据。
"""

import sqlite3
from collections import deque

# 并发补登导致取数持续变化时的最大重核次数（正常一次冲突后即稳定）。
_MAX_ATTEMPTS = 50

# explode() 与 create_work_order() 之间暂存取数指纹。
# sqlite3.Connection 不能挂自定义属性也不能弱引用，故以 id(conn) 为键；
# 连接为请求级独占、且取用前后都会 pop，不会串单。
_read_sets: dict[int, dict] = {}


class CycleError(Exception):
    """工单用料成环。cycle 为环上的料号（按环的顺序）。"""

    def __init__(self, cycle):
        self.cycle = list(cycle)
        self.order_no = None  # 重核场景下记录触发成环的工单号
        self.read_set = None  # 成环快照对应的取数指纹，供并发校验
        super().__init__("用料成环: " + " -> ".join(self.cycle + self.cycle[:1]))


class _BomRows(list):
    """明细列表，额外携带所选中的 boms.id。

    与普通 list 完全相等/可迭代，保持 get_effective_bom 的既有返回契约。
    """

    def __init__(self, items, bom_id):
        super().__init__(items)
        self.bom_id = bom_id


# ---------------------------------------------------------------------------
# 事务辅助
# ---------------------------------------------------------------------------

def _begin(conn, immediate=False):
    """开启由本调用负责的事务。

    连接处于 autocommit（app.db.connect 设置 isolation_level=None）时
    直接下发 BEGIN；若调用方已经开了事务（如测试用 with conn 包裹），
    则不重复开启，并返回 False 表示事务不由本函数提交/回滚。
    """
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        return True
    return False


def _rollback_if_owned(conn, owned):
    if owned and conn.in_transaction:
        conn.rollback()


def _commit_if_owned(conn, owned):
    if owned:
        conn.commit()


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------

def _fetch_selection(conn, part_no, date_str):
    """装配日当天或之前最近生效的用料单。

    返回 (bom_id, items)；无单时为 (None, None)。
    items 为 [(component, qty_per), ...]（保持查询的自然顺序）。
    """
    row = conn.execute(
        "SELECT id FROM boms WHERE part_no = ? AND effective_date <= ? "
        "ORDER BY effective_date DESC LIMIT 1",
        (part_no, date_str),
    ).fetchone()
    if row is None:
        return None, None
    bom_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
    rows = conn.execute(
        "SELECT component, qty_per FROM bom_items WHERE bom_id = ?",
        (bom_id,),
    ).fetchall()
    items = [(r["component"], r["qty_per"]) for r in rows]
    return bom_id, items


def get_effective_bom(conn, part_no, date_str):
    """取装配日当天或之前最近生效的用料单；无单返回 None（按基础件计）。"""
    bom_id, items = _fetch_selection(conn, part_no, date_str)
    if bom_id is None:
        return None
    return _BomRows(items, bom_id)


def _fingerprint_of(bom):
    """把一次取数结果转成可比较的指纹条目。

    条目为 (bom_id, items_signature)：
    * 无单 -> (None, None)，用于识别"基础件新有了生效单"；
    * 有单 -> bom_id 与排序后的明细签名。即使外部把 get_effective_bom
      替换成返回普通 list 的包装，也能退化为只比对明细签名。
    """
    if bom is None:
        return (None, None)
    bom_id = getattr(bom, "bom_id", None)
    signature = tuple(sorted((comp, float(qty)) for comp, qty in bom))
    return (bom_id, signature)


def _selection_changed(conn, part_no, date_str, fingerprint):
    """在最新已提交状态下复核单个料号的取数指纹是否已变化。"""
    old_id, old_sig = fingerprint
    new_id, items = _fetch_selection(conn, part_no, date_str)
    if old_id is None and old_sig is None:
        # 快照里无单（基础件）：现在选中了任一用料单即视为变化。
        return new_id is not None
    if new_id is None:
        return True  # 原有单现在选不到了（理论上不会物理删除，稳妥处理）
    if old_id is not None and old_id != new_id:
        return True  # 装配日改选了另一版用料单
    new_sig = tuple(sorted((comp, float(qty)) for comp, qty in items))
    return old_sig != new_sig  # 同一张单的明细被更正


def _read_set_changed(conn, read_set, date_str):
    for part_no, fingerprint in read_set.items():
        if _selection_changed(conn, part_no, date_str, fingerprint):
            return True
    return False


# ---------------------------------------------------------------------------
# 展开与成环检测
# ---------------------------------------------------------------------------

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

    本函数保持无事务、纯读取；需要"同一个已提交状态"时由调用方
    在外层开启一个事务。展开所用的取数指纹会暂存，供
    create_work_order 做入账前的并发校验。
    """
    # 1) 自成品向外取各层用料单，建立展开图
    children = {}  # 料号 -> [(子件, 单套用量)]，None 表示基础件
    read_set = {}  # 料号 -> 取数指纹
    involved = {product}
    stack = [product]
    while stack:
        part = stack.pop()
        if part in children:
            continue
        bom = get_effective_bom(conn, part, date_str)
        read_set[part] = _fingerprint_of(bom)
        children[part] = bom
        if bom:
            for comp, _ in bom:
                if comp not in involved:
                    involved.add(comp)
                    stack.append(comp)

    _read_sets[id(conn)] = read_set

    # 2) 成环检测
    cycle = _find_cycle(involved, children)
    if cycle:
        err = CycleError(cycle)
        err.read_set = read_set
        err.own_explode = True  # 标记为本轮展开自身发现的环
        raise err

    # 3) 拓扑序上聚合需求量（Kahn），基础件落账
    usage = _aggregate(children, involved, product, quantity)
    return usage, involved


def _aggregate(children, involved, product, quantity):
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
    return usage


# 保留一份不经过 monkeypatch 全局替换的真实展开入口，供缺少指纹时兜底。
_explode_impl = explode


def _snapshot_explode(conn, product, quantity, date_str):
    """在单一已提交快照内完成展开。

    返回 (read_set, cycle_error, candidate)。
    candidate 为 explode 的 (usage, involved)；成环时为 None。
    read_set 为 None 表示 explode 被整体替换且未留下指纹，
    调用方需在持写锁事务内做权威重算。

    若抛出的是注入逻辑里夹带的外来异常（不是本轮展开自身的成环），
    结束快照事务后原样抛出，不把它误判成本工单的环。
    """
    _read_sets.pop(id(conn), None)
    opened = _begin(conn)  # deferred BEGIN：第一条 SELECT 确定快照
    try:
        cycle_error = None
        candidate = None
        try:
            candidate = explode(conn, product, quantity, date_str)
        except CycleError as exc:
            if not getattr(exc, "own_explode", False):
                raise
            cycle_error = exc

        stashed = _read_sets.pop(id(conn), None)
        if cycle_error is not None:
            read_set = getattr(cycle_error, "read_set", None) or stashed
        else:
            read_set = stashed

        # 展开为只读事务，提交仅用于释放快照/锁。
        if opened and conn.in_transaction:
            conn.commit()
        return read_set, cycle_error, candidate
    except BaseException:
        # 外来异常路径：不提交可能夹带的同连接写入，回滚本快照事务。
        if opened and conn.in_transaction:
            conn.rollback()
        raise


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
    """登记工单并核算。

    入账前做乐观并发校验：若展开所选中的用料单/明细在期间已被
    补登或更正（含基础件新有生效单），按最新已提交数据重核；
    环的退回也以最新状态复核后的结果为准。
    成功时工单、耗料、涉及料号在同一事务内一并入账；任何失败整体回滚。
    """
    attempts = 0
    while True:
        attempts += 1
        read_set, cycle_error, candidate = _snapshot_explode(
            conn, product, quantity, assembly_date)

        opened = _begin(conn, immediate=True)
        committed = False
        try:
            if read_set is None:
                # 没有取数指纹（explode 被整体替换）：持写锁权威重算，
                # 其结果即最新稳定状态，直接据此入账/退环。
                try:
                    usage, involved = _explode_impl(
                        conn, product, quantity, assembly_date)
                except CycleError as exc:
                    exc.order_no = getattr(cycle_error, "order_no", None)
                    raise
            else:
                if _read_set_changed(conn, read_set, assembly_date):
                    _rollback_if_owned(conn, opened)
                    if attempts >= _MAX_ATTEMPTS:
                        raise sqlite3.OperationalError(
                            "用料单并发变更持续发生，工单登记重核次数超限")
                    continue
                if cycle_error is not None:
                    # 指纹未变 => 环来自当前已提交状态，按原接口退回。
                    raise cycle_error
                usage, involved = candidate

            cur = conn.execute(
                "INSERT INTO work_orders "
                "(order_no, assembly_date, product, quantity) "
                "VALUES (?, ?, ?, ?)",
                (order_no, assembly_date, product, quantity),
            )
            _save_usage(conn, cur.lastrowid, usage, involved)
            _commit_if_owned(conn, opened)
            committed = True
            return cur.lastrowid
        finally:
            # 未提交（异常或 continue 前未自行回滚）时兜底回滚。
            if not committed and opened and conn.in_transaction:
                conn.rollback()


def upsert_bom(conn, part_no, effective_date, items):
    """按料号+生效日整份登记或更正用料单，并重核受影响的工单。

    只读取并重核本单有效期 [生效日, 下一张同料号用料单生效日) 内、
    展开时涉及该料号的工单（包括原先把它算作基础件的）。
    任一工单重核失败（如用料成环）时整体回滚，沿用原单和原结果。
    返回实际重核的工单号列表。
    """
    opened = _begin(conn, immediate=True)
    try:
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
        sql += " ORDER BY w.id"

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

        _commit_if_owned(conn, opened)
        return recomputed
    except Exception:
        _rollback_if_owned(conn, opened)
        raise
