"""实收日志管理 API。"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query

from app.database import cursor

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
def list_logs(
    marker: Optional[str] = Query(None, description="按 marker 模糊过滤"),
    target: Optional[str] = Query(None, description="按目标模糊过滤"),
    path: Optional[str] = Query(None, description="按路径模糊过滤"),
    kw: Optional[str] = Query(None, description="全局关键词：模糊匹配请求头/请求体/响应头/响应体/路径/marker/目标"),
    resp_type: Optional[str] = Query(None, description="按响应类型精确过滤（success/fail/timeout/not_found/method_not_allowed）"),
    minutes: int = Query(0, ge=0, description="最近 N 分钟（0=不限）"),
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """实收消息全量模糊过滤查询（分页，最新在前）。"""
    sql = ("SELECT id, ts, target, method, path, content_type, headers, "
           "body, body_len, marker, status_code, response_type, "
           "response_headers, response_body "
           "FROM received_messages WHERE 1=1")
    params: list = []

    def like(col: str, val: str):
        nonlocal sql, params
        sql += f" AND {col} LIKE ?"
        params.append(f"%{val}%")

    if marker:
        like("marker", marker)
    if target:
        like("target", target)
    if path:
        like("path", path)
    if resp_type:
        sql += " AND response_type = ?"
        params.append(resp_type)
    if kw:
        k = f"%{kw}%"
        sql += (" AND (path LIKE ? OR coalesce(marker,'') LIKE ? "
                "OR coalesce(target,'') LIKE ? OR coalesce(headers,'') LIKE ? "
                "OR coalesce(body,'') LIKE ? OR coalesce(response_headers,'') LIKE ? "
                "OR coalesce(response_body,'') LIKE ?)")
        params += [k] * 7
    if minutes > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        sql += " AND ts >= ?"
        params.append(cutoff)

    # 总数
    count_sql = f"SELECT count(*) FROM ({sql})"
    with cursor() as cur:
        cur.execute(count_sql, params)
        total = cur.fetchone()[0]

    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with cursor() as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]

    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@router.get("/count")
def count_logs(marker: str = Query(..., description="按 marker 计数")):
    """按 marker 统计实收数量。"""
    with cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM received_messages WHERE marker = ?", (marker,))
        total = cur.fetchone()[0]

        cur.execute(
            "SELECT target, count(*) as cnt FROM received_messages "
            "WHERE marker = ? GROUP BY target", (marker,))
        by_target = {r["target"]: r["cnt"] for r in cur.fetchall()}

    return {"marker": marker, "count": total, "by_target": by_target}


@router.get("/stats")
def stats():
    """实收消息统计摘要。"""
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM received_messages")
        total = cur.fetchone()[0]

        cur.execute(
            "SELECT target, count(*) as cnt FROM received_messages "
            "GROUP BY target ORDER BY cnt DESC")
        by_target = {r["target"]: r["cnt"] for r in cur.fetchall()}

        cur.execute(
            "SELECT status_code, count(*) as cnt FROM received_messages "
            "GROUP BY status_code ORDER BY cnt DESC")
        by_status = {str(r["status_code"]): r["cnt"] for r in cur.fetchall()}

        cur.execute(
            "SELECT response_type, count(*) as cnt FROM received_messages "
            "GROUP BY response_type ORDER BY cnt DESC")
        by_type = {r["response_type"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT max(ts) FROM received_messages")
        last_ts = cur.fetchone()[0]

        # 近 24 小时收包数
        cutoff24 = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cur.execute("SELECT count(*) FROM received_messages WHERE ts >= ?",
                    (cutoff24,))
        recent_24h = cur.fetchone()[0]

        # 按配置指标统计（keyword 对 path/target 模糊匹配）
        by_metric = {}
        cur.execute("SELECT name, keyword FROM stat_metrics WHERE enabled = 1 "
                    "ORDER BY id")
        for m in cur.fetchall():
            k = f"%{m['keyword']}%"
            cur.execute(
                "SELECT count(*) FROM received_messages "
                "WHERE path LIKE ? OR target LIKE ?", (k, k))
            by_metric[m["name"]] = cur.fetchone()[0]

    return {"total": total, "recent_24h": recent_24h, "by_target": by_target,
            "by_status": by_status, "by_response_type": by_type,
            "by_metric": by_metric, "last_received": last_ts}


@router.post("/reset")
def reset_logs():
    """清空所有实收记录。"""
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM received_messages")
        deleted = cur.fetchone()[0]
        cur.execute("DELETE FROM received_messages")
    return {"deleted": deleted}


# 注意：动态路由必须注册在 /count /stats /reset 等静态路由之后，
# 否则 /api/logs/stats 会被 /{log_id} 拦截并返回 422
@router.get("/{log_id}")
def get_log(log_id: int):
    """查询单条实收消息完整详情（请求头/体 + 响应头/体）。"""
    with cursor() as cur:
        cur.execute(
            "SELECT id, ts, target, method, path, content_type, headers, "
            "body, body_len, marker, status_code, response_type, "
            "response_headers, response_body "
            "FROM received_messages WHERE id = ?", (log_id,))
        row = cur.fetchone()
    if not row:
        return {"detail": "not found"}
    return dict(row)
