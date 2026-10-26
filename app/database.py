"""SQLite 初始化与连接管理。"""

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from app import config

_lock = threading.Lock()
_local = threading.local()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS received_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    target        TEXT    NOT NULL,
    method        TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    content_type  TEXT,
    headers       TEXT,
    body          TEXT,
    body_len      INTEGER DEFAULT 0,
    marker        TEXT,
    status_code   INTEGER,
    response_type TEXT,
    response_headers TEXT,
    response_body TEXT
);

CREATE INDEX IF NOT EXISTS idx_msg_marker ON received_messages(marker);
CREATE INDEX IF NOT EXISTS idx_msg_target ON received_messages(target);
CREATE INDEX IF NOT EXISTS idx_msg_ts    ON received_messages(ts);

CREATE TABLE IF NOT EXISTS fault_mode (
    target          TEXT PRIMARY KEY,
    success_rate    INTEGER DEFAULT 100,
    fail_rate       INTEGER DEFAULT 0,
    timeout_rate    INTEGER DEFAULT 0,
    latency_ms      INTEGER DEFAULT 0,
    timeout_seconds INTEGER DEFAULT 30,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS mq_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    queue           TEXT    NOT NULL,
    body            TEXT,
    body_len        INTEGER DEFAULT 0,
    marker          TEXT,
    message_id      TEXT,
    correlation_id  TEXT,
    reply_to        TEXT,
    content_type    TEXT,
    headers         TEXT,
    reply_status    TEXT,
    ack_status      TEXT,
    response_type   TEXT
);

CREATE INDEX IF NOT EXISTS idx_mq_marker  ON mq_messages(marker);
CREATE INDEX IF NOT EXISTS idx_mq_queue   ON mq_messages(queue);
CREATE INDEX IF NOT EXISTS idx_mq_ts      ON mq_messages(ts);

CREATE TABLE IF NOT EXISTS targets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    path              TEXT    NOT NULL UNIQUE,
    name              TEXT    DEFAULT '',
    methods           TEXT    DEFAULT 'POST,PUT,GET',
    status_code       INTEGER DEFAULT 200,
    response_template TEXT    DEFAULT '',
    content_type      TEXT    DEFAULT 'application/json',
    enabled           INTEGER DEFAULT 1,
    created_at        TEXT,
    updated_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_targets_path ON targets(path);

CREATE TABLE IF NOT EXISTS global_params (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    remark     TEXT DEFAULT '',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS fault_scenarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    fault_type      TEXT    NOT NULL DEFAULT 'fail',   -- fail/timeout/latency/custom
    probability     INTEGER DEFAULT 100,               -- 触发概率 0-100
    status_code     INTEGER DEFAULT 500,               -- fail/custom 响应码
    latency_ms      INTEGER DEFAULT 0,                 -- latency 延迟毫秒
    timeout_seconds INTEGER DEFAULT 30,                -- timeout 睡眠秒数
    body_template   TEXT    DEFAULT '',                -- custom 响应体模板（支持 @参数）
    scope           TEXT    DEFAULT '[]',              -- JSON 数组：生效目标 path；空=全部
    enabled         INTEGER DEFAULT 1,
    updated_at      TEXT
);

CREATE TABLE IF NOT EXISTS stat_metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    keyword    TEXT NOT NULL,                          -- 对 path/target 模糊匹配
    enabled    INTEGER DEFAULT 1,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS mq_consumers (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT    NOT NULL UNIQUE,
    host               TEXT    DEFAULT '127.0.0.1',
    port               INTEGER DEFAULT 5672,
    vhost              TEXT    DEFAULT '/',
    username           TEXT    DEFAULT 'guest',
    password           TEXT    DEFAULT 'guest',
    queue              TEXT    NOT NULL,
    prefetch           INTEGER DEFAULT 1,
    declare_mode       TEXT    DEFAULT 'durable',   -- durable=声明durable队列 / passive=仅检查不创建
    ack_mode           TEXT    DEFAULT 'ack',       -- auto/ack/nack_requeue/nack_drop/reject/hold
    delay_ms           INTEGER DEFAULT 0,           -- 收到消息后延迟再执行动作
    reply_enabled      INTEGER DEFAULT 1,           -- 有 reply_to 时回写回复
    reply_template     TEXT    DEFAULT '{"messageId":"{{messageId}}","status":"{{status}}"}',
    reply_content_type TEXT    DEFAULT 'application/json',
    reply_headers      TEXT    DEFAULT '',          -- JSON：回复消息 headers
    fault_inject       INTEGER DEFAULT 1,           -- 复用 fault_mode(target=队列名) 故障注入
    enabled            INTEGER DEFAULT 1,
    created_at         TEXT,
    updated_at         TEXT
);

CREATE TABLE IF NOT EXISTS mq_producers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    host          TEXT    DEFAULT '127.0.0.1',
    port          INTEGER DEFAULT 5672,
    vhost         TEXT    DEFAULT '/',
    username      TEXT    DEFAULT 'guest',
    password      TEXT    DEFAULT 'guest',
    exchange      TEXT    DEFAULT '',                -- 空 = 默认交换机（直连队列）
    routing_key   TEXT    DEFAULT '',
    content_type  TEXT    DEFAULT 'application/json',
    delivery_mode INTEGER DEFAULT 2,                 -- 1=非持久 2=持久
    priority      INTEGER DEFAULT 0,
    sample_body   TEXT    DEFAULT '',                -- 发送卡片示例模板
    enabled       INTEGER DEFAULT 1,
    created_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS mq_send_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    producer_id   INTEGER,
    producer_name TEXT,
    exchange      TEXT,
    routing_key   TEXT,
    body          TEXT,
    body_len      INTEGER DEFAULT 0,
    result        TEXT,                              -- sent / unroutable / error
    detail        TEXT,
    elapsed_ms    INTEGER DEFAULT 0
);

-- ── 认证（auth）──
-- 全局认证配置（单行 id=1）：开关 / 认证方式 / 凭据 / TTL
CREATE TABLE IF NOT EXISTS auth_config (
    id                   INTEGER PRIMARY KEY,
    enabled              INTEGER DEFAULT 0,          -- 认证总开关（0=不校验，与历史行为一致）
    mode                 TEXT    DEFAULT 'none',     -- none/jwt/bearer/basic/apikey
    secret               TEXT    DEFAULT 'mock13-jwt-secret',  -- JWT HS256 密钥
    issuer               TEXT    DEFAULT 'mock13',
    audience             TEXT    DEFAULT '',
    access_ttl           INTEGER DEFAULT 1800,       -- access_token 有效期（秒）
    refresh_ttl          INTEGER DEFAULT 604800,     -- refresh_token 有效期（秒）
    client_id            TEXT    DEFAULT 'esb_client',
    client_secret        TEXT    DEFAULT 'esb_secret',
    token_endpoint_auth  TEXT    DEFAULT 'basic',    -- basic/apikey/body/none：换票端点凭据校验方式
    apikey_header        TEXT    DEFAULT 'X-API-Key',
    apikey_value         TEXT    DEFAULT '',
    basic_user           TEXT    DEFAULT '',
    basic_pass           TEXT    DEFAULT '',
    protect_scope        TEXT    DEFAULT 'business', -- business=仅业务端点 / all=含管理 API
    response_template    TEXT    DEFAULT '',         -- 换票响应模板（@access_token @refresh_token @expires_in @token_type @client_id）
    reuse_revoke_family  INTEGER DEFAULT 1,          -- refresh 复用时吊销同 client 全部 ACTIVE
    updated_at           TEXT
);

-- 认证规则（按路径命中不同认证方式；一条规则可多方式 OR，任一通过即放行）
-- 未命中任何规则时回退 auth_config.mode（全局默认方式）
CREATE TABLE IF NOT EXISTS auth_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    path_pattern   TEXT    NOT NULL,     -- 精确路径（/api/echo）或前缀通配（/api/echo*）
    modes          TEXT    NOT NULL,     -- 逗号分隔：jwt,bearer,basic,apikey,none（多值=OR）
    enabled        INTEGER DEFAULT 1,
    priority       INTEGER DEFAULT 0,    -- 大者优先；同级按 pattern 长度（更具体优先）
    secret         TEXT    DEFAULT '',   -- 以下均为「覆盖全局」字段，留空/0 表示不覆盖
    issuer         TEXT    DEFAULT '',
    audience       TEXT    DEFAULT '',
    access_ttl     INTEGER DEFAULT 0,
    refresh_ttl    INTEGER DEFAULT 0,
    client_id      TEXT    DEFAULT '',
    client_secret  TEXT    DEFAULT '',
    apikey_header  TEXT    DEFAULT '',
    apikey_value   TEXT    DEFAULT '',
    basic_user     TEXT    DEFAULT '',
    basic_pass     TEXT    DEFAULT '',
    remark         TEXT    DEFAULT '',
    updated_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_arule_enabled ON auth_rules(enabled);

-- 已签发令牌（access + refresh 成对，明文存储以便展示与脚本取用）
CREATE TABLE IF NOT EXISTS auth_tokens (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT    NOT NULL,
    client_id          TEXT    DEFAULT '',
    grant_type         TEXT    DEFAULT 'client_credentials',
    token_kind         TEXT    DEFAULT 'jwt',        -- jwt / opaque
    access_token       TEXT    NOT NULL,
    refresh_token      TEXT    DEFAULT '',
    access_expires_at  REAL    DEFAULT 0,           -- epoch 秒
    refresh_expires_at REAL    DEFAULT 0,
    status             TEXT    DEFAULT 'ACTIVE',    -- ACTIVE/REVOKED/ROTATED/EXPIRED
    rotated_to         INTEGER,                     -- 轮换后的新令牌 id
    revoked_at         TEXT,
    last_used_at       TEXT,
    use_count          INTEGER DEFAULT 0,
    source_ip          TEXT    DEFAULT '',
    scope              TEXT    DEFAULT '',
    updated_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_atok_access  ON auth_tokens(access_token);
CREATE INDEX IF NOT EXISTS idx_atok_refresh ON auth_tokens(refresh_token);
CREATE INDEX IF NOT EXISTS idx_atok_status  ON auth_tokens(status);

-- 认证相关请求留痕：换票 / 刷新 / 入站校验 / 失效
CREATE TABLE IF NOT EXISTS auth_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    kind            TEXT    DEFAULT 'token',        -- token/refresh/verify/revoke
    endpoint        TEXT    DEFAULT '',
    method          TEXT    DEFAULT '',
    grant_type      TEXT    DEFAULT '',
    client_id       TEXT    DEFAULT '',
    source_ip       TEXT    DEFAULT '',
    request_headers TEXT    DEFAULT '',
    request_body    TEXT    DEFAULT '',
    status_code     INTEGER DEFAULT 0,
    response_body   TEXT    DEFAULT '',
    result          TEXT    DEFAULT 'success',      -- success/failed/reuse_detected/rejected/revoked
    detail          TEXT    DEFAULT '',
    elapsed_ms      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_areq_ts   ON auth_requests(ts);
CREATE INDEX IF NOT EXISTS idx_areq_kind ON auth_requests(kind);
"""


def init_db():
    """启动时调用，创建表 + 预置默认故障模式。"""
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # 旧库迁移：补充 response_headers / response_body 列（新库已含，忽略报错）
        for col in ("response_headers", "response_body"):
            try:
                conn.execute(
                    f"ALTER TABLE received_messages ADD COLUMN {col} TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在
        # 旧库迁移：mq_messages 补充消费方实例相关列
        for col, coltype in (
            ("consumer_id", "INTEGER"), ("exchange", "TEXT"),
            ("routing_key", "TEXT"), ("redelivered", "INTEGER DEFAULT 0"),
            ("delivery_tag", "INTEGER"), ("properties_json", "TEXT"),
            ("ack_action", "TEXT"), ("latency_ms", "INTEGER DEFAULT 0"),
            ("reply_body", "TEXT"), ("reply_props", "TEXT"),
        ):
            try:
                conn.execute(
                    f"ALTER TABLE mq_messages ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # 列已存在
        for t in config.KNOWN_TARGETS:
            conn.execute(
                "INSERT OR IGNORE INTO fault_mode(target, success_rate, fail_rate, "
                "timeout_rate, latency_ms, timeout_seconds, updated_at) "
                "VALUES (?, 100, 0, 0, 0, 30, ?)",
                (t, _now()),
            )
        conn.commit()


def get_conn() -> sqlite3.Connection:
    """每个线程一个连接（sqlite3 默认 check_same_thread=True）。"""
    if not hasattr(_local, "conn"):
        _local.conn = sqlite3.connect(config.FAUX_DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


@contextmanager
def cursor():
    """获取游标的上下文管理器，自动提交。"""
    with _lock:
        conn = get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
