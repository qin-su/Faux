# Smart Faux Server 设计方案

> 版本: 1.2 | 日期: 2026-09-22

## 1. 项目概述

Smart Faux Server 是一个独立的 HTTP 服务，专为 ESB（企业服务总线）测试场景设计。它集成了**模拟目标系统**、**实收验证**、**故障注入**、**DB 断言代理**、**夹具管理**和 **MQ 消费测试**六大能力，通过统一的 REST API 对外暴露，同时提供 Web UI 供人工测试。

### 核心价值

| 能力 | 说明 |
|------|------|
| 多目标投递 | 支持多个模拟目标端点（`/api/echo`, `/api/echo2`, `/api/echo3`），适配 ESB 一对多/一对一路由 |
| 实收验证 | 所有收到的消息持久化到 SQLite，按 marker 计数、按路径过滤 |
| 故障注入 | 按目标独立配置 success/fail/timeout 概率 + 延迟，运行时动态切换 |
| DB 断言 | 代理查询 ESB PostgreSQL 的 `message_trace` / `routing_detail`，支持轮询等待重试完成 |
| 夹具管理 | 通过 ESB Admin API 创建/清理 SIT 测试夹具 |
| AI 友好 | 全部 REST API 返回 JSON，AI 用 curl 即可完成全链路测试 |
| UI 展示 | 内嵌 Web Dashboard，人工测试时浏览器打开即可 |
| MQ 消费测试 | 模拟 RabbitMQ 消费者，消费 ESB 投递的 MQ 消息并回复 CONSUMED/FAILED |

## 2. 架构设计

```
                          ┌──────────────────────────────────────────────────┐
                          │          Smart Faux Server (FastAPI)             │
                          │          端口: 8081 (可配置)                       │
                          │                                                   │
  ESB 投递 ──────────────►│  ── 目标端点 (ESB 路由目标) ──                     │
  (ingress /api/echo)     │  POST /api/echo        目标1 (本地 127.0.0.1)     │
                          │  POST /api/echo2       目标2 (模拟 192.168.2.206) │
                          │  POST /api/echo3       目标3 (模拟 206:5173)      │
                          │  POST /api/sit-not-found  返回 404               │
                          │  ANY  /api/{catch_all}  通用记录目标              │
                          │                                                   │
  Bruno / AI ───────────►│  ── 管理 API ──                                    │
  (curl / Bruno)          │  GET  /api/logs           查实收消息列表          │
                          │  GET  /api/logs/count     按 marker 计数         │
                          │  GET  /api/logs/stats     统计摘要               │
                          │  POST /api/logs/reset     清空记录               │
                          │  GET  /api/mode            查所有目标故障模式    │
                          │  PUT  /api/mode/{target}   设置故障注入           │
                          │  POST /api/mode/reset      重置故障模式          │
                          │                                                   │
                          │  ── 夹具管理 API ──                               │
                          │  POST /api/fixtures/install   创建 SIT 夹具       │
                          │  POST /api/fixtures/cleanup   清理 SIT 夹具       │
                          │  GET  /api/fixtures/status    检查夹具状态        │
                          │                                                   │
                          │  ── ESB 操作 API ──                               │
                          │  POST /api/esb/send           发送消息到 ESB     │
                          │  GET  /api/esb/config         读取 ESB 配置       │
                          │  PUT  /api/esb/config         修改 ESB 配置       │
                          │                                                   │
                          │  ── UI ──                                         │
                          │  GET  /               Web Dashboard              │
                          │  GET  /docs           Swagger API 文档           │
                          │                                                   │
                          │  存储: SQLite (mock13.db)                         │
                          │  代理: psycopg2 → ESB PostgreSQL (5432)           │
                          └──────────────────────────────────────────────────┘
```

## 3. 技术选型

| 组件 | 选择 | 理由 |
|------|------|------|
| Web 框架 | **FastAPI** | 异步支持长轮询、Swagger UI 自动生成、Pydantic 类型校验 |
| 存储 | **SQLite** | 零配置、单文件持久化、Python 内置 |
| DB 代理 | **psycopg2** | 与现有 `run_sit.py` 同驱动，直连 ESB PostgreSQL |
| HTTP 客户端 | **httpx** | FastAPI 生态原生支持、同步+异步双模式 |
| UI | **内嵌 HTML** | 无需前端构建工具，单文件交付 |

## 4. API 设计

### 4.1 目标端点（ESB 路由投递目标）

ESB 配置路由规则时，target 指向这些端点。所有端点收到请求后：
1. 记录到 SQLite（method, path, headers, body, marker, timestamp）
2. 查询该目标的故障模式
3. 按概率决定返回成功/失败/超时
4. 返回响应并记录返回的 status_code

| 方法 | 路径 | 默认行为 | 说明 |
|------|------|----------|------|
| POST/PUT | `/api/echo` | 200 | 主目标（本地） |
| POST/PUT | `/api/echo2` | 200 | 第二目标（模拟 206） |
| POST/PUT | `/api/echo3` | 200 | 第三目标（模拟 206:5173） |
| POST/PUT | `/api/sit-not-found` | 404 | 专门返回 404 的目标 |
| ANY | `/api/{path}` | 200 | 通用 catch-all，记录后返回 200 |

### 4.2 实收验证 API

```
GET /api/logs?marker=xxx&path=/api/echo&limit=100&offset=0
  → { "total": 42, "items": [{ id, ts, target, method, path, body, marker, status_code, response_type }] }

GET /api/logs/count?marker=xxx
  → { "marker": "xxx", "count": 3 }

GET /api/logs/stats
  → { "total": 42, "by_target": { "/api/echo": 30, "/api/echo2": 12 }, "by_status": { "200": 38, "500": 4 } }

POST /api/logs/reset
  → { "deleted": 42 }
```

### 4.3 故障注入 API

```
GET /api/mode
  → { "modes": [{ target: "echo", success_rate: 100, fail_rate: 0, timeout_rate: 0, latency_ms: 0 }] }

GET /api/mode/{target}
  → { target: "echo", success_rate: 100, fail_rate: 0, timeout_rate: 0, latency_ms: 0 }

PUT /api/mode/{target}
  Body: { "success_rate": 70, "fail_rate": 20, "timeout_rate": 10, "latency_ms": 500 }
  → { target: "echo", success_rate: 70, fail_rate: 20, timeout_rate: 10, latency_ms: 500, updated_at: "..." }

POST /api/mode/reset
  → { "reset": ["echo", "echo2", "echo3"] }
```

### 4.4 DB 代理 API（已移除）

> 2026-09 移除：faux 定位为独立 mock 工程，不直连 ESB 业务库。
> 排障请直接使用 ESB 侧工具或数据库客户端。实收消息的检索由
> `/api/logs`（全字段模糊过滤）承担。

### 4.5 夹具管理 API

```
POST /api/fixtures/install
  Body: { "systems": [...], "msg_types": [...] }
  → { "installed": { "systems": 3, "msg_types": 5 }, "errors": [] }

POST /api/fixtures/cleanup
  → { "deleted": { "routing_rule": 10, "message_type": 5, "business_system": 3 } }

GET /api/fixtures/status
  → { "systems": [{ code: "SIT_SYS", exists: true }], "msg_types": [{ code: "SIT_MSG", exists: true }] }
```

### 4.6 ESB 操作 API

```
POST /api/esb/send
  Body: { "system": "SIT_SYS", "ak": "xxx", "sk": "xxx", "msg_type": "SIT_MSG", "body": {...} }
  → { "code": 0, "data": { "messageId": "...", "status": "...", "totalTargets": 3, "successCount": 2, "failCount": 1 } }

GET /api/esb/config
  → { "retryEnabled": "true", "maxRetryCount": "3", ... }

PUT /api/esb/config
  Body: { "retryEnabled": "false" }
  → { "code": 0, "msg": "success" }
```

## 5. 数据模型

### SQLite 表结构

```sql
-- 收到的消息记录
CREATE TABLE IF NOT EXISTS received_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,           -- ISO 时间戳
    target        TEXT    NOT NULL,           -- 目标标识 (echo / echo2 / echo3 / sit-not-found)
    method        TEXT    NOT NULL,           -- HTTP 方法
    path          TEXT    NOT NULL,           -- 请求路径
    content_type  TEXT,                      -- Content-Type 头
    headers       TEXT,                      -- 全部请求头 (JSON)
    body          TEXT,                      -- 请求体
    body_len      INTEGER DEFAULT 0,         -- 请求体长度
    marker        TEXT,                      -- 从 body 中提取的 marker (用于计数)
    status_code   INTEGER,                   -- 返回的 HTTP 状态码
    response_type TEXT                       -- 响应类型 (success / fail / timeout / slow)
);

-- 故障注入模式（按目标独立配置）
CREATE TABLE IF NOT EXISTS fault_mode (
    target          TEXT PRIMARY KEY,        -- 目标标识
    success_rate    INTEGER DEFAULT 100,      -- 成功概率 (0-100)
    fail_rate       INTEGER DEFAULT 0,        -- 失败概率 (0-100)
    timeout_rate    INTEGER DEFAULT 0,        -- 超时概率 (0-100)
    latency_ms      INTEGER DEFAULT 0,        -- 成功时附加延迟 (毫秒)
    timeout_seconds INTEGER DEFAULT 30,      -- 超时时睡眠秒数
    updated_at      TEXT                      -- 最后修改时间
);

-- ESB 配置缓存（可选，用于 /api/esb/config 读写）
CREATE TABLE IF NOT EXISTS esb_config_cache (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_msg_marker ON received_messages(marker);
CREATE INDEX IF NOT EXISTS idx_msg_target ON received_messages(target);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON received_messages(ts);
```

## 6. 故障注入逻辑

```python
import random

def apply_fault_mode(target: str) -> tuple[int, dict, str]:
    """
    按目标的故障模式配置，决定本次请求的响应。
    返回 (status_code, response_body, response_type)
    """
    mode = get_fault_mode(target)

    # 概率分配：fail > timeout > success
    r = random.randint(1, 100)

    if r <= mode.fail_rate:
        return 500, {"error": "injected fail", "target": target}, "fail"

    if r <= mode.fail_rate + mode.timeout_rate:
        time.sleep(mode.timeout_seconds)
        return 504, {"error": "timeout", "target": target}, "timeout"

    if mode.latency_ms > 0:
        time.sleep(mode.latency_ms / 1000)

    return 200, {"status": "ok", "target": target}, "success"
```

**约束**: `success_rate + fail_rate + timeout_rate` 应等于 100。若不等于，按 fail > timeout > success 优先级分配。

## 7. 配置管理

通过 `.env` 文件或环境变量配置：

```ini
# Smart Faux 自身
FAUX_PORT=8081
FAUX_DB_PATH=./mock13.db

# ESB Ingress（消息投递入口）
ESB_INGRESS=http://127.0.0.1:48080

# ESB Admin（管理端 API）
ESB_ADMIN=http://127.0.0.1:48081
ESB_ADMIN_USER=admin
ESB_ADMIN_PASSWORD=admin123

# ESB PostgreSQL（DB 代理）
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=esb
DB_USER=esb
DB_PASSWORD=esb123

# 默认测试账号（用于 /api/esb/send）
DEFAULT_AK=test_ak
DEFAULT_SK=test_sk
DEFAULT_SYSTEM=SIT_SYS
```

## 7.5 MQ 消费测试

### 7.5.1 背景

ESB 支持三种目标类型：HTTP、SOAP、MQ（RabbitMQ）。MQ 投递流程：

```
ESB MqDispatcher → RabbitMQ 队列(routing_rule.target) → 消费者消费 → 回复 esb.status.{system} → ESB StatusReplyListener 更新状态
```

Smart Faux Server 可作为 **MQ 消费者**，模拟目标系统消费 ESB 投递的消息并回复确认。

### 7.5.2 架构

```
                          ┌──────────────────────────────────────────────────┐
                          │          Smart Faux Server (FastAPI)             │
                          │                                                   │
  ESB MqDispatcher        │  ── RabbitMQ 消费者 (独立线程) ──                 │
  投递到队列 ──────────────►│  consume(queue) → 记录 SQLite → 故障注入 → 回复    │
                          │  回复到 esb.status.{system} 队列                  │
                          │                                                   │
  Bruno / AI ───────────►│  ── MQ 管理 API ──                                │
  (curl / Bruno)          │  GET  /api/mq/status         消费者状态            │
                          │  POST /api/mq/start           启动消费者            │
                          │  POST /api/mq/stop            停止消费者            │
                          │  POST /api/mq/subscribe       订阅队列             │
                          │  DEL  /api/mq/subscribe       取消订阅             │
                          │  GET  /api/mq/messages        查 MQ 消息日志        │
                          │  GET  /api/mq/messages/count 按 marker 计数       │
                          │  GET  /api/mq/messages/stats  统计摘要             │
                          │  POST /api/mq/messages/reset  清空记录             │
                          └──────────────────────────────────────────────────┘
```

### 7.5.3 消费 + 回复逻辑

收到 MQ 消息后：
1. 提取 `correlation_id`（= ESB messageId）和 `reply_to`（= `esb.status.{system}`）
2. 记录到 SQLite `mq_messages` 表
3. 查该队列的故障模式（复用 `fault_mode` 表，`target` = 队列名）
4. 按概率决定回复行为：
   - **success** → 回复 `{"messageId":"xxx","status":"CONSUMED"}` + ack
   - **fail** → 回复 `{"messageId":"xxx","status":"FAILED","error":"injected fail"}` + ack
   - **timeout** → 延迟后回复 `{"messageId":"xxx","status":"FAILED","error":"injected timeout"}` + ack
5. ack 消息（不 requeue，避免无限循环）

### 7.5.4 配置

```ini
# .env
MQ_ENABLED=false          # 设为 true 启用
MQ_HOST=127.0.0.1
MQ_PORT=5672
MQ_USER=guest
MQ_PASSWORD=guest
MQ_VHOST=/
MQ_REPLY_QUEUE_PREFIX=esb.status.   # 与 ESB 约定一致
MQ_AUTO_QUEUES=                      # 启动时自动消费的队列（逗号分隔）
```

### 7.5.5 典型 MQ 测试流程

```bash
# 1. 启动 MQ 消费者
curl -X POST http://localhost:8081/api/mq/start

# 2. 订阅目标队列（ESB routing_rule.target 的值）
curl -X POST "http://localhost:8081/api/mq/subscribe?queue=sit.target.queue"

# 3. 设置故障注入（让该队列 30% 回复 FAILED）
curl -X PUT http://localhost:8081/api/mode/sit.target.queue \
  -H "Content-Type: application/json" \
  -d '{"success_rate":70,"fail_rate":30,"timeout_rate":0}'

# 4. 发送 MQ 消息到 ESB（ESB 路由到该队列）
curl -X POST http://localhost:8081/api/esb/send \
  -H "Content-Type: application/json" \
  -d '{"system":"SIT_SYS","msg_type":"SIT_MQ_MSG","body":{"marker":"TC-MQ-001"}}'

# 5. 验证 MQ 消费记录
curl "http://localhost:8081/api/mq/messages?marker=TC-MQ-001"
curl "http://localhost:8081/api/mq/messages/count?marker=TC-MQ-001"

# 7. 清理
curl -X POST http://localhost:8081/api/mq/messages/reset
```

### 7.5.6 mq_messages 消息记录字段说明

每条被消费的 MQ 消息在 `mq_messages` 表记一行（Web UI 概览页「MQ 实收消息」列表 + 点击行看详情）。字段分两组：基础列（建表 SCHEMA）+ 扩展列（`mq_workers` 消费方实例写入，旧库经 ALTER 迁移补充）。

#### 基础列

| 字段 | 类型 | 说明 | 值说明 |
|------|------|------|--------|
| `id` | INTEGER | 记录自增主键 | UI 列表「#N」即此值 |
| `ts` | TEXT | 记录时间 | ISO 8601 UTC（`datetime.now(timezone.utc).isoformat()`） |
| `queue` | TEXT NOT NULL | 被消费的队列名 | = ESB `routing_rule.target` |
| `body` | TEXT | 消息体原文 | UTF-8 解码，坏字节以 `�` 替换 |
| `body_len` | INTEGER | 消息体字节数 | 原始 bytes 长度 |
| `marker` | TEXT | 业务标记（测试用例号） | 从 body JSON 提取 `marker` 字段，缺省取 `tc` 字段，均无则 NULL |
| `message_id` | TEXT | 消息唯一标识 | 提取链：`body.messageId` → AMQP `message_id` → AMQP `correlation_id`（ESB MqDispatcher 将 messageId 放在 AMQP 属性而非报文体，故须属性兜底）。ESB 生成格式 `msg_{32位hex}`（见下方 ID 说明）。UI 列表列头截断显示前 14 字符，鼠标悬停显示完整值 |
| `correlation_id` | TEXT | AMQP `correlation_id` 属性原值 | ESB 投递时 = messageId 同值（JMS→AMQP 映射约定），作为消费回复匹配键，ESB StatusReplyListener 按其定位 `routing_detail` 回填状态。故详情页 MessageId / CorrId 一致 |
| `reply_to` | TEXT | 回复队列 | ESB 约定 `esb.status.{system}`；空则不回复 |
| `content_type` | TEXT | AMQP `content_type` | 通常 `application/json` |
| `headers` | TEXT | AMQP headers（JSON） | bytes 值已解码为字符串；消息曾进死信队列时含 `x-death` 链（可判断拒收史） |
| `reply_status` | TEXT | 本次回复状态 | `CONSUMED` / `FAILED`，由 `fault_mode`（target=队列名）按概率注入决定 |
| `ack_status` | TEXT | 确认状态 | 固定 `"ack"`（历史遗留列，实际确认动作看 `ack_action`） |
| `response_type` | TEXT | 故障注入类型 | `success` / `fail` / `timeout`（`fail` → 立即回复 FAILED；`timeout` → 睡 `timeout_seconds` 后回复 FAILED） |

#### 扩展列（消费方实例记录）

| 字段 | 类型 | 说明 | 值说明 |
|------|------|------|--------|
| `consumer_id` | INTEGER | 记录本条消息的消费方实例 ID | `mq_consumers.id`；legacy 单例（.env 驱动 `mq_consumer.py`）写入时为 NULL |
| `exchange` | TEXT | 投递交换机 | `method.exchange`，默认交换机为 `""` |
| `routing_key` | TEXT | 投递路由键 | `method.routing_key`，直连队列时 = 队列名 |
| `redelivered` | INTEGER | 是否重投 | 0/1；1 表示曾被 nack/reject 重新入队后再投（UI 详情显示 ⚠️ 重投消息） |
| `delivery_tag` | INTEGER | AMQP 投递标签 | 通道内单调递增，仅排障用 |
| `properties_json` | TEXT | 收到消息的 AMQP 属性完整快照 | content_type / headers / delivery_mode / priority / correlation_id / reply_to / message_id / expiration / timestamp / app_id 等 |
| `ack_action` | TEXT | 实际确认动作 | 消费方 `ack_mode` 配置值：`auto` / `ack` / `nack_requeue`（重回队列）/ `nack_drop`（转死信）/ `reject` / `hold`（不确认，测超时） |
| `latency_ms` | INTEGER | 本条处理耗时（毫秒） | 含 `delay_ms` 配置延迟 + 故障注入延迟（timeout 场景 = `timeout_seconds`×1000） |
| `reply_body` | TEXT | 回复快照·消息体 | 实际 publish 到 `reply_to` 的文本（默认模板 `{"messageId":"...","status":"CONSUMED"}`，可自定义占位符 `{{messageId}} {{status}} {{body.字段}}` 等）；未回复为 NULL，UI 显示「（未回复）」 |
| `reply_props` | TEXT | 回复快照·AMQP 属性 | content_type / delivery_mode=2（持久化）/ correlation_id；publish 失败时替换为 `{"publish_error": "..."}` |

#### messageId 生成算法与冲突说明

- ESB 侧 `DispatchEngine.generateMessageId()`：`"msg_" + UUID.randomUUID().toString().replace("-", "")`，即 **UUID v4**（`SecureRandom` 密码学安全随机源）去横线后的 32 位 hex，有效随机空间 2^122 ≈ 5.3×10^36。
- 碰撞概率（生日悖论）：需约 2.7×10^18 条才有 50% 碰撞概率；每天 1 亿条运行 10 年（≈3.65×10^11 条）碰撞概率 ≈ 2.4×10^-14，**运行数年无需担心冲突**。
- 该 ID 全链路唯一复用：ESB `message_trace.message_id` = AMQP `message_id` = AMQP `correlation_id` = 消费回复体 `messageId`，一 ID 贯穿追踪。

## 8. 与现有系统的关系

```
  现有 Python run_sit.py (不受影响)
    ├── 内置 Probe (18081)    ── 继续使用，不冲突
    ├── 外部 faux 控制         ── 可选：指向 Smart Faux (8081) 或保持原样
    └── DB 断言                ── 继续直连 PostgreSQL

  Smart Faux Server (新·独立进程·8081)
    ├── 目标端点               ── ESB 路由规则 target 指向这里
    ├── 管理 API               ── Bruno / AI / curl 调用
    ├── DB 代理                ── 代理查询 ESB PostgreSQL
    └── Web UI                 ── 浏览器打开 http://localhost:8081
```

**端口规划**:
- `8081` — Smart Faux Server（主服务 + 管理 API + UI）
- `18081` — 现有 Python Probe（`run_suite.py` 内置，不受影响）
- `48080` — ESB Ingress（被测系统）
- `48081` — ESB Admin（被测系统管理端）

## 9. 项目结构

```
D:\mock13\
├── docs/
│   └── design.md              # 本设计文档
├── app/
│   ├── __init__.py
│   ├── main.py                # FastAPI 主入口
│   ├── config.py              # 配置加载
│   ├── database.py            # SQLite 初始化 + 连接管理
│   ├── models.py              # Pydantic 数据模型
│   ├── routers/
│   │   ├── __init__.py
│   │   ├── target.py          # 目标端点 (echo/echo2/echo3/404/catch-all)
│   │   ├── logs.py            # 实收日志管理
│   │   ├── mode.py            # 故障注入模式管理
│   │   ├── fixtures.py        # SIT 夹具管理
│   │   └── esb.py             # ESB 操作 (send/health/config)
│   └── static/
│       └── index.html         # Web Dashboard
├── requirements.txt
├── .env.example
├── run.bat                    # Windows 启动脚本
└── README.md
```

## 10. 部署方式

```bash
# 1. 安装依赖
cd D:\mock13
pip install -r requirements.txt

# 2. 配置环境
copy .env.example .env
# 编辑 .env 填入 ESB 连接信息

# 3. 启动
run.bat
# 或: python -m uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload

# 4. 访问
# Web UI:      http://localhost:8081/
# Swagger 文档: http://localhost:8081/docs
# 实收日志:    http://localhost:8081/api/logs
```

## 11. 典型测试流程

### AI 自动化测试

```bash
# 1. 重置环境
curl -X POST http://localhost:8081/api/logs/reset
curl -X POST http://localhost:8081/api/mode/reset

# 2. 安装夹具
curl -X POST http://localhost:8081/api/fixtures/install -H "Content-Type: application/json" -d '{...}'

# 3. 设置故障注入（让 echo2 50% 失败）
curl -X PUT http://localhost:8081/api/mode/echo2 -H "Content-Type: application/json" -d '{"success_rate":50,"fail_rate":50,"timeout_rate":0}'

# 4. 发送消息到 ESB
curl -X POST http://localhost:8081/api/esb/send -H "Content-Type: application/json" -d '{"system":"SIT_SYS","msg_type":"SIT_MSG","body":{"marker":"TC-001"}}'

# 5. 验证实收
curl "http://localhost:8081/api/logs/count?marker=TC-001"

# 6. 清理
curl -X POST http://localhost:8081/api/fixtures/cleanup
```

### Bruno 人工测试

1. 打开 Bruno，导入 `docs/` 下的 `.bru` 集合（后续生成）
2. 按顺序点击：Reset → Install Fixtures → Set Mode → Send → Check Logs
3. Web Dashboard 实时展示实收消息和故障模式状态
