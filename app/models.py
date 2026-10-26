"""Pydantic 数据模型 — 请求体 / 响应体定义。"""

from typing import Optional
from pydantic import BaseModel, Field


# ── 故障注入 ──

class FaultMode(BaseModel):
    success_rate: int = Field(100, ge=0, le=100, description="成功概率 0-100")
    fail_rate: int = Field(0, ge=0, le=100, description="失败概率 0-100")
    timeout_rate: int = Field(0, ge=0, le=100, description="超时概率 0-100")
    latency_ms: int = Field(0, ge=0, description="成功时附加延迟（毫秒）")
    timeout_seconds: int = Field(30, ge=0, description="超时时睡眠秒数")


class FaultModeOut(FaultMode):
    target: str
    updated_at: str


# ── 实收日志 ──

class ReceivedMessage(BaseModel):
    id: int
    ts: str
    target: str
    method: str
    path: str
    content_type: Optional[str] = None
    headers: Optional[str] = None
    body: Optional[str] = None
    body_len: int = 0
    marker: Optional[str] = None
    status_code: Optional[int] = None
    response_type: Optional[str] = None


# ── 目标服务配置 ──

class TargetDefIn(BaseModel):
    """创建目标服务定义。"""
    path: str = Field(..., description="服务路径，以 /api/ 开头，如 /api/order/create")
    name: str = Field("", description="显示名称")
    methods: list[str] = Field(default_factory=lambda: ["POST", "PUT", "GET"],
                               description="支持的 HTTP 方法")
    status_code: int = Field(200, ge=100, le=599, description="成功时响应码")
    response_template: str = Field("", description="响应体模板，支持 @参数；留空返回默认 JSON")
    content_type: str = Field("application/json", description="响应 Content-Type")
    enabled: bool = Field(True, description="是否启用")


class TargetDefUpd(BaseModel):
    """更新目标服务定义（全部可选）。"""
    path: Optional[str] = None
    name: Optional[str] = None
    methods: Optional[list[str]] = None
    status_code: Optional[int] = Field(None, ge=100, le=599)
    response_template: Optional[str] = None
    content_type: Optional[str] = None
    enabled: Optional[bool] = None


class RenderIn(BaseModel):
    template: str = Field("", description="待渲染的模板文本")


# ── 全局参数 ──

class ParamIn(BaseModel):
    key: str = Field(..., description="参数名，字母/下划线开头（模板中以 @key 引用）")
    value: str = Field(..., description="参数值")
    remark: str = Field("", description="备注")


# ── 故障场景（故障类型固定 × 目标动态） ──

FAULT_TYPES = ("fail", "timeout", "latency", "custom")


class FaultScenarioIn(BaseModel):
    name: str = Field(..., description="场景名称（唯一），如：下游网关 502")
    fault_type: str = Field("fail", description="故障类型：fail/timeout/latency/custom")
    probability: int = Field(100, ge=0, le=100, description="触发概率 0-100")
    status_code: int = Field(500, ge=100, le=599, description="fail/custom 的响应码")
    latency_ms: int = Field(0, ge=0, description="latency 类型的延迟毫秒")
    timeout_seconds: int = Field(30, ge=0, description="timeout 类型的睡眠秒数")
    body_template: str = Field("", description="custom 类型的响应体模板（支持 @参数）")
    scope: list[str] = Field(default_factory=list, description="生效目标 path 列表；空 = 全部目标")
    enabled: bool = Field(True, description="是否启用")


class FaultScenarioUpd(BaseModel):
    """更新故障场景（全部可选）。"""
    name: Optional[str] = None
    fault_type: Optional[str] = None
    probability: Optional[int] = Field(None, ge=0, le=100)
    status_code: Optional[int] = Field(None, ge=100, le=599)
    latency_ms: Optional[int] = Field(None, ge=0)
    timeout_seconds: Optional[int] = Field(None, ge=0)
    body_template: Optional[str] = None
    scope: Optional[list[str]] = None
    enabled: Optional[bool] = None


# ── 统计指标 ──

class StatMetricIn(BaseModel):
    name: str = Field(..., description="指标名称（唯一），如：下单量")
    keyword: str = Field(..., description="匹配关键字（对实收消息的 path/target 模糊匹配）")
    enabled: bool = Field(True, description="是否启用")


class StatMetricUpd(BaseModel):
    name: Optional[str] = None
    keyword: Optional[str] = None
    enabled: Optional[bool] = None


# ── MQ 消费方 ──

MQ_ACK_MODES = ("auto", "ack", "nack_requeue", "nack_drop", "reject", "hold")
MQ_DECLARE_MODES = ("durable", "passive")


class MqConsumerIn(BaseModel):
    name: str = Field(..., description="消费方名称（唯一）")
    host: str = Field("127.0.0.1", description="RabbitMQ 地址")
    port: int = Field(5672, ge=1, le=65535)
    vhost: str = Field("/", description="虚拟主机")
    username: str = Field("guest")
    password: str = Field("guest")
    queue: str = Field(..., description="消费的队列名")
    prefetch: int = Field(1, ge=1, le=65535, description="QoS 预取数")
    declare_mode: str = Field("durable", description="durable=声明持久队列 / passive=仅检查")
    ack_mode: str = Field("ack", description="确认模式：auto/ack/nack_requeue/nack_drop/reject/hold")
    delay_ms: int = Field(0, ge=0, description="收到消息后延迟执行动作（毫秒）")
    reply_enabled: bool = Field(True, description="消息带 reply_to 时回写回复")
    reply_template: str = Field(
        '{"messageId":"{{messageId}}","status":"{{status}}"}',
        description="回复 body 模板，占位符 {{messageId}} {{correlation_id}} {{queue}} "
                    "{{status}} {{error}} {{ts}} {{body.字段}}")
    reply_content_type: str = Field("application/json", description="回复消息 content_type")
    reply_headers: str = Field("", description="回复消息 headers（JSON 对象文本）")
    fault_inject: bool = Field(True, description="复用 fault_mode(target=队列名) 故障注入")
    enabled: bool = Field(True, description="启用（保存后自动拉起）")


class MqConsumerUpd(BaseModel):
    """更新 MQ 消费方（全部可选）。"""
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)
    vhost: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    queue: Optional[str] = None
    prefetch: Optional[int] = Field(None, ge=1, le=65535)
    declare_mode: Optional[str] = None
    ack_mode: Optional[str] = None
    delay_ms: Optional[int] = Field(None, ge=0)
    reply_enabled: Optional[bool] = None
    reply_template: Optional[str] = None
    reply_content_type: Optional[str] = None
    reply_headers: Optional[str] = None
    fault_inject: Optional[bool] = None
    enabled: Optional[bool] = None


# ── MQ 生产者 ──

class MqProducerIn(BaseModel):
    name: str = Field(..., description="生产者名称（唯一）")
    host: str = Field("127.0.0.1", description="RabbitMQ 地址")
    port: int = Field(5672, ge=1, le=65535)
    vhost: str = Field("/", description="虚拟主机")
    username: str = Field("guest")
    password: str = Field("guest")
    exchange: str = Field("", description="交换机；空 = 默认交换机（直连队列）")
    routing_key: str = Field("", description="默认 routing key（发送时可覆盖）")
    content_type: str = Field("application/json")
    delivery_mode: int = Field(2, ge=1, le=2, description="1=非持久 2=持久")
    priority: int = Field(0, ge=0, le=9)
    sample_body: str = Field("", description="发送卡片示例消息体模板")
    enabled: bool = Field(True)


class MqProducerUpd(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)
    vhost: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    exchange: Optional[str] = None
    routing_key: Optional[str] = None
    content_type: Optional[str] = None
    delivery_mode: Optional[int] = Field(None, ge=1, le=2)
    priority: Optional[int] = Field(None, ge=0, le=9)
    sample_body: Optional[str] = None
    enabled: Optional[bool] = None


# ── 认证（auth）──

class AuthConfigUpd(BaseModel):
    """更新认证配置（全部可选，仅提交字段生效）。"""
    enabled: Optional[bool] = Field(None, description="认证总开关（False=不校验，与历史行为一致）")
    mode: Optional[str] = Field(None, description="认证方式：none/jwt/bearer/basic/apikey")
    secret: Optional[str] = Field(None, description="JWT HS256 签名密钥")
    issuer: Optional[str] = Field(None, description="JWT iss")
    audience: Optional[str] = Field(None, description="JWT aud（空=不下发）")
    access_ttl: Optional[int] = Field(None, ge=1, description="access_token 有效期（秒）")
    refresh_ttl: Optional[int] = Field(None, ge=1, description="refresh_token 有效期（秒）")
    client_id: Optional[str] = Field(None, description="换票客户端 ID")
    client_secret: Optional[str] = Field(None, description="换票客户端密钥")
    token_endpoint_auth: Optional[str] = Field(
        None, description="换票端点凭据校验方式：basic/apikey/body/none")
    apikey_header: Optional[str] = Field(None, description="apikey 模式的请求头名")
    apikey_value: Optional[str] = Field(None, description="apikey 模式的期望值")
    basic_user: Optional[str] = Field(None, description="basic 模式用户名（业务请求）")
    basic_pass: Optional[str] = Field(None, description="basic 模式密码（业务请求）")
    protect_scope: Optional[str] = Field(
        None, description="保护范围：business=仅业务端点 / all=含管理 API")
    response_template: Optional[str] = Field(
        None, description="换票响应模板，支持 @access_token @refresh_token "
                          "@expires_in @token_type @client_id；空=标准 OAuth2 结构")
    reuse_revoke_family: Optional[bool] = Field(
        None, description="refresh 复用时是否吊销同 client 全部令牌")


class AuthIssueIn(BaseModel):
    """管理端直接签发一对令牌（不做凭据校验，便于脚本取用）。"""
    client_id: str = Field("", description="客户端标识；留空用配置值")
    grant_type: str = Field("client_credentials", description="留痕用的 grant_type")
    scope: str = Field("", description="scope（JWT 模式下写入载荷）")
    access_ttl: Optional[int] = Field(None, ge=1, description="覆盖默认 access 有效期（秒）")
    refresh_ttl: Optional[int] = Field(None, ge=1, description="覆盖默认 refresh 有效期（秒）")
    kind: Optional[str] = Field(None, description="令牌形态：jwt / opaque；留空按配置与规则推断")
    path: Optional[str] = Field(None, description="目标业务路径（按该路径命中的规则取密钥与 TTL）")


class AuthRuleIn(BaseModel):
    """认证规则：按路径命中不同认证方式（多方式逗号分隔 = OR，任一通过即放行）。"""
    path_pattern: str = Field(..., description="精确路径（/api/echo）或前缀通配（/api/echo*）")
    modes: str = Field(..., description="认证方式，逗号分隔：none/jwt/bearer/basic/apikey")
    enabled: bool = Field(True, description="是否启用")
    priority: int = Field(0, description="优先级，大者优先；同级更具体的 pattern 优先")
    secret: Optional[str] = Field(None, description="覆盖全局 JWT 密钥")
    issuer: Optional[str] = Field(None, description="覆盖全局 iss")
    audience: Optional[str] = Field(None, description="覆盖全局 aud")
    access_ttl: Optional[int] = Field(None, ge=0, description="覆盖 access 有效期（秒，0=不覆盖）")
    refresh_ttl: Optional[int] = Field(None, ge=0, description="覆盖 refresh 有效期（秒，0=不覆盖）")
    client_id: Optional[str] = Field(None, description="覆盖 client_id")
    client_secret: Optional[str] = Field(None, description="覆盖 client_secret")
    apikey_header: Optional[str] = Field(None, description="覆盖 apikey 头名")
    apikey_value: Optional[str] = Field(None, description="覆盖 apikey 期望值")
    basic_user: Optional[str] = Field(None, description="覆盖 Basic 用户名")
    basic_pass: Optional[str] = Field(None, description="覆盖 Basic 密码")
    remark: Optional[str] = Field(None, description="备注")


class AuthRuleUpd(BaseModel):
    """更新认证规则（全部可选，仅提交字段生效）。"""
    path_pattern: Optional[str] = None
    modes: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None
    secret: Optional[str] = None
    issuer: Optional[str] = None
    audience: Optional[str] = None
    access_ttl: Optional[int] = Field(None, ge=0)
    refresh_ttl: Optional[int] = Field(None, ge=0)
    client_id: Optional[str] = None
    client_secret: Optional[str] = None
    apikey_header: Optional[str] = None
    apikey_value: Optional[str] = None
    basic_user: Optional[str] = None
    basic_pass: Optional[str] = None
    remark: Optional[str] = None


class AuthVerifyIn(BaseModel):
    """校验令牌（不产生业务副作用）。"""
    token: str = Field(..., description="access_token（jwt/bearer）；basic 模式传 base64(user:pass)；apikey 模式传 key")
    path: Optional[str] = Field(None, description="目标业务路径（按该路径规则的方式与密钥校验）")


class AuthRevokeIn(BaseModel):
    """批量失效入参。"""
    client_id: str = Field("", description="按 client 过滤；空=全部")


class MqSendIn(BaseModel):
    """通过生产者发送一条消息（未填项用生产者默认值）。"""
    body: str = Field(..., description="消息体文本")
    routing_key: Optional[str] = Field(None, description="覆盖默认 routing key")
    content_type: Optional[str] = None
    delivery_mode: Optional[int] = Field(None, ge=1, le=2)
    priority: Optional[int] = Field(None, ge=0, le=9)
    expiration: Optional[str] = Field(None, description="TTL，如 60000（毫秒字符串）")
    correlation_id: Optional[str] = Field(None, description="留空则自动 UUID")
    message_id: Optional[str] = Field(None, description="留空则自动 UUID")
    reply_to: Optional[str] = Field(None, description="回复队列名（留空=不回复；消费方收到后将消费确认回写到该队列）")
    headers: Optional[dict] = Field(None, description="消息 headers")
