"""响应模板渲染引擎 — 支持 @参数名 取值。

动态参数（内置，无需配置，时间均为北京时间 +8）：
  @time              当前时间 HH:MM:SS
  @date              当前日期 YYYY-MM-DD
  @datetime          日期时间 YYYY-MM-DD HH:MM:SS
  @timestamp         Unix 时间戳（秒）
  @uuid              随机 UUID v4
  @random            6 位随机整数（100000-999999）
  @random:1-100      范围内随机整数（min-max）
  @pick:a|b|c        列表中随机取一个（选项用 | 分隔，不含空格/引号/花括号）

全局参数：@key 引用「全局参数」中配置的键值（每次渲染现读，修改即时生效）。
未识别的 @token 原样保留。
"""

import random
import re
import uuid as uuidlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.database import cursor

TZ_BJ = timezone(timedelta(hours=8))

# UI 展示用的动态参数速查表
DYNAMIC_PARAMS = [
    ("@time", "当前时间 HH:MM:SS（北京）"),
    ("@date", "当前日期 YYYY-MM-DD（北京）"),
    ("@datetime", "日期时间 YYYY-MM-DD HH:MM:SS（北京）"),
    ("@timestamp", "Unix 时间戳（秒）"),
    ("@uuid", "随机 UUID"),
    ("@random", "6 位随机整数"),
    ("@random:1-100", "范围内随机整数"),
    ("@pick:a|b|c", "列表随机取值（| 分隔）"),
]

# @token：字母/下划线开头的名字，可选 ":参数" 后缀（不含引号/空白/花括号/@）
_TOKEN_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*(?::[^@\"'\s}]*)?)")


def load_global_params() -> dict:
    """读取全局参数表（每次现读，保证修改即时生效）。"""
    try:
        with cursor() as cur:
            cur.execute("SELECT key, value FROM global_params")
            return {r["key"]: r["value"] for r in cur.fetchall()}
    except Exception:
        return {}


def _resolve(token: str, params: dict) -> Optional[str]:
    """解析单个 token：内置动态参数 > 全局参数。未识别返回 None。"""
    now = datetime.now(TZ_BJ)
    if token == "time":
        return now.strftime("%H:%M:%S")
    if token == "date":
        return now.strftime("%Y-%m-%d")
    if token == "datetime":
        return now.strftime("%Y-%m-%d %H:%M:%S")
    if token == "timestamp":
        return str(int(now.timestamp()))
    if token == "uuid":
        return str(uuidlib.uuid4())
    if token == "random":
        return str(random.randint(100000, 999999))
    if token.startswith("random:"):
        rng = token[7:]
        if re.fullmatch(r"\d+-\d+", rng):
            lo, hi = rng.split("-", 1)
            return str(random.randint(int(lo), int(hi)))
        return None
    if token.startswith("pick:"):
        opts = [o for o in token[5:].split("|") if o]
        return random.choice(opts) if opts else None
    return params.get(token)


def render_template(tpl: str, params: Optional[dict] = None) -> str:
    """渲染模板：@token → 动态参数 / 全局参数。空模板返回空串。"""
    if not tpl:
        return ""
    if params is None:
        params = load_global_params()

    def repl(m: re.Match) -> str:
        v = _resolve(m.group(1), params)
        return m.group(0) if v is None else str(v)

    return _TOKEN_RE.sub(repl, tpl)
