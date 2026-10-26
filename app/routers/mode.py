"""故障注入模式管理 API。"""

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.database import cursor
from app.models import FaultMode, FaultModeOut

router = APIRouter(prefix="/api/mode", tags=["mode"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("")
def get_all_modes():
    """获取所有目标的故障模式。"""
    with cursor() as cur:
        cur.execute(
            "SELECT target, success_rate, fail_rate, timeout_rate, "
            "latency_ms, timeout_seconds, updated_at FROM fault_mode "
            "ORDER BY target")
        rows = [dict(r) for r in cur.fetchall()]
    return {"modes": rows}


@router.get("/{target}")
def get_mode(target: str):
    """获取指定目标的故障模式。"""
    with cursor() as cur:
        cur.execute(
            "SELECT target, success_rate, fail_rate, timeout_rate, "
            "latency_ms, timeout_seconds, updated_at FROM fault_mode "
            "WHERE target = ?", (target,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"目标 '{target}' 未注册")
    return dict(row)


@router.put("/{target}")
def set_mode(target: str, mode: FaultMode):
    """设置目标的故障注入模式。

    - success_rate: 成功概率 0-100
    - fail_rate: 失败概率 0-100
    - timeout_rate: 超时概率 0-100
    - latency_ms: 成功时附加延迟（毫秒）
    - timeout_seconds: 超时时睡眠秒数
    """
    total = mode.success_rate + mode.fail_rate + mode.timeout_rate
    if total > 100:
        raise HTTPException(400, f"概率之和 {total} 超过 100")

    ts = _now()
    with cursor() as cur:
        cur.execute(
            "INSERT INTO fault_mode (target, success_rate, fail_rate, "
            "timeout_rate, latency_ms, timeout_seconds, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target) DO UPDATE SET "
            "success_rate=excluded.success_rate, fail_rate=excluded.fail_rate, "
            "timeout_rate=excluded.timeout_rate, latency_ms=excluded.latency_ms, "
            "timeout_seconds=excluded.timeout_seconds, updated_at=excluded.updated_at",
            (target, mode.success_rate, mode.fail_rate, mode.timeout_rate,
             mode.latency_ms, mode.timeout_seconds, ts))

    return {"target": target, **mode.model_dump(), "updated_at": ts}


@router.post("/reset")
def reset_modes():
    """重置所有目标为默认模式（100% 成功）。"""
    ts = _now()
    with cursor() as cur:
        cur.execute("SELECT target FROM fault_mode")
        targets = [r["target"] for r in cur.fetchall()]
        cur.execute(
            "UPDATE fault_mode SET success_rate=100, fail_rate=0, "
            "timeout_rate=0, latency_ms=0, timeout_seconds=30, updated_at=?",
            (ts,))
    return {"reset": targets, "updated_at": ts}
