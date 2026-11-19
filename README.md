# 空运危险品保管链 API

面向断网后乱序补传场景的保管链（chain of custody）服务。数据员可以据此判断：
在某个时刻之前，哪些交接仍然可信、断链发生在哪里、当前可确认的保管方是谁。

技术栈：**FastAPI + SQLite（WAL 模式）+ Uvicorn**，可通过 `docker compose` 一键启动。

## 核心规则

1. **固定起始保管方**：创建运单时登记 `origin_party`，初始可确认保管方即该方；
   起始保管方以原登记的字面名称为准，字典替换不会改写它。
2. **批量提交**：每笔扫描携带 `scan_id`、正整数 `revision`、UTC 时刻 `ts_utc`、
   交出方 `giver`、接收方 `receiver`。时刻必须带时区偏移（如 `...Z`、`+08:00`），统一归一化为 UTC 存储。
3. **撤回**：只提交 `scan_id` 与更高的 `revision`，`event_type=WITHDRAWAL`，
   不得携带时刻或当事方；撤回只能针对已存在的扫描 ID。
4. **修订裁决**：
   - 同一 `scan_id` 仅采用**最高修订号**的版本；更低修订号提交返回 `409 stale`。
   - 同修订号、异文（时刻/交出方/接收方/类型不同）**整批拒绝**（`409`），已提交数据原样保留。
   - 同修订号、同文重复提交幂等，不计入写入数。
5. **简称字典**（同一简称可能指几家承运人）：
   - `PUT /api/aliases/{alias}` **整组替换**一个简称对应的正式保管方；空列表即注销。
     替换在单个 `BEGIN IMMEDIATE` 事务内完成：成功后按新字典重新核链，失败保留原字典与原结论。
   - **未登记的名称按正式名处理**，候选即其自身；已登记简称解析为该组全部正式保管方。
   - 字典带单调递增的 `dictionary_revision`，持久化在库中，**重启后按已提交的字典核链**。
6. **链核对**（按 UTC 时刻升序，乱序补传自动归位，字典与扫描取自同一已提交快照）：
   - 每笔扫描的交出方/接收方先解析为**候选列表**；本笔的**交出候选须包含当前保管方**
     （首笔对照起始保管方，其后对照上一笔的确认接收方），不包含即为 **GAP 断链**。
   - 接收候选唯一时直接确认接收方；**接收候选有多个时，只用紧接的下一时刻作旁证**：
     该时刻须**恰有一笔有效扫描**，且它的交出候选与本笔接收候选**只有一家重合**，
     才能确认接收方；否则从本笔 **AMBIGUOUS 断链**，并列出候选与原因。
   - **同一时刻存在多笔交接时不猜测顺序**：该时刻整组标记为 **SIMULTANEOUS** 断链，组内全部未证实。
   - 断链之后的所有扫描一律 `verified=false`，直到补报、更正或撤回补齐缺口；
     重算后断链位置可以前移、后移或消失。
   - 旁证扫描被**更正或撤回**后，前一笔会在下次核链时重新判定。
   - 查链与批次回执给出每笔的**确认交出/接收方、旁证扫描 ID 及其修订号**；
     无法确认时给出候选列表与 `unverified_detail` 原因。
7. **原子性与一致性**：
   - 每个写批次在单个 `BEGIN IMMEDIATE` 事务内完成，任何冲突整批回滚，**失败保留旧链/旧字典**。
   - 查链在单个只读事务内读取同一已提交快照（WAL），字典与扫描不会跨状态混合；
     批次回执对应本批提交后的已提交状态，并携带该状态的 `dictionary_revision`。
   - 数据持久化在 SQLite 文件中，进程重启后状态不变。原始扫描与同文重传规则照旧。

## 启动方式

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务： http://localhost:8000
# 文档： http://localhost:8000/docs
# 控制台：http://localhost:8000/
```

数据库保存在命名卷 `chain-data`（容器内 `/data/chain.db`），容器重启不丢数据。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 可选：APP_DB_PATH=/custom/path/chain.db
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/waybills` | 创建运单并固定起始保管方 |
| GET | `/api/waybills` | 列出全部运单 |
| POST | `/api/waybills/{waybill_id}/scans/batch` | 批量提交交接 / 更正 / 撤回 |
| GET | `/api/waybills/{waybill_id}/chain` | 查询可信前缀、断链原因、当前保管方 |
| GET | `/api/aliases` | 列出简称字典及字典修订号 |
| GET | `/api/aliases/{alias}` | 查询单个简称对应的正式保管方（未登记 404） |
| PUT | `/api/aliases/{alias}` | 整组替换简称对应的正式保管方（空列表注销） |
| DELETE | `/api/aliases/{alias}` | 注销简称（未登记 404） |
| GET | `/docs` | Swagger UI，可直接录入与查询 |

### 交接批次示例

```json
{
  "scans": [
    {"scan_id": "S1", "revision": 1, "ts_utc": "2026-11-18T08:00:00Z",
     "giver": "HKG-WAREHOUSE", "receiver": "CX-CARGO"},
    {"scan_id": "S2", "revision": 1, "ts_utc": "2026-11-18T10:30:00Z",
     "giver": "CX-CARGO", "receiver": "PVG-WAREHOUSE"}
  ]
}
```

### 撤回示例（修订号必须更高）

```json
{"scans": [{"scan_id": "S2", "revision": 2, "event_type": "WITHDRAWAL"}]}
```

### 简称字典示例

```bash
# 一个简称指两家承运人：整组替换
curl -X PUT http://localhost:8000/api/aliases/CARRIERS \
  -H 'Content-Type: application/json' \
  -d '{"formal_parties": ["CARRIER-A", "CARRIER-B"]}'
# -> {"alias":"CARRIERS","formal_parties":["CARRIER-A","CARRIER-B"],"dictionary_revision":1}

# 注销：之后 CARRIERS 作为未登记正式名处理
curl -X PUT http://localhost:8000/api/aliases/CARRIERS \
  -H 'Content-Type: application/json' -d '{"formal_parties": []}'
```

### 查询响应示例（发生断链时）

```json
{
  "waybill_id": "AWB-001",
  "origin_party": "HKG-WAREHOUSE",
  "current_custodian": "CX-CARGO",
  "dictionary_revision": 3,
  "verified_prefix": [
    {
      "scan_id": "S1", "revision": 1, "verified": true,
      "giver": "HKG-WAREHOUSE", "receiver": "CARRIERS",
      "giver_candidates": ["HKG-WAREHOUSE"],
      "receiver_candidates": ["CARRIER-A", "CARRIER-B"],
      "confirmed_giver": "HKG-WAREHOUSE", "confirmed_receiver": "CARRIER-A",
      "corroborating_scan_id": "S2", "corroborating_revision": 1
    }
  ],
  "unverified_scans": [
    {"scan_id": "S3", "verified": false, "unverified_reason": "AMBIGUOUS",
     "receiver_candidates": ["CARRIER-A", "CARRIER-B"],
     "unverified_detail": "receiver 'CARRIERS' resolves to candidates ...; "
}
  ],
  "chain_intact": false,
  "break_reason": "AMBIGUOUS",
  "break_detail": "...",
  "break_scan_ids": ["S3"]
}
```

`break_reason` 取值：`NONE` / `GAP`（交出候选不含当前保管方）/
`SIMULTANEOUS`（同时刻多笔，顺序不可判定）/
`AMBIGUOUS`（接收候选多个且旁证缺失、不唯一或无法唯一重合）。
`current_custodian` 只沿已证实交接推进，因此断链后仍可信地停留在最后一个已确认接收方。

## 测试

```bash
pip install pytest httpx
pytest -q
```

测试覆盖：乱序补传归位、时区归一化、最高修订裁决、同修订异文整批回滚、
撤回与补报修复断链、GAP/SIMULTANEOUS/AMBIGUOUS 检测、输入校验、真实线程下的写串行化、
交错读不到混合修订、进程重启持久化，以及简称字典的整组替换/注销/重启保持、
多候选的紧接下一时刻旁证确认、旁证更正/撤回触发前一笔重新判定。
另可用 `python tests/smoke_http.py` 对运行中的实例做端到端 HTTP 冒烟。
