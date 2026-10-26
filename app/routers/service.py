"""目标服务配置 + 全局参数 管理 API。

targets 表定义动态服务（路径/方法/响应码/响应模板），由 catch-all 命中后生效；
global_params 表存放全局参数，供模板 @key 引用。
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Response

from app.database import cursor
from app.models import TargetDefIn, TargetDefUpd, RenderIn, ParamIn
from app.templating import render_template

router_targets = APIRouter(prefix="/api/targets", tags=["targets"])
router_params = APIRouter(prefix="/api/params", tags=["params"])

ALLOWED_METHODS = ["POST", "PUT", "GET", "DELETE", "PATCH"]

# 代码内固定端点（优先级高于 targets 表，不允许覆盖）
FIXED_PATHS = {"/api/echo", "/api/echo2", "/api/echo3", "/api/sit-not-found"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_path(p: str) -> str:
    p = (p or "").strip()
    if not p.startswith("/"):
        p = "/" + p
    return p.rstrip("/") or "/"


# 管理 API 前缀（由固定路由响应，定义为 target 永远不会生效，保存时直接拒绝）
RESERVED_PREFIXES = (
    "/api/health", "/api/targets", "/api/params", "/api/logs", "/api/mode",
    "/api/mq", "/api/faults", "/api/metrics",
    "/api/auth",
)


def _validate_target_path(raw: str) -> str:
    """校验并规范化目标路径：任意前缀均可（/api/xxx、/svc/xxx 等），
    但不得为根路径、代码固定端点或管理 API 前缀。非法时抛 ValueError。"""
    path = _norm_path(raw)
    if path == "/":
        raise ValueError("path 不能为根路径 /，需含子路径")
    if path in FIXED_PATHS:
        raise ValueError(f"{path} 是代码固定端点，不可定义")
    for pref in RESERVED_PREFIXES:
        if path == pref or path.startswith(pref + "/"):
            raise ValueError(f"{path} 与管理 API 前缀冲突（{pref}），不可定义")
    return path


def _norm_methods(methods: Optional[list]) -> str:
    ms = []
    for m in (methods or []):
        mu = (m or "").strip().upper()
        if mu in ALLOWED_METHODS and mu not in ms:
            ms.append(mu)
    return ",".join(ms)


def get_target_by_path(path: str) -> Optional[dict]:
    """catch-all 调用：按路径取启用的服务定义。"""
    with cursor() as cur:
        cur.execute(
            "SELECT id, path, name, methods, status_code, response_template, "
            "content_type, enabled FROM targets WHERE path = ? AND enabled = 1",
            (path,))
        row = cur.fetchone()
    return dict(row) if row else None


@router_targets.get("")
def list_targets():
    """目标服务列表。"""
    with cursor() as cur:
        cur.execute(
            "SELECT id, path, name, methods, status_code, response_template, "
            "content_type, enabled, created_at, updated_at FROM targets ORDER BY path")
        rows = [dict(r) for r in cur.fetchall()]
    return {"targets": rows, "fixed_paths": sorted(FIXED_PATHS)}


@router_targets.post("")
def create_target(t: TargetDefIn):
    """新增目标服务定义。"""
    try:
        path = _validate_target_path(t.path)
    except ValueError as e:
        raise HTTPException(400, str(e))
    methods = _norm_methods(t.methods)
    if not methods:
        raise HTTPException(400, "methods 至少包含 POST/PUT/GET/DELETE/PATCH 之一")

    ts = _now()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO targets (path, name, methods, status_code, "
                "response_template, content_type, enabled, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (path, t.name, methods, t.status_code, t.response_template,
                 t.content_type, int(t.enabled), ts, ts))
            tid = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"路径 {path} 已存在")
        raise
    return {"id": tid, "path": path, "methods": methods, "created": True}


@router_targets.get("/export")
def export_targets():
    """一键导出全部配置为 JSON 文件。

    覆盖：目标服务 + 全局参数 + 故障模式/场景 + 统计指标 + MQ 消费方/生产者
    （MQ 连接密码以明文导出，供「一键导入」还原，请注意文件保存位置）。
    """
    with cursor() as cur:
        cur.execute(
            "SELECT path, name, methods, status_code, response_template, "
            "content_type, enabled FROM targets ORDER BY path")
        targets = [{
            "path": r["path"], "name": r["name"],
            "methods": [m for m in (r["methods"] or "").split(",") if m],
            "status_code": r["status_code"],
            "response_template": r["response_template"],
            "content_type": r["content_type"], "enabled": bool(r["enabled"]),
        } for r in cur.fetchall()]
        cur.execute("SELECT key, value, remark FROM global_params ORDER BY key")
        params = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT target, success_rate, fail_rate, timeout_rate, latency_ms, "
            "timeout_seconds FROM fault_mode ORDER BY target")
        modes = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT name, fault_type, probability, status_code, latency_ms, "
            "timeout_seconds, body_template, scope, enabled "
            "FROM fault_scenarios ORDER BY id")
        scenarios = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["scope"] = json.loads(d["scope"] or "[]")
            except (ValueError, TypeError):
                d["scope"] = []
            d["enabled"] = bool(d["enabled"])
            scenarios.append(d)
        cur.execute("SELECT name, keyword, enabled FROM stat_metrics ORDER BY id")
        stat_metrics = [dict(r) | {"enabled": bool(r["enabled"])}
                        for r in cur.fetchall()]
        cur.execute(
            "SELECT name, host, port, vhost, username, password, queue, prefetch, "
            "declare_mode, ack_mode, delay_ms, reply_enabled, reply_template, "
            "reply_content_type, reply_headers, fault_inject, enabled "
            "FROM mq_consumers ORDER BY id")
        mq_consumers = [dict(r) | {"enabled": bool(r["enabled"]),
                                   "reply_enabled": bool(r["reply_enabled"]),
                                   "fault_inject": bool(r["fault_inject"])}
                        for r in cur.fetchall()]
        cur.execute(
            "SELECT name, host, port, vhost, username, password, exchange, "
            "routing_key, content_type, delivery_mode, priority, sample_body, enabled "
            "FROM mq_producers ORDER BY id")
        mq_producers = [dict(r) | {"enabled": bool(r["enabled"])}
                        for r in cur.fetchall()]

    payload = {
        "version": 2, "tool": "faux-server", "exported_at": _now(),
        "targets": targets, "global_params": params, "fault_modes": modes,
        "fault_scenarios": scenarios, "stat_metrics": stat_metrics,
        "mq_consumers": mq_consumers, "mq_producers": mq_producers,
    }
    fname = "faux_export_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _valid_target_fields(t: dict) -> tuple:
    """导入校验：规范化并校验单条服务定义，非法时抛 ValueError。"""
    path = _validate_target_path(str(t.get("path") or ""))
    ms = t.get("methods") or []
    if isinstance(ms, str):
        ms = ms.split(",")
    methods = _norm_methods(ms)
    if not methods:
        raise ValueError("methods 至少包含 POST/PUT/GET/DELETE/PATCH 之一")
    status_code = int(t.get("status_code") or 200)
    if not (100 <= status_code <= 599):
        raise ValueError("status_code 需在 100-599")
    enabled = 1 if t.get("enabled", True) in (True, 1, "1", "true", "True") else 0
    return (path, str(t.get("name") or ""), methods, status_code,
            str(t.get("response_template") or ""),
            str(t.get("content_type") or "application/json"), enabled)


def _truthy(v, default=True) -> int:
    """导入用的宽松布尔转换（兼容 1/0/true/false/字符串）。"""
    if v is None:
        return 1 if default else 0
    return 1 if v in (True, 1, "1", "true", "True") else 0


def _import_mq_consumers(cur, items: list, ts: str, errors: list) -> int:
    """导入 MQ 消费方（按 name upsert）。密码为空/掩码时保留库中原值。返回处理条数。"""
    n = 0
    for mc in items:
        name = str(mc.get("name") or "").strip()
        if not name:
            continue
        ack = str(mc.get("ack_mode") or "ack")
        dm = str(mc.get("declare_mode") or "durable")
        if ack not in ("auto", "ack", "nack_requeue", "nack_drop", "reject", "hold"):
            errors.append({"mq_consumer": name, "error": f"未知 ack_mode {ack}，已跳过"})
            continue
        if dm not in ("durable", "passive"):
            errors.append({"mq_consumer": name, "error": f"未知 declare_mode {dm}，已跳过"})
            continue
        queue = str(mc.get("queue") or "").strip()
        if not queue:
            errors.append({"mq_consumer": name, "error": "queue 必填，已跳过"})
            continue
        password = str(mc.get("password") or "")
        cur.execute("SELECT id, password FROM mq_consumers WHERE name = ?", (name,))
        row = cur.fetchone()
        if row and (not password or password == "******"):
            password = row["password"]  # 掩码/空值不覆盖原密码
        vals = (name, str(mc.get("host") or "127.0.0.1"), int(mc.get("port") or 5672),
                str(mc.get("vhost") or "/"), str(mc.get("username") or "guest"),
                password, queue, int(mc.get("prefetch") or 1), dm, ack,
                int(mc.get("delay_ms") or 0),
                _truthy(mc.get("reply_enabled"), True),
                str(mc.get("reply_template")
                    or '{"messageId":"{{messageId}}","status":"{{status}}"}'),
                str(mc.get("reply_content_type") or "application/json"),
                str(mc.get("reply_headers") or ""),
                _truthy(mc.get("fault_inject"), True),
                _truthy(mc.get("enabled"), True))
        if row:
            cur.execute(
                "UPDATE mq_consumers SET name=?, host=?, port=?, vhost=?, username=?, "
                "password=?, queue=?, prefetch=?, declare_mode=?, ack_mode=?, delay_ms=?, "
                "reply_enabled=?, reply_template=?, reply_content_type=?, reply_headers=?, "
                "fault_inject=?, enabled=?, updated_at=? WHERE id=?",
                vals + (ts, row["id"]))
        else:
            cur.execute(
                "INSERT INTO mq_consumers (name, host, port, vhost, username, password, "
                "queue, prefetch, declare_mode, ack_mode, delay_ms, reply_enabled, "
                "reply_template, reply_content_type, reply_headers, fault_inject, "
                "enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                vals + (ts, ts))
        n += 1
    return n


def _import_mq_producers(cur, items: list, ts: str, errors: list) -> int:
    """导入 MQ 生产者（按 name upsert）。密码为空/掩码时保留库中原值。返回处理条数。"""
    n = 0
    for mp in items:
        name = str(mp.get("name") or "").strip()
        if not name:
            continue
        password = str(mp.get("password") or "")
        cur.execute("SELECT id, password FROM mq_producers WHERE name = ?", (name,))
        row = cur.fetchone()
        if row and (not password or password == "******"):
            password = row["password"]
        vals = (name, str(mp.get("host") or "127.0.0.1"), int(mp.get("port") or 5672),
                str(mp.get("vhost") or "/"), str(mp.get("username") or "guest"),
                password, str(mp.get("exchange") or ""), str(mp.get("routing_key") or ""),
                str(mp.get("content_type") or "application/json"),
                max(1, min(2, int(mp.get("delivery_mode") or 2))),
                max(0, min(9, int(mp.get("priority") or 0))),
                str(mp.get("sample_body") or ""),
                _truthy(mp.get("enabled"), True))
        if row:
            cur.execute(
                "UPDATE mq_producers SET name=?, host=?, port=?, vhost=?, username=?, "
                "password=?, exchange=?, routing_key=?, content_type=?, delivery_mode=?, "
                "priority=?, sample_body=?, enabled=?, updated_at=? WHERE id=?",
                vals + (ts, row["id"]))
        else:
            cur.execute(
                "INSERT INTO mq_producers (name, host, port, vhost, username, password, "
                "exchange, routing_key, content_type, delivery_mode, priority, "
                "sample_body, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                vals + (ts, ts))
        n += 1
    return n


def _fix_mojibake(obj):
    """递归修复 UTF-8 文本被按 Latin-1 误解码产生的乱码（如 æ\x88\x90å\x8a\x9f → 成功）。

    仅当字符串所有字符均在 Latin-1 范围内、且其字节序列恰好构成合法 UTF-8 时才转换
    （正常中文/ASCII 不受影响），用于自动修复经错误编码工具处理过的导入文件。
    """
    if isinstance(obj, str):
        try:
            return obj.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return obj
    if isinstance(obj, list):
        return [_fix_mojibake(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _fix_mojibake(v) for k, v in obj.items()}
    return obj


@router_targets.post("/import")
def import_targets(payload: dict, mode: str = "skip"):
    """一键批量导入配置（需「导出」生成的 JSON 文件格式）。

    mode=skip：路径已存在时跳过；mode=overwrite：覆盖更新。
    global_params / fault_modes / fault_scenarios / stat_metrics /
    mq_consumers / mq_producers 为附带导入（按唯一键 upsert，
    MQ 密码为空/掩码时保留库中原值；消费方导入后自动热同步运行实例）。
    """
    if mode not in ("skip", "overwrite"):
        raise HTTPException(400, "mode 需为 skip 或 overwrite")
    payload = _fix_mojibake(payload)
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise HTTPException(400, "请求体需为导出文件格式（含 targets 数组）")

    created = updated = skipped = 0
    errors = []
    ts = _now()
    with cursor() as cur:
        for i, t in enumerate(targets):
            try:
                if not isinstance(t, dict):
                    raise ValueError("条目需为对象")
                path, name, methods, status_code, tpl, ct, enabled = \
                    _valid_target_fields(t)
                cur.execute("SELECT id FROM targets WHERE path = ?", (path,))
                row = cur.fetchone()
                if row:
                    if mode == "overwrite":
                        cur.execute(
                            "UPDATE targets SET name=?, methods=?, status_code=?, "
                            "response_template=?, content_type=?, enabled=?, "
                            "updated_at=? WHERE id=?",
                            (name, methods, status_code, tpl, ct, enabled, ts,
                             row["id"]))
                        updated += 1
                    else:
                        skipped += 1
                else:
                    cur.execute(
                        "INSERT INTO targets (path, name, methods, status_code, "
                        "response_template, content_type, enabled, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (path, name, methods, status_code, tpl, ct, enabled,
                         ts, ts))
                    created += 1
            except Exception as e:
                errors.append({
                    "index": i,
                    "path": t.get("path") if isinstance(t, dict) else None,
                    "error": str(e)})

        for p in payload.get("global_params") or []:
            key = str(p.get("key") or "").strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                cur.execute(
                    "INSERT INTO global_params (key, value, remark, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                    "value=excluded.value, remark=excluded.remark, "
                    "updated_at=excluded.updated_at",
                    (key, str(p.get("value") or ""), str(p.get("remark") or ""), ts))

        for m in payload.get("fault_modes") or []:
            tgt = str(m.get("target") or "").strip()
            if not tgt:
                continue
            total = (int(m.get("success_rate") or 0) + int(m.get("fail_rate") or 0)
                     + int(m.get("timeout_rate") or 0))
            if total > 100:
                errors.append({"target": tgt,
                               "error": f"概率之和 {total} 超过 100，已跳过"})
                continue
            cur.execute(
                "INSERT INTO fault_mode (target, success_rate, fail_rate, "
                "timeout_rate, latency_ms, timeout_seconds, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(target) DO UPDATE SET "
                "success_rate=excluded.success_rate, fail_rate=excluded.fail_rate, "
                "timeout_rate=excluded.timeout_rate, latency_ms=excluded.latency_ms, "
                "timeout_seconds=excluded.timeout_seconds, "
                "updated_at=excluded.updated_at",
                (tgt, int(m.get("success_rate") or 0),
                 int(m.get("fail_rate") or 0), int(m.get("timeout_rate") or 0),
                 int(m.get("latency_ms") or 0), int(m.get("timeout_seconds") or 30),
                 ts))

        for sc in payload.get("fault_scenarios") or []:
            name = str(sc.get("name") or "").strip()
            ftype = str(sc.get("fault_type") or "fail").strip()
            if not name:
                continue
            if ftype not in ("fail", "timeout", "latency", "custom"):
                errors.append({"scenario": name, "error": f"未知类型 {ftype}，已跳过"})
                continue
            scope = sc.get("scope") or []
            if isinstance(scope, str):
                try:
                    scope = json.loads(scope)
                except (ValueError, TypeError):
                    scope = []
            cur.execute(
                "INSERT INTO fault_scenarios (name, fault_type, probability, "
                "status_code, latency_ms, timeout_seconds, body_template, "
                "scope, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "fault_type=excluded.fault_type, probability=excluded.probability, "
                "status_code=excluded.status_code, latency_ms=excluded.latency_ms, "
                "timeout_seconds=excluded.timeout_seconds, "
                "body_template=excluded.body_template, scope=excluded.scope, "
                "enabled=excluded.enabled, updated_at=excluded.updated_at",
                (name, ftype,
                 max(0, min(100, int(sc.get("probability") or 100))),
                 max(100, min(599, int(sc.get("status_code") or 500))),
                 max(0, int(sc.get("latency_ms") or 0)),
                 max(0, int(sc.get("timeout_seconds") or 30)),
                 str(sc.get("body_template") or ""),
                 json.dumps([s for s in scope if isinstance(s, str) and s.strip()]),
                 1 if sc.get("enabled", True) in (True, 1, "1", "true") else 0,
                 ts))

        for sm in payload.get("stat_metrics") or []:
            mname = str(sm.get("name") or "").strip()
            kw = str(sm.get("keyword") or "").strip()
            if not mname or not kw:
                continue
            cur.execute(
                "INSERT INTO stat_metrics (name, keyword, enabled, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "keyword=excluded.keyword, enabled=excluded.enabled, "
                "updated_at=excluded.updated_at",
                (mname, kw, _truthy(sm.get("enabled"), True), ts))

        mq_consumers_n = _import_mq_consumers(
            cur, payload.get("mq_consumers") or [], ts, errors)
        mq_producers_n = _import_mq_producers(
            cur, payload.get("mq_producers") or [], ts, errors)

    # 消费方配置有变 → 热同步运行实例（启停/热重启，无需重启服务）
    if mq_consumers_n:
        from app.mq_workers import mq_manager
        mq_manager.reload_all()

    return {"created": created, "updated": updated, "skipped": skipped,
            "params_imported": len(payload.get("global_params") or []),
            "modes_imported": len(payload.get("fault_modes") or []),
            "scenarios_imported": len(payload.get("fault_scenarios") or []),
            "metrics_imported": len(payload.get("stat_metrics") or []),
            "mq_consumers_imported": mq_consumers_n,
            "mq_producers_imported": mq_producers_n,
            "errors": errors}


@router_targets.put("/{tid}")
def update_target(tid: int, t: TargetDefUpd):
    """更新目标服务定义。"""
    with cursor() as cur:
        cur.execute("SELECT id FROM targets WHERE id = ?", (tid,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"目标 #{tid} 不存在")

        sets, vals = [], []
        if t.path is not None:
            try:
                path = _validate_target_path(t.path)
            except ValueError as e:
                raise HTTPException(400, str(e))
            sets.append("path=?")
            vals.append(path)
        if t.name is not None:
            sets.append("name=?")
            vals.append(t.name)
        if t.methods is not None:
            methods = _norm_methods(t.methods)
            if not methods:
                raise HTTPException(400, "methods 至少包含 POST/PUT/GET/DELETE/PATCH 之一")
            sets.append("methods=?")
            vals.append(methods)
        if t.status_code is not None:
            sets.append("status_code=?")
            vals.append(t.status_code)
        if t.response_template is not None:
            sets.append("response_template=?")
            vals.append(t.response_template)
        if t.content_type is not None:
            sets.append("content_type=?")
            vals.append(t.content_type)
        if t.enabled is not None:
            sets.append("enabled=?")
            vals.append(int(t.enabled))
        if not sets:
            raise HTTPException(400, "无更新字段")

        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(tid)
        cur.execute(f"UPDATE targets SET {', '.join(sets)} WHERE id = ?", vals)
    return {"id": tid, "updated": True}


@router_targets.delete("/{tid}")
def delete_target(tid: int):
    """删除目标服务定义。"""
    with cursor() as cur:
        cur.execute("DELETE FROM targets WHERE id = ?", (tid,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"目标 #{tid} 不存在")
    return {"id": tid, "deleted": True}


@router_targets.post("/render")
def render_preview(r: RenderIn):
    """模板渲染预览（动态参数 + 全局参数取值）。"""
    return {"rendered": render_template(r.template)}


# ── 全局参数 ──

@router_params.get("")
def list_params():
    """全局参数列表。"""
    with cursor() as cur:
        cur.execute("SELECT key, value, remark, updated_at FROM global_params ORDER BY key")
        return {"params": [dict(r) for r in cur.fetchall()]}


@router_params.put("")
def set_param(p: ParamIn):
    """新增/更新全局参数。"""
    key = (p.key or "").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise HTTPException(400, "key 需为字母/下划线开头的标识符（与模板 @key 引用一致）")
    ts = _now()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO global_params (key, value, remark, updated_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, remark=excluded.remark, updated_at=excluded.updated_at",
            (key, p.value, p.remark, ts))
    return {"key": key, "saved": True}


@router_params.delete("/{key}")
def delete_param(key: str):
    """删除全局参数。"""
    with cursor() as cur:
        cur.execute("DELETE FROM global_params WHERE key = ?", (key,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"参数 {key} 不存在")
    return {"key": key, "deleted": True}
