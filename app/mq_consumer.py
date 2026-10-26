"""RabbitMQ 消费者 — 模拟上游投递方 MQ 目标系统的消费行为。

上游投递方的 Dispatcher 将消息投递到 RabbitMQ 队列（routing_rule.target = 队列名），
本模块消费这些队列，记录消息到 SQLite，并按故障模式决定回复行为：

  success → ack + 回复 {"messageId":"xxx","status":"CONSUMED"}
  fail    → ack + 回复 {"messageId":"xxx","status":"FAILED","error":"injected fail"}
  timeout → 延迟后 ack + 回复 FAILED（模拟消费者处理超时）

回复发到消息的 reply_to 队列（前缀由 MQ_REPLY_QUEUE_PREFIX 指定），调用方的状态监听器
消费回复后更新 routing_detail 状态。

线程模型：pika BlockingConnection 是同步阻塞的，消费者在独立线程中运行。
"""

import json
import logging
import queue as _qmod
import random
import threading
import time
from datetime import datetime, timezone

import pika
import pika.exceptions

from app import config
from app.database import cursor, _now

logger = logging.getLogger("mq_consumer")


def _extract_field(body: str, field: str) -> str:
    """从 JSON body 中提取字段值（轻量解析，兼容非标准 JSON）。"""
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


def _get_fault_mode(queue: str) -> dict:
    """从 SQLite 读取队列的故障模式（复用 fault_mode 表，target = 队列名）。"""
    with cursor() as cur:
        cur.execute(
            "SELECT success_rate, fail_rate, timeout_rate, latency_ms, timeout_seconds "
            "FROM fault_mode WHERE target = ?",
            (queue,),
        )
        row = cur.fetchone()
    if not row:
        return {"success_rate": 100, "fail_rate": 0, "timeout_rate": 0,
                "latency_ms": 0, "timeout_seconds": 30}
    return dict(row)


def _apply_fault(queue: str) -> tuple[str, str]:
    """按故障模式决定本次消费的回复行为。返回 (reply_status, response_type)。

    reply_status: "CONSUMED" / "FAILED"
    response_type: "success" / "fail" / "timeout"
    """
    m = _get_fault_mode(queue)
    r = random.randint(1, 100)

    if r <= m["fail_rate"]:
        return "FAILED", "fail"

    if r <= m["fail_rate"] + m["timeout_rate"]:
        time.sleep(m["timeout_seconds"])
        return "FAILED", "timeout"

    if m["latency_ms"] > 0:
        time.sleep(m["latency_ms"] / 1000)

    return "CONSUMED", "success"


def _record_mq_message(queue: str, body_bytes: bytes, msg_properties, reply_status: str,
                       response_type: str) -> int | None:
    """记录 MQ 消息到 SQLite，返回新记录 id（供回复快照回填）。

    messageId 提取链（与 mq_workers.ConsumerWorker 对齐）：
      body.messageId → AMQP properties.message_id → properties.correlation_id。
    上游投递方按 JMS→AMQP 映射把 messageId 放在 AMQP 属性（不在报文体），
    报文体仅含业务 payload，故必须从属性兜底，否则记录为空。
    """
    body_str = body_bytes.decode("utf-8", "replace")
    marker = _extract_field(body_str, "marker") or _extract_field(body_str, "tc")
    message_id = (_extract_field(body_str, "messageId")
                  or (getattr(msg_properties, "message_id", None) or "")
                  or (getattr(msg_properties, "correlation_id", None) or ""))

    headers = {}
    if msg_properties.headers:
        headers = dict(msg_properties.headers)

    correlation_id = msg_properties.correlation_id or ""
    reply_to = msg_properties.reply_to or ""
    content_type = msg_properties.content_type or ""

    with cursor() as cur:
        cur.execute(
            "INSERT INTO mq_messages "
            "(ts, queue, body, body_len, marker, message_id, correlation_id, "
            "reply_to, content_type, headers, reply_status, ack_status, response_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), queue, body_str, len(body_bytes), marker or None,
             message_id or None, correlation_id or None, reply_to or None,
             content_type or None, json.dumps(headers, ensure_ascii=False),
             reply_status, "ack", response_type),
        )
        return cur.lastrowid


def _send_reply(channel, reply_to: str, correlation_id: str,
                message_id: str, status: str, error: str = ""):
    """向回复队列发送消费确认。返回 (回复体文本, 回复属性 JSON)。"""
    if not reply_to:
        return "", ""
    body = {"messageId": message_id, "status": status}
    if error:
        body["error"] = error
    payload = json.dumps(body, ensure_ascii=False)
    props = pika.BasicProperties(
        content_type="application/json",
        delivery_mode=2,  # persistent
        correlation_id=correlation_id,
    )
    props_text = json.dumps(
        {"content_type": props.content_type, "delivery_mode": props.delivery_mode,
         "correlation_id": correlation_id or None},
        ensure_ascii=False)
    try:
        channel.basic_publish(
            exchange="",
            routing_key=reply_to,
            body=payload.encode(),
            properties=props,
        )
    except Exception as e:
        logger.warning("[sendReply][回复失败 reply_to=%s: %s]", reply_to, e)
        props_text = json.dumps({"publish_error": str(e)}, ensure_ascii=False)
    return payload, props_text


class MqConsumer:
    """RabbitMQ 消费者管理器。

    在独立线程中运行 pika BlockingConnection，支持动态添加/移除队列消费者。
    """

    def __init__(self):
        self._connection = None
        self._channel = None
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._queues: set[str] = set()
        self._consumer_tags: dict[str, str] = {}
        self._ops: "_qmod.Queue[tuple]" = _qmod.Queue()
        self._running = False
        self._connected = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def queues(self) -> list[str]:
        with self._lock:
            return sorted(self._queues)

    def start(self, auto_queues: list[str] | None = None):
        """启动消费者线程。"""
        if self._running:
            logger.warning("[start] 消费者已在运行")
            return
        self._running = True
        self._stop_event.clear()
        if auto_queues:
            with self._lock:
                self._queues.update(auto_queues)
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="mq-consumer")
        self._thread.start()
        logger.info("[start] MQ 消费者线程已启动，初始队列: %s", self.queues)

    def stop(self):
        """停止消费者线程：只发信号 + 等线程退出，**不跨线程触碰连接**。

        pika BlockingConnection 非线程安全：从 API 线程调 connection.close() 抛异常
        且被吞掉后连接不会断，broker 侧消费者残留在线继续抢消息。连接由消费者线程
        在事件循环退出后自行关闭（见 _teardown）。
        """
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                logger.warning("[stop] 消费者线程停止等待超时（可能在故障注入 sleep 中），"
                               "线程将随后自行断开")
        self._thread = None
        self._connected = False
        logger.info("[stop] MQ 消费者已停止")

    def add_queue(self, queue: str):
        """动态添加一个队列消费者（经操作队列由消费者线程应用，保证线程安全）。"""
        with self._lock:
            self._queues.add(queue)
        self._ops.put(("add", queue))

    def remove_queue(self, queue: str):
        """动态移除一个队列消费者（经操作队列由消费者线程应用）。"""
        with self._lock:
            self._queues.discard(queue)
        self._ops.put(("remove", queue))

    def _run_loop(self):
        """消费者线程主循环：连接 → 声明队列 → 消费 → 断线重连。"""
        while self._running:
            try:
                self._connect()
                self._declare_and_consume()
                # 事件循环：每秒醒一次检查停止标志并应用队列增删操作。
                # （start_consuming 会无限阻塞且无法响应停止标志——曾导致
                #   "停止服务"后连接泄漏、broker 侧消费者持续在线抢消息。）
                while self._running and self._connection and self._connection.is_open:
                    self._connection.process_data_events(time_limit=1.0)
                    self._apply_pending_ops()
            except pika.exceptions.AMQPConnectionError as e:
                logger.warning("[runLoop] RabbitMQ 连接断开: %s，5 秒后重连...", e)
            except Exception as e:
                logger.error("[runLoop] 消费者异常: %s", e, exc_info=True)
            finally:
                self._teardown()

            if self._running:
                self._stop_event.wait(timeout=5)

    def _connect(self):
        """建立 RabbitMQ 连接。"""
        creds = pika.PlainCredentials(config.MQ_USER, config.MQ_PASSWORD)
        params = pika.ConnectionParameters(
            host=config.MQ_HOST,
            port=config.MQ_PORT,
            virtual_host=config.MQ_VHOST,
            credentials=creds,
            heartbeat=30,
            blocked_connection_timeout=300,
        )
        self._connection = pika.BlockingConnection(params)
        self._channel = self._connection.channel()
        self._channel.basic_qos(prefetch_count=1)
        self._connected = True
        logger.info("[connect] 已连接 RabbitMQ %s:%d", config.MQ_HOST, config.MQ_PORT)

    def _declare_and_consume(self):
        """声明所有队列并开始消费。"""
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                # 声明队列（幂等）：参数与上游投递方 declareQueue 完全一致
                # （durable + DLX 指向 MQ_DLX / MQ_DLQ）。若不带死信参数而队列已被
                # 上游声明，会因参数不匹配触发 406 PRECONDITION_FAILED 导致消费失败。
                self._channel.queue_declare(
                    queue=q, durable=True,
                    arguments={
                        "x-dead-letter-exchange": config.MQ_DLX,
                        "x-dead-letter-routing-key": config.MQ_DLQ,
                    },
                )
                # 保存 consumer_tag —— basic_cancel 只认 tag，不认队列名
                self._consumer_tags[q] = self._channel.basic_consume(
                    queue=q,
                    on_message_callback=self._on_message,
                    auto_ack=False,
                )
                logger.info("[declareConsume] 队列(%s) 已声明并开始消费", q)
            except Exception as e:
                logger.error("[declareConsume] 队列(%s) 声明/消费失败: %s", q, e)

    def _apply_pending_ops(self):
        """应用动态队列增删操作（仅由消费者线程调用，channel 操作线程安全）。"""
        while True:
            try:
                op, q = self._ops.get_nowait()
            except _qmod.Empty:
                return
            try:
                if not (self._channel and self._connected):
                    continue
                if op == "add" and q not in self._consumer_tags:
                    self._channel.queue_declare(
                        queue=q, durable=True,
                        arguments={
                            "x-dead-letter-exchange": config.MQ_DLX,
                            "x-dead-letter-routing-key": config.MQ_DLQ,
                        },
                    )
                    self._consumer_tags[q] = self._channel.basic_consume(
                        queue=q, on_message_callback=self._on_message, auto_ack=False)
                    logger.info("[applyOps] 已添加队列消费者: %s", q)
                elif op == "remove":
                    tag = self._consumer_tags.pop(q, None)
                    if tag:
                        # basic_cancel 参数是 consumer_tag（旧代码误传队列名，取消无效）
                        self._channel.basic_cancel(tag)
                        logger.info("[applyOps] 已移除队列消费者: %s (tag=%s)", q, tag)
            except Exception as e:
                logger.warning("[applyOps] %s 队列(%s)失败: %s", op, q, e)

    def _teardown(self):
        """由消费者线程自身关闭通道/连接并清理运行态（pika 连接不允许跨线程操作）。"""
        self._connected = False
        conn, self._connection = self._connection, None
        ch, self._channel = self._channel, None
        self._consumer_tags.clear()
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

    def _on_message(self, channel, method, properties, body):
        """消息到达回调 — 记录 + 故障注入 + 回复 + ack。"""
        queue = method.routing_key
        body_str = body.decode("utf-8", "replace")
        message_id = _extract_field(body_str, "messageId")

        # 从 correlation_id 补充 messageId
        if not message_id and properties.correlation_id:
            message_id = properties.correlation_id

        logger.info("[onMessage] 队列=%s messageId=%s body_len=%d", queue, message_id, len(body))

        # 故障注入
        reply_status, response_type = _apply_fault(queue)

        # 构造回复 error
        error = ""
        if reply_status == "FAILED":
            error = "injected fail" if response_type == "fail" else "injected timeout"

        # 记录到 SQLite
        row_id = _record_mq_message(queue, body, properties, reply_status, response_type)

        # 回复上游，并将回复快照回填到消息记录（概览「实际回复快照」展示用）
        reply_to = properties.reply_to or ""
        if reply_to:
            reply_body_text, reply_props_text = _send_reply(
                channel, reply_to,
                properties.correlation_id or "",
                message_id, reply_status, error,
            )
            if row_id and reply_body_text:
                try:
                    with cursor() as cur:
                        cur.execute(
                            "UPDATE mq_messages SET reply_body = ?, reply_props = ? WHERE id = ?",
                            (reply_body_text, reply_props_text, row_id),
                        )
                except Exception as e:
                    logger.warning("[onMessage][回复快照回填失败 id=%s: %s]", row_id, e)

        # ack 消息（无论成功失败都 ack，不 requeue）
        channel.basic_ack(delivery_tag=method.delivery_tag)

        logger.info("[onMessage] 队列=%s reply=%s/%s → %s",
                    queue, reply_status, response_type, reply_to or "(no reply_to)")


# 全局单例
mq_consumer = MqConsumer()
