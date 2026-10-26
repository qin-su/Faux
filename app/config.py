"""配置加载：从 .env 文件 + 环境变量读取所有连接参数。"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 从项目根目录加载 .env
_root = Path(__file__).resolve().parent.parent
load_dotenv(_root / ".env")


def _get(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _getint(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


# ── 服务自身 ──
FAUX_PORT: int = _getint("FAUX_PORT", 8081)
FAUX_DB_PATH: str = _get("FAUX_DB_PATH", str(_root / "mock13.db"))

# ── 预置目标列表 ──
KNOWN_TARGETS = ["echo", "echo2", "echo3", "sit-not-found"]

# ── RabbitMQ（MQ 消费测试）──
MQ_ENABLED: bool = _get("MQ_ENABLED", "false").lower() == "true"
MQ_HOST: str = _get("MQ_HOST", "127.0.0.1")
MQ_PORT: int = _getint("MQ_PORT", 5672)
MQ_USER: str = _get("MQ_USER", "guest")
MQ_PASSWORD: str = _get("MQ_PASSWORD", "guest")
MQ_VHOST: str = _get("MQ_VHOST", "/")
MQ_REPLY_QUEUE_PREFIX: str = _get("MQ_REPLY_QUEUE_PREFIX", "reply.")
# 死信拓扑（须与上游投递方的队列声明参数完全一致，否则同名队列声明 406）
MQ_DLX: str = _get("MQ_DLX", "faux.dlx")
MQ_DLQ: str = _get("MQ_DLQ", "faux.dlq")

# 启动时自动消费的队列列表
_auto_queues_raw = _get("MQ_AUTO_QUEUES", "")
MQ_AUTO_QUEUES: list[str] = [q.strip() for q in _auto_queues_raw.split(",") if q.strip()]

MQ_CONFIG: dict = {
    "host": MQ_HOST,
    "port": MQ_PORT,
    "credentials": None,  # pika.PlainCredentials 在 mq_consumer 中初始化
    "virtual_host": MQ_VHOST,
}
