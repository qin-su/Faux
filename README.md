# Smart Faux Server

> 版本: 1.3 | 日期: 2026-09-22

轻量级通用 Mock 服务端（FastAPI + SQLite，零外部依赖即可跑），原为 ESB 测试场景孵化，本身**不绑定任何被测系统**：任何 HTTP 调用方都可以把它当作"可编程的假服务端"使用。

核心能力一览：

| 能力 | 说明 | 入口 |
|------|------|------|
| 目标服务定义 | 动态注册任意 `/api/*` 路径的响应（方法/状态码/响应模板），无需写代码 | `/api/targets` |
| 模板渲染引擎 | 响应体支持 `@参数`（内置动态参数 + 全局参数，每次请求现渲染） | `/api/params`、`/api/targets/render` |
| 故障注入 | 按**场景**组织：故障类型（失败/超时/延迟/自定义）× 生效目标（动态多选）× 触发概率 | `/api/faults`（推荐）、`/api/mode`（旧版兼容） |
| 实收验证 | 记录收到的每个请求（头/体/响应全量），多条件模糊查询 + marker 计数断言 | `/api/logs` |
| 统计指标 | 自定义"名称 + 关键字"指标，统计概览按指标展示收包计数 | `/api/metrics` |

| MQ 消费测试 | **多实例** RabbitMQ 消费方（DB 配置：6 种确认模式 / 可编辑回复模板 / 故障注入 / 断线重连），兼有 `.env` 驱动旧版单例 | `/api/mq/consumers` |
| MQ 生产发送 | 多环境 RabbitMQ 生产者（DB 配置），发送卡片临时覆盖参数，confirm + mandatory 校验 | `/api/mq/producers` |
| 认证（模拟下游鉴权） | 认证开关 + 5 种方式（none/JWT/Bearer/Basic/API Key）+ **路径规则多重认证**（按路径分方式、规则内多方式 OR），真实 JWT 签发校验、refresh_token 轮换、令牌即时失效、认证请求留痕 | `/api/auth`、`/api/auth/token` |
| 配置迁移 | 一键导出/导入全部配置（目标服务 + 参数 + 故障 + 指标） | `/api/targets/export`、`/api/targets/import` |
| Web Dashboard | 9 页签管理界面：概览 / 目标服务 / 全局参数 / 统计指标 / 故障注入 / MQ 消费方 / MQ 生产者 / MQ 发送 / **认证** + 实收日志 | `http://localhost:8081/` |

---

## 目录

- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [请求处理链（核心概念）](#请求处理链核心概念)
- [API 总览](#api-总览)
- [目标服务定义](#目标服务定义)
- [模板渲染引擎](#模板渲染引擎)
- [全局参数](#全局参数)
- [故障注入（场景）](#故障注入场景)
- [故障注入（旧版模式，兼容）](#故障注入旧版模式兼容)
- [统计指标](#统计指标)
- [实收日志](#实收日志)
- [认证（模拟下游鉴权）](#认证模拟下游鉴权)
- [配置导出/导入](#配置导出导入)

- [MQ 消费方（多实例，推荐）](#mq-消费方多实例推荐)
- [MQ 生产者（发送）](#mq-生产者发送)
- [MQ 消费测试（旧版单例，兼容）](#mq-消费测试旧版单例兼容)
- [Web Dashboard](#web-dashboard)
- [数据模型](#数据模型)
- [项目结构](#项目结构)
- [完整测试流程示例](#完整测试流程示例)

---

## 快速开始

### 环境要求

- Python 3.10+
- 仅 MQ 测试需要外部组件（RabbitMQ），纯 Mock 用法零依赖

### 安装与启动

```bash
# Windows
run.bat

# 或手动启动
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
```

首次启动自动创建 SQLite（`mock13.db`）并建表，无需迁移。`.env` 可选（默认值见下文）。

### 验证

```bash
# 健康检查
curl http://localhost:8081/api/health
# {"status":"ok","service":"smart-faux","version":"1.0.0"}

# 任意未注册路径也会被记录并返回 200
curl -X POST http://localhost:8081/api/anything/here -d '{"k":"v"}'

# Swagger 文档（全部 API 可交互调试）
# http://localhost:8081/docs

# Web Dashboard
# http://localhost:8081/
```

---

## 配置说明

通过 `.env` 文件或环境变量管理（均有默认值，纯 Mock 用法无需配置）：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FAUX_PORT` | `8081` | 服务端口 |
| `FAUX_DB_PATH` | `./mock13.db` | SQLite 数据库路径 |
| `MQ_ENABLED` | `false` | 是否启用 **旧版单例**消费者（新版多实例消费方走 DB 配置，见 [MQ 消费方](#mq-消费方多实例推荐)） |
| `MQ_HOST` / `MQ_PORT` / `MQ_USER` / `MQ_PASSWORD` / `MQ_VHOST` | `127.0.0.1` / `5672` / `guest` / `guest` / `/` | RabbitMQ 连接（旧版单例用） |
| `MQ_REPLY_QUEUE_PREFIX` | `esb.status.` | 回复队列前缀（与 ESB 约定一致，旧版单例用） |
| `MQ_AUTO_QUEUES` | *(空)* | 启动时自动消费的队列（逗号分隔，旧版单例用） |

---

## 请求处理链（核心概念）

到达本服务的请求分两类处理（所有请求**先记录后响应**，实收日志永远完整）：

```
请求 /api/{path}
│
├─ 管理 API（/api/logs /api/targets /api/faults /api/metrics /api/params ...）
│    → 正常 REST 处理，不经注入
│
├─ 代码固定端点：/api/echo /api/echo2 /api/echo3 /api/sit-not-found
│    → sit-not-found 固定 404；其余：故障场景 → 旧版模式 → 200
│
└─ 其余路径（catch-all）：
     ① targets 表有定义且启用？
        ├─ 方法不匹配 → 405 method_not_allowed
        ├─ 故障场景命中（fault_scenarios，scope 匹配）→ 注入响应
        ├─ 旧版故障模式命中（fault_mode）→ 按概率 500/504/延迟
        └─ 按定义响应：渲染 response_template(@参数) → status_code/content_type
     ② 无定义 → 故障场景（scope=全部 时命中）→ fault_mode（已注册时）→ 记录 + 200
```

要点：

- **故障注入优先级**：场景（`fault_scenarios`）> 旧版模式（`fault_mode`）
- **scope = 全部** 的场景对任何路径生效（含未定义路径与固定端点）
- **latency 类型**只增加耗时、不改变响应内容（延迟后继续走正常链路）
- 模板中的 `@参数` 每次请求实时渲染，修改全局参数即时生效

---

## API 总览

| 模块 | 前缀/路径 | 说明 |
|------|-----------|------|
| 健康检查 | `GET /api/health` | 自身存活检查 |
| 目标服务 | `/api/targets` | 动态服务定义 CRUD + 渲染预览 |
| 全局参数 | `/api/params` | 模板可引用的键值参数 |
| 故障场景 | `/api/faults` | 场景化故障注入（推荐） |
| 故障模式 | `/api/mode` | 旧版按目标概率注入（兼容） |
| 统计指标 | `/api/metrics` | 统计概览的自定义指标配置 |
| 实收日志 | `/api/logs` | 请求记录查询/统计/清空 |
| 认证 | `/api/auth` | 认证开关/方式配置、令牌管理、认证请求留痕（详见[认证](#认证模拟下游鉴权)） |
| 认证规则 | `/api/auth/rules` | 按路径的多重认证规则 CRUD（详见[多重认证](#多重认证路径规则)） |
| 换票端点 | `/api/auth/token` | OAuth2 风格换票（client_credentials / password / refresh_token） |
| 导出导入 | `/api/targets/export`、`/api/targets/import` | 全量配置迁移 |
| MQ 消费方 | `/api/mq/consumers` | 多实例消费方 CRUD + 启停（推荐） |
| MQ 生产者 | `/api/mq/producers` | 生产者 CRUD + 发送 + 发送日志 |
| MQ 消费 | `/api/mq` | 旧版单例消费者 + 消息日志（兼容） |

---

## 目标服务定义

在 `targets` 表注册任意 `/api/*` 路径的响应行为，由 catch-all 命中生效，**无需重启服务**。代码固定端点（`/api/echo` 等 4 个）不可覆盖。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/targets` | 列表（含 fixed_paths 固定端点清单） |
| `POST` | `/api/targets` | 新增（path 须以 `/api/` 开头且唯一） |
| `PUT` | `/api/targets/{id}` | 更新（字段可选） |
| `DELETE` | `/api/targets/{id}` | 删除 |
| `POST` | `/api/targets/render` | 模板渲染预览（不落库） |

### 字段

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `path` | str | 必填 | 服务路径，如 `/api/order/create`（`/` 分隔，target 名自动为 `order-create`） |
| `name` | str | `""` | 显示名 |
| `methods` | list | `["POST","PUT","GET"]` | 允许的 HTTP 方法（其余返回 405） |
| `status_code` | int | `200` | 响应状态码 100-599 |
| `response_template` | str | `""` | 响应体模板，支持 `@参数`；空则返回 `{"status":"ok","target":...}` |
| `content_type` | str | `application/json` | 响应 Content-Type |
| `enabled` | bool | `true` | 是否启用 |

**示例：**

```bash
# 注册一个下单接口：60% 概率成功、40% 库存不足
curl -X POST http://localhost:8081/api/targets -H "Content-Type: application/json" -d '{
  "path": "/api/order/create",
  "name": "下单接口",
  "methods": ["POST"],
  "status_code": 200,
  "response_template": "{\"code\":0,\"orderNo\":\"@uuid\",\"time\":\"@datetime\",\"pick\":\"@pick:PAY_OK|PAY_FAIL\"}"
}'

# 调用（任意未注册路径自动记录并按定义响应）
curl -X POST http://localhost:8081/api/order/create -d '{"sku":"A1"}'
# {"code":0,"orderNo":"3f2c...","time":"2026-09-20 15:30:00","pick":"PAY_OK"}

# 模板渲染预览（不入库，调试用）
curl -X POST http://localhost:8081/api/targets/render \
  -H "Content-Type: application/json" -d '{"template":"now=@datetime id=@uuid"}'
```

---

## 模板渲染引擎

`response_template` 与故障场景 `body_template` 中的 `@token` 会被实时替换（时间均为北京时间 +8）：

| Token | 渲染结果 |
|-------|----------|
| `@time` | 当前时间 `HH:MM:SS` |
| `@date` | 当前日期 `YYYY-MM-DD` |
| `@datetime` | 日期时间 `YYYY-MM-DD HH:MM:SS` |
| `@timestamp` | Unix 时间戳（秒） |
| `@uuid` | 随机 UUID v4 |
| `@random` | 6 位随机整数（100000-999999） |
| `@random:1-100` | 范围内随机整数 |
| `@pick:a\|b\|c` | 列表中随机取一个（`\|` 分隔，不含空格/引号/花括号） |
| `@自定义key` | 引用「全局参数」中的键值 |

未识别的 `@token` 原样保留。全局参数每次渲染现读，修改即时生效。

---

## 全局参数

供模板以 `@key` 引用的键值对（如环境号、版本号、开关值），改动对所有引用它的模板即时生效。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/params` | 参数列表 |
| `PUT` | `/api/params` | 新增/更新（upsert） |
| `DELETE` | `/api/params/{key}` | 删除 |

**示例：**

```bash
# 设置
curl -X PUT http://localhost:8081/api/params -H "Content-Type: application/json" \
  -d '{"key":"env","value":"SIT","remark":"当前环境"}'

# 模板中使用（渲染为 SIT）
# {"env":"@env"}
```

key 需为字母/下划线开头的标识符。

---

## 故障注入（场景）

> 推荐方式。**场景 = 固定故障类型 × 动态生效目标 × 触发概率**，与具体业务解耦。

四种故障类型：

| 类型 | 行为 | 关键参数 |
|------|------|----------|
| `fail` | 返回错误状态码（默认 500），响应体 `{"error":"injected fail","scenario":"场景名"}` | `status_code` |
| `timeout` | 睡眠 N 秒后返回 504 | `timeout_seconds` |
| `latency` | 仅增加响应耗时（毫秒），**不改变响应结果**，可叠加在正常链路上 | `latency_ms` |
| `custom` | 自定义状态码 + 响应体模板（支持 `@参数`） | `status_code` + `body_template` |

`scope` 为生效目标 path 列表（如 `["/api/echo","/api/order/create"]`），**空数组 = 对全部请求生效**（含未定义路径与固定端点）。多场景按 id 顺序依次掷骰，首个命中即生效；`latency` 命中后仅延迟、继续检查后续场景。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/faults` | 场景列表 |
| `POST` | `/api/faults` | 新增（name 唯一） |
| `PUT` | `/api/faults/{id}` | 更新（字段可选，可只传 `{"enabled":false}` 启停） |
| `DELETE` | `/api/faults/{id}` | 删除 |
| `POST` | `/api/faults/disable-all` | 一键停用全部场景（不删除） |

**示例：**

```bash
# 场景 1：下游网关 502，30% 概率，只影响 /api/echo 与 /api/order/create
curl -X POST http://localhost:8081/api/faults -H "Content-Type: application/json" -d '{
  "name": "下游网关 502",
  "fault_type": "fail",
  "probability": 30,
  "status_code": 502,
  "scope": ["/api/echo", "/api/order/create"]
}'

# 场景 2：全链路慢 2 秒（不影响结果）
curl -X POST http://localhost:8081/api/faults -H "Content-Type: application/json" -d '{
  "name": "网络抖动",
  "fault_type": "latency",
  "latency_ms": 2000,
  "scope": []
}'

# 场景 3：自定义降级响应
curl -X POST http://localhost:8081/api/faults -H "Content-Type: application/json" -d '{
  "name": "熔断降级",
  "fault_type": "custom",
  "probability": 100,
  "status_code": 200,
  "body_template": "{\"code\":9999,\"msg\":\"服务降级\",\"retryAfter\":@random:30-60}",
  "scope": ["/api/echo"]
}'

# 启停 / 删除 / 全部停用
curl -X PUT http://localhost:8081/api/faults/1 -H "Content-Type: application/json" -d '{"enabled":false}'
curl -X DELETE http://localhost:8081/api/faults/1
curl -X POST http://localhost:8081/api/faults/disable-all
```

---

## 故障注入（旧版模式，兼容）

按 target（路径中 `/` 换 `-`，如 `/api/echo` → `echo`）设置成功/失败/超时概率。优先级低于场景，保留是为了兼容既有脚本。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/mode` | 所有目标的故障模式 |
| `GET` | `/api/mode/{target}` | 指定目标的故障模式 |
| `PUT` | `/api/mode/{target}` | 设置故障模式 |
| `POST` | `/api/mode/reset` | 重置所有目标为 100% 成功 |

参数：`success_rate` / `fail_rate`（500）/ `timeout_rate`（504）/ `latency_ms`（成功附加延迟）/ `timeout_seconds`，概率之和 ≤ 100。

**示例：**

```bash
curl -X PUT http://localhost:8081/api/mode/echo -H "Content-Type: application/json" \
  -d '{"success_rate":60,"fail_rate":30,"timeout_rate":10,"latency_ms":0,"timeout_seconds":5}'
```

> MQ 消费的故障注入也复用此表（target = 队列名）。

---

## 统计指标

配置"名称 + 关键字"指标（关键字对实收消息的 `path`/`target` 做 SQL LIKE 模糊匹配，如 `order`、`echo`、`/api/pay`），配置后 `/api/logs/stats` 的 `by_metric` 按指标返回计数，Dashboard 概览页同步展示。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/metrics` | 指标列表 |
| `POST` | `/api/metrics` | 新增（name 唯一） |
| `PUT` | `/api/metrics/{id}` | 更新（字段可选） |
| `DELETE` | `/api/metrics/{id}` | 删除 |

**示例：**

```bash
curl -X POST http://localhost:8081/api/metrics -H "Content-Type: application/json" \
  -d '{"name":"下单量","keyword":"order"}'

curl http://localhost:8081/api/logs/stats | python -m json.tool
# { "total": 272, "recent_24h": 10,
#   "by_response_type": {"success": 250, "fail": 20, ...},
#   "by_metric": {"下单量": 120 }, ... }
```

---

## 实收日志

所有到达 `/api/*` 的请求（含注入的）全量入库：请求方法/头/体、响应码/头/体、marker、response_type。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/logs` | 列表（多条件过滤 + 分页，最新在前） |
| `GET` | `/api/logs/count?marker=xxx` | 按 marker 计数（投递次数断言） |
| `GET` | `/api/logs/stats` | 统计摘要（总数/近24h/类型汇总/自定义指标） |
| `GET` | `/api/logs/{id}` | 单条完整详情（请求头/体 + 响应头/体） |
| `POST` | `/api/logs/reset` | 清空全部记录 |

### 过滤参数（`GET /api/logs`，可组合）

| 参数 | 匹配 | 说明 |
|------|------|------|
| `kw` | 模糊 | 全局关键词：请求头/体 + 响应头/体 + 路径/marker/目标 |
| `marker` / `target` / `path` | 模糊 | 单字段过滤 |
| `resp_type` | 精确 | `success` / `fail` / `timeout` / `not_found` / `method_not_allowed` / `custom` |
| `minutes` | — | 最近 N 分钟 |
| `limit` / `offset` | — | 分页（limit ≤ 1000） |

**示例：**

```bash
# marker 计数断言（验证 ESB 重试投递了 3 次）
curl "http://localhost:8081/api/logs/count?marker=TC-001"
# {"marker":"TC-001","count":3,"by_target":{"echo":2,"echo2":1}}

# 全局关键词搜报文内容
curl "http://localhost:8081/api/logs?kw=orderNo&limit=50"

# 看单条完整详情（含响应体）
curl http://localhost:8081/api/logs/123
```

---

## 认证（模拟下游鉴权）

把本服务伪装成「要求认证的下游系统」：默认**不做任何认证**（与历史行为完全一致，来者不拒）；
打开开关后按所选方式校验入站业务请求的凭据，并提供完整的换票 / 刷新 / 失效链路，
用于验证 ESB 出站动态令牌（`OutboundTokenManager`）的
「首调换票 → 缓存复用 → 临期刷新 → 401 重取重投」行为。

**零新增依赖**：JWT 用标准库 `hmac` + `hashlib` + `base64` 实现 HS256
（与 jwt.io / 任意语言 JWT 库互通，可用 `PyJWT` 等其他库独立校验签名）。

### 认证方式

| mode | 入站校验方式 | 说明 |
|------|-------------|------|
| `none` | 不校验 | **默认**，与历史行为一致；换票端点仍可用（可先配令牌再开校验） |
| `jwt` | `Authorization: Bearer <JWT>` | 真实 JWT：校验 HS256 签名 + `exp` + `jti` 状态（可即时失效） |
| `bearer` | `Authorization: Bearer <token>` | 不透明令牌，查 `auth_tokens` 表 |
| `basic` | `Authorization: Basic base64(user:pass)` | 比对 `basic_user/basic_pass`（未配则回退 client_id/client_secret） |
| `apikey` | 自定义头（默认 `X-API-Key`） | 比对 `apikey_value`（未配则回退 client_secret） |

保护范围：默认 `business` = 仅业务端点（`/api/echo*` 与 catch-all 的任意路径）；
管理 API（`/api/logs`、`/api/targets`…）与认证端点自身（`/api/auth/*`）**永不拦截**，
避免锁死页面；`protect_scope=all` 时管理 API 也纳入保护（慎用）。

校验失败返回 `401` + `WWW-Authenticate` 头 + `{"error":"unauthorized","detail":...}`，
并同时写 `auth_requests`（kind=verify）与 `received_messages`（保持实收日志完整）。

### 换票端点 `POST|GET /api/auth/token`

支持 **JSON / form / query** 三种传参（ESB 的 `bodyTemplate` 两种形态都能对接）。

| grant_type | 入参 | 响应 |
|-----------|------|------|
| `client_credentials`（默认） | 端点凭据（Basic 头 / API Key 头 / 请求体） | 令牌对 |
| `password` | `username` / `password` | 令牌对 |
| `refresh_token` | `refresh_token` | 新令牌对（旧的轮换作废） |

成功（标准 OAuth2 结构）：

```json
{"access_token":"eyJhbGciOiJIUzI1Ni...","token_type":"Bearer","expires_in":1800,
 "refresh_token":"mkr_...","refresh_expires_in":604800,"scope":""}
```

失败（对齐 OAuth2 错误语义）：`400/401` + `{"error":"invalid_grant|invalid_client","error_description":"..."}`。

`response_template` 可改写响应结构（适配自研网关的固定包装），支持占位符
`@access_token` `@refresh_token` `@expires_in` `@refresh_expires_in` `@token_type` `@client_id`。

**refresh_token 语义**（对齐 ESB 12 号稿 refreshEndpoint 设计）：
一次一换，旧 refresh 立即作废；旧串被再次使用 = 疑似泄漏 →
返回 `invalid_grant` 并默认吊销该 client 全部 ACTIVE 令牌（家族吊销，RFC 6819）。

### API（SIT 脚本可直接调用）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/config` | 读取配置（含派生字段 `active`） |
| PUT | `/api/auth/config` | 更新配置（仅提交字段生效） |
| POST·GET | `/api/auth/token` | 换票端点（client_credentials / password / refresh_token） |
| POST | `/api/auth/issue` | 管理端直接签发一对令牌（不做凭据校验，脚本取 token 用） |
| GET | `/api/auth/tokens` | 令牌列表（含**明文** access/refresh；`?status=ACTIVE`、`?client_id=`） |
| GET | `/api/auth/tokens/{id}` | 单条令牌 |
| POST | `/api/auth/tokens/{id}/revoke` | **立即失效**单条（access + refresh 同时作废） |
| POST | `/api/auth/revoke-all` | **批量失效**（`{"client_id":""}` 空=全部） |
| POST | `/api/auth/expire-access` | 仅让 access 立即过期，**refresh 仍有效**（验证 401→refresh 分支） |
| POST | `/api/auth/verify` | 校验 token 是否有效（无业务副作用） |
| GET | `/api/auth/requests` | 认证请求留痕（含请求头体与响应体；`?kind=&result=&client_id=`） |
| POST | `/api/auth/requests/reset` | 清空留痕 |
| GET | `/api/auth/stats` | 概览计数（有效/吊销/轮换/失败数） |
| POST | `/api/auth/reset` | 清空令牌 + 留痕（保留配置） |

### 快速开始（JWT 模式）

```bash
# 1) 开启 JWT 认证（access 60s，便于观察轮换）
curl -X PUT http://localhost:8081/api/auth/config -H "Content-Type: application/json" -d ^
"{\"enabled\":true,\"mode\":\"jwt\",\"secret\":\"sit-secret\",\"access_ttl\":60,\"client_id\":\"esb_client\",\"client_secret\":\"esb_secret\"}"

# 2) 无凭据调用业务端点 → 401
curl -X POST http://localhost:8081/api/echo -d "{}"
# {"error":"unauthorized","detail":"缺少 Authorization: Bearer <JWT> 头","mode":"jwt"}

# 3) 换票（Basic 头带 clientId:clientSecret）
curl -X POST http://localhost:8081/api/auth/token -u esb_client:esb_secret ^
     -d "grant_type=client_credentials"
# {"access_token":"eyJ...","token_type":"Bearer","expires_in":60,"refresh_token":"mkr_..."}

# 4) 携带 JWT 调用 → 200
curl -X POST http://localhost:8081/api/echo -H "Authorization: Bearer eyJ..." -d "{}"

# 5) 立即失效（页面按钮或接口）→ 该 token 立刻 401，ESB 将触发重取重投
curl -X POST http://localhost:8081/api/auth/tokens/1/revoke
curl -X POST http://localhost:8081/api/auth/revoke-all -H "Content-Type: application/json" -d "{}"

# 6) 关闭认证（恢复历史行为）
curl -X PUT http://localhost:8081/api/auth/config -H "Content-Type: application/json" -d "{\"enabled\":false}"
```

Python（httpx）示例：

```python
import base64, httpx
B = "http://127.0.0.1:8081"
c = httpx.Client(timeout=10)
c.put(f"{B}/api/auth/config", json={"enabled": True, "mode": "jwt",
                                     "secret": "sit-secret", "access_ttl": 60})
auth = "Basic " + base64.b64encode(b"esb_client:esb_secret").decode()
tk = c.post(f"{B}/api/auth/token", data={"grant_type": "client_credentials"},
            headers={"Authorization": auth}).json()
r = c.post(f"{B}/api/echo", json={"a": 1},
           headers={"Authorization": "Bearer " + tk["access_token"]})
assert r.status_code == 200
c.post(f"{B}/api/auth/tokens/1/revoke")          # 立即失效
assert c.post(f"{B}/api/echo", json={"a": 1},
              headers={"Authorization": "Bearer " + tk["access_token"]}).status_code == 401
```

### 与 ESB 出站动态令牌对接

ESB 侧（`业务系统 → 出站认证`，或按 12 号稿实施管理台表单后）配置：

```jsonc
"bearer": { "tokenEndpoint": {
    "url":          "http://<mock-host>:8081/api/auth/token",
    "method":       "POST",
    "authType":     "basic",              // 或 apikey（与 token_endpoint_auth 对应）
    "clientId":     "esb_client",         // 与 mock 配置一致
    "clientSecret": "esb_secret",
    "grantType":    "client_credentials",
    "tokenPath":    "access_token",       // 默认即 access_token
    "expiresInPath":"expires_in"          // 决定 ESB 缓存多久（clamp 60s~86400s）
}}
```

验证要点与坑：

| 目标 | 做法 | 预期 |
|------|------|------|
| 触发 ESB 重取重投 | 页面「全部失效」或 `POST /api/auth/revoke-all` | 下一条消息 401 → ESB 重取 → 重投成功（仅重投一次） |
| 验证 401→refresh 分支 | `POST /api/auth/expire-access`（refresh 仍有效） | ESB 实施 refreshEndpoint 后应走刷新；当前版本 ESB 无 refresh，表现为重新完整认证 |
| 观察换票节奏 | `GET /api/auth/requests?kind=token` | 每条换票/刷新一行，含请求头体与响应体 |
| ESB 缓存时长 | 配置 `access_ttl` | ESB 按 `expires_in` 缓存，剩余 <30s 提前刷新，最小有效值 60s |
| **本地联调必踩坑** | 换票地址过 `OutboundTargetGuard` 双检 | 默认禁止 `127.0.0.0/8` → 需把 `esb.security.deny-target-cidrs` 显式设为 `none`（填空串=恢复默认），或改用内网 IP |

JWT 载荷：`{"iss","sub","iat","exp","jti","aud"(可选),"scope"(可选)}`；
`jti` = `auth_tokens.id`，因此**吊销立即生效**（JWT 未过期也会被拒）——
这是验证「ESB 401 重取」最干净的手段，无需等待 token 过期。

### 令牌状态

| 状态 | 含义 | 对端表现 |
|------|------|---------|
| `ACTIVE` | 有效 | 正常 200 |
| `EXPIRED` | access 已过期（refresh 可能仍有效） | 401 → 可刷新 |
| `ROTATED` | 已被刷新轮换 | 401 → 需重新认证 |
| `REVOKED` | 已吊销（手动或家族吊销） | 401 → 需重新认证 |

### 多重认证（路径规则）

一个 mock13 可同时扮演多个要求不同认证的下游系统：按路径配规则，**规则内可多选方式（OR，任一通过即放行）**。

匹配与生效顺序：

1. 取所有 `enabled=1` 的规则，按 `priority` 降序；同级按 `path_pattern` 长度降序（更具体优先）
2. 路径匹配：`/api/pay*` = 前缀通配；`/api/echo` = 精确匹配
3. 命中即用该规则的 `modes` 与覆盖参数；**未命中任何规则 → 回退全局 `auth_config.mode`**

| 规则 modes | 效果 |
|-----------|------|
| `jwt` | 该路径只认 JWT |
| `jwt,apikey` | JWT **或** API Key 任一通过即放行 |
| `none` | 该路径**免认证**（可实现"部分端点不校验"） |

规则可**覆盖**全局参数（留空/0 = 沿用全局）：`secret`、`issuer`、`audience`、`access_ttl`、`refresh_ttl`、`client_id`、`client_secret`、`apikey_header`、`apikey_value`、`basic_user`、`basic_pass`。

```bash
# 全局默认 JWT（未命中规则的路径用它）
curl -X PUT http://localhost:8081/api/auth/config -H "Content-Type: application/json" -d ^
"{\"enabled\":true,\"mode\":\"jwt\",\"secret\":\"global-secret\"}"

# 规则1：/api/echo* 走 Basic（模拟系统 A）
curl -X POST http://localhost:8081/api/auth/rules -H "Content-Type: application/json" -d ^
"{\"path_pattern\":\"/api/echo*\",\"modes\":\"basic\",\"basic_user\":\"u1\",\"basic_pass\":\"p1\",\"priority\":10}"

# 规则2：/api/pay* 走「JWT 或 API Key」二选一（模拟系统 B，且用独立密钥）
curl -X POST http://localhost:8081/api/auth/rules -H "Content-Type: application/json" -d ^
"{\"path_pattern\":\"/api/pay*\",\"modes\":\"jwt,apikey\",\"secret\":\"rule-secret\",\"apikey_value\":\"k-pay\",\"priority\":5}"

# 规则3：/api/open* 免认证
curl -X POST http://localhost:8081/api/auth/rules -H "Content-Type: application/json" -d ^
"{\"path_pattern\":\"/api/open*\",\"modes\":\"none\"}"

# 查某路径最终生效的方式/密钥（调试神器，无副作用）
curl "http://localhost:8081/api/auth/resolve?path=/api/pay/1"
# {"path":"/api/pay/1","protected":true,"modes":["jwt","apikey"],
#  "rule":{"id":2,...},"token_kind":"jwt","effective":{"secret":"rule-secret",...}}
```

隔离性验证（冒烟已覆盖）：全局密钥签的 JWT **过不了** `/api/pay*` 的规则（签名不匹配 `rule-secret`）；`/api/echo*` 只认 Basic，带 JWT 也 401。

**换票要带 `path=`**：规则覆盖了密钥时，换票需指定目标路径才能用对应密钥签发：

```bash
curl -X POST http://localhost:8081/api/auth/token -u esb_client:esb_secret ^
     -d "grant_type=client_credentials&path=/api/pay/1"
```

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/auth/rules` | 规则列表（按优先级排序） |
| POST | `/api/auth/rules` | 新增规则（`modes` 逗号分隔；非法值 400） |
| PUT | `/api/auth/rules/{id}` | 更新规则（仅提交字段生效；可用于启停） |
| DELETE | `/api/auth/rules/{id}` | 删除规则 |
| GET | `/api/auth/resolve?path=` | 查询某路径命中的规则与生效参数（调试用） |

Dashboard「认证」页签第二张卡即**认证规则**：路径模式 / 方式（多选 chips）/ 优先级 / 覆盖参数 / 启停 / 删除。

---

## 配置导出/导入

一键迁移全部配置（目标服务 + 全局参数 + 故障模式 + 故障场景 + 统计指标）。
认证配置（`auth_config`）与令牌不在导出范围内（含凭据与令牌明文，不随配置迁移）。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/targets/export` | 导出 JSON 文件（`faux_export_时间戳.json`） |
| `POST` | `/api/targets/import?mode=skip\|overwrite` | 批量导入 |

导入规则：目标服务按 `mode` 跳过或覆盖；参数/模式/场景/指标按唯一键 upsert；固定端点拒绝导入；非法条目记入 `errors` 不中断。

**示例：**

```bash
curl -o backup.json http://localhost:8081/api/targets/export

curl -X POST "http://localhost:8081/api/targets/import?mode=overwrite" \
  -H "Content-Type: application/json" --data-binary @backup.json
# {"created":5,"updated":2,"skipped":0,"params_imported":3,"scenarios_imported":2,"metrics_imported":1,"errors":[]}
```

---

## MQ 消费方（多实例，推荐）

**DB 配置驱动**的多实例 RabbitMQ 消费方：每个消费方 = 独立连接 + 单队列 + 独立行为配置，可同时连接**不同环境**的 MQ，无需改 `.env`、无需重启服务（配置变更自动热重启）。

```
RabbitMQ 队列 ──> 消费方实例（独立线程 + 独立连接）
                   ├─ 记录 SQLite（Headers 含 x-death / Properties / 处理耗时 / 回复快照）
                   ├─ 故障注入（可选，复用 fault_mode，target = 队列名）
                   ├─ 处理延迟 delay_ms
                   ├─ 回复模板渲染 ──> reply_to 队列（带 content_type / headers）
                   └─ 确认动作（ack / nack / reject / hold）──> 队列或死信
```

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/mq/consumers` | 消费方列表（含运行状态 / 已消费数 / 最近错误，密码打码） |
| `POST` | `/api/mq/consumers` | 新增（`enabled=true` 保存后立即拉起） |
| `PUT` | `/api/mq/consumers/{id}` | 更新（字段可选；配置变更后若处于启用态自动热重启） |
| `DELETE` | `/api/mq/consumers/{id}` | 删除（先停止再删） |
| `POST` | `/api/mq/consumers/{id}/start` | 启动 |
| `POST` | `/api/mq/consumers/{id}/stop` | 停止 |

### 字段

**连接**：

| 字段 | 默认 | 说明 |
|------|------|------|
| `name` | 必填 | 消费方名称（唯一） |
| `host` / `port` / `vhost` / `username` / `password` | `127.0.0.1` / `5672` / `/` / `guest` / `guest` | 每个消费方独立，可指向不同环境 |
| `queue` | 必填 | 消费的队列 |
| `prefetch` | `1` | QoS 预取数（测并发可调大） |
| `declare_mode` | `durable` | `durable`=声明持久队列（幂等，参数须与服务端一致）；`passive`=仅检查存在不创建（防误建） |

**行为**：

| 字段 | 默认 | 说明 |
|------|------|------|
| `ack_mode` | `ack` | 确认模式，见下表 |
| `delay_ms` | `0` | 收到消息后等待 N 毫秒再执行动作/回复（模拟慢消费） |
| `fault_inject` | `true` | 复用故障注入：按 `fault_mode`（target = 队列名）决定回复 success/FAILED |

确认模式（`ack_mode`）：

| 值 | 行为 | 典型用途 |
|----|------|----------|
| `auto` | 自动确认 | 吞吐最高（不记 ack 动作） |
| `ack` | 手动确认 | 标准流 |
| `nack_requeue` | nack + requeue | 消息退回队列重投 → 测 redelivered 标记 / 消费幂等 |
| `nack_drop` | nack 不重投 | 消息进死信队列 → 测 DLX |
| `reject` | reject | 拒绝（同不重投） |
| `hold` | 不回任何确认 | unacked 挂起 → 测消费者超时 / prefetch 占用 |

**回复**（消息带 `reply_to` 时）：

| 字段 | 默认 | 说明 |
|------|------|------|
| `reply_enabled` | `true` | 是否回写 `reply_to` 队列 |
| `reply_template` | `{"messageId":"{{messageId}}","status":"{{status}}"}` | 回复 body 模板，占位符见下 |
| `reply_content_type` | `application/json` | 回复消息 content_type |
| `reply_headers` | *(空)* | 回复消息 headers（JSON 对象文本） |

模板占位符：`{{messageId}}`、`{{correlationId}}`、`{{queue}}`、`{{status}}`（CONSUMED/FAILED）、`{{error}}`、`{{ts}}`、`{{body.字段}}`（从消息 JSON body 取值，支持多级）。

**示例：**

```bash
# 1. 新增消费方（连 SIT MQ，nack+requeue 测幂等，启用即拉起）
curl -X POST http://localhost:8081/api/mq/consumers -H "Content-Type: application/json" -d '{
  "name": "SIT-下单结果消费", "host": "10.0.0.11", "queue": "sit.order.result",
  "ack_mode": "nack_requeue", "delay_ms": 500, "enabled": true,
  "reply_template": "{\"msgId\":\"{{messageId}}\",\"code\":\"OK\",\"orderNo\":\"{{body.orderNo}}\"}"
}'

# 2. 给该队列注入 30% 回复失败（消费方 fault_inject 默认开启）
curl -X PUT http://localhost:8081/api/mode/sit.order.result \
  -H "Content-Type: application/json" -d '{"success_rate":70,"fail_rate":30,"timeout_rate":0}'

# 3. 验证（详见下方消息查询）
curl "http://localhost:8081/api/mq/messages?queue=sit.order.result&reply_status=FAILED"
```

> 运行状态 `runtime_status`：`consuming`（正常消费）/ `reconnecting`（断线自动重连，5s 间隔）/ `stopped`。消费日志记录 Redelivered 标记、delivery_tag、消息 Headers（含 x-death 死信链）、Properties、实际回复快照与确认动作。

---

## MQ 生产者（发送）

多环境 RabbitMQ 生产者配置 + 手工发送（模拟上游源系统投递消息），发送走 **publisher confirm + mandatory**：broker 确认 / 不可路由（unroutable）/ 连接失败（error）三种结果都明确返回并落发送日志。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/mq/producers` | 生产者列表（密码打码） |
| `POST` | `/api/mq/producers` | 新增（name 唯一） |
| `PUT` | `/api/mq/producers/{id}` | 更新（字段可选） |
| `DELETE` | `/api/mq/producers/{id}` | 删除 |
| `POST` | `/api/mq/producers/{id}/send` | 发送一条消息（未填项用生产者默认值） |
| `GET` | `/api/mq/sends` | 最近发送日志（result / 耗时 / 明细） |

**发送参数**（`POST /api/mq/producers/{id}/send`，均可选，覆盖生产者默认值）：
`body`（必填）、`routing_key`、`content_type`、`delivery_mode`（1/2）、`priority`（0-9）、`expiration`（TTL 毫秒）、`correlation_id` / `message_id`（留空自动 UUID）、`headers`（JSON 对象）。

**示例：**

```bash
# 1. 新增生产者（模拟 SIT 上游源系统）
curl -X POST http://localhost:8081/api/mq/producers -H "Content-Type: application/json" -d '{
  "name": "SIT-上游源系统", "host": "10.0.0.11", "routing_key": "sit.order.result",
  "sample_body": "{\"marker\":\"TC-MQ-001\",\"bizData\":{\"orderNo\":\"A123\"}}"
}'

# 2. 发送（临时覆盖 TTL 与优先级，模拟死信）
curl -X POST http://localhost:8081/api/mq/producers/1/send -H "Content-Type: application/json" \
  -d '{"body":"{\"orderNo\":\"A123\"}","expiration":"30000","priority":9}'

# 3. 看发送结果与日志
curl http://localhost:8081/api/mq/sends
```

> Dashboard「概览 → MQ 生产者发送」卡片可选用生产者、临时改参、格式化 body、内联查看 sent/unroutable/error 结果与最近发送记录。

---

## MQ 消费测试（旧版单例，兼容）

> `.env` 驱动的旧版单例消费者（`MQ_ENABLED` + `MQ_AUTO_QUEUES`），保留兼容既有脚本。新用法建议用上方**多实例消费方**。

```
ESB MqDispatcher → RabbitMQ 队列 → 本服务消费 → 记录 SQLite → 故障注入 → 回复 esb.status.{system} → ESB 更新状态
```

收到消息后按该队列的 `fault_mode`（target = 队列名）决定回复：success → `{"messageId":"xxx","status":"CONSUMED"}`；fail/timeout → `{"status":"FAILED","error":"injected ..."}`；一律 ack 不 requeue。

### API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/mq/status` | 旧版单例状态（运行/连接/队列） |
| `POST` | `/api/mq/start`、`/api/mq/stop` | 启停旧版单例 |
| `POST` / `DELETE` | `/api/mq/subscribe?queue=xxx` | 订阅/退订队列 |
| `POST` | `/api/mq/messages/reset` | 清空记录 |

### MQ 消息查询（新旧消费方共用）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/mq/messages` | 消息日志（过滤 + 分页，最新在前） |
| `GET` | `/api/mq/messages/count?marker=xxx` | 按 marker 计数 |
| `GET` | `/api/mq/messages/stats` | 统计摘要 |

`GET /api/mq/messages` 过滤参数（可组合）：`queue`（精确）、`marker`（精确）、`consumer_id`（按消费方实例，新版）、`reply_status`（`CONSUMED` / `FAILED`，新版）、`limit` / `offset`。

**典型流程：**

```bash
# 1. 旧版：启动并订阅
curl -X POST http://localhost:8081/api/mq/start
curl -X POST "http://localhost:8081/api/mq/subscribe?queue=sit.target.queue"

# 2. 给该队列注入 30% 回复失败
curl -X PUT http://localhost:8081/api/mode/sit.target.queue \
  -H "Content-Type: application/json" -d '{"success_rate":70,"fail_rate":30,"timeout_rate":0}'

# 3. ESB 发送 MQ 消息后验证
curl "http://localhost:8081/api/mq/messages/count?marker=TC-MQ-001"
```

---

## Web Dashboard

浏览器打开 `http://localhost:8081/`：

- **概览**：统计概览（总数/近24h/类型汇总/自定义指标）+ 故障注入场景表格 + **MQ 生产者发送卡片**（选生产者 / 临时覆盖参数 / 内联结果 / 最近发送）+ **MQ 实收消息卡片**（过滤分页 + 详情弹窗：Redelivered 警示、Headers 含 x-death、回复快照、确认动作）
- **目标服务**：服务定义 CRUD + 响应模板编辑（@参数点击插入 + 渲染预览）
- **全局参数**：键值参数管理
- **统计指标**：指标配置
- **MQ 消费方**：多实例消费方 CRUD + 启停 + 运行状态徽章（consuming / reconnecting / stopped）+ 已消费数 + 最近错误悬停；编辑弹窗含连接 / 行为（确认模式、延迟、故障注入）/ 回复模板（占位符 chip 点击插入）
- **MQ 生产者**：多环境生产者 CRUD（连接 / 默认投递参数 / 示例消息体）
- **认证**：① 认证配置（总开关 + 方式下拉 + 密钥/TTL/凭据/换票端点认证/保护范围/响应模板，保存即时生效）② **认证规则**（路径模式 + 方式多选 chips + 优先级 + 覆盖参数 + 启停/删除，实现按路径多重认证）③ 当前令牌表（类型/Client/状态徽章/access 与 refresh 令牌——点击切换打码⇄明文、复制、单条**立即失效**，顶部还有「＋ 签发一对」「access 立即过期」「全部失效」）③ 认证请求留痕表（换票/刷新/入站校验/失效，含请求头体与响应体，点击行看完整详情）
- **实收日志**（底部）：实时消息列表 + 详情弹窗 + 导入导出
- 标题右侧：自动刷新间隔下拉（`-`/`5ms`/`15ms`/`30ms`/`1m`/`5m`/`20m`，ms=秒、m=分钟，默认 15s）、清空日志 / 刷新统计
- 统计 / 日志 / MQ 数据按所选间隔自动刷新（保持当前页与过滤条件）

Swagger API 文档：`http://localhost:8081/docs`

---

## 数据模型

SQLite 单库（`FAUX_DB_PATH`），启动自动建表：

| 表 | 用途 |
|----|------|
| `received_messages` | 实收消息全量记录（请求/响应头体、marker、response_type） |
| `targets` | 目标服务定义（path/methods/状态码/响应模板） |
| `global_params` | 全局参数（模板 @key 引用） |
| `fault_scenarios` | 故障场景（类型 × scope × 概率） |
| `stat_metrics` | 统计指标（名称 + 关键字） |
| `fault_mode` | 旧版故障模式（按 target 概率） |
| `mq_consumers` | MQ 消费方配置（连接 / 确认模式 / 回复模板 / 故障注入） |
| `mq_producers` | MQ 生产者配置（连接 / 默认投递参数 / 示例消息体） |
| `mq_messages` | MQ 消费记录（Headers / Properties / 回复快照 / 确认动作 / 耗时） |
| `mq_send_logs` | MQ 发送日志（result=sent/unroutable/error、耗时、明细） |
| `auth_config` | 认证配置（单行 id=1：开关 / 方式 / 密钥 / TTL / 凭据 / 保护范围） |
| `auth_rules` | 认证规则（路径 pattern → 多方式 OR + 覆盖参数 + 优先级） |
| `auth_tokens` | 已签发令牌（access + refresh 明文、过期时间、状态、轮换指向、使用计数） |
| `auth_requests` | 认证请求留痕（换票/刷新/入站校验/失效，含请求头体与响应体） |

---

## 项目结构

```
mock13/
├── app/
│   ├── main.py              # FastAPI 入口 + 生命周期 + catch-all 请求链
│   ├── config.py            # 配置加载（.env + 环境变量）
│   ├── database.py          # SQLite 初始化（建表/迁移）与连接管理
│   ├── models.py            # Pydantic 请求/响应模型
│   ├── templating.py        # @参数模板渲染引擎（动态参数 + 全局参数）
│   ├── authcore.py          # JWT（HS256）签发/校验 + 令牌生成（标准库实现，零依赖）
│   ├── authservice.py       # 认证服务：配置/签发/刷新/入站校验/失效/留痕
│   ├── mq_consumer.py       # RabbitMQ 消费者（旧版单例，独立线程）
│   ├── mq_workers.py        # MQ 多实例消费方 + 管理器 + 发送（新版）
│   ├── routers/
│   │   ├── auth.py          # 认证：配置 / 换票端点 / 令牌管理 / 留痕查询
│   │   ├── target.py        # 固定端点（echo/echo2/echo3/sit-not-found）
│   │   ├── service.py       # 目标服务 + 全局参数 + 导出导入
│   │   ├── faults.py        # 故障场景 CRUD + 匹配引擎
│   │   ├── metrics.py       # 统计指标 CRUD
│   │   ├── mode.py          # 旧版故障模式（兼容）
│   │   ├── logs.py          # 实收日志

│   │   └── mq.py            # MQ 消费方/生产者管理 + 消息查询（含旧版单例端点）
│   └── static/index.html    # Web Dashboard（单文件）
├── docs/design.md           # 设计文档
├── requirements.txt         # 依赖
├── run.bat                  # Windows 启动脚本
└── README.md                # 本文件
```

### 依赖

| 包 | 用途 |
|----|------|
| `fastapi` + `uvicorn` | Web 框架 + ASGI |
| `python-dotenv` | .env 加载 |
| `pika` | RabbitMQ 消费 |

认证能力**不引入新依赖**：JWT（HS256）由标准库 `hmac`/`hashlib`/`base64` 实现，
与 jwt.io 及其他语言 JWT 库互通。

---

## 完整测试流程示例

以"注册接口 → 注入 30% 失败 → 发送 → 断言"为例（纯 Mock 用法，不依赖 ESB）：

```bash
# 1. 注册目标服务（模板含动态参数）
curl -X POST http://localhost:8081/api/targets -H "Content-Type: application/json" -d '{
  "path": "/api/order/create", "name": "下单接口", "methods": ["POST"],
  "response_template": "{\"code\":0,\"orderNo\":\"@uuid\",\"time\":\"@datetime\"}"
}'

# 2. 配故障场景：30% 概率 502
curl -X POST http://localhost:8081/api/faults -H "Content-Type: application/json" -d '{
  "name": "下单偶发 502", "fault_type": "fail", "probability": 30,
  "status_code": 502, "scope": ["/api/order/create"]
}'

# 3. 配统计指标
curl -X POST http://localhost:8081/api/metrics -H "Content-Type: application/json" \
  -d '{"name":"下单量","keyword":"order"}'

# 4. 压测调用（marker 便于断言）
for i in 1..100; curl -X POST http://localhost:8081/api/order/create -d '{"marker":"TC-ORDER","sku":"A1"}'

# 5. 断言与统计
curl "http://localhost:8081/api/logs/count?marker=TC-ORDER"          # 应为 100
curl "http://localhost:8081/api/logs/stats"                          # by_metric.下单量=100，fail≈30
curl "http://localhost:8081/api/logs?marker=TC-ORDER&resp_type=fail" # 看失败明细

# 6. 清理
curl -X POST http://localhost:8081/api/faults/disable-all
curl -X POST http://localhost:8081/api/logs/reset
```

更多用法按需组合目标服务定义、故障注入与统计指标即可，无需外部系统配合。
