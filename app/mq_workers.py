"""MQ 消费方工作线程 + 管理器 — mq_consumers 表驱动的多实例消费者。

与 mq_consumer.py（legacy 单例，.env 驱动）并存：
  - 本模块：每个消费方一条 DB 配置（独立连接/队列/确认模式/回复模板），
            供「MQ 消费方」页签管理，消息记录到 mq_messages（带 consumer_id 等扩展列）。
  - legacy：保留 /api/mq/start|stop|subscribe 的旧语义，不受影响。

回复模板占位符：{{messageId}} {{correlation_id}} {{queue}} {{status}} {{error}} {{ts}} {{body.字段}}
确认模式 ack_mode：auto / ack / nack_requeue / nack_drop / reject / hold（不回确认，测超时）
"""

import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone

import pika
import pika.exceptions

from app.database import cursor, _now

logger = logging.getLogger("mq_workers")

ACK_MODES = ("auto", "ack", "nack_requeue", "nack_drop", "reject", "hold")

_TPL_TOKEN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")


def _extract_field(body: str, field: str) -> str:
    """从 JSON body 中提取字段值（兼容非 JSON 返回空）。"""
    if not body:
        return ""
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            v = obj.get(field)
            return str(v) if v is not None else ""
    except Exception:
        pass
    return ""


def _render_reply(template: str, ctx: dict, body_obj) -> str:
    """渲染回复模板。ctx 提供标量占位符；body.字段 从收到的 JSON body 取值。"""
    def sub(m):
        key = m.group(1)
        if key.startswith("body."):
            field = key[5:]
            if isinstance(body_obj, dict):
                v = body_obj.get(field)
                return "" if v is None else str(v)
            return ""
        v = ctx.get(key)
        return "" if v is None else str(v)
    return _TPL_TOKEN.sub(sub, template or "")


def _apply_fault(queue: str) -> tuple[str, str, float]:
    """复用 fault_mode(target=队列名) 决定回复状态。返回 (reply_status, response_type, 额外延迟秒)。"""
    with cursor() as cur:
        cur.execute(
            "SELECT success_rate, fail_rate, timeout_rate, latency_ms, timeout_seconds "
            "FROM fault_mode WHERE target = ?", (queue,))
        row = cur.fetchone()
    if not row:
        return "CONSUMED", "success", 0.0
    m = dict(row)
    import random
    r = random.randint(1, 100)
    if r <= m["fail_rate"]:
        return "FAILED", "fail", 0.0
    if r <= m["fail_rate"] + m["timeout_rate"]:
        return "FAILED", "timeout", float(m["timeout_seconds"] or 0)
    return "CONSUMED", "success", (m["latency_ms"] or 0) / 1000.0


def _props_to_dict(props) -> dict:
    """pika BasicProperties → 可 JSON 序列化 dict（保留关键字段）。"""
    if props is None:
        return {}
    out = {}
    for k in ("content_type", "content_encoding", "headers", "delivery_mode",
              "priority", "correlation_id", "reply_to", "expiration",
              "message_id", "timestamp", "type", "app_id", "user_id",
              "cluster_id", "delivery_mode"):
        v = getattr(props, k, None)
        if v is None:
            continue
        if k == "timestamp" and not isinstance(v, (int, float)):
            try:
                v = int(v)
            except Exception:
                continue
        if k == "headers" and isinstance(v, dict):
            v = {str(hk): (hv.decode("utf-8", "replace") if isinstance(hv, bytes) else hv)
                 for hk, hv in v.items()}
        out[k] = v
    return out


class ConsumerWorker:
    """单个消费方实例：一条配置 → 一个线程 + 一条连接 + 一个队列。"""

    def __init__(self, cfg: dict):
        self.cfg = dict(cfg)
        self.id: int = cfg["id"]
        self.name: str = cfg["name"]
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._conn = None
        self._channel = None
        self._connected = False
        self._last_error = ""
        self.consumed = 0
        self.last_msg_ts = ""

    # ── 状态 ──
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def status(self) -> str:
        if not self.running:
            return "stopped"
        if self._connected:
            return "consuming"
        return "reconnecting"

    def runtime_info(self) -> dict:
        return {
            "runtime_status": self.status(),
            "connected": self._connected,
            "consumed": self.consumed,
            "last_msg_ts": self.last_msg_ts,
            "last_error": self._last_error,
        }

    # ── 生命周期 ──
    def start(self):
        if self.running:
            return
        self._stop_event.clear()
        self._last_error = ""
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"mq-worker-{self.id}")
        self._thread.start()
        logger.info("[worker-%s] 启动 %s@%s:%s queue=%s ack=%s",
                    self.id, self.name, self.cfg["host"], self.cfg["port"],
                    self.cfg["queue"], self.cfg["ack_mode"])

    def stop(self):
        """请求停止：只发信号 + 等线程退出，**不跨线程触碰连接**。

        pika BlockingConnection 非线程安全：从 API 线程调 conn.close() 抛异常且
        被吞掉后连接不会断，broker 侧消费者残留在线继续抢消息（曾导致"停止服务"
        后新到消息仍被消费并回 CONSUMED）。连接由 worker 线程在事件循环退出后
        自行关闭（见 _teardown）。
        """
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                logger.warning("[worker-%s] 停止等待超时（可能在故障注入 sleep 中），"
                               "线程将随后自行断开", self.id)
        self._thread = None
        self._connected = False

    def cfg_matches(self, cfg: dict) -> bool:
        return self.cfg == cfg

    # ── 主循环 ──
    def _run_loop(self):
        c = self.cfg
        while not self._stop_event.is_set():
            try:
                creds = pika.PlainCredentials(c["username"], c["password"])
                params = pika.ConnectionParameters(
                    host=c["host"], port=int(c["port"]), virtual_host=c["vhost"],
                    credentials=creds, heartbeat=30, blocked_connection_timeout=300)
                self._conn = pika.BlockingConnection(params)
                self._channel = self._conn.channel()
                self._channel.basic_qos(prefetch_count=int(c["prefetch"] or 1))

                if c["declare_mode"] == "passive":
                    self._channel.queue_declare(queue=c["queue"], passive=True)
                else:
                    # 声明参数与上游投递方 / 旧版 mq_consumer 保持一致（durable + DLX）。
                    # 不带死信参数而队列已被上游声明时，会因参数不匹配触发 406 PRECONDITION_FAILED。
                    from app import config as _config
                    self._channel.queue_declare(
                        queue=c["queue"], durable=True,
                        arguments={
                            "x-dead-letter-exchange": _config.MQ_DLX,
                            "x-dead-letter-routing-key": _config.MQ_DLQ,
                        },
                    )

                self._channel.basic_consume(
                    queue=c["queue"],
                    on_message_callback=self._on_message,
                    auto_ack=(c["ack_mode"] == "auto"),
                )
                self._connected = True
                self._last_error = ""
                logger.info("[worker-%s] 已连接并消费队列 %s", self.id, c["queue"])
                # 事件循环：每秒醒一次检查停止标志（start_consuming 会无限阻塞，
                # 停止只能依赖连接被外部关闭——正是连接泄漏的第二条路径）
                while not self._stop_event.is_set() and self._conn and self._conn.is_open:
                    self._conn.process_data_events(time_limit=1.0)
            except pika.exceptions.AMQPConnectionError as e:
                self._last_error = f"连接断开/失败: {e}"
                logger.warning("[worker-%s] %s，5 秒后重连", self.id, self._last_error)
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                logger.error("[worker-%s] 异常: %s", self.id, self._last_error)
            finally:
                self._teardown()

            if not self._stop_event.is_set():
                self._stop_event.wait(timeout=5)
        logger.info("[worker-%s] 线程退出", self.id)

    def _teardown(self):
        """由 worker 线程自身关闭通道/连接并清理运行态（pika 连接不允许跨线程操作）。"""
        self._connected = False
        conn, self._conn = self._conn, None
        ch, self._channel = self._channel, None
        if ch and ch.is_open:
            try:
                ch.close()
            except Exception:
                pass
        if conn and conn.is_open:
            try:
                conn.close()
            except Exception:
                pass

    # ── 消息处理 ──
    def _on_message(self, channel, method, properties, body: bytes):
        c = self.cfg
        t0 = time.perf_counter()
        queue = c["queue"]
        body_str = body.decode("utf-8", "replace")

        try:
            body_obj = json.loads(body_str)
        except Exception:
            body_obj = None

        message_id = (_extract_field(body_str, "messageId")
                      or (properties.message_id if properties else "")
                      or (properties.correlation_id if properties else "")
                      or "")

        # 故障注入（可选）→ 决定回复状态与额外延迟
        fault_status, fault_type, fault_delay = "CONSUMED", "success", 0.0
        if c["fault_inject"]:
            fault_status, fault_type, fault_delay = _apply_fault(queue)
        reply_status = fault_status
        error = "" if reply_status == "CONSUMED" else f"injected {fault_type}"

        # 配置延迟
        delay = (int(c["delay_ms"] or 0) / 1000.0) + fault_delay
        if delay > 0:
            time.sleep(delay)

        # 回复（reply_to 回写）
        reply_body_text = ""
        reply_props_text = ""
        reply_to = (properties.reply_to if properties else "") or ""
        if c["reply_enabled"] and reply_to:
            ctx = {
                "messageId": message_id,
                "correlationId": (properties.correlation_id if properties else "") or "",
                "correlation_id": (properties.correlation_id if properties else "") or "",
                "queue": queue,
                "status": reply_status,
                "error": error,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            reply_body_text = _render_reply(c["reply_template"] or "", ctx, body_obj)
            try:
                reply_headers = json.loads(c["reply_headers"] or "{}") if (c["reply_headers"] or "").strip() else {}
                if not isinstance(reply_headers, dict):
                    reply_headers = {}
            except Exception:
                reply_headers = {}
            props_out = pika.BasicProperties(
                content_type=c["reply_content_type"] or "application/json",
                delivery_mode=2,
                correlation_id=(properties.correlation_id if properties else None),
                headers=reply_headers or None,
            )
            reply_props_text = json.dumps(_props_to_dict(props_out), ensure_ascii=False)
            try:
                channel.basic_publish(exchange="", routing_key=reply_to,
                                      body=reply_body_text.encode("utf-8"),
                                      properties=props_out)
            except Exception as e:
                logger.warning("[worker-%s] 回复失败 reply_to=%s: %s", self.id, reply_to, e)
                reply_props_text = json.dumps({"publish_error": str(e)}, ensure_ascii=False)

        # 确认动作
        ack_mode = c["ack_mode"]
        if ack_mode != "auto":
            try:
                if ack_mode == "ack":
                    channel.basic_ack(delivery_tag=method.delivery_tag)
                elif ack_mode == "nack_requeue":
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                elif ack_mode == "nack_drop":
                    channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                elif ack_mode == "reject":
                    channel.basic_reject(delivery_tag=method.delivery_tag, requeue=False)
                # hold：不回任何确认
            except Exception as e:
                logger.warning("[worker-%s] 确认动作(%s)失败: %s", self.id, ack_mode, e)

        latency_ms = int((time.perf_counter() - t0) * 1000)
        marker = _extract_field(body_str, "marker") or _extract_field(body_str, "tc")

        try:
            with cursor() as cur:
                cur.execute(
                    "INSERT INTO mq_messages "
                    "(ts, queue, body, body_len, marker, message_id, correlation_id, "
                    " reply_to, content_type, headers, reply_status, ack_status, response_type, "
                    " consumer_id, exchange, routing_key, redelivered, delivery_tag, "
                    " properties_json, ack_action, latency_ms, reply_body, reply_props) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_now(), queue, body_str, len(body), marker or None,
                     message_id or None,
                     ((properties.correlation_id if properties else "") or None),
                     (reply_to or None),
                     ((properties.content_type if properties else "") or None),
                     json.dumps((properties.headers if properties else {}) or {},
                                ensure_ascii=False, default=str),
                     reply_status, "ack", fault_type,
                     self.id,
                     (method.exchange or None),
                     (method.routing_key or None),
                     1 if method.redelivered else 0,
                     method.delivery_tag,
                     json.dumps(_props_to_dict(properties), ensure_ascii=False, default=str),
                     ack_mode, latency_ms,
                     reply_body_text or None, reply_props_text or None))
        except Exception as e:
            logger.error("[worker-%s] 记录消息失败: %s", self.id, e)

        self.consumed += 1
        self.last_msg_ts = _now()
        logger.info("[worker-%s] 消息 queue=%s messageId=%s reply=%s ack=%s %dms",
                    self.id, queue, message_id, reply_status, ack_mode, latency_ms)


class MqManager:
    """消费方实例管理器：按 mq_consumers 表拉起/停止/热重启。"""

    def __init__(self):
        self._workers: dict[int, ConsumerWorker] = {}
        self._lock = threading.Lock()

    # ── 工具 ──
    @staticmethod
    def _load_cfg(consumer_id: int) -> dict | None:
        with cursor() as cur:
            cur.execute("SELECT * FROM mq_consumers WHERE id = ?", (consumer_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def list_cfgs() -> list[dict]:
        with cursor() as cur:
            cur.execute("SELECT * FROM mq_consumers ORDER BY id")
            return [dict(r) for r in cur.fetchall()]

    # ── 生命周期 ──
    def reload_all(self):
        """启动时按表内 enabled 拉起；停掉不在表内/未启用的 worker。"""
        cfgs = self.list_cfgs()
        active_ids = set()
        for cfg in cfgs:
            if cfg["enabled"]:
                self.ensure(cfg)
                active_ids.add(cfg["id"])
        with self._lock:
            for wid in list(self._workers):
                if wid not in active_ids:
                    self._stop_worker(wid)

    def ensure(self, cfg: dict):
        """配置存在则热重启（配置变了），否则启动。"""
        with self._lock:
            w = self._workers.get(cfg["id"])
            if w and w.cfg_matches(cfg):
                return
            if w:
                self._stop_worker(cfg["id"])
            w = ConsumerWorker(cfg)
            self._workers[cfg["id"]] = w
        w.start()

    def start(self, consumer_id: int) -> str:
        cfg = self._load_cfg(consumer_id)
        if not cfg:
            return "not_found"
        with self._lock:
            w = self._workers.get(consumer_id)
            if w and w.cfg_matches(cfg):
                return "already_running"
            if w:
                self._stop_worker(consumer_id)
            w = ConsumerWorker(cfg)
            self._workers[consumer_id] = w
        w.start()
        return "started"

    def stop(self, consumer_id: int) -> str:
        with self._lock:
            w = self._workers.get(consumer_id)
            if not w:
                return "not_running"
            self._stop_worker(consumer_id)
        return "stopped"

    def _stop_worker(self, consumer_id: int):
        w = self._workers.pop(consumer_id, None)
        if w:
            try:
                w.stop()
            except Exception as e:
                logger.warning("[manager] 停止 worker-%s 失败: %s", consumer_id, e)

    def remove(self, consumer_id: int):
        with self._lock:
            self._stop_worker(consumer_id)

    def stop_all(self):
        with self._lock:
            for wid in list(self._workers):
                self._stop_worker(wid)

    # ── 状态 ──
    def statuses(self) -> dict[int, dict]:
        with self._lock:
            items = {wid: w.runtime_info() for wid, w in self._workers.items()}
        return items


# 全局单例
mq_manager = MqManager()


# ── 生产者发送（短连接 + publisher confirm） ──

def mq_send(cfg: dict, body: str, overrides: dict) -> dict:
    """按生产者配置 + 覆盖项发送一条消息。返回 {result, detail, elapsed_ms, ...}。"""
    t0 = time.perf_counter()
    routing_key = overrides.get("routing_key") or cfg["routing_key"] or ""
    headers = overrides.get("headers") or {}

    correlation_id = overrides.get("correlation_id") or str(uuid.uuid4())
    message_id = overrides.get("message_id") or str(uuid.uuid4())

    props = pika.BasicProperties(
        content_type=overrides.get("content_type") or cfg["content_type"] or "application/json",
        delivery_mode=int(overrides.get("delivery_mode") or cfg["delivery_mode"] or 2),
        priority=int(overrides.get("priority") if overrides.get("priority") is not None
                     else (cfg["priority"] or 0)),
        expiration=overrides.get("expiration") or None,
        correlation_id=correlation_id,
        message_id=message_id,
        reply_to=overrides.get("reply_to") or None,
        headers=headers or None,
    )
    exchange = cfg["exchange"] or ""
    payload = (body or "").encode("utf-8")

    result, detail = "sent", ""
    try:
        creds = pika.PlainCredentials(cfg["username"], cfg["password"])
        params = pika.ConnectionParameters(
            host=cfg["host"], port=int(cfg["port"]), virtual_host=cfg["vhost"],
            credentials=creds, heartbeat=30)
        conn = pika.BlockingConnection(params)
        try:
            ch = conn.channel()
            ch.confirm_delivery()
            ch.basic_publish(exchange=exchange, routing_key=routing_key,
                             body=payload, properties=props, mandatory=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except pika.exceptions.UnroutableError:
        result, detail = "unroutable", "消息不可路由（exchange/routing key 无匹配队列，且 mandatory=true）"
    except Exception as e:
        result, detail = "error", f"{type(e).__name__}: {e}"

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "result": result, "detail": detail, "elapsed_ms": elapsed_ms,
        "exchange": exchange, "routing_key": routing_key,
        "correlation_id": correlation_id, "message_id": message_id,
        "reply_to": overrides.get("reply_to") or "",
    }
