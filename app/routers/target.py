"""目标端点 — 上游投递方路由转发的接收方。

所有 /api/echo* 和 /api/sit-not-found 端点共享同一处理逻辑：
1. 记录请求到 SQLite
2. 查该目标的故障模式
3. 按概率决定返回成功/失败/超时
4. 返回响应
"""

import json
import random
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from app.database import cursor
from app.routers import faults

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_marker(body: str) -> str:
    """从 JSON body 中提取 marker 字段（测试用例约定的追踪标记）。"""
    if not body:
        return ""
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            return str(obj.get("marker") or obj.get("tc") or "")
    except Exception:
        pass
    return ""


def _get_mode(target: str) -> dict:
    """从 SQLite 读取目标的故障模式。"""
    with cursor() as cur:
        cur.execute(
            "SELECT success_rate, fail_rate, timeout_rate, latency_ms, timeout_seconds "
            "FROM fault_mode WHERE target = ?", (target,))
        row = cur.fetchone()
    if not row:
        return {"success_rate": 100, "fail_rate": 0, "timeout_rate": 0,
                "latency_ms": 0, "timeout_seconds": 30}
    return dict(row)


def _apply_fault(target: str) -> tuple[int, str, str]:
    """决定固定端点的本次响应。返回 (status_code, body_str, response_type)。

    优先级：故障场景（fault_scenarios，scope 可匹配固定端点）→ 旧版 fault_mode。
    """
    inj = faults.apply_scenarios("/api/" + target.replace("-", "/"), target)
    if inj:
        return inj

    m = _get_mode(target)
    r = random.randint(1, 100)

    # 优先级：fail > timeout > success
    if r <= m["fail_rate"]:
        return 500, json.dumps(
            {"error": "injected fail", "target": target}, ensure_ascii=False), "fail"

    if r <= m["fail_rate"] + m["timeout_rate"]:
        time.sleep(m["timeout_seconds"])
        return 504, json.dumps(
            {"error": "timeout", "target": target}, ensure_ascii=False), "timeout"

    if m["latency_ms"] > 0:
        time.sleep(m["latency_ms"] / 1000)

    return 200, json.dumps(
        {"status": "ok", "target": target}, ensure_ascii=False), "success"


def _record(target: str, request: Request, raw_bytes: bytes,
            resp: Response, resp_type: str):
    """记录收到的消息及实际返回的响应到 SQLite。"""
    body_str = raw_bytes.decode("utf-8", "replace")
    marker = _extract_marker(body_str)
    headers_json = json.dumps(dict(request.headers), ensure_ascii=False)
    resp_headers_json = json.dumps(dict(resp.headers), ensure_ascii=False)
    resp_body_str = resp.body.decode("utf-8", "replace") if resp.body else ""

    with cursor() as cur:
        cur.execute(
            "INSERT INTO received_messages "
            "(ts, target, method, path, content_type, headers, body, body_len, "
            "marker, status_code, response_type, response_headers, response_body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), target, request.method, str(request.url.path),
             request.headers.get("content-type"), headers_json,
             body_str, len(raw_bytes), marker or None,
             resp.status_code, resp_type, resp_headers_json, resp_body_str))


def _json_response(body: dict, status_code: int) -> Response:
    payload = json.dumps(body, ensure_ascii=False).encode()
    return Response(content=payload, media_type="application/json",
                    status_code=status_code)


def _text_response(body: str, status_code: int) -> Response:
    return Response(content=body.encode("utf-8"), media_type="application/json",
                    status_code=status_code)


# ── 路由定义 ──

@router.api_route("/api/echo", methods=["POST", "PUT", "GET"])
async def echo(request: Request):
    """主目标端点 — 默认返回 200，受故障场景/模式控制。"""
    raw_bytes = await request.body()
    status_code, body, resp_type = _apply_fault("echo")
    resp = _text_response(body, status_code)
    _record("echo", request, raw_bytes, resp, resp_type)
    return resp


@router.api_route("/api/echo2", methods=["POST", "PUT", "GET"])
async def echo2(request: Request):
    """第二目标端点（模拟 192.168.2.206）。"""
    raw_bytes = await request.body()
    status_code, body, resp_type = _apply_fault("echo2")
    resp = _text_response(body, status_code)
    _record("echo2", request, raw_bytes, resp, resp_type)
    return resp


@router.api_route("/api/echo3", methods=["POST", "PUT", "GET"])
async def echo3(request: Request):
    """第三目标端点（模拟 206:5173）。"""
    raw_bytes = await request.body()
    status_code, body, resp_type = _apply_fault("echo3")
    resp = _text_response(body, status_code)
    _record("echo3", request, raw_bytes, resp, resp_type)
    return resp


@router.api_route("/api/sit-not-found", methods=["POST", "PUT", "GET"])
async def sit_not_found(request: Request):
    """专门返回 404 的目标端点。"""
    raw_bytes = await request.body()
    resp = _json_response({"error": "not found", "target": "sit-not-found"}, 404)
    _record("sit-not-found", request, raw_bytes, resp, "not_found")
    return resp


# catch-all 路由在 main.py 中最后注册，避免拦截 /api/logs /api/mode 等管理 API
