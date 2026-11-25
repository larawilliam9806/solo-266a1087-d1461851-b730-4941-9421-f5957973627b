# 工单标准耗料核对接口

成本组核对工单标准耗料的供数接口。FastAPI + SQLite，支持 docker compose 启动。

## 业务规则

- **工单**：填单号、装配日、成品料号、产量。登记时即核算并保存各基础件总用量。
- **用料单**：按 `料号 + 生效日` 整份登记或更正（同键重复提交即整份替换明细），明细为子件料号和单套用量。
- **展开口径**：各层按工单装配日取"当天或之前最近已生效"的用料单；无单的料号算基础件。
- **用量核算**：按工单查各基础件总用量，合计所有路径的用量；计算在料号节点上聚合
  （拓扑排序逐层汇总，O(料号数 + 明细行数)），不逐条展开路径。
- **补登/更正用料单**：只读取并重核本单有效期 `[生效日, 同料号下一张单生效日)` 内、
  展开时涉及该料号的工单（包括原先把它算作基础件的工单），其余工单不动。
- **成环与失败回退**：工单用料成环即退回，响应列出环上的料号；核算或保存失败时
  整体回滚，沿用原单和原结果。

## 启动

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，接口文档 http://localhost:8000/docs
```

本地开发：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest            # 运行测试
```

数据库文件默认在 `data/app.db`（容器内 `/data/app.db`，由卷 `app-data` 持久化），
可用环境变量 `APP_DB_PATH` 覆盖。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/work-orders` | 登记工单并核算。成环返回 422 并列出环上料号；单号重复返回 409 |
| GET | `/work-orders` | 工单列表 |
| GET | `/work-orders/{order_no}` | 工单详情 |
| GET | `/work-orders/{order_no}/usage` | 该工单各基础件总用量 |
| PUT | `/boms` | 按料号+生效日整份登记/更正用料单，自动重核受影响工单；失败回滚并返回 422 |
| GET | `/boms/{part_no}` | 该料号各生效日版本列表 |
| GET | `/boms/{part_no}/{effective_date}` | 某版用料单明细 |

### 示例

```bash
# 登记用料单
curl -X PUT localhost:8000/boms -H 'Content-Type: application/json' -d '{
  "part_no": "CAR", "effective_date": "2024-01-01",
  "items": [{"component": "WHEEL", "qty_per": 4}, {"component": "AXLE", "qty_per": 2}]
}'

# 登记工单
curl -X POST localhost:8000/work-orders -H 'Content-Type: application/json' -d '{
  "order_no": "WO-1001", "assembly_date": "2024-06-01",
  "product": "CAR", "quantity": 10
}'

# 查工单各基础件总用量
curl localhost:8000/work-orders/WO-1001/usage
```

成环时的响应（工单登记或用料单保存均可能返回）：

```json
{
  "detail": {
    "message": "重核时用料成环，保存失败，已沿用原单和原结果",
    "cycle": ["STEEL", "CAR", "WHEEL"],
    "order_no": "WO-1001"
  }
}
```

## 结构

```
app/
  main.py     # 路由与请求模型
  service.py  # 展开聚合、成环检测、重核与回滚
  db.py       # SQLite 连接与建表
tests/test_app.py
Dockerfile / docker-compose.yml
```
