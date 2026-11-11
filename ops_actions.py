"""把 ops-quick-actions 的快捷 RPC 操作接入钉钉机器人。

职责：
- 把群里 @机器人 的自然语言（一键认证 / 改手机号 / 强制注销 等）或 `/ops` 指令，
  转成对 actions.yaml 分发器 ``scripts/dispatch.py`` 的调用。
- 维护「plan → 二次确认 → run」的会话级待定状态，严格沿用 skill 的硬约束：
  write / destructive 必须等用户在群里明确确认，token 与「操作+环境+参数」绑定。

实现说明：
- dispatch.py 只接受 k=v 形式的参数，别名解析、二次确认 token 都由它来做；
  本模块不做业务常量，只负责「识别意图 → 调 dispatch → 暂存待定 → 等确认」。
- 调用 dispatch.py 必须用系统 Python（含 PyYAML / certifi），见 config.OpsDispatchPy。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("dingtalk-mcp-bot.ops")

# ───────────────────────────── 常量 ─────────────────────────────

# 二次确认触发词（不区分大小写）；注意这些词可能出现在日常对话，
# 但只有在存在「待定操作」时才会被当作确认。
CONFIRM_WORDS = {"确认", "确认执行", "执行", "可以", "ok", "yes", "run"}
CANCEL_WORDS = {"取消", "取消操作", "cancel", "算了"}

# 自由文本里的 k=v 提取：value 取到下一个空白为止，兼容中文值（如 realName=张三）
_KV_RE = re.compile(r"(\w+)\s*=\s*(\S+)")

# dispatch.py 子进程超时（含网络调用）
DISPATCH_TIMEOUT = 30.0

# 从文本里抠出 env=test / --env test（与 bot.py 的规则保持一致）
_ENV_RE = re.compile(r"(?:^|\s)(?:--env[=\s]+|env=)(test|product)(?=\s|$)", re.IGNORECASE)

# 中文环境词 → env（dispatch 只支持 product / test）
_CN_ENV = (
    ("线上", "product"), ("生产", "product"), ("正式", "product"),
    ("测试", "test"), ("预发", "test"), ("预发布", "test"),
)

# 字段取值正则。mobile 用 `1[3-9]\d{9}` 而非 `1\d{10}`，避免把 11 位的 uid 误当手机号。
_FIELD_PATTERNS = {
    "idCard": re.compile(r"(?<!\d)(\d{17}[\dXx]|\d{15})(?!\d)"),
    "mobile": re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"),
    "uid": re.compile(r"(?<!\d)(\d{5,12})(?!\d)"),
    "realName": re.compile(r"([\u4e00-\u9fa5]{2,4})"),
}
# 位置推断顺序：越具体的字段越先取，防止 uid 抢走手机号/身份证的数字
_FIELD_ORDER = ("idCard", "mobile", "uid", "realName")

# 中文标签 → 规范字段名（插入顺序即匹配优先级，"手机号" 先于 "手机"）
_LABEL_TO_FIELD = {
    "用户ID": "uid", "用户id": "uid", "账号ID": "uid", "账号id": "uid", "uid": "uid",
    "手机号": "mobile", "新手机号": "mobile", "手机": "mobile", "电话": "mobile", "联系方式": "mobile",
    "身份证号": "idCard", "身份证": "idCard", "证件号": "idCard",
    "真实姓名": "realName", "姓名": "realName", "名字": "realName",
}

# realName 位置推断时要排除的常见非姓名词（别名已先行剥离，这里兜底）
_NAME_STOPWORDS = {
    "帮我", "麻烦", "请帮", "用户", "账号", "主播", "认证", "手机", "手机号", "身份证",
    "姓名", "名字", "环境", "线上", "生产", "测试", "预发", "换绑", "修改", "强制",
    "注销", "一键", "过了", "一下", "这个", "那个", "帮忙",
}


@dataclass
class PendingPlan:
    """一次 plan 产生的待确认操作。"""

    action: str
    env: str
    variables: dict
    token: str
    risk: str
    desc: str = ""


@dataclass
class OpsOutcome:
    """ops 意图路由结果。

    - ``reply`` 非空：ops 已处理，直接把该文本回给用户。
    - ``reply`` 为空、``fallback_hint`` 非空：命中 ops 但无法完成（如参数不全），
      调用方回退到全局检索，并把 hint 作为提示前置到检索结果前。
    - 两者皆空：不是 ops 意图，正常走检索。
    """

    reply: Optional[str] = None
    fallback_hint: Optional[str] = None


def _detect_env(text: str, default: str = "product") -> str:
    """识别环境：优先显式 ``env=test`` / ``--env test``，再认中文字眼（线上/测试…）。"""
    match = _ENV_RE.search(text)
    if match:
        return match.group(1).lower()
    for word, env in _CN_ENV:
        if word in text:
            return env
    return default


# ─────────────────────────── dispatch 封装 ───────────────────────────


class OpsDispatcher:
    """封装对 ops-quick-actions/scripts/dispatch.py 的调用。"""

    def __init__(self, python_bin: str, script: str) -> None:
        self._python = python_bin
        self._script = script
        # alias(lower) -> action key；以及 action key -> 元信息（require/risk/desc/aliases）
        self._aliases: Optional[dict] = None
        self._meta: dict = {}

    def load_aliases(self) -> None:
        """启动期加载一次别名表（单一事实源 = dispatch.py list --json）。

        失败不致命：降级为只支持 ``/ops`` 显式指令，自然语言别名命中会失效。
        """
        try:
            stdout, _, rc = self._run(["list", "--json"])
        except (FileNotFoundError, PermissionError) as exc:
            logger.warning("ops 分发器不可用，仅 /ops 显式指令生效：%s", exc)
            return
        if rc != 0:
            logger.warning("加载 ops 操作列表失败（rc=%s），仅 /ops 显式指令生效", rc)
            return
        try:
            rows = json.loads(stdout)
        except ValueError:
            logger.warning("ops 操作列表解析失败，仅 /ops 显式指令生效")
            return
        aliases: dict = {}
        meta: dict = {}
        for row in rows:
            key = row.get("action", "")
            meta[key] = {
                "require": row.get("require", []),
                "risk": row.get("risk", "write"),
                "desc": row.get("desc", ""),
                "aliases": [str(a) for a in row.get("aliases", [])],
            }
            for alias in [key] + row.get("aliases", []):
                aliases[str(alias).lower()] = key
        self._aliases = aliases
        self._meta = meta
        logger.info("ops 已加载 %d 个快捷操作", len(meta))

    @property
    def ready(self) -> bool:
        return self._aliases is not None

    def match_alias(self, text: str) -> Optional[str]:
        """文本中出现某个操作别名（大小写不敏感）时返回操作 key。

        按别名长度倒序匹配：短别名（如「认证」）是长别名（如「查询认证信息」）
        的子串，按插入顺序会先命中，导致查询请求被当成认证写操作。
        """
        if not self._aliases:
            return None
        lowered = text.lower()
        for alias in sorted(self._aliases, key=len, reverse=True):
            if alias and alias in lowered:
                return self._aliases[alias]
        return None

    def action_meta(self, key: str) -> dict:
        return self._meta.get(key, {})

    # ───────────── 底层同步调用（由上层用 to_thread 包成异步） ─────────────

    def _run(self, args: list[str], timeout: float = DISPATCH_TIMEOUT):
        cmd = [self._python, self._script, *args]
        logger.info("ops 调用 dispatch：%s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout, proc.stderr, proc.returncode

    async def run(self, args: list[str]) -> tuple[str, str, int]:
        return await asyncio.to_thread(self._run, args)


# ─────────────────────────── 意图路由 ───────────────────────────


class OpsRouter:
    """在钉钉群里承接 ops 快捷操作，管理 plan/确认/run 多轮状态。"""

    def __init__(self, dispatcher: OpsDispatcher, allowed_senders: Optional[set[str]] = None) -> None:
        self._d = dispatcher
        # conversation_id -> 待确认操作
        self._pending: dict[str, PendingPlan] = {}
        # 可选发送者白名单（dingtalk senderId / senderStaffId），None 表示不限制
        self._allowed = allowed_senders

    def _conv_of(self, message) -> str:
        return getattr(message, "conversation_id", None) or getattr(message, "sender_id", None) or "default"

    def _sender_ids(self, message) -> set[str]:
        return {getattr(message, "sender_id", None), getattr(message, "sender_staff_id", None)} - {None}

    async def maybe_handle(self, text: str, message) -> OpsOutcome:
        """优先按 ops 快捷操作处理；不是 ops 意图则返回空 outcome 让调用方走检索。"""
        stripped = (text or "").strip()
        lowered = stripped.lower()
        conv = self._conv_of(message)

        # 0) 白名单（若配置）
        if self._allowed is not None and not (self._sender_ids(message) & self._allowed):
            return OpsOutcome(reply="⚠️ 你不在 ops 快捷操作授权名单内，无法执行。\n（由.env 的 OPS_ALLOWED_SENDERS 配置）")

        pending = self._pending.get(conv)
        if pending is not None:
            if lowered in CONFIRM_WORDS:
                return OpsOutcome(reply=await self._run_pending(conv))
            if lowered in CANCEL_WORDS:
                self._pending.pop(conv, None)
                return OpsOutcome(reply="已取消本次操作，未执行任何调用。")

        # 1) 显式 /ops 指令（用户明确要 ops，失败也不回退检索）
        if lowered == "/ops" or stripped.startswith("/ops "):
            return OpsOutcome(reply=await self._command(stripped, conv))

        # 2) 自然语言别名命中
        key = self._d.match_alias(text)
        if key is not None:
            return await self._plan_from_text(key, text, conv)

        return OpsOutcome()

    # ───────────────────────── 内部 ─────────────────────────

    def _kv_args(self, kv: dict) -> list[str]:
        return [f"{k}={v}" for k, v in kv.items() if v != ""]

    def _extract_params(self, action: str, text: str) -> dict:
        """把中文自然语言抠成 dispatch 需要的 ``k=v``。

        支持的三种写法（按优先级）：
        1. 显式 ``k=v``，如 ``uid=594301073 mobile=17682488695``；
        2. 中文标签，如 ``手机号：176 8248 8695``、``身份证号：110...``、``姓名：张三``；
        3. 位置推断，按字段类型补剩下的必填项，如 ``594301073 强制注销`` 里的 uid。

        例：``594301073 换绑成手机号：176 8248 8695 线上环境``
              → ``{"uid": "594301073", "mobile": "17682488695"}``
        """
        meta = self._d.action_meta(action)
        required = [str(f) for f in (meta.get("require") or [])]
        kv: dict = {}

        # 1) 显式 k=v 最高优先（去掉中文句读，避免 "123，" 带上逗号）
        for m in _KV_RE.finditer(text):
            kv[m.group(1)] = m.group(2).rstrip("，。；、,.;：:")

        # 去掉别名 / 环境词 / 「环境」等噪音，防止污染 realName 这类中文取值
        work = _KV_RE.sub(" ", text)
        for word in list(meta.get("aliases") or []) + [action, "环境"]:
            if word:
                work = work.replace(str(word), " ")
        for word, _ in _CN_ENV:
            work = work.replace(word, " ")
        # 合并被空格/横杠拆开的数字串：'176 8248 8695' → '17682488695'
        compact = re.sub(r"(?<=\d)[\s\-]+(?=\d)", "", work)

        def consume(value: str) -> None:
            """把已取用的值从工作串里剔除，避免被后面的字段重复匹配。"""
            nonlocal work, compact
            if value:
                work = work.replace(value, " ")
                compact = compact.replace(value, " ")

        # 2) 中文标签式
        for label, field in _LABEL_TO_FIELD.items():
            if field not in required or kv.get(field):
                continue
            idx = compact.find(label)
            if idx < 0:
                continue
            tail = compact[idx + len(label) :]
            match = _FIELD_PATTERNS[field].search(tail)
            if match:
                kv[field] = match.group(1)
                consume(match.group(1))

        # 3) 位置推断补齐
        for field in _FIELD_ORDER:
            if field not in required or kv.get(field):
                continue
            source = work if field == "realName" else compact
            value = None
            for match in _FIELD_PATTERNS[field].finditer(source):
                candidate = match.group(1)
                if field == "realName" and candidate in _NAME_STOPWORDS:
                    continue
                value = candidate
                break
            if value:
                kv[field] = value
                consume(value)

        if kv:
            logger.info("ops 参数解析 action=%s text=%r -> %s", action, text, kv)
        return kv

    async def _command(self, stripped: str, conv: str) -> str:
        rest = stripped[len("/ops") :].strip()
        if rest in ("", "list"):
            out, err, rc = await self._d.run(["list"])
            return out if rc == 0 else f"操作列表获取失败：{err.strip()}"
        if rest.lower().startswith("show "):
            action = rest[len("show ") :].strip()
            out, err, rc = await self._d.run(["show", action])
            return out if rc == 0 else f"查看失败：{err.strip()}"

        tokens = rest.split()
        if not tokens:
            return (
                "用法：\n"
                "- `/ops list` 列出全部操作\n"
                "- `/ops show <操作>` 看参数要求\n"
                "- `/ops <操作> k=v ... [--env test]` 预演（plan）\n"
                "- `/ops <操作> k=v ... --confirm <token>` 执行"
            )
        action = tokens[0]
        kv: dict = {}
        confirm: Optional[str] = None
        env: Optional[str] = None
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok == "--confirm" and i + 1 < len(tokens):
                confirm = tokens[i + 1]
                i += 2
                continue
            if tok == "--env" and i + 1 < len(tokens):
                env = tokens[i + 1]
                i += 2
                continue
            if "=" in tok:
                k, v = tok.split("=", 1)
                kv[k.strip()] = v
            i += 1
        if confirm:
            return await self._exec(action, env or "product", kv, confirm, conv)
        return await self._plan(action, env or "product", kv, conv)

    async def _plan_from_text(self, key: str, text: str, conv: str) -> OpsOutcome:
        """自然语言命中操作后：参数齐全就 plan，缺参数则回退全局检索并给出提示。"""
        env = _detect_env(text, "product")
        kv = self._extract_params(key, text)

        meta = self._d.action_meta(key)
        required = [str(f) for f in (meta.get("require") or [])]
        missing = [f for f in required if not kv.get(f)]
        if missing:
            desc = meta.get("desc") or key
            logger.info("ops 命中 %s 但缺少参数 %s，回退全局检索", key, missing)
            example = " ".join(f"{f}=..." for f in required)
            hint = (
                f"识别到 ops 操作 **`{key}`**（{desc}），但缺少参数：`{'、'.join(missing)}`。\n"
                f"可直接发：`{example}`（也可用中文，如「手机号：176xxxx」，环境说「线上/测试」）。\n"
                f"没帮你执行，下面按全局检索找相关接口："
            )
            return OpsOutcome(fallback_hint=hint)

        return OpsOutcome(reply=await self._plan(key, env, kv, conv))

    async def _plan(self, action: str, env: str, kv: dict, conv: str) -> str:
        out, err, rc = await self._d.run(
            ["plan", action, *self._kv_args(kv), "--env", env, "--json"]
        )
        if rc != 0:
            return f"**plan 失败**\n\n{err.strip() or out.strip()}"
        try:
            data = json.loads(out)
        except ValueError:
            return out

        meta = self._d.action_meta(data.get("action", action))
        risk = data.get("risk", meta.get("risk", "write"))
        logger.info(
            "ops plan action=%s env=%s risk=%s vars=%s conv=%s",
            data.get("action", action),
            env,
            risk,
            data.get("variables"),
            conv,
        )

        # read 类没有副作用，免二次确认：plan 后直接执行，少一轮交互
        if risk == "read":
            return await self._exec(
                data["action"], data["env"], data["variables"],
                data.get("confirm_token", ""), conv,
            )

        self._pending[conv] = PendingPlan(
            action=data["action"],
            env=data["env"],
            variables=data["variables"],
            token=data["confirm_token"],
            risk=risk,
            desc=data.get("target", ""),
        )
        return self._render_plan(data)

    async def _run_pending(self, conv: str) -> str:
        pending = self._pending.pop(conv, None)
        if pending is None:
            return "当前没有待确认的操作。"
        return await self._exec(pending.action, pending.env, pending.variables, pending.token, conv)

    async def _exec(self, action: str, env: str, kv: dict, confirm: str, conv: str) -> str:
        out, err, rc = await self._d.run(
            ["run", action, *self._kv_args(kv), "--env", env, "--confirm", confirm, "--json"]
        )
        if rc != 0 and not out.strip():
            return f"**执行失败**\n\n{err.strip()}"
        try:
            data = json.loads(out)
        except ValueError:
            return out

        http = data.get("http_status")
        success = data.get("success")
        # 有的接口不返回 success，只用业务码 retCode；此时不能一律判失败
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        ret_code = result.get("retCode")
        ok = success is True or (success is None and ret_code in (0, None) and http == 200)
        logger.info(
            "ops run action=%s env=%s http=%s success=%s retCode=%s conv=%s",
            action,
            env,
            http,
            success,
            ret_code,
            conv,
        )
        if not ok:
            logger.warning(
                "ops run 未成功 action=%s env=%s http=%s retCode=%s errorCode=%s errorMsg=%s",
                action,
                env,
                http,
                ret_code,
                data.get("errorCode"),
                data.get("errorMsg"),
            )
        return self._render_run(data)

    # ───────────────────────── 渲染 ─────────────────────────

    @staticmethod
    def _render_plan(data: dict) -> str:
        lines = [
            f"**ops 操作预览：`{data['action']}`**　risk=`{data['risk']}`",
            f"- env：`{data['env']}`",
            f"- target：`{data['target']}`",
            f"- 参数：{json.dumps(data['variables'], ensure_ascii=False)}",
            "\n**请求体**（请核对后回复「确认」执行）：",
            "```json",
            json.dumps(data["request_body"], ensure_ascii=False, indent=2),
            "```",
            f"\n确认 token：`{data['confirm_token']}`",
            "回复 **确认 / 可以** 即执行；回复 **取消** 放弃。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _render_run(data: dict) -> str:
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        if data.get("success") is not None:
            status = f"success=`{data['success']}`"
        elif result.get("retCode") is not None:
            status = f"retCode=`{result['retCode']}`"
        else:
            status = "success=`-`"
        lines = [
            f"**ops 执行结果：`{data['action']}`**（env=`{data['env']}`）",
            f"- HTTP：`{data['http_status']}`　耗时：`{data['duration_ms']}ms`　{status}",
        ]
        if data.get("errorCode") not in (None, 0):
            lines.append(f"- errorCode：`{data['errorCode']}`")
        if data.get("errorMsg"):
            lines.append(f"- errorMsg：`{data['errorMsg']}`")
        lines.append("\n**响应**：")
        lines.append("```json")
        lines.append(json.dumps(data["result"], ensure_ascii=False, indent=2))
        lines.append("```")
        return "\n".join(lines)
