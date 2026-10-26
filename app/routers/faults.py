"""故障场景管理 API + 请求链场景匹配引擎。

设计：故障类型固定（fail / timeout / latency / custom），目标服务动态选择
（scope 为 targets 表中的 path 列表，空 = 全部目标），按概率触发。
场景优先于旧版 fault_mode（兼容保留）。
"""

import json
import random
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.database import cursor
from app.models import FAULT_TYPES, FaultScenarioIn, FaultScenarioUpd
from app.templating import render_template

router = APIRouter(prefix="/api/faults", tags=["faults"])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_scope(scope: Optional[list]) -> list[str]:
    """规范化 scope：去空白、去重、排序（空列表 = 全部目标）。"""
    return sorted({s.strip() for s in (scope or []) if s and s.strip()})


# ── 场景匹配引擎（main.py / target.py 请求链调用） ──

def _scope_match(scope_json: str, full_path: str, target_name: str) -> bool:
    """scope 匹配：空 = 全部；否则 path 精确匹配或 target 名匹配。"""
    try:
        scopes = json.loads(scope_json or "[]")
    except (ValueError, TypeError):
        scopes = []
    if not scopes:
        return True
    return full_path in scopes or target_name in scopes


def apply_scenarios(full_path: str, target_name: str) -> Optional[tuple[int, str, str]]:
    """按启用的场景依次掷骰（id 顺序，首个命中即应用）。

    返回 (status_code, body_str, resp_type)；无注入返回 None。
    latency 类型特殊：仅延迟，不产生响应（延迟后继续正常链路）。
    """
    with cursor() as cur:
        cur.execute(
            "SELECT name, fault_type, probability, status_code, latency_ms, "
            "timeout_seconds, body_template, scope FROM fault_scenarios "
            "WHERE enabled = 1 ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]

    for sc in rows:
        if not _scope_match(sc["scope"], full_path, target_name):
            continue
        if random.randint(1, 100) > sc["probability"]:
            continue
        ftype = sc["fault_type"]
        if ftype == "latency":
            if sc["latency_ms"] > 0:
                time.sleep(sc["latency_ms"] / 1000)
            continue  # 延迟后继续检查后续场景 / 正常响应
        if ftype == "timeout":
            time.sleep(sc["timeout_seconds"])
            body = json.dumps({"error": "timeout", "scenario": sc["name"]},
                              ensure_ascii=False)
            return 504, body, "timeout"
        if ftype == "custom":
            body = render_template(sc["body_template"] or "") or json.dumps(
                {"scenario": sc["name"], "status": "injected"},
                ensure_ascii=False)
            return sc["status_code"], body, "custom"
        # fail
        body = json.dumps({"error": "injected fail", "scenario": sc["name"]},
                          ensure_ascii=False)
        return sc["status_code"], body, "fail"
    return None


# ── CRUD ──

_COLS = ("id, name, fault_type, probability, status_code, latency_ms, "
         "timeout_seconds, body_template, scope, enabled, updated_at")


@router.get("")
def list_scenarios():
    """故障场景列表。"""
    with cursor() as cur:
        cur.execute(f"SELECT {_COLS} FROM fault_scenarios ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
    return {"scenarios": rows}


def _validate(s: FaultScenarioIn):
    if not (s.name or "").strip():
        raise HTTPException(400, "name 必填")
    if s.fault_type not in FAULT_TYPES:
        raise HTTPException(400, f"fault_type 需为 {'/'.join(FAULT_TYPES)} 之一")
    return s.name.strip()


@router.post("")
def create_scenario(s: FaultScenarioIn):
    """新增故障场景。"""
    name = _validate(s)
    ts = _now()
    try:
        with cursor() as cur:
            cur.execute(
                "INSERT INTO fault_scenarios (name, fault_type, probability, "
                "status_code, latency_ms, timeout_seconds, body_template, "
                "scope, enabled, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, s.fault_type, s.probability, s.status_code, s.latency_ms,
                 s.timeout_seconds, s.body_template, json.dumps(_norm_scope(s.scope)),
                 int(s.enabled), ts))
            sid = cur.lastrowid
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(409, f"场景名称 '{name}' 已存在")
        raise
    return {"id": sid, "created": True}


@router.put("/{sid}")
def update_scenario(sid: int, s: FaultScenarioUpd):
    """更新故障场景（全部可选字段）。"""
    with cursor() as cur:
        cur.execute("SELECT id FROM fault_scenarios WHERE id = ?", (sid,))
        if cur.fetchone() is None:
            raise HTTPException(404, f"场景 #{sid} 不存在")

        sets, vals = [], []
        if s.name is not None:
            name = s.name.strip()
            if not name:
                raise HTTPException(400, "name 不能为空")
            sets.append("name=?")
            vals.append(name)
        if s.fault_type is not None:
            if s.fault_type not in FAULT_TYPES:
                raise HTTPException(400, f"fault_type 需为 {'/'.join(FAULT_TYPES)} 之一")
            sets.append("fault_type=?")
            vals.append(s.fault_type)
        if s.probability is not None:
            sets.append("probability=?")
            vals.append(s.probability)
        if s.status_code is not None:
            sets.append("status_code=?")
            vals.append(s.status_code)
        if s.latency_ms is not None:
            sets.append("latency_ms=?")
            vals.append(s.latency_ms)
        if s.timeout_seconds is not None:
            sets.append("timeout_seconds=?")
            vals.append(s.timeout_seconds)
        if s.body_template is not None:
            sets.append("body_template=?")
            vals.append(s.body_template)
        if s.scope is not None:
            sets.append("scope=?")
            vals.append(json.dumps(_norm_scope(s.scope)))
        if s.enabled is not None:
            sets.append("enabled=?")
            vals.append(int(s.enabled))
        if not sets:
            raise HTTPException(400, "无更新字段")

        sets.append("updated_at=?")
        vals.append(_now())
        vals.append(sid)
        try:
            cur.execute(
                f"UPDATE fault_scenarios SET {', '.join(sets)} WHERE id = ?", vals)
        except Exception as e:
            if "UNIQUE" in str(e):
                raise HTTPException(409, "场景名称已存在")
            raise
    return {"id": sid, "updated": True}


@router.delete("/{sid}")
def delete_scenario(sid: int):
    """删除故障场景。"""
    with cursor() as cur:
        cur.execute("DELETE FROM fault_scenarios WHERE id = ?", (sid,))
        if cur.rowcount == 0:
            raise HTTPException(404, f"场景 #{sid} 不存在")
    return {"id": sid, "deleted": True}


@router.post("/disable-all")
def disable_all():
    """一键停用全部场景（不删除）。"""
    with cursor() as cur:
        cur.execute("SELECT count(*) FROM fault_scenarios WHERE enabled = 1")
        n = cur.fetchone()[0]
        cur.execute(
            "UPDATE fault_scenarios SET enabled = 0, updated_at = ? WHERE enabled = 1",
            (_now(),))
    return {"disabled": n}
