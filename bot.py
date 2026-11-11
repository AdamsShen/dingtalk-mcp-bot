"""钉钉企业内部机器人（Stream 模式）→ mcp-gateway。

群里 @机器人 并带上指令，机器人调用 mcp-gateway 的元工具并把结果回帖。
无需公网地址：Stream 模式是机器人主动连钉钉的 WebSocket。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
import sys

import dingtalk_stream
from dingtalk_stream import AckMessage, ChatbotHandler, ChatbotMessage

from config import Settings, load_settings
from mcp_client import McpGatewayError, McpStdioClient
from ops_actions import OpsDispatcher, OpsRouter
from search_flow import SearchFlow, extract_env

logger = logging.getLogger("dingtalk-mcp-bot")

HELP_TEXT = """**mcp-gateway 机器人使用说明**

- `/search <关键词>`　语义检索接口方法（最常用）
- `/search <关键词> env=test`　切到测试环境（默认 env=product）
- 检索后回复 **第N条**　选定方法并提示补参数、指定环境
- 检索后回复 **换一组**　换一批没展示过的候选方法
- 给参数：`uid=586218703 env=test`，随后回复 **确认** 执行只读调用
- 回复 **取消**　结束本次检索流程
- `/tools`　列出 mcp-gateway 暴露的工具
- `/call <工具名> {json参数}`　直接调用某个工具
- `/ops list`　列出线上运营快捷操作（一键认证 / 改手机号 / 强制注销）
- `/ops <操作> k=v ... [--env test]`　预演（plan，回显请求体）
- 待确认时回复 **确认 / 可以** 执行，回复 **取消** 放弃
- `/help`　显示本说明

也可以省掉 `/search`，直接 @机器人 说「修改用户昵称」这类业务话术；
或说「给 uid 123 主播认证」这类话术触发 ops 快捷操作。
写操作（invoke_rpc）默认关闭，且写方法一律不自动执行，需要网关侧
confirm_token 的流程请走 IDE。"""


def _ensure_tls_ca() -> None:
    """修复 macOS 上 WebSocket 的 TLS 握手失败。

    python.org 发行的 Python 在 macOS 上默认不带根证书（ssl 默认信任库为空，
    cert_store_stats 的 x509_ca 为 0），此时 dingtalk-stream 内部的 websockets
    建连会抛 CERTIFICATE_VERIFY_FAILED，导致 Stream 长连接反复重连。
    项目已依赖 certifi（requests 的传递依赖），这里在信任库为空时把标准库
    ssl 指向 certifi 的 CA 包（通过 SSL_CERT_FILE 环境变量生效）。
    """
    if ssl.create_default_context().cert_store_stats().get("x509_ca"):
        return
    try:
        import certifi
    except ImportError:  # certifi 缺失时保持原状，由调用方报错提示
        logger.warning("ssl 默认信任库为空且未安装 certifi，WebSocket TLS 可能失败")
        return
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())


def _message_text(message: ChatbotMessage) -> str:
    content = ""
    if message.text is not None and message.text.content:
        content = message.text.content
    content = content.replace("\u2005", " ").strip()
    # 群里偶尔会残留 @提及，去掉行首的 @xxx
    if content.startswith("@"):
        head, _, tail = content.partition(" ")
        content = tail.strip()
    return content


def _code_block(text: str, lang: str = "json") -> str:
    return f"```{lang}\n{text}\n```"


class GatewayConnection:
    """把 mcp-gateway 会话绑定到当前事件循环。

    钉钉 SDK 每次重连都会用一个新的事件循环（`asyncio.run`），
    绑在旧循环上的子进程会话随之失效，所以这里检测到换循环就重建。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: McpStdioClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def client(self) -> McpStdioClient:
        loop = asyncio.get_running_loop()
        if self._client is None or self._loop is not loop:
            self._client = McpStdioClient(
                command=self._settings.gateway_bin,
                args=self._settings.gateway_args(),
                env=self._settings.gateway_env(),
                call_timeout=self._settings.call_timeout_seconds,
            )
            self._loop = loop
        if not self._client.alive:
            await self._client.start()
        return self._client

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


class GatewayBotHandler(ChatbotHandler):
    def __init__(
        self,
        settings: Settings,
        connection: GatewayConnection,
        ops: OpsRouter,
        search_flow: SearchFlow,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.connection = connection
        self.ops = ops
        self.search_flow = search_flow

    async def process(self, callback: dingtalk_stream.CallbackMessage):
        message = ChatbotMessage.from_dict(callback.data)
        text = _message_text(message)
        logger.info(
            "收到消息 sender=%s conversation=%s text=%r",
            message.sender_nick,
            message.conversation_type,
            text[:120],
        )
        try:
            reply = await self._dispatch(text, message)
        except McpGatewayError as exc:
            logger.warning("网关调用失败：%s", exc)
            reply = f"**网关调用失败**\n\n{exc}"
        except Exception as exc:  # noqa: BLE001 - 机器人不能因为异常静默
            logger.exception("处理消息异常")
            reply = f"**处理异常**\n\n`{type(exc).__name__}: {exc}`"

        await self._reply(reply, message)
        return AckMessage.STATUS_OK, "OK"

    # ------------------------------------------------------------------ #

    async def _dispatch(self, raw: str, message: ChatbotMessage) -> str:
        text = (raw or "").strip()
        if not text or text.lower() in {"help", "/help", "帮助", "?", "？"}:
            return HELP_TEXT

        # 检索流程已生成请求体预览时，确认词必须归它，否则会被 ops 的待定计划抢走
        if self.search_flow.wants_confirm(text, message):
            reply = await self.search_flow.maybe_handle(text, message)
            if reply is not None:
                return reply

        # ops 快捷操作优先（含 plan → 二次确认 → run 的多轮状态）
        outcome = await self.ops.maybe_handle(text, message)
        if outcome.reply is not None:
            return outcome.reply
        # ops 命中但走不通（如参数不全）：回退全局检索，并把提示前置
        fallback_hint = outcome.fallback_hint

        # 显式指令优先，避免「/search xxx env=test」被多轮流程当成补参数误接管
        raw_text = text
        if raw_text.lower() in {"/tools", "tools"} or raw_text in {"工具", "工具列表"}:
            return await self._render_tools()

        env, text, env_explicit = extract_env(raw_text, self.settings.default_env)
        lowered = text.lower()
        if lowered.startswith("/search"):
            query = text[len("/search") :].strip()
            if not query:
                return "用法：`/search <关键词>`，例如 `/search 修改用户昵称`"
            return await self._search(query, env, message, env_explicit)

        if lowered.startswith("/call"):
            return await self._call(text[len("/call") :].strip(), env)

        # 检索多轮流程：第N条 / 换一组 / 补参数 / 确认 / 取消。
        # 传原始文本，让流程自己解析中文环境词，否则「测试」会被提前剥掉。
        flow_reply = await self.search_flow.maybe_handle(
            raw_text, message, allow_params=fallback_hint is None
        )
        if flow_reply is not None:
            return flow_reply

        # 没写指令：当成检索词，同时建立一轮可继续追问的检索会话
        reply = await self._search(text, env, message, env_explicit)
        if fallback_hint:
            reply = f"{fallback_hint}\n\n---\n{reply}"
        return reply

    async def _render_tools(self) -> str:
        client = await self.connection.client()
        tools = await client.list_tools()
        lines = [
            "**mcp-gateway 工具**",
            f"服务端：`{client.server_info.get('name', '?')} "
            f"{client.server_info.get('version', '')}`",
        ]
        for tool in tools:
            name = tool.get("name", "?")
            description = (tool.get("description") or "").strip().splitlines()
            lines.append(f"- `{name}`：{description[0] if description else '（无描述）'}")
        opened = ", ".join(self.settings.allowed_tools)
        lines.append(f"\n机器人已开放：{opened}")
        return "\n".join(lines)

    async def _search(self, query: str, env: str, message, env_explicit: bool) -> str:
        """发起一轮检索并交给他会话流程托管，后续可继续选第 N 条或换一组。"""
        return await self.search_flow.begin_search(query, env, message, env_explicit)

    async def _call(self, rest: str, env: str) -> str:
        if not rest:
            return (
                '用法：`/call <工具名> {"key": "value"}`\n'
                '例：`/call list_methods {"iface":"com.ximalaya.userservice.facade.thrift.'
                'UpdateServiceFacade$Iface","artifact_id":"standalone-pom"}`'
            )

        parts = rest.split(None, 1)
        name = parts[0]
        raw_args = parts[1].strip() if len(parts) > 1 else "{}"

        if name not in self.settings.allowed_tools:
            return (
                f"工具 `{name}` 未对机器人开放。\n"
                f"当前可用：{', '.join(self.settings.allowed_tools)}\n"
                "如需放行 `invoke_rpc`，在 .env 里设 `ALLOW_INVOKE_RPC=true` 后重启。"
            )

        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as exc:
            return f"参数不是合法 JSON：`{exc}`"
        if not isinstance(arguments, dict):
            return "参数必须是 JSON 对象。"

        arguments.setdefault("env", env)
        client = await self.connection.client()
        raw, is_error = await client.call_tool(name, arguments)
        header = "调用失败" if is_error else "调用完成"
        return f"**{header}：`{name}`**（env=`{arguments['env']}`）\n\n{_code_block(raw)}"

    async def _reply(self, text: str, message: ChatbotMessage) -> None:
        limit = self.settings.max_reply_chars
        body = text if len(text) <= limit else text[: limit - 20] + "\n…（内容已截断）"
        try:
            await asyncio.to_thread(
                self.reply_markdown, "mcp-gateway", body, message
            )
        except Exception:  # noqa: BLE001 - 回帖失败不能影响 ack
            logger.exception("回帖失败")


def main() -> int:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    settings.require_credentials()
    _ensure_tls_ca()

    # ops 快捷操作：用系统 Python 调 dispatch.py，启动时加载一次别名表
    allowed = {s.strip() for s in settings.ops_allowed_senders.split(",") if s.strip()}
    dispatcher = OpsDispatcher(settings.ops_dispatch_py, settings.ops_dispatch_script)
    dispatcher.load_aliases()
    ops_router = OpsRouter(dispatcher, allowed or None)

    connection = GatewayConnection(settings)
    search_flow = SearchFlow(settings, connection.client)
    handler = GatewayBotHandler(settings, connection, ops_router, search_flow)

    credential = dingtalk_stream.Credential(settings.client_id, settings.client_secret)
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(ChatbotMessage.TOPIC, handler)

    logger.info("机器人已启动，等待群里 @机器人（env 默认 %s）", settings.default_env)
    try:
        client.start_forever()
    except KeyboardInterrupt:
        logger.info("收到中断，退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
