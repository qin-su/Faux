"""MQ 消费管理 API — 控制 RabbitMQ 消费者、查看 MQ 消息日志。

两组能力并存：
  - legacy：/api/mq/start|stop|subscribe（.env 驱动的旧单例消费者）
  - 新版：/api/mq/consumers|producers（DB 配置驱动的多实例，见 mq_workers.py）
"""

import json
from fastapi import APIRouter, HTTPException, Query

from app import models
from app.database import cursor, _now
from app.mq_consumer import mq_consumer
from app.mq_workers import mq_manager, mq_send, ACK_MODES
from app.templating import render_template

router = APIRouter(prefix="/api/mq", tags=["mq"])


# ══════════════ 新版：MQ 消费方 CRUD ══════════════

def _get_consumer_row(cur, consumer_id: int):
    cur.execute("SELECT * FROM mq_consumers WHERE id = ?", (consumer_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "consumer not found")
    return dict(row)


def _validate_consumer_payload(data: models.MqConsumerIn | models.MqConsumerUpd):
    ack = data.ack_mode
    if ack is not None and ack not in ACK_MODES:
        raise HTTPException(422, f"ack_mode 需为 {ACK_MODES} 之一")
    dm = data.declare_mode
    if dm is not None and dm not in ("durable", "passive"):
        raise HTTPException(422, "declare_mode 需为 durable/passive")
    if data.reply_headers:
        try:
            obj = json.loads(data.reply_headers)
            if not isinstance(obj, dict):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "reply_headers 需为 JSON 对象文本")


@router.get("/consumers")
async def mq_consumers_list():
    """消费方列表（含运行状态）。"""
    statuses = mq_manager.statuses()
    cfgs = mq_manager.list_cfgs()
    items = []
    for cfg in cfgs:
        cid = cfg["id"]
        item = dict(cfg)
        item["password"] = "******" if cfg["password"] else ""
        item.update(statuses.get(cid, {"runtime_status": "stopped", "connected": False,
                                       "consumed": 0, "last_msg_ts": "", "last_error": ""}))
        items.append(item)
    return {"items": items}


@router.post("/consumers")
async def mq_consumers_create(data: models.MqConsumerIn):
    """新增消费方；enabled=true 时立即拉起。"""
    _validate_consumer_payload(data)
    if not data.name.strip() or not data.queue.strip():
        raise HTTPException(422, "name / queue 必填")
    with cursor() as cur:
        cur.execute("SELECT 1 FROM mq_consumers WHERE name = ?", (data.name,))
        if cur.fetchone():
            raise HTTPException(409, f"名称已存在: {data.name}")
        cur.execute(
            "INSERT INTO mq_consumers (name, host, port, vhost, username, password, queue, "
            "prefetch, declare_mode, ack_mode, delay_ms, reply_enabled, reply_template, "
            "reply_content_type, reply_headers, fault_inject, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.name, data.host, data.port, data.vhost, data.username, data.password,
             data.queue, data.prefetch, data.declare_mode, data.ack_mode, data.delay_ms,
             1 if data.reply_enabled else 0, data.reply_template, data.reply_content_type,
             data.reply_headers, 1 if data.fault_inject else 0,
             1 if data.enabled else 0, _now(), _now()))
        cid = cur.lastrowid
        cfg = _get_consumer_row(cur, cid)
    if cfg["enabled"]:
        mq_manager.ensure(cfg)
    return {"ok": True, "id": cid}


@router.put("/consumers/{cid}")
async def mq_consumers_update(cid: int, data: models.MqConsumerUpd):
    """更新消费方配置；配置变更后热重启（若处于启用状态）。"""
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "无更新字段")
    _validate_consumer_payload(data)
    bool_map = {"reply_enabled": "reply_enabled", "fault_inject": "fault_inject", "enabled": "enabled"}
    with cursor() as cur:
        _get_consumer_row(cur, cid)  # 404 检查
        if "name" in fields and fields["name"]:
            cur.execute("SELECT 1 FROM mq_consumers WHERE name = ? AND id != ?", (fields["name"], cid))
            if cur.fetchone():
                raise HTTPException(409, f"名称已存在: {fields['name']}")
        for col, val in fields.items():
            if col in bool_map:
                val = 1 if val else 0
            cur.execute(f"UPDATE mq_consumers SET {col} = ?, updated_at = ? WHERE id = ?",
                        (val, _now(), cid))
        cfg = _get_consumer_row(cur, cid)
    if cfg["enabled"]:
        mq_manager.ensure(cfg)
    else:
        mq_manager.stop(cid)
    return {"ok": True}


@router.delete("/consumers/{cid}")
async def mq_consumers_delete(cid: int):
    """删除消费方（先停止）。"""
    with cursor() as cur:
        _get_consumer_row(cur, cid)
        cur.execute("DELETE FROM mq_consumers WHERE id = ?", (cid,))
    mq_manager.remove(cid)
    return {"ok": True}


@router.post("/consumers/{cid}/start")
async def mq_consumers_start(cid: int):
    res = mq_manager.start(cid)
    if res == "not_found":
        raise HTTPException(404, "consumer not found")
    return {"ok": True, "message": res}


@router.post("/consumers/{cid}/stop")
async def mq_consumers_stop(cid: int):
    res = mq_manager.stop(cid)
    return {"ok": True, "message": res}


# ══════════════ 新版：MQ 生产者 CRUD + 发送 ══════════════

def _get_producer_row(cur, pid: int):
    cur.execute("SELECT * FROM mq_producers WHERE id = ?", (pid,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "producer not found")
    return dict(row)


@router.get("/producers")
async def mq_producers_list():
    with cursor() as cur:
        cur.execute("SELECT * FROM mq_producers ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
    for p in rows:
        p["password"] = "******" if p["password"] else ""
    return {"items": rows}


@router.post("/producers")
async def mq_producers_create(data: models.MqProducerIn):
    if not data.name.strip():
        raise HTTPException(422, "name 必填")
    with cursor() as cur:
        cur.execute("SELECT 1 FROM mq_producers WHERE name = ?", (data.name,))
        if cur.fetchone():
            raise HTTPException(409, f"名称已存在: {data.name}")
        cur.execute(
            "INSERT INTO mq_producers (name, host, port, vhost, username, password, exchange, "
            "routing_key, content_type, delivery_mode, priority, sample_body, enabled, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.name, data.host, data.port, data.vhost, data.username, data.password,
             data.exchange, data.routing_key, data.content_type, data.delivery_mode,
             data.priority, data.sample_body, 1 if data.enabled else 0, _now(), _now()))
        pid = cur.lastrowid
    return {"ok": True, "id": pid}


@router.put("/producers/{pid}")
async def mq_producers_update(pid: int, data: models.MqProducerUpd):
    fields = data.model_dump(exclude_unset=True)
    if not fields:
        raise HTTPException(422, "无更新字段")
    with cursor() as cur:
        _get_producer_row(cur, pid)
        if "name" in fields and fields["name"]:
            cur.execute("SELECT 1 FROM mq_producers WHERE name = ? AND id != ?", (fields["name"], pid))
            if cur.fetchone():
                raise HTTPException(409, f"名称已存在: {fields['name']}")
        for col, val in fields.items():
            if col == "enabled":
                val = 1 if val else 0
            cur.execute(f"UPDATE mq_producers SET {col} = ?, updated_at = ? WHERE id = ?",
                        (val, _now(), pid))
        row = _get_producer_row(cur, pid)
    return {"ok": True}


@router.delete("/producers/{pid}")
async def mq_producers_delete(pid: int):
    with cursor() as cur:
        _get_producer_row(cur, pid)
        cur.execute("DELETE FROM mq_producers WHERE id = ?", (pid,))
    return {"ok": True}


@router.post("/producers/{pid}/send")
async def mq_producers_send(pid: int, data: models.MqSendIn):
    """通过生产者发送一条消息（未填项用生产者默认值），记录发送日志。

    消息体发送前经 @参数 模板渲染（全局参数 + 内置动态参数，与目标服务
    响应模板同一引擎）；发送日志记录渲染后的实际消息体。
    """
    with cursor() as cur:
        cfg = _get_producer_row(cur, pid)
    rendered_body = render_template(data.body or "")
    overrides = data.model_dump(exclude={"body"}, exclude_unset=True)
    result = mq_send(cfg, rendered_body, overrides)

    with cursor() as cur:
        cur.execute(
            "INSERT INTO mq_send_logs (ts, producer_id, producer_name, exchange, routing_key, "
            "body, body_len, result, detail, elapsed_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (_now(), pid, cfg["name"], result["exchange"], result["routing_key"],
             rendered_body, len(rendered_body.encode("utf-8")),
             result["result"], result["detail"], result["elapsed_ms"]))
    return {"ok": result["result"] == "sent", "body_rendered": rendered_body, **result}


@router.get("/sends")
async def mq_sends_list(limit: int = Query(20, ge=1, le=200)):
    """最近发送日志。"""
    with cursor() as cur:
        cur.execute(
            "SELECT id, ts, producer_name, exchange, routing_key, body, body_len, "
            "result, detail, elapsed_ms FROM mq_send_logs ORDER BY id DESC LIMIT ?",
            (limit,))
        items = [dict(r) for r in cur.fetchall()]
    return {"items": items}


# ══════════════ legacy：.env 驱动的旧单例消费者（保留兼容） ══════════════


@router.get("/status")
async def mq_status():
    """MQ 消费者状态。"""
    return {
        "enabled": mq_consumer.running,
        "connected": mq_consumer.connected,
        "queues": mq_consumer.queues,
    }


@router.post("/start")
async def mq_start():
    """启动 MQ 消费者（若已运行则无操作）。"""
    if mq_consumer.running:
        return {"ok": True, "message": "already running", "queues": mq_consumer.queues}
    mq_consumer.start()
    return {"ok": True, "message": "started", "queues": mq_consumer.queues}


@router.post("/stop")
async def mq_stop():
    """停止 MQ 消费者。"""
    if not mq_consumer.running:
        return {"ok": True, "message": "not running"}
    mq_consumer.stop()
    return {"ok": True, "message": "stopped"}


@router.post("/subscribe")
async def mq_subscribe(queue: str = Query(..., description="要消费的队列名")):
    """添加一个队列消费者。"""
    if not mq_consumer.running:
        return {"ok": False, "error": "consumer not running, call /api/mq/start first"}
    mq_consumer.add_queue(queue)
    # 在 fault_mode 表中注册该队列（复用故障注入表）
    with cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO fault_mode(target, success_rate, fail_rate, "
            "timeout_rate, latency_ms, timeout_seconds, updated_at) "
            "VALUES (?, 100, 0, 0, 0, 30, datetime('now'))",
            (queue,),
        )
    return {"ok": True, "queue": queue, "queues": mq_consumer.queues}


@router.delete("/subscribe")
async def mq_unsubscribe(queue: str = Query(..., description="要移除的队列名")):
    """移除一个队列消费者。"""
    mq_consumer.remove_queue(queue)
    return {"ok": True, "queue": queue, "queues": mq_consumer.queues}


@router.get("/messages")
async def mq_messages(
    queue: str = Query("", description="按队列过滤"),
    marker: str = Query("", description="按 marker 过滤"),
    consumer_id: int = Query(0, description="按消费方 ID 过滤（0=全部）"),
    reply_status: str = Query("", description="按回复状态过滤：CONSUMED/FAILED"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """查看 MQ 消费日志。"""
    where = []
    params = []
    if queue:
        where.append("queue = ?")
        params.append(queue)
    if marker:
        where.append("marker = ?")
        params.append(marker)
    if consumer_id:
        where.append("consumer_id = ?")
        params.append(consumer_id)
    if reply_status:
        where.append("reply_status = ?")
        params.append(reply_status)
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    with cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) as total FROM mq_messages{where_clause}", params
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT * FROM mq_messages{where_clause} "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        items = [dict(r) for r in cur.fetchall()]

    return {"total": total, "items": items}


@router.get("/messages/count")
async def mq_messages_count(marker: str = Query(..., description="按 marker 计数")):
    """按 marker 统计 MQ 消息数。"""
    with cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) as count FROM mq_messages WHERE marker = ?", (marker,)
        )
        count = cur.fetchone()["count"]
    return {"marker": marker, "count": count}


@router.get("/messages/stats")
async def mq_messages_stats():
    """MQ 消息统计摘要。"""
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) as total FROM mq_messages")
        total = cur.fetchone()["total"]

        cur.execute(
            "SELECT queue, COUNT(*) as cnt FROM mq_messages GROUP BY queue ORDER BY cnt DESC"
        )
        by_queue = {r["queue"]: r["cnt"] for r in cur.fetchall()}

        cur.execute(
            "SELECT response_type, COUNT(*) as cnt FROM mq_messages "
            "GROUP BY response_type ORDER BY cnt DESC"
        )
        by_type = {r["response_type"]: r["cnt"] for r in cur.fetchall()}

        cur.execute(
            "SELECT reply_status, COUNT(*) as cnt FROM mq_messages "
            "GROUP BY reply_status ORDER BY cnt DESC"
        )
        by_status = {r["reply_status"]: r["cnt"] for r in cur.fetchall()}

        cur.execute("SELECT ts FROM mq_messages ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        last = row["ts"] if row else None

    return {
        "total": total,
        "by_queue": by_queue,
        "by_response_type": by_type,
        "by_reply_status": by_status,
        "last_received": last,
    }


@router.post("/messages/reset")
async def mq_messages_reset():
    """清空 MQ 消息记录。"""
    with cursor() as cur:
        cur.execute("DELETE FROM mq_messages")
        deleted = cur.rowcount
    return {"deleted": deleted}
