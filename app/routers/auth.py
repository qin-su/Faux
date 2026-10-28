"""认证 API — 配置开关、换票端点、令牌管理、认证请求留痕。

全部能力均有 REST 接口，SIT 脚本可直接调用（无需页面）：
  配置    GET/PUT  /api/auth/config
  换票    POST|GET /api/auth/token          （client_credentials / password / refresh_token）
  签发    POST     /api/auth/issue          （管理端直接签发，跳过凭据校验）
  令牌    GET      /api/auth/tokens         （含明文，便于脚本取用）
          GET      /api/auth/tokens/{id}
          POST     /api/auth/tokens/{id}/revoke
          POST     /api/auth/revoke-all     （整对作废：refresh 也废）
          POST     /api/auth/expire-access  （仅 access 过期，refresh 仍可刷新）
  校验    POST     /api/auth/verify
  留痕    GET      /api/auth/requests
          POST     /api/auth/requests/reset
  统计    GET      /api/auth/stats
  重置    POST     /api/auth/reset          （清令牌 + 留痕，保留配置）
"""

import json
import time
from typing import Optional
from urllib.parse import parse_qs

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app import authservice
from app.models import (AuthConfigUpd, AuthIssueIn, AuthRevokeIn, AuthRuleIn,
                        AuthRuleUpd, AuthVerifyIn)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _headers_json(request: Request) -> str:
    return json.dumps(dict(request.headers), ensure_ascii=False)


async def _collect(request: Request) -> dict:
    """汇总请求参数：query + JSON body + form body（兼容调用方 json/form 形态）。"""
    params: dict = {k: v for k, v in request.query_params.items()}
    ctype = (request.headers.get("content-type") or "").lower()
    raw = (await request.body()).decode("utf-8", "replace")
    if raw.strip():
        if "json" in ctype or raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    params.update({str(k): v for k, v in obj.items()})
            except ValueError:
                pass
        else:
            try:
                parsed = parse_qs(raw, keep_blank_values=True)
                params.update({k: v[0] for k, v in parsed.items() if v})
            except Exception:
                pass
    return params


def _elapsed(start: float) -> int:
    return int((time.time() - start) * 1000)


def _effective_for(cfg: dict, params: dict) -> dict:
    """按可选 path 参数解析该路径的生效配置（规则覆盖密钥/TTL，并决定令牌形态）。"""
    path = str((params or {}).get("path") or "").strip()
    if not path:
        eff = dict(cfg)
        eff["_modes"] = authservice.parse_modes(cfg.get("mode"))
        return eff
    _, _, eff = authservice.resolve(cfg, path)
    return eff


# ── 配置 ──

@router.get("/config")
def get_config():
    """读取认证配置（含派生字段 active=开关开启且方式非 none）。"""
    return authservice.public_config(authservice.get_config())


@router.put("/config")
def set_config(patch: AuthConfigUpd):
    """更新认证配置（仅提交字段生效）。

    mode=none 或 enabled=False → 不做任何入站校验（历史行为）。
    """
    data = patch.model_dump(exclude_unset=True)
    if "mode" in data and data["mode"] not in authservice.MODES:
        return JSONResponse(
            {"error": "bad_mode",
             "detail": f"mode 需为 {list(authservice.MODES)}"}, status_code=400)
    if "token_endpoint_auth" in data and \
            data["token_endpoint_auth"] not in authservice.ENDPOINT_AUTHS:
        return JSONResponse(
            {"error": "bad_token_endpoint_auth",
             "detail": f"token_endpoint_auth 需为 {list(authservice.ENDPOINT_AUTHS)}"},
            status_code=400)
    if "protect_scope" in data and data["protect_scope"] not in authservice.PROTECT_SCOPES:
        return JSONResponse(
            {"error": "bad_protect_scope",
             "detail": f"protect_scope 需为 {list(authservice.PROTECT_SCOPES)}"},
            status_code=400)
    cfg = authservice.update_config(data)
    return authservice.public_config(cfg)


# ── 换票端点（对调用方 / 客户端）──

@router.api_route("/token", methods=["POST", "GET"])
async def token(request: Request):
    """OAuth2 风格换票端点（支持 JSON / form / query 三种传参）。

    成功：200 + {"access_token","token_type","expires_in","refresh_token",...}
    失败：400/401 + {"error","error_description"}（对齐 OAuth2 错误语义）
    """
    cfg = authservice.get_config()
    start = time.time()
    params = await _collect(request)
    # 可选 path=：按目标业务路径命中的规则取密钥 / TTL / 令牌形态（多重认证场景用）
    eff = _effective_for(cfg, params)
    headers = {str(k).lower(): v for k, v in request.headers.items()}
    grant = str(params.get("grant_type") or "client_credentials").strip()
    ip = _ip(request)
    log_common = {
        "endpoint": "/api/auth/token", "method": request.method, "grant_type": grant,
        "source_ip": ip, "request_headers": _headers_json(request),
        "request_body": json.dumps(params, ensure_ascii=False),
    }

    ok, cid, why = authservice.check_endpoint_credentials(cfg, headers, params)
    if not ok:
        body = {"error": "invalid_client", "error_description": why}
        authservice.log_request(
            kind="token", client_id=cid, status_code=401, result="failed",
            detail=why, response_body=json.dumps(body, ensure_ascii=False),
            elapsed_ms=_elapsed(start), **log_common)
        return JSONResponse(body, status_code=401)

    if grant == "refresh_token":
        res = authservice.grant_refresh(eff, str(params.get("refresh_token") or ""), ip)
        kind = "refresh"
    elif grant == "password":
        res = authservice.grant_password(eff, params, ip)
        kind = "token"
    elif grant in ("client_credentials", ""):
        res = authservice.grant_client_credentials(
            eff, cid, ip, str(params.get("scope") or ""))
        kind = "token"
    else:
        res = {"status": 400, "result": "failed",
               "body": {"error": "unsupported_grant_type",
                        "error_description": f"不支持 grant_type={grant}"}}
        kind = "token"

    resp_body = res["body"]
    extra_headers = None
    if isinstance(resp_body, dict) and isinstance(resp_body.get("_headers"), dict):
        # response_template 支持响应头注入（供把令牌置于响应头的调用方使用）；
        # 占位符已在 _render_response 统一替换
        extra_headers = resp_body.pop("_headers")
    detail = ""
    if res.get("rotated_from"):
        detail = f"由令牌 #{res['rotated_from']} 轮换而来"
    elif res["result"] == "reuse_detected":
        detail = "refresh_token 复用（疑似泄漏）"
    authservice.log_request(
        kind=kind, client_id=(res.get("token") or {}).get("client_id", cid),
        status_code=res["status"], result=res["result"],
        detail=detail or (res.get("body", {}).get("error_description", "")),
        response_body=json.dumps(res["body"], ensure_ascii=False)[:4000],
        elapsed_ms=_elapsed(start), **log_common)
    resp = JSONResponse(resp_body, status_code=res["status"])
    if extra_headers:
        for _k, _v in extra_headers.items():
            resp.headers[str(_k)] = str(_v)
    return resp


# ── 令牌管理 ──

@router.post("/issue")
async def issue(req: AuthIssueIn, request: Request):
    """管理端直接签发一对令牌（不做凭据校验）。

    path= 可按目标业务路径命中的规则取密钥 / TTL；kind= 可显式指定 jwt / opaque。
    """
    cfg = authservice.get_config()
    eff = _effective_for(cfg, {"path": req.path or ""})
    start = time.time()
    row = authservice.issue_tokens(
        eff, client_id=req.client_id or (eff.get("client_id") or ""),
        grant_type=req.grant_type, source_ip=_ip(request), scope=req.scope,
        access_ttl=req.access_ttl, refresh_ttl=req.refresh_ttl, kind=req.kind)
    authservice.log_request(
        kind="token", endpoint="/api/auth/issue", method="POST",
        grant_type=req.grant_type, client_id=row["client_id"], source_ip=_ip(request),
        status_code=200, result="success", detail="管理端签发",
        response_body=json.dumps({"id": row["id"],
                                  "access_fp": row["access_fp"]}, ensure_ascii=False),
        elapsed_ms=_elapsed(start))
    return row


@router.get("/tokens")
def list_tokens(status: str = Query("", description="按状态过滤：ACTIVE/EXPIRED/ROTATED/REVOKED"),
                client_id: str = Query("", description="按 client 模糊过滤"),
                limit: int = Query(100, ge=1, le=1000)):
    """令牌列表（含明文 access/refresh，便于脚本取用与页面展示）。"""
    rows = authservice.list_tokens(status=status, client_id=client_id, limit=limit)
    return {"total": len(rows), "items": rows}


@router.get("/tokens/{tid}")
def get_token(tid: int):
    row = authservice.find_by_id(tid)
    if row is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return row


@router.post("/tokens/{tid}/revoke")
def revoke_token(tid: int):
    """立即失效单条令牌（access 与 refresh 同时作废）。"""
    row = authservice.revoke_token(tid, reason="页面/接口手动失效")
    if row is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    return {"id": tid, "status": row["status"], "revoked": True}


@router.post("/revoke-all")
def revoke_all(req: AuthRevokeIn = None):
    """批量失效（整对作废，refresh 也废 → 对端只能重新完整认证）。"""
    cid = (req.client_id if req else "") or ""
    n = authservice.revoke_all(client_id=cid)
    return {"revoked": n, "client_id": cid or "*"}


@router.post("/expire-access")
def expire_access(req: AuthRevokeIn = None):
    """仅让 access 立即过期，refresh 仍有效（用于验证 401 → refresh 分支）。"""
    cid = (req.client_id if req else "") or ""
    n = authservice.expire_access(client_id=cid)
    return {"expired": n, "client_id": cid or "*"}


@router.post("/verify")
def verify(req: AuthVerifyIn):
    """校验令牌是否有效（不产生业务副作用）。

    可带 path= 指定业务路径：按该路径命中的规则（方式 / 密钥）校验，多方式 OR。
    """
    cfg = authservice.get_config()
    result = authservice.verify_token(cfg, req.token, req.path or "")
    authservice.log_request(
        kind="verify", endpoint="/api/auth/verify", method="POST",
        status_code=200, result="success" if result.get("valid") else "failed",
        detail=result.get("reason", ""),
        request_body=json.dumps({"token": req.token[:16] + "…"}, ensure_ascii=False),
        response_body=json.dumps(result, ensure_ascii=False))
    return result


# ── 认证规则（按路径多重认证）──

def _validate_modes(modes: str) -> tuple:
    """校验方式串：返回 (合法, 解析结果)。"""
    parsed = authservice.parse_modes(modes)
    if not parsed:
        return False, parsed
    return True, parsed


@router.get("/rules")
def list_rules():
    """认证规则列表（priority 大者优先，同级 pattern 更长者优先）。"""
    return {"items": authservice.list_rules()}


@router.post("/rules")
def create_rule(req: AuthRuleIn):
    """新增认证规则。modes 逗号分隔（jwt,apikey = 任一通过即放行）。"""
    ok, parsed = _validate_modes(req.modes)
    if not ok:
        return JSONResponse(
            {"error": "bad_modes",
             "detail": f"modes 需为 {list(authservice.MODES)} 中的一项或多项（逗号分隔）"},
            status_code=400)
    data = req.model_dump(exclude_unset=True)
    row = authservice.create_rule(data)
    log_rule("create", row, f"新增规则 {row['path_pattern']} → {','.join(parsed)}")
    return row


@router.put("/rules/{rid}")
def update_rule(rid: int, req: AuthRuleUpd):
    """更新认证规则（仅提交字段生效）。"""
    data = req.model_dump(exclude_unset=True)
    if "modes" in data:
        ok, parsed = _validate_modes(data["modes"])
        if not ok:
            return JSONResponse(
                {"error": "bad_modes",
                 "detail": f"modes 需为 {list(authservice.MODES)} 中的一项或多项"},
                status_code=400)
    row = authservice.update_rule(rid, data)
    if row is None:
        return JSONResponse({"detail": "not found"}, status_code=404)
    log_rule("update", row, f"更新规则 #{rid}")
    return row


@router.delete("/rules/{rid}")
def delete_rule(rid: int):
    row = authservice.get_rule(rid)
    if not authservice.delete_rule(rid):
        return JSONResponse({"detail": "not found"}, status_code=404)
    log_rule("delete", {"id": rid, "path_pattern": (row or {}).get("path_pattern", ""),
                        "client_id": ""}, f"删除规则 #{rid}")
    return {"deleted": True, "id": rid}


@router.get("/resolve")
def resolve(path: str = Query(..., description="业务路径，如 /api/echo"),
            probe: str = Query("", description="可选的冒烟路径（同上，用于 GET 便捷调用）")):
    """调试用：查看某路径最终命中哪条规则、用哪些方式与密钥（不产生副作用）。"""
    cfg = authservice.get_config()
    target = path or probe
    modes, rule, eff = authservice.resolve(cfg, target)
    return {
        "path": target,
        "enabled": bool(cfg.get("enabled")),
        "protected": bool(cfg.get("enabled")) and bool(modes) and "none" not in modes,
        "modes": modes,
        "rule": rule,
        "token_kind": authservice._default_token_kind(eff),
        "effective": {k: eff.get(k) for k in (
            "secret", "issuer", "audience", "access_ttl", "refresh_ttl",
            "apikey_header", "apikey_value", "basic_user",
            "client_id", "client_secret")},
    }


def log_rule(action: str, row: dict, detail: str):
    authservice.log_request(
        kind="revoke", endpoint="/api/auth/rules", method=action.upper(),
        status_code=200, result="success", detail=detail,
        request_body=json.dumps({"action": action}, ensure_ascii=False),
        response_body=json.dumps({k: row.get(k) for k in
                                  ("id", "path_pattern", "modes", "priority")},
                                 ensure_ascii=False))


# ── 留痕 ──

@router.get("/requests")
def list_requests(kind: str = Query("", description="token/refresh/verify/revoke"),
                  result: str = Query("", description="success/failed/rejected/reuse_detected/revoked"),
                  client_id: str = Query("", description="按 client 模糊过滤"),
                  limit: int = Query(100, ge=1, le=1000),
                  offset: int = Query(0, ge=0)):
    """认证请求留痕（换票/刷新/入站拒绝/失效），含请求头体与响应体。"""
    total, rows = authservice.list_requests(
        kind=kind, result=result, client_id=client_id, limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@router.post("/requests/reset")
def reset_requests():
    n = authservice.reset_requests()
    return {"deleted": n}


@router.get("/stats")
def stats():
    """认证概览计数。"""
    return authservice.stats()


@router.post("/reset")
def reset_all():
    """清空令牌与留痕（保留配置）。"""
    return authservice.reset_all()
