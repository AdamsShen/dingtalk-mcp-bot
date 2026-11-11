"""配置：全部来自环境变量 / .env，不要把凭据写进代码。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class Settings:
    # 钉钉企业内部机器人（Stream 模式）
    client_id: str
    client_secret: str

    # mcp-gateway
    gateway_bin: str
    gateway_config: str
    gateway_local_user: str
    gateway_local_allow_destructive: bool

    # 行为
    default_env: str = "product"
    allow_invoke_rpc: bool = False
    search_limit: int = 5
    call_timeout_seconds: float = 90.0
    max_reply_chars: int = 3500
    log_level: str = "INFO"

    # 检索会话持久化：SQLite 单文件，重启不丢（不需要按人隔离，仍按群会话）
    session_db: str = str(BASE_DIR / "data" / "sessions.db")

    # ops-quick-actions：线上运营快捷 RPC（复用 actions.yaml 分发器）
    # 必须用系统 Python（含 PyYAML / certifi），项目虚拟环境没有这些依赖
    ops_dispatch_py: str = "/Library/git-root/thriftTester/mcp-gateway/.venv/bin/python"
    ops_dispatch_script: str = str(Path.home() / ".codebuddy/skills/ops-quick-actions/scripts/dispatch.py")
    # 可选：仅这些发送者（钉钉 senderId / senderStaffId，逗号分隔）能触发 ops；
    # 留空表示群里所有人都能触发（write/destructive 仍需二次确认）。
    ops_allowed_senders: str = ""

    readonly_tools: tuple[str, ...] = field(
        default=("search_api", "list_methods", "describe_method", "get_servers")
    )

    # ------------------------------------------------------------------ #

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        tools = list(self.readonly_tools)
        if self.allow_invoke_rpc:
            tools.append("invoke_rpc")
        return tuple(tools)

    def gateway_args(self) -> list[str]:
        args = ["stdio"]
        if self.gateway_config:
            args += ["--config", self.gateway_config]
        return args

    def gateway_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # 让网关审计里能看出调用来自钉钉机器人
        env["GATEWAY_LOCAL_USER"] = self.gateway_local_user
        if self.gateway_local_allow_destructive:
            env["GATEWAY_LOCAL_ALLOW_DESTRUCTIVE"] = "true"
        else:
            env.pop("GATEWAY_LOCAL_ALLOW_DESTRUCTIVE", None)
        return env

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("DINGTALK_CLIENT_ID", self.client_id),
                ("DINGTALK_CLIENT_SECRET", self.client_secret),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "缺少钉钉凭据："
                + ", ".join(missing)
                + "\n请复制 .env.example 为 .env 并填写后重试。"
            )


def load_settings() -> Settings:
    return Settings(
        client_id=_env("DINGTALK_CLIENT_ID"),
        client_secret=_env("DINGTALK_CLIENT_SECRET"),
        gateway_bin=_env(
            "GATEWAY_BIN",
            "/Library/git-root/thriftTester/mcp-gateway/.venv/bin/mcp-gateway",
        ),
        gateway_config=_env("GATEWAY_CONFIG", str(Path.home() / ".config/mcp-gateway/config.yaml")),
        gateway_local_user=_env("GATEWAY_LOCAL_USER", "dingtalk-bot"),
        gateway_local_allow_destructive=_bool_env("GATEWAY_LOCAL_ALLOW_DESTRUCTIVE"),
        default_env=_env("DEFAULT_ENV", "product"),
        allow_invoke_rpc=_bool_env("ALLOW_INVOKE_RPC"),
        search_limit=int(_env("SEARCH_LIMIT", "5")),
        call_timeout_seconds=float(_env("CALL_TIMEOUT_SECONDS", "90")),
        max_reply_chars=int(_env("MAX_REPLY_CHARS", "3500")),
        log_level=_env("LOG_LEVEL", "INFO").upper(),
        session_db=_env("SESSION_DB", str(BASE_DIR / "data" / "sessions.db")),
        ops_dispatch_py=_env(
            "OPS_DISPATCH_PY",
            "/Library/git-root/thriftTester/mcp-gateway/.venv/bin/python",
        ),
        ops_dispatch_script=_env(
            "OPS_DISPATCH_SCRIPT",
            str(Path.home() / ".codebuddy/skills/ops-quick-actions/scripts/dispatch.py"),
        ),
        ops_allowed_senders=_env("OPS_ALLOWED_SENDERS", ""),
    )
