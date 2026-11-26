import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from bot import GatewayBotHandler
from ops_actions import OpsDispatcher, OpsRouter


class OpsSemanticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        rows = [
            dict(action="change-mobile", desc="修改绑定手机号", aliases=["换绑"], require=["uid", "mobile"]),
            dict(action="query-user", desc="查询用户资料", aliases=["查资料"], require=["uid"]),
        ]
        self.dispatcher = OpsDispatcher("python", "dispatch.py")
        self.dispatcher._run = Mock(return_value=(json.dumps(rows), "", 0))
        self.dispatcher.load_aliases()
        self.dispatcher.run = AsyncMock(side_effect=AssertionError("semantic matches must never call dispatch"))
        self.handler = GatewayBotHandler(
            SimpleNamespace(default_env="product"),
            Mock(),
            OpsRouter(self.dispatcher),
            SimpleNamespace(
                wants_confirm=Mock(return_value=False),
                maybe_handle=AsyncMock(return_value=None),
                is_confirm_word=Mock(return_value=False),
            ),
        )
        self.handler._search = AsyncMock(return_value="global results")
        self.message = SimpleNamespace(conversation_id="group")

    @staticmethod
    def embedding_response(vectors):
        response = io.BytesIO(json.dumps({"embeddings": vectors}).encode())
        response.status = 200
        return response

    @patch("http.client.HTTPConnection")
    async def test_semantic_result_suggests_ops_and_reuses_index(self, connection):
        client = connection.return_value
        client.getresponse.side_effect = [
            self.embedding_response([[1, 0], [0, 1], [1, 0]]),
            self.embedding_response([[1, 0]]),
        ]
        for _ in range(2):
            reply = await self.handler._dispatch("把绑定的号码换掉 uid=123 env=test", self.message)
            self.assertIn("/ops change-mobile uid=... mobile=... --env test", reply)
        connection.assert_called_with("127.0.0.1", 11434, timeout=8)
        first, second = [json.loads(call.args[2]) for call in client.request.call_args_list]
        self.assertEqual(first["model"], "bge-m3")
        self.assertEqual(first["input"], ["修改绑定手机号", "查询用户资料", "把绑定的号码换掉"])
        self.assertEqual(second["input"], ["把绑定的号码换掉"])
        self.handler._search.assert_not_awaited()
        self.dispatcher.run.assert_not_awaited()

    @patch("http.client.HTTPConnection")
    async def test_uncertain_match_uses_global_search(self, connection):
        connection.return_value.getresponse.side_effect = [
            self.embedding_response([[1, 0], [1, 0.1], [1, 0]]),
            self.embedding_response([[0, 1]]),
        ]
        self.assertEqual(await self.handler._dispatch("这个怎么操作", self.message), "global results")
        self.assertEqual(await self.handler._dispatch("这是什么", self.message), "global results")

    @patch("http.client.HTTPConnection")
    async def test_offline_ollama_uses_global_search(self, connection):
        connection.return_value.request.side_effect = ConnectionRefusedError
        self.assertEqual(await self.handler._dispatch("这是什么", self.message), "global results")
        connection.return_value.request.assert_called_once()

    @patch("http.client.HTTPConnection")
    async def test_existing_alias_and_explicit_search_skip_ollama(self, connection):
        self.assertIn("缺少参数", await self.handler._dispatch("换绑", self.message))
        self.assertEqual(await self.handler._dispatch("/search 接口", self.message), "global results")
        connection.assert_not_called()

    @patch("http.client.HTTPConnection")
    async def test_existing_write_waits_for_confirmation(self, connection):
        plan = dict(
            action="change-mobile", env="test", risk="write", confirm_token="test-token",
            variables={"uid": "123", "mobile": "13000000000"}, target="stub", request_body={},
        )
        self.dispatcher.run.side_effect = [(json.dumps(plan), "", 0), ("completed", "", 0)]
        await self.handler._dispatch("换绑 uid=123 mobile=13000000000 env=test", self.message)
        self.assertEqual(self.dispatcher.run.await_args.args[0][0], "plan")
        self.dispatcher.run.assert_awaited_once()

        await self.handler._dispatch("确认", self.message)
        self.assertEqual(self.dispatcher.run.await_args.args[0][0], "run")
        connection.assert_not_called()
