"""mcp-gateway 的 stdio MCP 客户端。

为什么手写而不引官方 SDK：只用到 initialize / tools.list / tools.call 三个方法，
按行分隔的 JSON-RPC 2.0 就够，少一个重依赖，也便于排查（stderr 直接转日志）。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging

logger = logging.getLogger("mcp_client")

PROTOCOL_VERSION = "2024-11-05"


class McpGatewayError(RuntimeError):
    """MCP 层错误（进程挂了、超时、服务端返回 error）。"""


class McpStdioClient:
    """把 mcp-gateway 当成一个长驻子进程，复用同一条 stdio 会话。"""

    def __init__(
        self,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        call_timeout: float = 90.0,
    ) -> None:
        self._command = command
        self._args = args
        self._env = env
        self._call_timeout = call_timeout

        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_reader: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._closed = False

        self.server_info: dict = {}
        self.tools: list[dict] = []

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def alive(self) -> bool:
        return (
            not self._closed
            and self._proc is not None
            and self._proc.returncode is None
        )

    async def start(self) -> None:
        if self.alive:
            return

        logger.info("启动 mcp-gateway：%s %s", self._command, " ".join(self._args))
        # 默认 StreamReader 行上限 64KB；search_api 一次取 10 条候选时单行 JSON-RPC
        # 会超过它并抛 ValueError('Separator is found, but chunk is longer than limit')，
        # 读管道随之崩掉、请求只能等超时。这里按网关 max_response_bytes 留足余量。
        self._proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            limit=2 * 1024 * 1024,
        )
        self._closed = False
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr_reader = asyncio.create_task(self._read_stderr())

        result = await self._request("initialize", self._initialize_params())
        self.server_info = result.get("serverInfo") or {}
        await self._notify("notifications/initialized", {})
        logger.info("mcp-gateway 已就绪：%s", self.server_info)

    async def close(self) -> None:
        self._closed = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
        for task in (self._reader, self._stderr_reader):
            if task is not None:
                task.cancel()
        self._proc = None

    def _initialize_params(self) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "dingtalk-mcp-bot", "version": "1.0.0"},
        }

    # ------------------------------------------------------------------ #
    # MCP 方法
    # ------------------------------------------------------------------ #

    async def list_tools(self) -> list[dict]:
        result = await self._request("tools/list", {})
        self.tools = result.get("tools") or []
        return self.tools

    async def call_tool(self, name: str, arguments: dict) -> tuple[str, bool]:
        """返回 (文本内容, 是否 isError)。"""
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        return _extract_text(result), bool(result.get("isError"))

    # ------------------------------------------------------------------ #
    # JSON-RPC 细节
    # ------------------------------------------------------------------ #

    async def _request(self, method: str, params: dict) -> dict:
        if not self.alive:
            raise McpGatewayError("mcp-gateway 未运行")
        req_id = next(self._ids)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout=self._call_timeout)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise McpGatewayError(
                f"{method} 超时（>{self._call_timeout:g}s）"
            ) from None

    async def _notify(self, method: str, params: dict) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise McpGatewayError("mcp-gateway 进程不可用")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        proc.stdin.write(data)
        await proc.stdin.drain()

    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stream = proc.stdout
        while True:
            line = await stream.readline()
            if not line:
                break
            raw = line.decode("utf-8", "replace").strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("跳过无法解析的报文：%s", raw[:200])
                continue
            msg_id = message.get("id")
            if msg_id is None:
                logger.debug("收到通知：%s", message.get("method"))
                continue
            future = self._pending.pop(msg_id, None)
            if future is None or future.done():
                continue
            error = message.get("error")
            if error:
                if isinstance(error, dict):
                    text = error.get("message") or json.dumps(error, ensure_ascii=False)
                else:
                    text = str(error)
                future.set_exception(McpGatewayError(text))
            else:
                future.set_result(message.get("result") or {})
        self._fail_pending("mcp-gateway 进程已退出")

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            logger.debug("[gateway] %s", line.decode("utf-8", "replace").rstrip())

    def _fail_pending(self, reason: str) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(McpGatewayError(reason))
        self._pending.clear()


def _extract_text(result: dict) -> str:
    """tools/call 的返回是 content 数组，取其中的文本。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text" and item.get("text") is not None:
            parts.append(str(item["text"]))
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    text = "\n".join(parts).strip()
    if text:
        return text
    return json.dumps(result, ensure_ascii=False)
