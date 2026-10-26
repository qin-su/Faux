"""认证服务 — 配置管理、令牌签发/刷新/校验/失效、认证请求留痕。

面向两类使用方：
  1) 作为「被调用的下游」：开启认证后，业务请求必须携带合法凭据，否则 401；
     提供 /api/auth/token 换票端点（client_credentials / password / refresh_token），
     与调用方出站动态令牌（令牌管理器 + refreshEndpoint 形式的刷新）对接。
  2) SIT 脚本：全部能力均有 REST 接口（见 routers/auth.py），不依赖页面。

令牌状态：
  ACTIVE   可用（未过期、未吊销）
  EXPIRED  access 已过期（refresh 可能仍有效 → 可用于刷新）
  ROTATED  已被刷新轮换（旧 refresh 复用 = 疑似泄漏）
  REVOKED  已人工/自动吊销
"""

import base64
import json
import time
from datetime import datetime, timezone
from typing import Optional

from app import authcore
from app.database import cursor

# 认证方式
MODES = ("none", "jwt", "bearer", "basic", "apikey")
# 换票端点凭据校验方式
ENDPOINT_AUTHS = ("basic", "apikey", "body", "none")
# 保护范围
PROTECT_SCOPES = ("business", "all")

# 不做认证的路径前缀（管理 API / 认证端点自身 / 静态与文档）
MANAGEMENT_PREFIXES = (
    "/api/auth", "/api/logs", "/api/targets", "/api/params", "/api/mode",
    "/api/faults", "/api/metrics", "/api/mq",
    "/api/health",
)
OPEN_PATHS = {"/", "/docs", "/redoc", "/openapi.json", "/favicon.ico"}
OPEN_PREFIXES = ("/static", "/docs", "/redoc", "/openapi.json")

DEFAULT_CONFIG = {
    "enabled": 0,
    "mode": "none",
    "secret": "faux-jwt-secret",
    "issuer": "faux",
    "audience": "",
    "access_ttl": 1800,
    "refresh_ttl": 604800,
    "client_id": "mock_client",
    "client_secret": "mock_secret",
    "token_endpoint_auth": "basic",
    "apikey_header": "X-API-Key",
    "apikey_value": "",
    "basic_user": "",
    "basic_pass": "",
    "protect_scope": "business",
    "response_template": "",
    "reuse_revoke_family": 1,
}

CONFIG_FIELDS = tuple(DEFAULT_CONFIG.keys())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(epoch: float) -> Optional[str]:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat() if epoch else None


# ── 配置 ──

def get_config() -> dict:
    """读取全局认证配置（单行 id=1，不存在则建默认行）。"""
    with cursor() as cur:
        cur.execute("SELECT * FROM auth_config WHERE id = 1")
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT OR IGNORE INTO auth_config (id, updated_at) VALUES (1, ?)",
                (_now(),))
            cur.execute("SELECT * FROM auth_config WHERE id = 1")
            row = cur.fetchone()
        return dict(row)


def update_config(patch: dict) -> dict:
    """局部更新配置（只接受 CONFIG_FIELDS 中的键）。"""
    sets, vals = [], []
    for key, value in (patch or {}).items():
        if key not in CONFIG_FIELDS:
            continue
        if key in ("enabled", "reuse_revoke_family", "access_ttl", "refresh_ttl"):
            value = int(value)
        sets.append(f"{key}=?")
        vals.append(value)
    if not sets:
        return get_config()
    sets.append("updated_at=?")
    vals.append(_now())
    vals.append(1)
    with cursor() as cur:
        cur.execute(f"UPDATE auth_config SET {', '.join(sets)} WHERE id = ?", vals)
    return get_config()


def public_config(cfg: dict) -> dict:
    """对外展示的配置（补上派生字段）。"""
    out = {k: cfg.get(k) for k in CONFIG_FIELDS}
    out["id"] = cfg.get("id", 1)
    out["updated_at"] = cfg.get("updated_at")
    out["active"] = bool(cfg.get("enabled")) and cfg.get("mode") != "none"
    return out


# ── 令牌表操作 ──

def _row_to_token(row) -> dict:
    d = dict(row)
    now = time.time()
    status = d.get("status") or "ACTIVE"
    access_expired = bool(d.get("access_expires_at")) and now > d["access_expires_at"]
    refresh_expired = bool(d.get("refresh_expires_at")) and now > d["refresh_expires_at"]
    d["computed_status"] = "EXPIRED" if (status == "ACTIVE" and access_expired) else status
    d["access_expired"] = access_expired
    d["refresh_expired"] = refresh_expired
    d["access_expires_iso"] = _iso(d.get("access_expires_at") or 0)
    d["refresh_expires_iso"] = _iso(d.get("refresh_expires_at") or 0)
    d["access_left_s"] = max(0, int((d.get("access_expires_at") or 0) - now))
    d["refresh_left_s"] = max(0, int((d.get("refresh_expires_at") or 0) - now))
    d["access_masked"] = authcore.mask(d.get("access_token") or "")
    d["refresh_masked"] = authcore.mask(d.get("refresh_token") or "")
    d["access_fp"] = authcore.fingerprint(d.get("access_token") or "")
    return d


def _default_token_kind(cfg: dict) -> str:
    """推断令牌形态：全局方式或任一启用规则含 jwt → 签 JWT。

    JWT 串同样登记在 auth_tokens 表，因此 bearer 模式也能校验它；
    反之不透明令牌无法过 jwt 模式，故「有场景需要 JWT 就签 JWT」最兼容。
    """
    modes = cfg.get("_modes") or parse_modes(cfg.get("mode"))
    if "jwt" in modes:
        return "jwt"
    for rule in list_rules(enabled_only=True):
        if "jwt" in parse_modes(rule.get("modes")):
            return "jwt"
    return "opaque"


def issue_tokens(cfg: dict, client_id: str = "", grant_type: str = "client_credentials",
                 source_ip: str = "", scope: str = "",
                 access_ttl: Optional[int] = None,
                 refresh_ttl: Optional[int] = None,
                 kind: Optional[str] = None) -> dict:
    """签发一对令牌（access + refresh），返回 auth_tokens 行（明文）。

    kind 可显式指定 "jwt"/"opaque"；留空则按当前配置与规则推断。
    """
    now = time.time()
    a_ttl = int(access_ttl if access_ttl is not None else cfg.get("access_ttl") or 1800)
    r_ttl = int(refresh_ttl if refresh_ttl is not None else cfg.get("refresh_ttl") or 604800)
    kind = (kind or "").strip().lower() or _default_token_kind(cfg)

    with cursor() as cur:
        cur.execute(
            "INSERT INTO auth_tokens (ts, client_id, grant_type, token_kind, "
            "access_token, refresh_token, access_expires_at, refresh_expires_at, "
            "status, source_ip, scope, updated_at) "
            "VALUES (?,?,?,?,'','',?,?,'ACTIVE',?,?,?)",
            (_now(), client_id or "", grant_type, kind,
             now + a_ttl, now + r_ttl, source_ip or "", scope or "", _now()))
        tid = cur.lastrowid

    if kind == "jwt":
        payload = {
            "iss": cfg.get("issuer") or "faux",
            "sub": client_id or "",
            "iat": int(now),
            "exp": int(now + a_ttl),
            "jti": str(tid),
        }
        if cfg.get("audience"):
            payload["aud"] = cfg["audience"]
        if scope:
            payload["scope"] = scope
        access = authcore.sign_jwt(payload, cfg.get("secret") or "")
    else:
        access = authcore.new_opaque_token()
    refresh = authcore.new_refresh_token()

    with cursor() as cur:
        cur.execute(
            "UPDATE auth_tokens SET access_token=?, refresh_token=?, updated_at=? "
            "WHERE id=?", (access, refresh, _now(), tid))
        cur.execute("SELECT * FROM auth_tokens WHERE id=?", (tid,))
        return _row_to_token(cur.fetchone())


def find_by_access(token: str) -> Optional[dict]:
    if not token:
        return None
    with cursor() as cur:
        cur.execute("SELECT * FROM auth_tokens WHERE access_token=?", (token,))
        row = cur.fetchone()
    return _row_to_token(row) if row else None


def find_by_refresh(token: str) -> Optional[dict]:
    if not token:
        return None
    with cursor() as cur:
        cur.execute("SELECT * FROM auth_tokens WHERE refresh_token=? "
                    "ORDER BY id DESC LIMIT 1", (token,))
        row = cur.fetchone()
    return _row_to_token(row) if row else None


def find_by_id(tid: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM auth_tokens WHERE id=?", (tid,))
        row = cur.fetchone()
    return _row_to_token(row) if row else None


def touch(tid: int):
    with cursor() as cur:
        cur.execute("UPDATE auth_tokens SET last_used_at=?, use_count=use_count+1 "
                    "WHERE id=?", (_now(), tid))


def revoke_token(tid: int, reason: str = "manual") -> Optional[dict]:
    """吊销单条令牌（整对作废：access 与 refresh 同时失效）。"""
    with cursor() as cur:
        cur.execute(
            "UPDATE auth_tokens SET status='REVOKED', revoked_at=?, updated_at=? "
            "WHERE id=? AND status IN ('ACTIVE','EXPIRED')",
            (_now(), _now(), tid))
        if cur.rowcount == 0:
            cur.execute("SELECT * FROM auth_tokens WHERE id=?", (tid,))
            row = cur.fetchone()
            return _row_to_token(row) if row else None
        cur.execute("SELECT * FROM auth_tokens WHERE id=?", (tid,))
        row = _row_to_token(cur.fetchone())
    log_request(kind="revoke", endpoint=f"/api/auth/tokens/{tid}/revoke",
                client_id=row.get("client_id", ""), result="revoked",
                status_code=200, detail=reason,
                response_body=json.dumps({"id": tid, "status": "REVOKED"},
                                         ensure_ascii=False))
    return row


def revoke_all(client_id: str = "") -> int:
    """吊销全部（或指定 client 的）有效令牌，返回条数。"""
    with cursor() as cur:
        if client_id:
            cur.execute(
                "UPDATE auth_tokens SET status='REVOKED', revoked_at=?, updated_at=? "
                "WHERE status IN ('ACTIVE','EXPIRED') AND client_id=?",
                (_now(), _now(), client_id))
        else:
            cur.execute(
                "UPDATE auth_tokens SET status='REVOKED', revoked_at=?, updated_at=? "
                "WHERE status IN ('ACTIVE','EXPIRED')", (_now(), _now()))
        n = cur.rowcount
    log_request(kind="revoke", endpoint="/api/auth/revoke-all",
                client_id=client_id or "*", result="revoked", status_code=200,
                detail=f"吊销 {n} 条", response_body=json.dumps({"revoked": n}))
    return n


def expire_access(client_id: str = "") -> int:
    """仅让 access 立即过期（refresh 仍有效）——用于验证「401 → refresh」分支。

    与 revoke 的区别：revoke 整对作废（refresh 也废，对端只能重新完整认证）；
    expire-access 保留 refresh，对端应走刷新流程拿到新 access。
    """
    now = time.time()
    with cursor() as cur:
        if client_id:
            cur.execute(
                "UPDATE auth_tokens SET access_expires_at=?, status='EXPIRED', "
                "updated_at=? WHERE status='ACTIVE' AND client_id=?",
                (now - 1, _now(), client_id))
        else:
            cur.execute(
                "UPDATE auth_tokens SET access_expires_at=?, status='EXPIRED', "
                "updated_at=? WHERE status='ACTIVE'", (now - 1, _now()))
        n = cur.rowcount
    log_request(kind="revoke", endpoint="/api/auth/expire-access",
                client_id=client_id or "*", result="revoked", status_code=200,
                detail=f"access 立即过期 {n} 条（refresh 仍可用）",
                response_body=json.dumps({"expired": n}))
    return n


def list_tokens(status: str = "", client_id: str = "", limit: int = 100) -> list:
    sql = "SELECT * FROM auth_tokens WHERE 1=1"
    params: list = []
    if client_id:
        sql += " AND client_id LIKE ?"
        params.append(f"%{client_id}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(limit, 1000)))
    with cursor() as cur:
        cur.execute(sql, params)
        rows = [_row_to_token(r) for r in cur.fetchall()]
    if status:
        rows = [r for r in rows if r["computed_status"].upper() == status.upper()]
    return rows


def stats() -> dict:
    now = time.time()
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM auth_tokens")
        total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_tokens WHERE status='ACTIVE'")
        active_db = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM auth_tokens WHERE status='ACTIVE' "
            "AND access_expires_at > ?", (now,))
        active_valid = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_tokens WHERE status='REVOKED'")
        revoked = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_tokens WHERE status='ROTATED'")
        rotated = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_requests")
        requests_total = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_requests WHERE kind='token'")
        issues = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM auth_requests WHERE kind='refresh'")
        refreshes = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM auth_requests WHERE result IN "
            "('failed','rejected','reuse_detected')")
        failures = cur.fetchone()[0]
    return {"tokens_total": total, "tokens_active": active_valid,
            "tokens_active_expired": active_db - active_valid,
            "tokens_revoked": revoked, "tokens_rotated": rotated,
            "requests_total": requests_total, "issues": issues,
            "refreshes": refreshes, "failures": failures}


# ── 认证请求留痕 ──

def log_request(kind: str, endpoint: str = "", method: str = "", grant_type: str = "",
                client_id: str = "", source_ip: str = "", request_headers: str = "",
                request_body: str = "", status_code: int = 0, response_body: str = "",
                result: str = "success", detail: str = "", elapsed_ms: int = 0):
    """写入 auth_requests（认证相关请求的一行留痕）。"""
    with cursor() as cur:
        cur.execute(
            "INSERT INTO auth_requests (ts, kind, endpoint, method, grant_type, "
            "client_id, source_ip, request_headers, request_body, status_code, "
            "response_body, result, detail, elapsed_ms) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), kind, endpoint, method, grant_type, client_id, source_ip,
             request_headers, request_body, status_code, response_body, result,
             detail, elapsed_ms))


def list_requests(kind: str = "", result: str = "", client_id: str = "",
                 limit: int = 100, offset: int = 0) -> tuple:
    sql = "SELECT * FROM auth_requests WHERE 1=1"
    params: list = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if result:
        sql += " AND result=?"
        params.append(result)
    if client_id:
        sql += " AND client_id LIKE ?"
        params.append(f"%{client_id}%")
    count_sql = f"SELECT count(*) FROM ({sql})"
    with cursor() as cur:
        cur.execute(count_sql, params)
        total = cur.fetchone()[0]
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += [max(1, min(limit, 1000)), max(0, offset)]
    with cursor() as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    return total, rows


def reset_requests() -> int:
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM auth_requests")
        n = cur.fetchone()[0]
        cur.execute("DELETE FROM auth_requests")
    return n


def reset_all() -> dict:
    """重置全部认证数据（令牌 + 留痕），不重置配置。"""
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM auth_tokens")
        t = cur.fetchone()[0]
        cur.execute("DELETE FROM auth_tokens")
        cur.execute("SELECT count(*) FROM auth_requests")
        r = cur.fetchone()[0]
        cur.execute("DELETE FROM auth_requests")
    return {"tokens_deleted": t, "requests_deleted": r}


# ── 认证规则（按路径命中不同认证方式；一条规则可多方式 OR）──

RULE_FIELDS = ("path_pattern", "modes", "enabled", "priority", "secret", "issuer",
               "audience", "access_ttl", "refresh_ttl", "client_id", "client_secret",
               "apikey_header", "apikey_value", "basic_user", "basic_pass", "remark")
RULE_INT_FIELDS = ("enabled", "priority", "access_ttl", "refresh_ttl")
RULE_OVERRIDE_STR = ("secret", "issuer", "audience", "client_id", "client_secret",
                     "apikey_header", "apikey_value", "basic_user", "basic_pass")
RULE_OVERRIDE_INT = ("access_ttl", "refresh_ttl")


def parse_modes(text) -> list:
    """解析方式（"jwt,apikey" → ["jwt","apikey"]）；也接受已解析的列表。

    非法值丢弃，去重保序。
    """
    items = text if isinstance(text, (list, tuple)) else str(text or "").split(",")
    out = []
    for item in items:
        m = str(item).strip().lower()
        if m and m in MODES and m not in out:
            out.append(m)
    return out


def _rule_to_dict(row) -> dict:
    d = dict(row)
    d["modes"] = parse_modes(d.get("modes"))
    return d


def list_rules(enabled_only: bool = False) -> list:
    sql = "SELECT * FROM auth_rules"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY priority DESC, LENGTH(path_pattern) DESC, id DESC"
    with cursor() as cur:
        cur.execute(sql)
        return [_rule_to_dict(r) for r in cur.fetchall()]


def get_rule(rid: int) -> Optional[dict]:
    with cursor() as cur:
        cur.execute("SELECT * FROM auth_rules WHERE id=?", (rid,))
        row = cur.fetchone()
    return _rule_to_dict(row) if row else None


def create_rule(data: dict) -> dict:
    fields = [f for f in RULE_FIELDS if f in data]
    if not fields:
        raise ValueError("缺少可写入字段")
    vals = [int(data[f]) if f in RULE_INT_FIELDS else data[f] for f in fields]
    with cursor() as cur:
        cur.execute(
            f"INSERT INTO auth_rules ({', '.join(fields)}, updated_at) "
            f"VALUES ({', '.join('?' * len(fields))}, ?)", vals + [_now()])
        rid = cur.lastrowid
    return get_rule(rid)


def update_rule(rid: int, data: dict) -> Optional[dict]:
    fields = [f for f in RULE_FIELDS if f in data]
    if not fields:
        return get_rule(rid)
    vals = [int(data[f]) if f in RULE_INT_FIELDS else data[f] for f in fields]
    with cursor() as cur:
        cur.execute(
            f"UPDATE auth_rules SET {', '.join(f + '=?' for f in fields)}, "
            f"updated_at=? WHERE id=?", vals + [_now(), rid])
    return get_rule(rid)


def delete_rule(rid: int) -> bool:
    with cursor() as cur:
        cur.execute("DELETE FROM auth_rules WHERE id=?", (rid,))
        return cur.rowcount > 0


def _pattern_match(pattern: str, path: str) -> bool:
    """路径匹配：以 * 结尾=前缀通配，否则精确匹配。"""
    p = (pattern or "").strip()
    if not p:
        return False
    if p.endswith("*"):
        return path.startswith(p[:-1])
    return path == p


def match_rule(path: str) -> Optional[dict]:
    """返回命中的规则：priority 大者优先，同级按 pattern 长度（更具体优先）。"""
    for rule in list_rules(enabled_only=True):
        if _pattern_match(rule.get("path_pattern"), path):
            return rule
    return None


def effective_config(cfg: dict, rule: Optional[dict]) -> dict:
    """用规则中的非空字段覆盖全局配置（留空 / 0 表示不覆盖）。"""
    eff = dict(cfg)
    if not rule:
        return eff
    for key in RULE_OVERRIDE_STR:
        if rule.get(key):
            eff[key] = rule[key]
    for key in RULE_OVERRIDE_INT:
        if int(rule.get(key) or 0) > 0:
            eff[key] = int(rule[key])
    return eff


def resolve(cfg: dict, path: str) -> tuple:
    """解析某路径的生效认证方式。返回 (modes, rule, effective_cfg)。

    modes 为空或含 none → 该路径不做认证。
    """
    rule = match_rule(path)
    modes = parse_modes(rule.get("modes")) if rule else parse_modes(cfg.get("mode"))
    eff = effective_config(cfg, rule)
    eff["_modes"] = modes          # 供签发时推断令牌形态（JWT 可被 bearer 模式同样校验）
    return modes, rule, eff


# ── 入站校验 ──

def should_protect(cfg: dict, path: str) -> bool:
    """该路径是否需要认证（管理 API 与认证端点自身永不拦截）。

    由「路径规则 → 全局默认方式」解析出的 modes 决定：
    modes 为空或含 none → 该路径不认证（可实现「部分端点免认证」）。
    """
    if not cfg.get("enabled"):
        return False
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return False
    if path.startswith(MANAGEMENT_PREFIXES):
        # protect_scope=all 时连管理 API 也保护（慎用，页面会 401）
        return cfg.get("protect_scope") == "all"
    modes, _, _ = resolve(cfg, path)
    return bool(modes) and "none" not in modes


def gate(cfg: dict, path: str, headers: dict, source_ip: str = "") -> dict:
    """中间件总入口：一次完成「是否需保护 + 是否通过」判定。

    排除顺序：总开关 → 开放路径 → 管理 API（protect_scope=all 除外）→ 路径规则解析。
    """
    if not cfg.get("enabled"):
        return {"need_auth": False, "ok": True, "detail": "", "modes": [], "rule": None}
    if path in OPEN_PATHS or path.startswith(OPEN_PREFIXES):
        return {"need_auth": False, "ok": True, "detail": "", "modes": [], "rule": None}
    if path.startswith(MANAGEMENT_PREFIXES) and cfg.get("protect_scope") != "all":
        return {"need_auth": False, "ok": True, "detail": "", "modes": [], "rule": None}
    return check_request(cfg, path, headers, source_ip=source_ip)


def check_request(cfg: dict, path: str, headers: dict, method: str = "",
                  source_ip: str = "") -> dict:
    """入站校验总入口（中间件用）：一次解析 + 多方式 OR 判定（任一通过即放行）。

    返回 {need_auth, ok, detail, modes, rule, matched_mode}
    """
    modes, rule, eff = resolve(cfg, path)
    if not modes or "none" in modes:
        return {"need_auth": False, "ok": True, "detail": "", "modes": modes,
                "rule": rule}
    reasons = []
    for m in modes:
        ok, detail = _verify_mode(eff, m, headers)
        if ok:
            return {"need_auth": True, "ok": True, "detail": "", "modes": modes,
                    "rule": rule, "matched_mode": m}
        reasons.append(f"[{m}] {detail}")
    return {"need_auth": True, "ok": False, "detail": "；".join(reasons),
            "modes": modes, "rule": rule}


def _basic_pair(header_value: str) -> tuple:
    """解析 Basic 头 → (user, password)。"""
    raw = (header_value or "").strip()
    if not raw.lower().startswith("basic "):
        return "", ""
    try:
        decoded = base64.b64decode(raw[6:].strip()).decode("utf-8", "replace")
    except Exception:
        return "", ""
    user, _, pwd = decoded.partition(":")
    return user, pwd


def verify_request(cfg: dict, headers: dict, path: str = "", method: str = "",
                   source_ip: str = "") -> tuple:
    """校验业务请求。返回 (是否通过, 失败原因)。

    按路径规则解析出的一到多种方式依次尝试，任一通过即放行（OR）。
    """
    if not cfg.get("enabled"):
        return True, ""
    result = check_request(cfg, path, headers, method, source_ip)
    return result["ok"], result["detail"]


def _verify_mode(cfg: dict, mode: str, headers: dict) -> tuple:
    """按单一方式校验。返回 (是否通过, 失败原因)。

    jwt=校验 Bearer JWT（签名+exp+jti 状态）；bearer=不透明令牌查表；
    basic=Basic 用户名密码；apikey=自定义头比对。
    """
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    auth = lower.get("authorization", "") or ""

    if mode == "jwt":
        if not auth.lower().startswith("bearer "):
            return False, "缺少 Authorization: Bearer <JWT> 头"
        token = auth[7:].strip()
        ok, payload, reason = authcore.verify_jwt(token, cfg.get("secret") or "")
        if not ok:
            return False, reason
        jti = (payload or {}).get("jti")
        if jti is None or not str(jti).isdigit():
            return False, "JWT 缺少有效 jti（非本服务签发）"
        row = find_by_id(int(jti))
        if row is None:
            return False, "令牌未在本服务签发（jti 未知）"
        if row["status"] == "REVOKED":
            return False, "令牌已被吊销"
        if row["status"] == "ROTATED":
            return False, "令牌已被刷新轮换（请使用新 access_token）"
        if row["access_expires_at"] and time.time() > row["access_expires_at"]:
            return False, "令牌已过期"
        touch(row["id"])
        return True, ""

    if mode == "bearer":
        if not auth.lower().startswith("bearer "):
            return False, "缺少 Authorization: Bearer <token> 头"
        token = auth[7:].strip()
        row = find_by_access(token)
        if row is None:
            return False, "令牌无效（未签发或已清理）"
        if row["status"] != "ACTIVE":
            return False, f"令牌已失效（status={row['status']}）"
        if row["access_expires_at"] and time.time() > row["access_expires_at"]:
            return False, "令牌已过期"
        touch(row["id"])
        return True, ""

    if mode == "basic":
        user, pwd = _basic_pair(auth)
        if not user:
            return False, "缺少 Authorization: Basic 头"
        expect_user = cfg.get("basic_user") or cfg.get("client_id") or ""
        expect_pass = cfg.get("basic_pass") or cfg.get("client_secret") or ""
        if user != expect_user or pwd != expect_pass:
            return False, "Basic 用户名或密码错误"
        return True, ""

    if mode == "apikey":
        header_name = (cfg.get("apikey_header") or "X-API-Key").strip()
        provided = lower.get(header_name.lower(), "")
        expect = cfg.get("apikey_value") or cfg.get("client_secret") or ""
        if not provided:
            return False, f"缺少 {header_name} 头"
        if provided != expect:
            return False, f"{header_name} 值错误"
        return True, ""

    return True, ""


def verify_token(cfg: dict, token: str, path: str = "") -> dict:
    """独立校验一个令牌（供 SIT 脚本断言，不产生业务副作用）。

    按路径规则解析出的多种方式依次尝试，任一通过即视为有效（OR）。
    """
    modes, _, eff = resolve(cfg, path)
    modes = modes or [cfg.get("mode") or "none"]
    last = None
    for m in modes:
        res = _verify_token_mode(eff, m, token)
        if res.get("valid"):
            return res
        last = res
    return last or {"valid": False, "reason": "无可用认证方式"}


def _verify_token_mode(cfg: dict, mode: str, token: str) -> dict:
    if mode == "jwt":
        ok, payload, reason = authcore.verify_jwt(token, cfg.get("secret") or "")
        if not ok:
            return {"valid": False, "mode": mode, "reason": reason}
        jti = (payload or {}).get("jti")
        row = find_by_id(int(jti)) if str(jti).isdigit() else None
        if row is None:
            return {"valid": False, "mode": mode, "reason": "jti 未登记",
                    "payload": payload}
        if row["status"] != "ACTIVE":
            return {"valid": False, "mode": mode,
                    "reason": f"令牌状态 {row['status']}", "payload": payload,
                    "token_id": row["id"], "client_id": row["client_id"]}
        return {"valid": True, "mode": mode, "payload": payload,
                "token_id": row["id"], "client_id": row["client_id"],
                "expires_at": row["access_expires_iso"]}

    row = find_by_access(token)
    if mode == "bearer":
        if row is None:
            return {"valid": False, "mode": mode, "reason": "令牌未登记"}
        if row["status"] != "ACTIVE":
            return {"valid": False, "mode": mode,
                    "reason": f"令牌状态 {row['status']}",
                    "token_id": row["id"], "client_id": row["client_id"]}
        expired = bool(row["access_expires_at"]) and time.time() > row["access_expires_at"]
        return {"valid": not expired, "mode": mode,
                "reason": "" if not expired else "令牌已过期",
                "token_id": row["id"], "client_id": row["client_id"],
                "expires_at": row["access_expires_iso"]}

    # basic / apikey 模式：按「原始凭据」比对
    if mode == "basic":
        user, pwd = _basic_pair(token if token.lower().startswith("basic ")
                                else "Basic " + token)
        okc = user == (cfg.get("basic_user") or cfg.get("client_id") or "") and \
            pwd == (cfg.get("basic_pass") or cfg.get("client_secret") or "")
        return {"valid": okc, "mode": mode, "reason": "" if okc else "Basic 凭据不匹配"}
    if mode == "apikey":
        okc = token == (cfg.get("apikey_value") or cfg.get("client_secret") or "")
        return {"valid": okc, "mode": mode, "reason": "" if okc else "API Key 不匹配"}
    return {"valid": False, "mode": mode, "reason": f"未知认证方式 {mode}"}


# ── 换票端点 ──

def _render_response(cfg: dict, row: dict) -> dict:
    """按 response_template 渲染换票响应；未配置则返回标准 OAuth2 结构。"""
    std = {
        "access_token": row["access_token"],
        "token_type": "Bearer",
        "expires_in": int(row["access_expires_at"] - time.time()),
        "refresh_token": row["refresh_token"],
        "refresh_expires_in": int(row["refresh_expires_at"] - time.time()),
        "scope": row.get("scope") or "",
    }
    tpl = (cfg.get("response_template") or "").strip()
    if not tpl:
        return std
    rendered = (tpl
                .replace("@access_token", std["access_token"])
                .replace("@refresh_token", std["refresh_token"])
                .replace("@token_type", std["token_type"])
                .replace("@expires_in", str(std["expires_in"]))
                .replace("@refresh_expires_in", str(std["refresh_expires_in"]))
                .replace("@client_id", row.get("client_id") or ""))
    try:
        parsed = json.loads(rendered)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    return {"_text": rendered}


def check_endpoint_credentials(cfg: dict, headers: dict, params: dict) -> tuple:
    """校验换票请求携带的客户端凭据。返回 (是否通过, client_id, 失败原因)。"""
    how = (cfg.get("token_endpoint_auth") or "basic").strip().lower()
    client_id = str(params.get("client_id") or "")
    client_secret = str(params.get("client_secret") or "")
    if how == "basic":
        user, pwd = _basic_pair(headers.get("authorization", ""))
        if user:
            client_id, client_secret = user, pwd
    elif how == "apikey":
        name = (cfg.get("apikey_header") or "X-API-Key").strip()
        provided = headers.get(name.lower()) or headers.get(name) or ""
        if provided:
            client_secret = provided
    # body / none：直接用参数里的 client_id / client_secret

    expect_id = cfg.get("client_id") or ""
    expect_secret = cfg.get("client_secret") or ""
    if not expect_id and not expect_secret:
        # 未配置凭据 → 宽松通过（沿用「无配置即不校验」的服务口径）
        return True, client_id, ""
    if client_id != expect_id or client_secret != expect_secret:
        return False, client_id, "client_id / client_secret 校验失败"
    return True, client_id, ""


def grant_client_credentials(cfg: dict, client_id: str, source_ip: str = "",
                             scope: str = "") -> dict:
    row = issue_tokens(cfg, client_id=client_id or (cfg.get("client_id") or ""),
                       grant_type="client_credentials", source_ip=source_ip, scope=scope)
    return {"status": 200, "body": _render_response(cfg, row), "token": row,
            "result": "success"}


def grant_password(cfg: dict, params: dict, source_ip: str = "") -> dict:
    """password 模式：用户名/密码换票。

    未单独配置 basic_user/basic_pass 时，回退用 client_id/client_secret 比对。
    """
    user = str(params.get("username") or "")
    pwd = str(params.get("password") or "")
    expect_user = cfg.get("basic_user") or cfg.get("client_id") or ""
    expect_pass = cfg.get("basic_pass") or cfg.get("client_secret") or ""
    if (expect_user or expect_pass) and (user != expect_user or pwd != expect_pass):
        return {"status": 401, "result": "failed",
                "body": {"error": "invalid_grant",
                         "error_description": "username 或 password 错误"}}
    row = issue_tokens(cfg, client_id=user or (cfg.get("client_id") or ""),
                       grant_type="password", source_ip=source_ip)
    return {"status": 200, "body": _render_response(cfg, row), "token": row,
            "result": "success"}


def record_rejected(path: str, method: str, headers: dict, detail: str,
                    source_ip: str = ""):
    """入站校验失败留痕：auth_requests（kind=verify）+ received_messages（保持实收日志完整）。"""
    body = json.dumps({"error": "unauthorized", "detail": detail}, ensure_ascii=False)
    lower = {str(k).lower(): v for k, v in (headers or {}).items()}
    log_request(kind="verify", endpoint=path, method=method, source_ip=source_ip,
                request_headers=json.dumps(dict(headers or {}), ensure_ascii=False),
                status_code=401, response_body=body, result="rejected", detail=detail)
    target_name = (path or "/").lstrip("/").replace("/", "-") or "root"
    with cursor() as cur:
        cur.execute(
            "INSERT INTO received_messages (ts, target, method, path, content_type, "
            "headers, body, body_len, marker, status_code, response_type, "
            "response_headers, response_body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), target_name, method, path, lower.get("content-type"),
             json.dumps(dict(headers or {}), ensure_ascii=False), "", 0, None,
             401, "unauthorized",
             json.dumps({"WWW-Authenticate": "Bearer"}, ensure_ascii=False), body))


def grant_refresh(cfg: dict, refresh_token: str, source_ip: str = "") -> dict:
    """refresh_token 模式：一次一换（轮换），旧 refresh 作废。

    复用旧 refresh（ROTATED/REVOKED）视为疑似泄漏：默认吊销同 client 全部 ACTIVE。
    """
    if not refresh_token:
        return {"status": 400, "result": "failed",
                "body": {"error": "invalid_grant",
                         "error_description": "缺少 refresh_token"}}
    row = find_by_refresh(refresh_token)
    if row is None:
        return {"status": 400, "result": "failed",
                "body": {"error": "invalid_grant",
                         "error_description": "refresh_token 不存在或已失效"}}
    if row["status"] in ("ROTATED", "REVOKED"):
        revoked = 0
        if cfg.get("reuse_revoke_family"):
            revoked = revoke_all(client_id=row.get("client_id") or "")
        return {"status": 400, "result": "reuse_detected",
                "body": {"error": "invalid_grant",
                         "error_description":
                             f"refresh_token 已被使用（疑似泄漏），已吊销同 client "
                             f"{revoked} 条令牌，请重新完整认证"}}
    if row["refresh_expires_at"] and time.time() > row["refresh_expires_at"]:
        with cursor() as cur:
            cur.execute("UPDATE auth_tokens SET status='EXPIRED', updated_at=? "
                        "WHERE id=?", (_now(), row["id"]))
        return {"status": 400, "result": "failed",
                "body": {"error": "invalid_grant",
                         "error_description": "refresh_token 已过期，请重新认证"}}

    new_row = issue_tokens(cfg, client_id=row.get("client_id") or "",
                           grant_type="refresh_token", source_ip=source_ip,
                           scope=row.get("scope") or "")
    with cursor() as cur:
        cur.execute("UPDATE auth_tokens SET status='ROTATED', rotated_to=?, "
                    "updated_at=? WHERE id=?",
                    (new_row["id"], _now(), row["id"]))
    return {"status": 200, "body": _render_response(cfg, new_row),
            "token": new_row, "result": "success", "rotated_from": row["id"]}
