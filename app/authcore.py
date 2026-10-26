"""认证内核 — 零依赖 JWT（HS256）签发/校验 + 不透明令牌生成。

不引入 PyJWT 等第三方库（保持「零外部依赖即可跑」）：JWT 由
header.payload.signature 三段 base64url 组成，签名用 hmac-sha256，
与标准库/其他语言实现互通（可用 jwt.io 或任意 JWT 库校验）。

JWT 载荷：
    iss  签发者（auth_config.issuer）
    sub  client_id
    aud  受众（可空）
    iat  签发时间（epoch 秒）
    exp  过期时间（epoch 秒）
    jti  令牌 id（对应 auth_tokens.id，用于即时失效）
    scope 可选
"""

import base64
import hashlib
import hmac
import json
import secrets
import time


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _json_bytes(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_jwt(payload: dict, secret: str) -> str:
    """签发 HS256 JWT。payload 需含 exp（过期可不填，由调用方保证）。"""
    header = _b64e(_json_bytes({"alg": "HS256", "typ": "JWT"}))
    body = _b64e(_json_bytes(payload))
    signing_input = f"{header}.{body}".encode("ascii")
    sig = hmac.new((secret or "").encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{header}.{body}.{_b64e(sig)}"


def verify_jwt(token: str, secret: str) -> tuple[bool, dict | None, str]:
    """校验 JWT。返回 (是否通过, payload, 失败原因)。

    校验顺序：结构 → alg → 签名（恒定时间比较）→ exp。
    """
    if not token or token.count(".") != 2:
        return False, None, "令牌不是 JWT 结构（应为三段）"
    header_b64, payload_b64, sig_b64 = token.split(".")
    try:
        header = json.loads(_b64d(header_b64))
        payload = json.loads(_b64d(payload_b64))
        provided_sig = _b64d(sig_b64)
    except Exception:
        return False, None, "令牌无法解析（base64/JSON 非法）"
    if not isinstance(payload, dict) or not isinstance(header, dict):
        return False, None, "令牌载荷不是 JSON 对象"
    if str(header.get("alg", "")).upper() != "HS256":
        return False, None, f"不支持的签名算法 alg={header.get('alg')}"
    expect = hmac.new((secret or "").encode("utf-8"),
                      f"{header_b64}.{payload_b64}".encode("ascii"),
                      hashlib.sha256).digest()
    if not hmac.compare_digest(expect, provided_sig):
        return False, None, "签名校验失败（密钥不匹配或令牌被篡改）"
    exp = payload.get("exp")
    if exp is not None:
        try:
            if time.time() > float(exp):
                return False, None, "令牌已过期（exp）"
        except (TypeError, ValueError):
            return False, None, "exp 字段非法"
    nbf = payload.get("nbf")
    if nbf is not None:
        try:
            if time.time() < float(nbf):
                return False, None, "令牌尚未生效（nbf）"
        except (TypeError, ValueError):
            return False, None, "nbf 字段非法"
    return True, payload, ""


def decode_insecure(token: str) -> dict | None:
    """不校验签名地解析 JWT（仅供展示载荷；不可用于鉴权判定）。"""
    try:
        parts = token.split(".")
        if len(parts) != 2 and len(parts) != 3:
            return None
        payload = json.loads(_b64d(parts[1]))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def new_opaque_token(prefix: str = "mk") -> str:
    """生成不透明令牌（bearer 模式）。"""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def new_refresh_token() -> str:
    return new_opaque_token("mkr")


def new_jti() -> str:
    return secrets.token_hex(8)


def fingerprint(token: str, n: int = 8) -> str:
    """令牌指纹（日志/列表展示用，避免明文外泄）。"""
    if not token:
        return "-"
    return token[:n] + ("…" if len(token) > n else "")


def mask(token: str, keep: int = 12) -> str:
    """打码展示：保留前 keep 位，其余以 … 结尾。"""
    if not token:
        return ""
    return token if len(token) <= keep else token[:keep] + "…"
