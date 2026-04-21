from __future__ import annotations

import json
import sys
import unittest
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
        self.assertEqual(
            payload["turn_steer_required"],
            ["expectedTurnId", "input", "threadId"],
        )

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


if __name__ == "__main__":
    unittest.main()
