"""统计指标配置管理 API。

指标 = 名称 + 关键字（对实收消息的 path/target 模糊匹配），
配置后 /api/logs/stats 按 by_metric 返回各指标计数，供概览展示。
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.database import cursor
from app.models import StatMetricIn, StatMetricUpd

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("")
def list_metrics():
    """统计指标列表。"""
    with cursor() as cur:
        cur.execute("SELECT id, name, keyword, enabled, updated_at "
                    "FROM stat_metrics ORDER BY id")
        return {"metrics": [dict(r) for r in cur.fetchall()]}


@router.post("")
def create_metric(m: StatMetricIn):
    """新增统计指标。"""
    name = (m.name or "").strip()
    keyword = (m.keyword or "").strip()
    if not name or not keyword:
        raise HTTPException(400, "name / keyword 必填")
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO stat_metrics (name, keyword, enabled, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, keyword, int(m.enabled), _now()))
            mid = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"指标名称 '{name}' 已存在")
        raise
    return {"id": mid, "created": True}


@router.put("/{mid}")
def update_metric(mid: int, m: StatMetricUpd):
    """更新统计指标（全部可选）。"""
    with cursor() as cur:
        cur.execute("SELECT id FROM stat_metrics WHERE id = ?", (mid,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"指标 #{mid} 不存在")

        sets, vals = [], []
        if m.name is not None:
            name = m.name.strip()
            if not name:
                raise HTTPException(400, "name 不能为空")
            sets.append("name=?")
            vals.append(name)
        if m.keyword is not None:
            keyword = m.keyword.strip()
            if not keyword:
                raise HTTPException(400, "keyword 不能为空")
            sets.append("keyword=?")
            vals.append(keyword)
        if m.enabled is not None:
            sets.append("enabled=?")
            vals.append(int(m.enabled))
        if not sets:
            raise HTTPException(400, "无更新字段")

        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(mid)
        try:
            cur.execute(f"UPDATE stat_metrics SET {', '.join(sets)} WHERE id = ?",
                        vals)
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "指标名称已存在")
            raise
    return {"id": mid, "updated": True}


@router.delete("/{mid}")
def delete_metric(mid: int):
    """删除统计指标。"""
    with cursor() as cur:
        cur.execute("DELETE FROM stat_metrics WHERE id = ?", (mid,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"指标 #{mid} 不存在")
    return {"id": mid, "deleted": True}
