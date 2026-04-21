from __future__ import annotations

import asyncio
import json
import tempfile
import sys
import time
import unittest
from unittest import mock
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot


class ProtocolSnapshotTests(unittest.TestCase):
    def test_snapshot_contains_required_methods(self) -> None:
        snapshot_path = ROOT / "schema" / "app-server-0.122.0.snapshot.json"
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertIn("turn/steer", payload["request_methods"])
        self.assertIn("initialize", payload["request_methods"])
        self.assertIn("initialized", payload["notification_methods"])
        self.assertIn("model/rerouted", payload["server_notification_methods"])
        self.assertIn("item/tool/call", payload["server_request_methods"])
        self.assertIn("item/commandExecution/requestApproval", payload["server_request_methods"])
        self.assertIn("item/fileChange/requestApproval", payload["server_request_methods"])
        self.assertEqual(
            payload["turn_steer_required"],
            ["expectedTurnId", "input", "threadId"],
        )

    def test_build_structured_input_parses_skill_and_mention(self) -> None:
        text, items = bot.build_structured_input(
            prompt=(
                "[$talk-normal](C:/Users/admin/.codex/skills/talk-normal/SKILL.md) "
                "[GitHub](app://github) 修复这个问题"
            )
        )
        self.assertEqual(text, "修复这个问题")
        self.assertEqual(items[0]["type"], "text")
        self.assertEqual(items[1]["type"], "skill")
        self.assertEqual(items[2]["type"], "mention")

    def test_build_structured_input_promotes_local_images(self) -> None:
        with tempfile.TemporaryDirectory(prefix="input-items-") as temp_dir_raw:
            image_path = Path(temp_dir_raw) / "shot.png"
            image_path.write_bytes(b"img")
            text, items = bot.build_structured_input(
                prompt="看图",
                attachments=[("图片", image_path, None)],
            )
        self.assertIn("看图", text)
        self.assertIn("附件", text)
        self.assertIn(str(image_path.resolve()), text)
        self.assertEqual(items[1]["type"], "localImage")

    def test_render_run_status_has_task_card_sections(self) -> None:
        turn = bot.ActiveTurn(
            chat_id=1,
            repo_key="default",
            repo_path=ROOT,
            thread_id="thread-1",
            turn_id="turn-1",
            prompt="修复 Telegram edit queue",
            stage="执行命令",
            running_command="pytest -q",
        )
        bot.append_unique(turn.modified_files, "bot.py")
        bot.append_unique(turn.test_commands, "pytest -q")
        text = bot.render_run_status(turn, "new", state="running")
        self.assertIn("目标", text)
        self.assertIn("当前命令", text)
        self.assertIn("已修改文件", text)
        self.assertIn("验证", text)

    def test_format_tool_event_for_steer(self) -> None:
        text = bot.format_tool_event(
            {"kind": "steer", "prompt": "继续看刚才的报错"},
            "new",
        )
        self.assertIn("已追加指令", text)
        self.assertIn("继续看刚才的报错", text)

    def test_split_message_preserves_code_fence_boundaries(self) -> None:
        text = "前言\n```python\n" + "\n".join(f"print({i})" for i in range(300)) + "\n```\n结尾"
        chunks = bot.split_message(text, limit=500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.count("```") % 2 == 0 for chunk in chunks[:-1]))

    def test_env_example_uses_safe_public_defaults(self) -> None:
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("CODEX_APPROVAL=on-request", env_example)
        self.assertIn("CODEX_SANDBOX=workspace-write", env_example)
        self.assertIn("CODEX_DEFAULT_SANDBOX=workspace-write", env_example)
        self.assertIn("ALLOW_ALL_CHATS=false", env_example)


class ApprovalManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="approval-tests-")
        self.store = bot.SessionStore(Path(self.temp_dir.name) / "sessions.json")
        self.settings = bot.Settings(
            telegram_bot_token="x",
            allowed_chat_ids={1},
            allow_all_chats=False,
            repos={"default": ROOT},
            state_dir=Path(self.temp_dir.name),
            codex_command=["codex"],
            codex_approval="on-request",
            codex_sandbox="workspace-write",
            codex_default_sandbox="workspace-write",
            full_access_ttl_seconds=1800,
            codex_model="gpt-5.4",
            codex_reasoning_effort="medium",
            max_prompt_chars=12000,
            stream_edit_interval=0.8,
            max_message_chars=3800,
            auto_archive_on_new=True,
        )
        self.conversations = SimpleNamespace(active_by_turn={})
        self.manager = bot.ApprovalManager(self.settings, self.store, self.conversations)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_permissions_session_grants_requested_profile(self) -> None:
        loop = asyncio.get_running_loop()
        pending = bot.PendingApproval(
            token="t1",
            request_id=1,
            method="item/permissions/requestApproval",
            chat_id=1,
            thread_id="thread",
            turn_id="turn",
            item_id="item",
            prompt_text="权限审批",
            result_future=loop.create_future(),
            requested_permissions={"network": {"enabled": True}},
        )
        self.manager.pending[pending.token] = pending
        _, result = self.manager.resolve_callback("t1", "session")
        self.assertEqual(result, {"permissions": {"network": {"enabled": True}}, "scope": "session"})

    async def test_user_input_pick_returns_answer_payload(self) -> None:
        loop = asyncio.get_running_loop()
        pending = bot.PendingApproval(
            token="t2",
            request_id=2,
            method="item/tool/requestUserInput",
            chat_id=1,
            thread_id="thread",
            turn_id="turn",
            item_id="item",
            prompt_text="需要补充输入",
            result_future=loop.create_future(),
            user_input_questions=[
                {
                    "id": "q1",
                    "question": "选择一个模型",
                    "options": [
                        {"label": "GPT-5.4", "description": "默认"},
                        {"label": "GPT-5.4-Mini", "description": "更快"},
                    ],
                }
            ],
        )
        self.manager.pending[pending.token] = pending
        _, result = self.manager.resolve_callback("t2", "pick", ["0", "1"])
        self.assertEqual(result, {"answers": {"q1": {"answers": ["GPT-5.4-Mini"]}}})

    async def test_mcp_elicitation_decline_returns_decline_payload(self) -> None:
        loop = asyncio.get_running_loop()
        pending = bot.PendingApproval(
            token="t3",
            request_id=3,
            method="mcpServer/elicitation/request",
            chat_id=1,
            thread_id="thread",
            turn_id="turn",
            item_id="item",
            prompt_text="MCP 交互请求",
            result_future=loop.create_future(),
        )
        self.manager.pending[pending.token] = pending
        _, result = self.manager.resolve_callback("t3", "decline")
        self.assertEqual(result, {"action": "decline", "content": None})

    async def test_dynamic_tool_echo_returns_content_items(self) -> None:
        loop = asyncio.get_running_loop()
        pending = bot.PendingApproval(
            token="t4",
            request_id=4,
            method="item/tool/call",
            chat_id=1,
            thread_id="thread",
            turn_id="turn",
            item_id="item",
            prompt_text="动态工具调用",
            result_future=loop.create_future(),
            dynamic_tool_name="demo-tool",
            dynamic_tool_arguments={"hello": "world"},
        )
        self.manager.pending[pending.token] = pending
        _, result = self.manager.resolve_callback("t4", "echo")
        self.assertTrue(result["success"])
        self.assertEqual(result["contentItems"][0]["type"], "inputText")

    async def test_dynamic_tool_echo_promotes_image_items(self) -> None:
        with tempfile.TemporaryDirectory(prefix="dynamic-tool-image-") as temp_dir_raw:
            image_path = Path(temp_dir_raw) / "shot.png"
            image_path.write_bytes(b"img")
            result = bot.build_dynamic_tool_result(
                "demo-tool",
                {"previewImage": str(image_path.resolve())},
                success=True,
                mode="echo",
            )
        item_types = [item["type"] for item in result["contentItems"]]
        self.assertIn("inputText", item_types)
        self.assertIn("inputImage", item_types)

    async def test_dynamic_tool_echo_accepts_extensionless_image_url_when_key_hints_image(self) -> None:
        result = bot.build_dynamic_tool_result(
            "demo-tool",
            {"imageUrl": "https://example.com/render?id=42"},
            success=True,
            mode="echo",
        )
        item_types = [item["type"] for item in result["contentItems"]]
        self.assertIn("inputImage", item_types)

    async def test_dynamic_tool_echo_accepts_binary_image_envelope(self) -> None:
        result = bot.build_dynamic_tool_result(
            "demo-tool",
            {"preview": {"mimeType": "image/png", "base64": "aGVsbG8="}},
            success=True,
            mode="echo",
        )
        image_items = [item for item in result["contentItems"] if item["type"] == "inputImage"]
        self.assertTrue(image_items)
        self.assertTrue(image_items[0]["imageUrl"].startswith("data:image/png;base64,"))

    def test_approval_keyboard_callback_data_stays_within_telegram_limit(self) -> None:
        keyboard = bot.build_approval_keyboard("1234567890abcdef", include_session=True)
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertTrue(callback_data)
        self.assertTrue(all(len(item) <= 64 for item in callback_data))

    def test_user_input_keyboard_callback_data_stays_within_telegram_limit(self) -> None:
        keyboard = bot.build_user_input_keyboard(
            "1234567890abcdef",
            [
                {
                    "id": "q1",
                    "question": "选择一个模型",
                    "options": [
                        {"label": "GPT-5.4", "description": "默认"},
                        {"label": "GPT-5.4-Mini", "description": "更快"},
                    ],
                }
            ],
        )
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertTrue(callback_data)
        self.assertTrue(all(len(item) <= 64 for item in callback_data))

    def test_dynamic_tool_keyboard_callback_data_stays_within_telegram_limit(self) -> None:
        keyboard = bot.build_dynamic_tool_keyboard("1234567890abcdef")
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
            if button.callback_data
        ]
        self.assertTrue(callback_data)
        self.assertTrue(all(len(item) <= 64 for item in callback_data))

    async def test_runtime_cache_short_circuits_thread_list_fallback(self) -> None:
        self.store.set_thread_id(1, "thread-1")
        self.store.set_runtime_snapshot(1, thread_id="thread-1", model="gpt-5.4", effort="medium")

        class FailingClient:
            async def list_threads(self, *args, **kwargs):
                raise AssertionError("resolve_effective_runtime_info should not call list_threads")

        model, effort = await bot.resolve_effective_runtime_info(
            self.settings,
            self.store,
            FailingClient(),
            1,
            "default",
        )
        self.assertEqual((model, effort), ("gpt-5.4", "medium"))

    async def test_restore_stale_pending_request_from_store(self) -> None:
        self.store.save_pending_request(
            {
                "token": "restore-me",
                "request_id": 9,
                "method": "item/commandExecution/requestApproval",
                "chat_id": 1,
                "thread_id": "thread",
                "turn_id": "turn",
                "item_id": "item",
                "prompt_text": "命令审批",
                "created_at": time.time(),
            }
        )
        restored = bot.ApprovalManager(self.settings, self.store, self.conversations)
        pending, result = restored.resolve_callback("restore-me", "once")
        self.assertIsNotNone(pending)
        self.assertTrue(pending.stale)
        self.assertEqual(result, {"decision": "accept"})

    async def test_monitor_bridge_health_flushes_pending_reconnect_notice(self) -> None:
        fake_bot = SimpleNamespace()
        sent_messages: list[str] = []

        async def send_message(*, chat_id: int, text: str):
            sent_messages.append(text)

        fake_bot.send_message = send_message
        health = bot.PollingHealth(
            last_disconnect_at=time.time() - 12,
            last_ok_at=time.time() - 1,
            reconnect_notice_pending=True,
        )
        application = SimpleNamespace(
            bot=fake_bot,
            bot_data={
                "settings": self.settings,
                "store": self.store,
                "health": health,
            },
        )
        await bot.monitor_bridge_health(SimpleNamespace(application=application))
        self.assertTrue(any("Telegram 连接已恢复" in text for text in sent_messages))
        self.assertFalse(health.reconnect_notice_pending)

    async def test_install_polling_probe_marks_reconnect_on_get_updates_success(self) -> None:
        class FakePollingBot:
            async def get_updates(self, *args, **kwargs):
                return []

            async def send_message(self, *, chat_id: int, text: str):
                sent_messages.append(text)

        sent_messages: list[str] = []
        fake_bot = FakePollingBot()
        health = bot.PollingHealth(
            last_disconnect_at=time.time() - 8,
            reconnect_notice_pending=True,
        )
        application = SimpleNamespace(
            bot=fake_bot,
            bot_data={
                "settings": self.settings,
                "store": self.store,
                "health": health,
            },
        )
        bot.install_polling_probe(fake_bot, application)
        await fake_bot.get_updates()
        await asyncio.sleep(0)
        self.assertTrue(any("Telegram 连接已恢复" in text for text in sent_messages))
        self.assertFalse(health.reconnect_notice_pending)


class ConversationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="conversation-tests-")
        self.store = bot.SessionStore(Path(self.temp_dir.name) / "sessions.json")
        self.settings = bot.Settings(
            telegram_bot_token="x",
            allowed_chat_ids={1},
            allow_all_chats=False,
            repos={"default": ROOT},
            state_dir=Path(self.temp_dir.name),
            codex_command=["codex"],
            codex_approval="on-request",
            codex_sandbox="workspace-write",
            codex_default_sandbox="workspace-write",
            full_access_ttl_seconds=1800,
            codex_model="gpt-5.4",
            codex_reasoning_effort="medium",
            max_prompt_chars=12000,
            stream_edit_interval=0.8,
            max_message_chars=3800,
            auto_archive_on_new=True,
        )

        class FakeClient:
            def add_notification_handler(self, handler):
                self.handler = handler

        self.client = FakeClient()
        self.manager = bot.ConversationManager(self.settings, self.store, self.client)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_model_rerouted_updates_runtime_snapshot(self) -> None:
        self.store.set_thread_id(1, "thread-1")
        self.store.set_runtime_snapshot(1, thread_id="thread-1", model="gpt-5.4", effort="medium")
        turn = bot.ActiveTurn(
            chat_id=1,
            repo_key="default",
            repo_path=ROOT,
            thread_id="thread-1",
            turn_id="turn-1",
            prompt="测试 reroute",
        )
        self.manager.active_by_chat[1] = turn
        self.manager.active_by_turn["turn-1"] = turn

        await self.manager._handle_notification(
            {
                "method": "model/rerouted",
                "params": {
                    "threadId": "thread-1",
                    "turnId": "turn-1",
                    "fromModel": "gpt-5.4",
                    "toModel": "gpt-5.4-mini",
                    "reason": "highRiskCyberActivity",
                },
            }
        )

        self.assertEqual(
            self.store.get_runtime_snapshot(1),
            ("thread-1", "gpt-5.4-mini", "medium"),
        )
        tool_event = await turn.queue.get()
        status_event = await turn.queue.get()
        self.assertEqual(tool_event["kind"], "model_rerouted")
        self.assertEqual(status_event["type"], "status")


class LocalThreadIndexTests(unittest.TestCase):
    def test_list_local_threads_reuses_cached_index_for_same_signature(self) -> None:
        sample_entry = {
            "id": "thread-1",
            "cwd": str(ROOT.resolve()),
            "preview": "thread-1",
            "name": "thread-1",
            "path": "C:/tmp/thread-1.jsonl",
            "updated_at": "2026-04-21T00:00:00+00:00",
            "status": {},
        }
        with mock.patch.object(bot, "local_thread_index_signature", return_value=(1.0, 2.0, 3.0)):
            with mock.patch.object(
                bot,
                "build_local_thread_index",
                side_effect=[
                    bot.LocalThreadIndexState(
                        signature=(1.0, 2.0, 3.0),
                        entries=[sample_entry],
                        by_thread_id={"thread-1": sample_entry},
                        last_loaded_at=time.time(),
                    )
                ],
            ) as build_mock:
                bot.invalidate_local_thread_index_cache()
                first = bot.list_local_threads(ROOT, [], limit=20)
                second = bot.list_local_threads(ROOT, [], limit=20)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(build_mock.call_count, 1)

    def test_build_local_thread_index_prefers_persisted_cache_when_signature_matches(self) -> None:
        sample_entry = {
            "id": "thread-2",
            "cwd": str(ROOT.resolve()),
            "preview": "thread-2",
            "name": "thread-2",
            "path": "C:/tmp/thread-2.jsonl",
            "updated_at": "2026-04-21T00:00:00+00:00",
            "status": {},
        }
        cached_payload = {
            "version": bot.LOCAL_THREAD_INDEX_CACHE_VERSION,
            "signature": [1.0, 2, 3, 4.0, "a", 5, 6.0, "b"],
            "entries": [sample_entry],
        }
        with mock.patch.object(bot, "local_thread_index_signature", return_value=tuple(cached_payload["signature"])):
            with mock.patch.object(bot, "read_local_thread_index_cache", return_value=cached_payload):
                with mock.patch.object(bot, "load_session_index_map", side_effect=AssertionError("unexpected rebuild")):
                    state = bot.build_local_thread_index()
        self.assertEqual(state.entries[0]["id"], "thread-2")

    def test_warm_local_thread_index_cache_runs_background_refresh(self) -> None:
        called = []

        def fake_get_local_thread_index():
            called.append("ok")
            return bot.LocalThreadIndexState()

        async def runner():
            with mock.patch.object(bot, "get_local_thread_index", side_effect=fake_get_local_thread_index):
                await bot.warm_local_thread_index_cache()

        asyncio.run(runner())
        self.assertEqual(called, ["ok"])


if __name__ == "__main__":
    unittest.main()
