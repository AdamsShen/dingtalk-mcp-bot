"""不依赖钉钉凭据的自检：直接拉起 mcp-gateway 走一遍 MCP 握手。

    python smoke_test.py                       # 握手 + 列工具
    python smoke_test.py --search 修改用户昵称   # 再跑一次 search_api
    python smoke_test.py --env test --search uid
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from config import load_settings
from mcp_client import McpStdioClient


async def run(query: str, env: str, limit: int) -> int:
    settings = load_settings()
    client = McpStdioClient(
        command=settings.gateway_bin,
        args=settings.gateway_args(),
        env=settings.gateway_env(),
        call_timeout=settings.call_timeout_seconds,
    )
    await client.start()
    try:
        tools = await client.list_tools()
        print(f"[1/2] 握手成功：{client.server_info}")
        print(f"      工具 {len(tools)} 个：")
        for tool in tools:
            desc = (tool.get("description") or "").strip().splitlines()
            print(f"        - {tool.get('name')}：{desc[0] if desc else ''}")

        if query:
            print(f"\n[2/2] search_api(env={env}, query={query!r}, limit={limit})")
            raw, is_error = await client.call_tool(
                "search_api", {"env": env, "query": query, "limit": limit}
            )
            print("      isError =", is_error)
            try:
                data = json.loads(raw)
            except ValueError:
                print(raw[:2000])
            else:
                print(f"      检索模式 = {data.get('retrieval_mode')}，命中 {len(data.get('results') or [])} 条")
                for index, item in enumerate((data.get("results") or [])[:limit], 1):
                    print(
                        f"        {index}. {item.get('iface')}#{item.get('method')}"
                        f"  [{item.get('danger_level')}]"
                    )
    finally:
        await client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="mcp-gateway 连通性自检")
    parser.add_argument("--search", default="", help="额外执行一次 search_api 的关键词")
    parser.add_argument("--env", default="test", choices=["test", "product"], help="search 用的环境")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    return asyncio.run(run(args.search, args.env, args.limit))


if __name__ == "__main__":
    raise SystemExit(main())
