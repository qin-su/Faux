"""Smart Faux Server — FastAPI 主入口。

启动: uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
"""

import json
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import authservice, config
from app.database import init_db, cursor
from app.routers import target, logs, mode, mq, service, faults, metrics, auth
from app.templating import render_template
from app.mq_consumer import mq_consumer
from app.mq_workers import mq_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化 SQLite。"""
    init_db()
    print(f"[Smart Faux] SQLite 已初始化: {config.FAUX_DB_PATH}")
    # MQ 消费者
    if config.MQ_ENABLED:
        print(f"[Smart Faux] MQ Consumer: {config.MQ_HOST}:{config.MQ_PORT}")
        mq_consumer.start(auto_queues=config.MQ_AUTO_QUEUES)
    else:
        print("[Smart Faux] MQ Consumer: disabled (set MQ_ENABLED=true to enable)")
    # MQ 消费方实例（mq_consumers 表，enabled=1 的自动拉起）
    try:
        mq_manager.reload_all()
        n = len(mq_manager.statuses())
        if n:
            print(f"[Smart Faux] MQ 消费方实例已拉起: {n} 个")
    except Exception as e:
        print(f"[Smart Faux] MQ 消费方启动异常: {e}")
    yield
    # 停止 MQ 消费者
    if mq_consumer.running:
        mq_consumer.stop()
    mq_manager.stop_all()


app = FastAPI(
    title="Smart Faux Server",
    description="通用 Mock 目标服务 + 管理 API + Web Dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

# ── 静态文件 ──
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# ── 固定路由（优先于 catch-all）──

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Web Dashboard 主页。"""
    index = _static_dir / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Smart Faux Server</h1><p>static/index.html not found</p>")


@app.get("/api/health")
async def health_self():
    """Smart Faux 自身健康检查。"""
    return {"status": "ok", "service": "smart-faux", "version": "1.0.0"}


# ── 注册管理 API 路由 ──
app.include_router(target.router)      # /api/echo, /api/echo2, /api/echo3, /api/sit-not-found
app.include_router(logs.router)        # /api/logs/*
app.include_router(mode.router)        # /api/mode/*
app.include_router(mq.router)          # /api/mq/*
app.include_router(service.router_targets)  # /api/targets/*（服务配置）
app.include_router(service.router_params)   # /api/params/*（全局参数）
app.include_router(faults.router)           # /api/faults/*（故障场景）
app.include_router(metrics.router)          # /api/metrics/*（统计指标）
app.include_router(auth.router)             # /api/auth/*（认证：配置/换票/令牌/留痕）


# ── 入站认证（默认关闭，关闭时与历史行为完全一致：来者不拒）──
# 开启后（auth_config.enabled=1 且 mode != none）对「业务端点」校验凭据：
#   jwt    → Authorization: Bearer <JWT>，校验签名 / exp / jti 状态（可即时失效）
#   bearer → Authorization: Bearer <token>，查 auth_tokens 表
#   basic  → Authorization: Basic base64(user:pass)
#   apikey → 自定义头（默认 X-API-Key）比对
# 管理 API（/api/logs、/api/targets…）与认证端点自身（/api/auth/*）永不拦截，
# 避免锁死页面；protect_scope=all 时管理 API 也纳入保护。
@app.middleware("http")
async def auth_gate(request: Request, call_next):
    try:
        cfg = authservice.get_config()
    except Exception:
        return await call_next(request)  # 配置不可读时放行，不影响既有链路

    path = request.url.path
    headers = {str(k).lower(): v for k, v in request.headers.items()}
    result = authservice.gate(cfg, path, headers,
                              request.client.host if request.client else "")
    if not result["need_auth"] or result["ok"]:
        return await call_next(request)

    detail = result["detail"]
    modes = result.get("modes") or []
    mode = ",".join(modes) or cfg.get("mode")
    if "basic" in modes:
        challenge = 'Basic realm="mock13"'
    elif "apikey" in modes:
        challenge = 'ApiKey realm="mock13"'
    elif "jwt" in modes or "bearer" in modes:
        challenge = 'Bearer realm="mock13", error="invalid_token"'
    else:
        challenge = 'Bearer realm="mock13"'
    body = json.dumps({"error": "unauthorized", "detail": detail,
                       "mode": mode}, ensure_ascii=False)
    authservice.record_rejected(path, request.method, request.headers, detail,
                                request.client.host if request.client else "")
    return Response(content=body.encode("utf-8"), status_code=401,
                    media_type="application/json",
                    headers={"WWW-Authenticate": challenge})


# ── catch-all 目标端点（必须最后注册，避免拦截管理 API）──
# 支持任意路径前缀（/api/xxx、/svc/xxx 等）；固定路由与管理 API 先注册，优先级更高。
# 注意：装饰器自下而上注册，/api/{path:path} 需先入路由表，保证 /api/xxx 的
# target_name 维持旧语义（不含 api- 前缀），兼容存量 fault_mode / 消息记录。
@app.api_route("/{path:path}", methods=["POST", "PUT", "GET", "DELETE", "PATCH"])
@app.api_route("/api/{path:path}", methods=["POST", "PUT", "GET", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request):
    """通用 catch-all — 命中 targets 表定义则按配置响应（模板渲染）；否则默认记录 + 200。

    响应链：targets 表定义（启用 + 方法匹配）→ 故障模式叠加 → @参数模板渲染。
    """
    target_name = path.replace("/", "-")
    full_path = request.url.path
    raw_bytes = await request.body()

    # 1) 服务定义（targets 表，enabled=1）
    tdef = service.get_target_by_path(full_path)
    if tdef:
        allowed = [m.strip().upper() for m in (tdef["methods"] or "").split(",") if m.strip()]
        if request.method.upper() not in allowed:
            resp = target._json_response(
                {"error": "method not allowed", "target": target_name,
                 "allowed": allowed}, 405)
            target._record(target_name, request, raw_bytes, resp, "method_not_allowed")
            return resp

        # 故障场景注入（fault_scenarios 表，优先于旧版 fault_mode）
        inj = faults.apply_scenarios(full_path, target_name)
        if inj:
            code, body, rtype = inj
            resp = Response(content=body.encode("utf-8"), status_code=code,
                            media_type="application/json")
            target._record(target_name, request, raw_bytes, resp, rtype)
            return resp

        # 故障模式叠加（fault_mode 表，未配置则 100% 成功）
        m = target._get_mode(target_name)
        roll = random.randint(1, 100)
        if roll <= m["fail_rate"]:
            resp = target._json_response(
                {"error": "injected fail", "target": target_name}, 500)
            target._record(target_name, request, raw_bytes, resp, "fail")
            return resp
        if roll <= m["fail_rate"] + m["timeout_rate"]:
            time.sleep(m["timeout_seconds"])
            resp = target._json_response(
                {"error": "timeout", "target": target_name}, 504)
            target._record(target_name, request, raw_bytes, resp, "timeout")
            return resp
        if m["latency_ms"] > 0:
            time.sleep(m["latency_ms"] / 1000)

        body = render_template(tdef["response_template"] or "")
        if not body:
            body = json.dumps({"status": "ok", "target": target_name},
                              ensure_ascii=False)
        resp = Response(content=body.encode("utf-8"), status_code=tdef["status_code"],
                        media_type=tdef["content_type"] or "application/json")
        target._record(target_name, request, raw_bytes, resp, "success")
        return resp

    # 2) 未定义路径：故障场景注入（scope 空 = 对全部未定义路径生效）
    inj = faults.apply_scenarios(full_path, target_name)
    if inj:
        code, body, rtype = inj
        resp = Response(content=body.encode("utf-8"), status_code=code,
                        media_type="application/json")
        target._record(target_name, request, raw_bytes, resp, rtype)
        return resp

    # 3) fault_mode 已注册则应用故障模式，否则记录 + 返回 200
    with cursor() as cur:
        cur.execute("SELECT 1 FROM fault_mode WHERE target = ?", (target_name,))
        registered = cur.fetchone() is not None

    if registered:
        status_code, resp_body, resp_type = target._apply_fault(target_name)
        resp = target._json_response(resp_body, status_code)
        target._record(target_name, request, raw_bytes, resp, resp_type)
        return resp

    # 未注册目标：记录 + 返回 200
    resp = target._json_response(
        {"status": "ok", "target": target_name, "path": path}, 200)
    target._record(target_name, request, raw_bytes, resp, "success")
    return resp
