"""检索结果的会话式多轮流程。

解决的问题：群里 @机器人 检索接口后，机器人原先只回一页候选就结束，
用户说「第4个方法」会被当成新的检索词。本模块把检索结果按会话保存下来，
支持「选第 N 条 → 补参数和环境 → 请求体预览 → 确认」以及「换一组」。

关于「换一组」的实现约束：
mcp-gateway 的 search_api 只支持 limit（网关上限 10），没有 offset / exclude。
因此一次检索先取满候选池，本地按页展示；池用尽后，用改写过的 query
重新发起一次向量检索，并按 (iface, method, artifact_id) 去重，
保证换出来的是真正没展示过的新候选，而不是把同一批结果再贴一遍。
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from config import Settings
from mcp_client import McpStdioClient

logger = logging.getLogger("dingtalk-mcp-bot.search")

# 单次检索取满候选池；网关 max_search_results 当前为 10，取 10 不会超限
POOL_LIMIT = 10
# 会话有效期 2 小时（按最后一次交互滑动计时）：群里多人共用会话，
# 保留足够长的追问窗口，同时避免隔夜的「第2条」串到新对话
SESSION_TTL_SECONDS = 7200.0
# 换组时改写 query 的后缀，按顺序轮转；仅当候选池用尽才触发新一轮向量检索
QUERY_SUFFIXES = (" 接口", " 方法", " 服务", " 查询", " 获取", " 列表", " 详情")

# 选第 N 条：「第2条」「第4个方法」「选 3」「2」「第二条」
_PICK_RE = re.compile(
    r"^(?:第\s*([0-9]+|[一二三四五六七八九十])\s*(?:个方法|条|个|项)?"
    r"|选\s*([0-9]+)\s*(?:条|个)?|([0-9]+))$"
)
_CN_DIGITS = {c: i for i, c in enumerate("一二三四五六七八九十", 1)}

_NEXT_WORDS = {
    "换一组", "换一组方法", "换一批", "换一批方法", "下一组", "下一页",
    "再来一组", "看看别的", "换", "more", "next",
}
_CANCEL_WORDS = {"取消", "取消操作", "放弃", "算了", "cancel", "不用了"}
# 群里实际会说的确认话术都比「确认」随意，收全一点，避免掉回检索
_CONFIRM_WORDS = {
    "确认", "确认执行", "确定", "确定执行", "执行", "可以", "好的", "好",
    "行", "是", "嗯", "对", "没问题", "开始", "发起", "调用",
    "ok", "okay", "yes", "y", "run", "go",
}

# 与 ops_actions.py 的中文环境词规则保持一致；执行环境必须由用户显式指定
_CN_ENV = (
    ("线上", "product"), ("生产", "product"), ("正式", "product"),
    ("测试", "test"), ("预发", "test"), ("预发布", "test"),
)
_ENV_RE = re.compile(r"(?:^|\s)(?:--env[=\s]+|env=)(test|product)(?=\s|$)", re.IGNORECASE)
_KV_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*([^\s，,；;、]+)")
# 位置参数：纯数字且不在 k=v 里（排除 operId=11932 这类显式值），用于「599602175 查询」写法
_POSITIONAL_NUM_RE = re.compile(r"(?<![\w=])(\d{4,20})(?![\w])")
# 中文数字与标点混排的分隔，便于从自由文本里提取参数
_SEP_RE = re.compile(r"[，,；;、\s]+")


def _looks_like_params(text: str) -> bool:
    """判断一条消息是否在补请求参数。

    用户常见写法有三种：``uid=1``、``线上环境``（只补环境）、``599602175 查询``
    （位置参数）。只要是这几种就应留在当前会话里，不能被当成新的检索词——
    否则会话会被重建，用户前面给的参数全部丢失。
    """
    candidate = (text or "").strip()
    if not candidate:
        return False
    if "=" in candidate:
        return True
    if _parse_env(candidate):
        return True
    # 纯数字（可带中文标点分隔）视为可能的 uid / roomId 等位置参数
    return bool(re.fullmatch(r"[\d，,；;、\s]+", candidate))


def _conv_of(message) -> str:
    """取会话标识：优先 conversation_id，单聊回退到 sender_id。"""
    return (
        getattr(message, "conversation_id", None)
        or getattr(message, "sender_id", None)
        or "default"
    )


def _item_key(item: dict) -> str:
    """候选去重键：同名方法在不同 artifact 下路由不同，算两条候选。"""
    return f"{item.get('iface', '')}#{item.get('method', '')}#{item.get('artifact_id', '')}"


def _short_iface(iface: str) -> str:
    return str(iface or "").rsplit(".", 1)[-1]


def _parse_pick(text: str) -> Optional[int]:
    """把「第4个方法 / 选 3 / 2 / 第二条」解析成序号，非选择指令返回 None。"""
    match = _PICK_RE.match(text.strip())
    if not match:
        return None
    raw = next((g for g in match.groups() if g), None)
    if raw is None:
        return None
    return _CN_DIGITS[raw] if raw in _CN_DIGITS else int(raw)


def extract_env(text: str, default: str) -> tuple[str, str, bool]:
    """识别并剥离环境写法，返回 (环境, 清理后的文本, 是否显式指定)。

    检索阶段与执行阶段共用同一套规则：既认 ``env=test`` / ``--env product``，
    也认中文「线上 / 生产 / 测试 / 预发」，避免两处对同一句话理解不一致。
    例：``extract_env("查用户 env=test", "product")`` → ``("test", "查用户", True)``
    """
    match = _ENV_RE.search(text)
    if match:
        cleaned = (text[: match.start()] + " " + text[match.end() :]).strip()
        return match.group(1).lower(), cleaned, True
    for word, env in _CN_ENV:
        if word in text:
            return env, text.replace(word, " ").strip(), True
    return default, text, False


def _parse_env(text: str) -> Optional[str]:
    """识别显式环境：env=test / --env product / 中文「线上」「测试」。"""
    return extract_env(text, "")[0] or None


def _strip_env_words(text: str) -> str:
    """剔除环境写法，避免把 env=test 当成业务参数。"""
    cleaned = _ENV_RE.sub(" ", text)
    for word, _ in _CN_ENV:
        cleaned = cleaned.replace(word, " ")
    return cleaned.strip()


def _is_numeric_type(type_name: Optional[str]) -> bool:
    """判断 thrift 参数是否为整数/浮点类型，用于位置参数只填数字字段。"""
    lowered = str(type_name or "").lower()
    return any(token in lowered for token in ("i8", "i16", "i32", "i64", "int", "long", "double", "float"))


def _is_bool_type(type_name: Optional[str]) -> bool:
    """bool 参数有模板默认值，缺省时沿用模板，不要求用户显式提供。"""
    return "bool" in str(type_name or "").lower()


def _coerce(raw: str, existing) -> object:
    """按 request_param_template 里的占位类型把字符串转成正确类型。"""
    if isinstance(existing, bool):
        return raw.strip().lower() in {"1", "true", "yes", "y"}
    if isinstance(existing, int):
        return int(raw)
    if isinstance(existing, float):
        return float(raw)
    return raw


@dataclass
class _Session:
    """一个会话的检索状态。"""

    query: str
    search_env: str
    # 检索消息里是否显式带过环境；带过时允许后续沿用，否则执行前必须补环境
    env_explicit: bool
    pool: list[dict] = field(default_factory=list)
    shown: set[str] = field(default_factory=set)
    page: list[dict] = field(default_factory=list)
    group_no: int = 1
    variant_index: int = 0
    # 本次换组是否重新发起过向量检索，用于回帖里如实说明候选来源
    refetched: bool = False
    stage: str = "list"  # list → params → preview
    picked: Optional[dict] = None
    # 选中项在当前页里的序号，用于预览标题；换组后页内容变化，不能靠 page.index 反查
    picked_index: int = 0
    describe: Optional[dict] = None
    request_body: Optional[dict] = None
    exec_env: str = ""
    # 用户分多轮给的参数要累积保存：只说环境、参数不全时不能把已收到的参数丢掉
    saved_kv: dict = field(default_factory=dict)
    updated_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.updated_at = time.monotonic()


def _render_invoke_result(item: dict, env: str, raw: str, is_error: bool) -> str:
    """渲染 invoke_rpc 结果。

    网关把业务返回再包了一层：外层是调用元信息，``result`` 往往还是 JSON 字符串，
    里面的 ``content`` 又是 JSON。直接贴原文群里只能看到一堆反斜杠转义，
    这里逐层解开后按缩进展示，只保留业务数据本身。
    """
    header = "调用失败" if is_error else "调用完成"
    lines = [f"**{header}：`{item.get('method')}`**（env=`{env}`）"]
    try:
        outer = json.loads(raw)
    except (TypeError, ValueError):
        return f"{lines[0]}\n\n```text\n{raw}\n```"
    if not isinstance(outer, dict):
        return f"{lines[0]}\n\n```json\n{json.dumps(outer, ensure_ascii=False, indent=2)}\n```"

    if outer.get("ok") is False or is_error:
        lines.append(f"- ok：`{outer.get('ok')}`")
        error = outer.get("error") or outer.get("errorMsg")
        if error:
            lines.append(f"- error：`{error}`")

    payload = outer.get("result")
    # result 是 JSON 字符串时先解开，再尝试解开其中的 content
    for _ in range(2):
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
                continue
            except (TypeError, ValueError):
                break
        break
    if isinstance(payload, dict) and isinstance(payload.get("content"), str):
        try:
            payload["content"] = json.loads(payload["content"])
        except (TypeError, ValueError):
            pass

    if payload is None:
        return "\n".join(lines)
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload, ensure_ascii=False, indent=2))
    lines.append("```")
    return "\n".join(lines)


class SearchFlow:
    """管理「检索 → 选第 N 条 → 补参数/环境 → 预览 → 确认」的会话状态。"""

    def __init__(self, settings: Settings, client_provider) -> None:
        self._settings = settings
        self._client_provider = client_provider
        self._sessions: dict[str, _Session] = {}

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    async def begin_search(self, query: str, env: str, message, env_explicit: bool) -> str:
        """开始一轮新检索，覆盖该会话的旧状态。

        参数:
            query: 用户原话或 /search 的关键词。
            env: 本次检索环境（仅影响 search_api，不等同于执行环境）。
            env_explicit: 用户是否在这条消息里显式写过环境。

        返回: 第 1 组候选的 markdown 文本。
        """
        conv = _conv_of(message)
        client: McpStdioClient = await self._client_provider()
        raw, is_error = await client.call_tool(
            "search_api", {"env": env, "query": query, "limit": POOL_LIMIT}
        )
        if is_error:
            self._sessions.pop(conv, None)
            return f"**检索失败**（env=`{env}`）\n\n```text\n{raw}\n```"

        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            self._sessions.pop(conv, None)
            return f"**检索：{query}**（env=`{env}`）\n\n```text\n{raw}\n```"
        if isinstance(data, dict) and data.get("ok") is False:
            self._sessions.pop(conv, None)
            return f"检索失败：{data.get('error') or data}"

        results = (data.get("results") if isinstance(data, dict) else None) or []
        session = _Session(query=query, search_env=env, env_explicit=env_explicit, pool=list(results))
        self._sessions[conv] = session
        logger.info(
            "新检索 conv=%s query=%r env=%s 候选=%d", conv, query, env, len(results)
        )
        return self._fill_page(session)

    @staticmethod
    def is_confirm_word(text: str) -> bool:
        """判断是否为确认类话术，供机器人区分「确认」和普通检索词。"""
        return (text or "").strip().lower() in _CONFIRM_WORDS

    def has_session(self, message) -> bool:
        """当前会话是否还有未结束的检索流程。"""
        session = self._sessions.get(_conv_of(message))
        return (
            session is not None
            and time.monotonic() - session.updated_at <= SESSION_TTL_SECONDS
        )

    def wants_confirm(self, text: str, message) -> bool:
        """预览阶段的「确认」是否必须交给检索流程处理。

        ops 快捷操作也用「确认」执行写操作，若群里同时留着上一次 ops 的待定计划，
        用户本意是确认请求体预览，却会误触发写操作。故检索流程处于 preview 时
        由 bot 在 ops 之前优先拦截确认词。
        """
        session = self._sessions.get(_conv_of(message))
        return (
            session is not None
            and session.stage == "preview"
            and (text or "").strip().lower() in _CONFIRM_WORDS
            and time.monotonic() - session.updated_at <= SESSION_TTL_SECONDS
        )

    async def maybe_handle(
        self, text: str, message, allow_params: bool = True
    ) -> Optional[str]:
        """处理会话内的多轮指令；不是本流程的消息返回 None，交给主路由。

        参数:
            allow_params: 是否允许把含 ``k=v`` 的消息当作补参数。ops 命中但参数不全时
                主路由会带回退提示，那类消息应交给 ops 回退检索，不能被本流程截走。

        返回 None 表示「当普通检索处理」；返回字符串表示已处理完，直接回帖。
        """
        conv = _conv_of(message)
        session = self._sessions.get(conv)
        if session is None:
            return None
        if time.monotonic() - session.updated_at > SESSION_TTL_SECONDS:
            self._sessions.pop(conv, None)
            logger.info("检索会话过期 conv=%s", conv)
            return None

        stripped = (text or "").strip()
        lowered = stripped.lower()

        if lowered in _CANCEL_WORDS:
            self._sessions.pop(conv, None)
            return "已取消本次检索流程，未执行任何调用。"

        if lowered in _NEXT_WORDS:
            session.touch()
            return await self._next_group(conv, session)

        # 「换一组」之后是明确的选条阶段；补参数阶段不能再把纯数字当序号，
        # 否则「599602175」会被理解成「第 599602175 条」。
        if session.stage == "list":
            pick = _parse_pick(stripped)
            if pick is not None and session.page:
                session.touch()
                return await self._pick(conv, session, pick)

        # 确认词只要会话还在就接管：参数没补齐时给出引导，不能掉回检索把状态冲掉
        if lowered in _CONFIRM_WORDS:
            session.touch()
            if session.stage == "preview":
                return await self._confirm(conv, session)
            return "当前还没有可确认的请求体。请先回复 **第N条**，再补齐参数和环境。"

        # 补参数阶段：含 k=v、只补环境、或纯数字位置参数都留在会话里累计
        if allow_params and session.stage in {"params", "preview"} and _looks_like_params(stripped):
            session.touch()
            return await self._preview(conv, session, stripped)

        # 其余文本（含新的业务关键词）交给主路由重新检索，旧会话会被 begin_search 覆盖
        return None

    # ------------------------------------------------------------------ #
    # 分页与换组
    # ------------------------------------------------------------------ #

    def _fill_page(self, session: _Session) -> str:
        """从候选池里取出下一组未展示过的候选作为当前页。"""
        page_size = max(1, self._settings.search_limit)
        available = [it for it in session.pool if _item_key(it) not in session.shown]
        page = available[:page_size]
        if not page:
            return (
                f"**检索：{session.query}**　env=`{session.search_env}`\n\n"
                "没有更多候选了。换个更精确的关键词（接口名 / 方法名 / 字段名）再试一次，"
                "或用 `/call describe_method {...}` 直接看某个方法的签名。"
            )
        session.page = page
        session.shown.update(_item_key(it) for it in page)
        session.stage = "list"
        session.picked = None
        session.picked_index = 0
        session.describe = None
        session.request_body = None
        session.saved_kv = {}
        return self._render_page(session)

    async def _next_group(self, conv: str, session: _Session) -> str:
        """换一组候选：先用池内剩余；池用尽则改写 query 重新做一次向量检索。

        网关没有 offset/exclude，所以用后缀轮转改写 query 触发新一轮召回，
        再按 _item_key 去重，确保换出来的是没展示过的方法。
        """
        page_size = max(1, self._settings.search_limit)
        remaining = [it for it in session.pool if _item_key(it) not in session.shown]
        client: McpStdioClient = await self._client_provider()

        session.refetched = len(remaining) < page_size and session.variant_index < len(QUERY_SUFFIXES)
        while len(remaining) < page_size and session.variant_index < len(QUERY_SUFFIXES):
            variant = session.query + QUERY_SUFFIXES[session.variant_index]
            session.variant_index += 1
            raw, is_error = await client.call_tool(
                "search_api",
                {"env": session.search_env, "query": variant, "limit": POOL_LIMIT},
            )
            if is_error:
                logger.warning("换组检索失败 conv=%s query=%r error=%s", conv, variant, raw[:200])
                break
            try:
                results = (json.loads(raw).get("results") or [])
            except (TypeError, ValueError):
                logger.warning("换组检索返回无法解析 conv=%s query=%r", conv, variant)
                break
            fresh = [it for it in results if _item_key(it) not in session.shown]
            session.pool.extend(fresh)
            remaining = [it for it in session.pool if _item_key(it) not in session.shown]
            logger.info(
                "换组重检索 conv=%s query=%r 新增候选=%d 剩余=%d",
                conv, variant, len(fresh), len(remaining),
            )

        session.group_no += 1
        return self._fill_page(session)

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #

    def _render_page(self, session: _Session) -> str:
        """渲染候选页；末尾给出「选第 N 条」「换一组」的操作提示。"""
        source = "重新向量检索" if session.refetched else "本轮检索候选"
        lines = [
            f"**检索：{session.query}**　env=`{session.search_env}`"
            f"　第 {session.group_no} 组（本组 {len(session.page)} 条，来源：{source}）"
        ]
        for index, item in enumerate(session.page, 1):
            params = (item.get("param_fields") or "").strip() or "-"
            # 一行一个接口：只保留定位与参数，群聊里更易扫读
            lines.append(
                f"{index}. `{_short_iface(item.get('iface'))}#{item.get('method')}`"
                f"　artifact=`{item.get('artifact_id')}`"
                f"　参数：{params}"
            )
        lines.append(
            "\n回复 **第N条** 选定方法；没想要的就回复 **换一组**；回复 **取消** 结束。"
        )
        return "\n".join(lines)

    @staticmethod
    def _render_pick(index: int, item: dict, describe: dict, env_explicit: bool, search_env: str) -> str:
        """选中后提示用户补请求参数与环境，附参数类型与可直接改的示例。"""
        params = describe.get("param_fields") or []
        if isinstance(params, str):
            params = [p.strip() for p in params.split(",") if p.strip()]
        types = describe.get("param_types") or {}
        required = ", ".join(f"`{p}`({types.get(p, '?')})" for p in params) or "（该方法无入参）"
        example = " ".join(f"{p}=..." for p in params).strip()
        example = f"{example} env=test".strip()
        env_hint = (
            f"（当前检索环境 `{search_env}`，不写则沿用）" if env_explicit
            else "（**环境未指定**，请写 `env=product` 或 `env=test`）"
        )
        group = describe.get("group") or "-"
        scope = describe.get("scope") or "-"
        lines = [
            f"**已选择第 {index} 条：`{_short_iface(item.get('iface'))}#{item.get('method')}`**"
            f"　[{item.get('danger_level') or '-'}]",
            f"- artifact：`{item.get('artifact_id')}`　group：`{group}` / `{scope}`",
            f"- 需要参数：{required}",
            "",
            f"请回复 **请求参数与环境** {env_hint}，例如：",
            f"`{example}`",
            "环境也可说「线上 / 生产」「测试 / 预发」。",
            "回复 **换一组** 重新挑方法，回复 **取消** 放弃。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _render_preview(index: int, item: dict, describe: dict, body: dict, env: str) -> str:
        """请求体预览；read 之外的方法明确提示需走 IDE 人工确认。"""
        danger = item.get("danger_level") or "-"
        payload = {
            "env": env,
            "application": item.get("application"),
            "artifact_id": item.get("artifact_id"),
            "iface": item.get("iface"),
            "method": item.get("method"),
            "group": describe.get("group"),
            "scope": describe.get("scope"),
            "req_param": body,
        }
        lines = [
            f"**请求体预览：第 {index} 条 `{item.get('method')}`**"
            f"　env=`{env}`　[{danger}]",
            "```json",
            json.dumps(payload, ensure_ascii=False, indent=2),
            "```",
        ]
        if danger in {"write", "destructive"}:
            lines.append(
                "该方法为写操作，机器人不执行。请把上面的请求体带到 IDE 里走 "
                "`describe_method → invoke_rpc` 的人工确认流程。"
            )
            lines.append("回复 **取消** 结束，或回复 **换一组** 重新挑方法。")
        else:
            lines.append(
                "核对后回复 **确认** 调用；回复 **取消** 放弃；改参数直接重新发 `k=v`。"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 选条 / 参数 / 确认
    # ------------------------------------------------------------------ #

    async def _pick(self, conv: str, session: _Session, index: int) -> str:
        """选定当前页第 N 条，取 describe_method 详情后提示补参数和环境。"""
        if not 1 <= index <= len(session.page):
            return f"当前组只有 {len(session.page)} 条，请回复 1~{len(session.page)} 的序号。"
        item = session.page[index - 1]
        client: McpStdioClient = await self._client_provider()
        raw, is_error = await client.call_tool(
            "describe_method",
            {
                "env": session.search_env,
                "artifact_id": item.get("artifact_id", ""),
                "iface": item.get("iface", ""),
                "method": item.get("method", ""),
            },
        )
        if is_error:
            return f"**取方法详情失败**\n\n```text\n{raw}\n```"
        try:
            describe = json.loads(raw)
        except (TypeError, ValueError):
            return f"**方法详情解析失败**\n\n```text\n{raw}\n```"
        if describe.get("ok") is False:
            return f"取方法详情失败：{describe.get('error') or describe}"

        session.picked = item
        session.picked_index = index
        session.describe = describe
        session.stage = "params"
        # 换了一条方法就重置参数，避免把上一条的参数带进来
        session.saved_kv = {}
        logger.info(
            "选定候选 conv=%s index=%d iface=%s method=%s",
            conv, index, item.get("iface"), item.get("method"),
        )
        return self._render_pick(
            index, item, describe, session.env_explicit, session.search_env
        )

    async def _preview(self, conv: str, session: _Session, text: str) -> str:
        """解析 k=v / 位置参数与显式环境，套用 request_param_template 生成请求体。

        参数分多轮给也要能累积：先合并历史 ``saved_kv``，再解析本条消息。
        缺环境时先保存参数再提示，否则用户补一句「线上」，前面的参数会丢。
        """
        if session.picked is None or session.describe is None:
            return "请先回复 **第N条** 选定方法。"

        work = _strip_env_words(text)
        kv = dict(session.saved_kv)
        for match in _KV_RE.finditer(work):
            kv[match.group(1)] = match.group(2)

        # 位置参数：用户常写「599602175， operId=11932」，孤立数字按字段顺序补
        # 只填数字类型的未填字段，避免把 uid 塞进 bool/struct 参数里。
        params = session.describe.get("param_fields") or []
        if isinstance(params, str):
            params = [p.strip() for p in params.split(",") if p.strip()]
        types = session.describe.get("param_types") or {}

        positional = _POSITIONAL_NUM_RE.findall(_KV_RE.sub(" ", work))
        if positional:
            numeric_fields = [
                p for p in params
                if p not in kv and _is_numeric_type(types.get(p))
            ]
            for field, value in zip(numeric_fields, positional):
                kv[field] = value

        session.saved_kv = kv

        env = _parse_env(text)
        if env is None:
            env = session.search_env if session.env_explicit else ""
        if not env:
            received = json.dumps(kv, ensure_ascii=False) if kv else "（无）"
            return (
                f"还缺**环境**。已收到参数 {received}，请再补一句环境即可，例如：\n"
                "`env=product` / `env=test`，或直接说「线上」「测试」。"
            )

        try:
            template = json.loads(session.describe.get("request_param_template") or "{}")
        except (TypeError, ValueError):
            template = {}
        if not isinstance(template, dict):
            template = {}

        # bool 参数在模板里有默认值（通常是 false），不该强迫运营每次手写
        missing = [
            k for k in template
            if k not in kv and not _is_bool_type(types.get(k))
        ]
        if missing:
            return f"还缺参数：`{'、'.join(missing)}`。请补全后重新发一次。"

        body = dict(template)
        for key, value in kv.items():
            if key in body:
                try:
                    body[key] = _coerce(value, body[key])
                except ValueError:
                    return f"参数 `{key}` 需要数值，收到 `{value}`。"
            else:
                body[key] = value

        session.request_body = body
        session.exec_env = env
        session.stage = "preview"
        logger.info(
            "生成请求体预览 conv=%s method=%s env=%s 参数=%s",
            conv, session.picked.get("method"), env, sorted(kv),
        )
        return self._render_preview(
            session.picked_index, session.picked, session.describe, body, env
        )

    async def _confirm(self, conv: str, session: _Session) -> str:
        """预览确认后执行只读调用；写操作与未放行 invoke_rpc 时明确拒绝。"""
        item = session.picked or {}
        danger = item.get("danger_level") or "-"
        if danger in {"write", "destructive"}:
            return (
                f"`{item.get('method')}` 是 {danger} 操作，机器人不执行。"
                "请走 IDE 的 `describe_method → invoke_rpc` 人工确认流程。"
            )
        if "invoke_rpc" not in self._settings.allowed_tools:
            return (
                "当前机器人未放行 `invoke_rpc`（`.env` 的 `ALLOW_INVOKE_RPC=false`），"
                "无法直接调用。\n可在 IDE 里执行，或管理员放行后重启机器人。\n"
                f"请求体：\n```json\n{json.dumps(session.request_body, ensure_ascii=False, indent=2)}\n```"
            )

        client: McpStdioClient = await self._client_provider()
        describe = session.describe or {}
        arguments = {
            "env": session.exec_env,
            "application": item.get("application", "") or "",
            "artifact_id": item.get("artifact_id", "") or "",
            "iface": item.get("iface", "") or "",
            "method": item.get("method", "") or "",
            # 网关工具层把 req_param 声明为 JSON 字符串，传 dict 会被 pydantic 拒绝
            "req_param": json.dumps(session.request_body or {}, ensure_ascii=False),
            "user_confirmed": True,
            # 明确带上路由，避免同名方法在多个 group 下被网关判为歧义
            "group": describe.get("group") or "",
            "scope": describe.get("scope") or "",
        }
        if describe.get("confirm_token"):
            arguments["confirm_token"] = describe["confirm_token"]
        raw, is_error = await client.call_tool("invoke_rpc", arguments)
        self._sessions.pop(conv, None)
        logger.info(
            "确认调用 conv=%s method=%s env=%s isError=%s",
            conv, item.get("method"), session.exec_env, is_error,
        )
        return _render_invoke_result(item, session.exec_env, raw, is_error)
