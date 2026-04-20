from __future__ import annotations

import atexit
import asyncio
import ctypes
import glob
import json
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.error import BadRequest, Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


LOCK_PATH = Path(__file__).with_name("bot.lock")
MUTEX_NAME = "Local\\TelegramCodexBridgeBot"
_mutex_handle: int | None = None
VALID_VERBOSE_LEVELS = ("off", "thinking", "new", "all", "verbose")
THREAD_CALLBACK_PREFIX = "thread"
STREAM_BUFFER_THRESHOLD = 40
SUMMARY_MODEL_TIMEOUT_SECONDS = 20


def parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            values.add(int(part))
    return values


def parse_repo_map(raw: str) -> dict[str, Path]:
    repos: dict[str, Path] = {}
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"REPOS 配置格式错误: {item}")
        name, path_str = item.split("=", 1)
        name = name.strip()
        path = Path(path_str.strip()).expanduser().resolve()
        if not name:
            raise ValueError(f"REPOS 名称为空: {item}")
        if not path.exists():
            raise ValueError(f"仓库路径不存在: {path}")
        repos[name] = path
    if not repos:
        raise ValueError("REPOS 不能为空")
    return repos


def default_repo_map() -> dict[str, Path]:
    cwd = Path.cwd().resolve()
    return {"default": cwd}


def parse_command(raw: str) -> list[str]:
    parts = shlex.split(raw, posix=os.name != "nt")
    if not parts:
        raise ValueError("CODEX_COMMAND 不能为空")
    return parts


def utc_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def split_message(text: str, limit: int = 3800) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    return chunks


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, SystemError, ValueError):
        return False
    return True


def acquire_single_instance_lock(lock_path: Path = LOCK_PATH) -> None:
    global _mutex_handle
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()

    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            raise RuntimeError("无法创建单实例互斥锁。")
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(handle)
            raise RuntimeError("bot 已在运行。")
        _mutex_handle = handle

    if lock_path.exists():
        try:
            old_pid = int(lock_path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            old_pid = 0
        if old_pid and old_pid != pid and process_exists(old_pid):
            raise RuntimeError(f"bot 已在运行，PID={old_pid}")
        try:
            lock_path.unlink()
        except OSError:
            pass

    lock_path.write_text(str(pid), encoding="utf-8")

    def cleanup() -> None:
        try:
            if lock_path.exists():
                current = lock_path.read_text(encoding="utf-8").strip()
                if current == str(pid):
                    lock_path.unlink()
        except OSError:
            pass
        if os.name == "nt" and _mutex_handle:
            try:
                ctypes.windll.kernel32.CloseHandle(_mutex_handle)
            except OSError:
                pass

    atexit.register(cleanup)


def trim_for_stream(text: str, limit: int = 3800) -> str:
    text = text.strip()
    if not text:
        return "思考中…"
    if len(text) <= limit:
        return text
    head = max(900, limit // 3)
    tail = limit - head - 5
    return f"{text[:head].rstrip()}\n...\n{text[-tail:].lstrip()}"


@dataclass(slots=True)
class Settings:
    telegram_bot_token: str
    allowed_chat_ids: set[int]
    repos: dict[str, Path]
    state_dir: Path
    codex_command: list[str]
    codex_approval: str
    codex_sandbox: str
    codex_default_sandbox: str
    codex_model: str | None
    codex_reasoning_effort: str | None
    max_prompt_chars: int
    stream_edit_interval: float
    max_message_chars: int
    auto_archive_on_new: bool

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv()

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("缺少 TELEGRAM_BOT_TOKEN")

        repos_raw = os.getenv("REPOS", "").strip()
        state_dir = Path(os.getenv("STATE_DIR", "./state")).expanduser().resolve()
        state_dir.mkdir(parents=True, exist_ok=True)

        chat_ids_raw = os.getenv("ALLOWED_CHAT_IDS", "").strip()
        return cls(
            telegram_bot_token=token,
            allowed_chat_ids=parse_int_set(chat_ids_raw) if chat_ids_raw else set(),
            repos=parse_repo_map(repos_raw) if repos_raw else default_repo_map(),
            state_dir=state_dir,
            codex_command=parse_command(
                os.getenv("CODEX_COMMAND", "npx @openai/codex@latest")
            ),
            codex_approval=os.getenv("CODEX_APPROVAL", "never").strip() or "never",
            codex_sandbox=os.getenv("CODEX_SANDBOX", "danger-full-access").strip()
            or "danger-full-access",
            codex_default_sandbox=os.getenv("CODEX_DEFAULT_SANDBOX", "workspace-write").strip()
            or "workspace-write",
            codex_model=os.getenv("CODEX_MODEL", "gpt-5.4").strip() or "gpt-5.4",
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "medium").strip()
            or "medium",
            max_prompt_chars=int(os.getenv("MAX_PROMPT_CHARS", "12000")),
            stream_edit_interval=float(os.getenv("STREAM_EDIT_INTERVAL", "0.8")),
            max_message_chars=int(os.getenv("MAX_MESSAGE_CHARS", "3800")),
            auto_archive_on_new=os.getenv("AUTO_ARCHIVE_ON_NEW", "true").strip().lower()
            not in {"0", "false", "no", "off"},
        )


class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, Any]] = {"chat_prefs": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self._migrate()

    def _migrate(self) -> None:
        changed = False
        prefs = self.data.setdefault("chat_prefs", {})
        for value in prefs.values():
            if not isinstance(value, dict):
                continue
            thread_id = value.get("thread_id")
            history = value.get("thread_history")
            if isinstance(thread_id, str) and thread_id:
                if not isinstance(history, list):
                    value["thread_history"] = [thread_id]
                    changed = True
                elif thread_id not in history:
                    history.insert(0, thread_id)
                    del history[30:]
                    changed = True
        if changed:
            self.save()

    def save(self) -> None:
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self.path)

    def _chat(self, chat_id: int) -> dict[str, Any]:
        prefs = self.data.setdefault("chat_prefs", {})
        chat = prefs.setdefault(str(chat_id), {})
        thread_id = chat.get("thread_id")
        history = chat.get("thread_history")
        if isinstance(thread_id, str) and thread_id:
            if not isinstance(history, list):
                chat["thread_history"] = [thread_id]
            elif thread_id not in history:
                history.insert(0, thread_id)
                del history[30:]
        return chat

    def get_repo_key(self, chat_id: int) -> str | None:
        return self._chat(chat_id).get("repo_key")

    def set_repo_key(self, chat_id: int, repo_key: str) -> None:
        chat = self._chat(chat_id)
        chat["repo_key"] = repo_key
        chat.pop("thread_id", None)
        self.save()

    def get_thread_id(self, chat_id: int) -> str | None:
        return self._chat(chat_id).get("thread_id")

    def get_model(self, chat_id: int) -> str | None:
        value = self._chat(chat_id).get("model")
        return value if isinstance(value, str) and value else None

    def get_sandbox(self, chat_id: int) -> str | None:
        value = self._chat(chat_id).get("sandbox")
        return value if isinstance(value, str) and value else None

    def set_sandbox(self, chat_id: int, sandbox: str | None) -> None:
        chat = self._chat(chat_id)
        if sandbox:
            chat["sandbox"] = sandbox
        else:
            chat.pop("sandbox", None)
        self.save()

    def set_model(self, chat_id: int, model: str | None) -> None:
        chat = self._chat(chat_id)
        if model:
            chat["model"] = model
        else:
            chat.pop("model", None)
        self.save()

    def get_reasoning_effort(self, chat_id: int) -> str | None:
        value = self._chat(chat_id).get("reasoning_effort")
        return value if isinstance(value, str) and value else None

    def set_reasoning_effort(self, chat_id: int, effort: str | None) -> None:
        chat = self._chat(chat_id)
        if effort:
            chat["reasoning_effort"] = effort
        else:
            chat.pop("reasoning_effort", None)
        self.save()

    def set_thread_id(self, chat_id: int, thread_id: str) -> None:
        chat = self._chat(chat_id)
        chat["thread_id"] = thread_id
        history = chat.setdefault("thread_history", [])
        if thread_id in history:
            history.remove(thread_id)
        history.insert(0, thread_id)
        del history[30:]
        self.save()

    def clear_thread_id(self, chat_id: int) -> None:
        chat = self._chat(chat_id)
        if "thread_id" in chat:
            chat.pop("thread_id", None)
            self.save()

    def get_thread_history(self, chat_id: int) -> list[str]:
        history = self._chat(chat_id).get("thread_history", [])
        return [item for item in history if isinstance(item, str)]

    def remove_thread_from_history(self, chat_id: int, thread_id: str) -> None:
        chat = self._chat(chat_id)
        history = chat.get("thread_history", [])
        if thread_id in history:
            history = [item for item in history if item != thread_id]
            chat["thread_history"] = history
            self.save()

    def get_verbose_level(self, chat_id: int) -> str | None:
        value = self._chat(chat_id).get("verbose")
        return value if isinstance(value, str) and value in VALID_VERBOSE_LEVELS else None

    def set_verbose_level(self, chat_id: int, level: str | None) -> None:
        chat = self._chat(chat_id)
        if level and level in VALID_VERBOSE_LEVELS:
            chat["verbose"] = level
        else:
            chat.pop("verbose", None)
        self.save()


@dataclass
class PollingHealth:
    last_ok_at: float | None = None
    last_conflict_at: float | None = None
    last_error_text: str | None = None
    alerted_conflict: bool = False


@dataclass
class ActiveTurn:
    chat_id: int
    repo_key: str
    repo_path: Path
    thread_id: str
    turn_id: str
    prompt: str
    started_at: float = field(default_factory=time.time)
    text: str = ""
    final_text: str = ""
    error_text: str | None = None
    stage: str = "准备中"
    running_command: str | None = None
    recent_commands: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    tool_notes: list[str] = field(default_factory=list)
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    last_event_at: float = field(default_factory=time.time)

    async def push(self, event: dict[str, Any]) -> None:
        self.last_event_at = time.time()
        await self.queue.put(event)

    async def events(self) -> Any:
        while True:
            event = await self.queue.get()
            yield event
            if event["type"] in {"done", "error", "interrupted"}:
                return


def shorten_middle(text: str, limit: int = 120) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = max(40, limit // 2 - 10)
    tail = max(20, limit - head - 3)
    return f"{text[:head].rstrip()}...{text[-tail:].lstrip()}"


def append_unique(items: list[str], value: str, limit: int = 8) -> None:
    value = value.strip()
    if not value:
        return
    if value in items:
        items.remove(value)
    items.append(value)
    del items[:-limit]


def extract_shell_command(arguments: Any) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    command = arguments.get("command")
    return command if isinstance(command, str) and command.strip() else None


def extract_updated_files(output_text: str) -> list[str]:
    matches = re.findall(r"^[AMDR]\s+(.+)$", output_text, flags=re.MULTILINE)
    cleaned: list[str] = []
    for match in matches:
        path = match.strip()
        if path:
            cleaned.append(path)
    return cleaned


def stage_progress(stage: str) -> int:
    if "准备" in stage or "理解" in stage:
        return 10
    if "读取" in stage or "搜索" in stage:
        return 30
    if "执行命令" in stage:
        return 50
    if "修改文件" in stage:
        return 70
    if "整理" in stage or "总结" in stage:
        return 90
    if "完成" in stage:
        return 100
    return 20


def render_progress_bar(percent: int, width: int = 10) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def format_elapsed(seconds: float) -> str:
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def render_run_status(
    turn: ActiveTurn,
    verbose: str,
    *,
    state: str = "running",
) -> str:
    if verbose == "off":
        return ""

    elapsed = format_elapsed(time.time() - turn.started_at)
    if state == "running":
        lines = ["思考中…", f"已运行 {elapsed}"]
    elif state == "done":
        lines = ["已完成", f"总用时 {elapsed}"]
    elif state == "error":
        lines = ["执行失败", f"总用时 {elapsed}"]
    else:
        lines = ["已中断", f"总用时 {elapsed}"]

    if state == "running" and verbose in {"all", "verbose"}:
        lines.append(f"阶段: {turn.stage}")
        idle_seconds = int(max(0, time.time() - turn.last_event_at))
        if idle_seconds >= 4:
            lines.append(f"等待下一段输出: {idle_seconds}s")
    if state == "running" and turn.running_command and verbose == "verbose":
        lines.extend(["", "当前命令", shorten_middle(turn.running_command, 240)])
    return trim_for_stream("\n".join(lines), 600)


def format_tool_event(event: dict[str, Any], verbose: str) -> str | None:
    if verbose == "off":
        return None

    kind = str(event.get("kind") or "")
    if kind == "shell":
        command = str(event.get("command") or "").strip()
        if not command:
            return None
        return "\n".join(
            [
                "已运行命令",
                shorten_middle(command, 500 if verbose in {"all", "verbose"} else 220),
            ]
        )

    if kind == "shell_done":
        if verbose not in {"all", "verbose"}:
            return None
        command = str(event.get("command") or "").strip()
        if not command:
            return "命令已完成"
        return "\n".join(
            [
                "命令已完成",
                shorten_middle(command, 400),
            ]
        )

    if kind == "read":
        if verbose not in {"all", "verbose"}:
            return None
        name = str(event.get("name") or "read_context")
        return f"已读取上下文\n{name}"

    if kind == "patch":
        if verbose != "verbose":
            return None
        return "开始应用补丁"

    if kind == "patch_output":
        files = [str(item) for item in event.get("files") or [] if str(item).strip()]
        if not files:
            return None
        limit = 5 if verbose in {"thinking", "new"} else 10
        lines = ["已修改文件"]
        lines.extend(f"- {path}" for path in files[:limit])
        if len(files) > limit:
            lines.append(f"- 还有 {len(files) - limit} 个文件")
        return "\n".join(lines)

    return None


async def sync_text_bubbles(
    application: Application,
    chat_id: int,
    message_ids: list[int],
    previous_chunks: list[str],
    text: str,
    limit: int,
) -> tuple[list[int], list[str]]:
    chunks = split_message(text, limit)
    if not chunks:
        return message_ids, previous_chunks

    current_ids = list(message_ids)
    for index, chunk in enumerate(chunks):
        if index < len(current_ids):
            if index < len(previous_chunks) and previous_chunks[index] == chunk:
                continue
            current_ids[index] = await update_stream_message(
                application,
                chat_id,
                current_ids[index],
                chunk,
            )
            continue
        message = await application.bot.send_message(chat_id=chat_id, text=chunk)
        current_ids.append(message.message_id)
    return current_ids, chunks


class CodexAppServerClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.request_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.request_id = 0
        self.stdout_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.notification_handlers: list[Any] = []
        self.loaded_threads: set[str] = set()
        self.err_log_path = self.settings.state_dir / "app_server.err.log"
        self.log = logging.getLogger("codex.appserver")

    def add_notification_handler(self, handler: Any) -> None:
        self.notification_handlers.append(handler)

    async def ensure_started(self) -> None:
        async with self.lifecycle_lock:
            if self.process and self.process.returncode is None:
                return
            await self._start_process()

    async def _start_process(self) -> None:
        self.loaded_threads.clear()
        command = [*self.settings.codex_command, "app-server", "--listen", "stdio://"]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.stdout_task = asyncio.create_task(self._stdout_loop())
        self.stderr_task = asyncio.create_task(self._stderr_loop())
        await self._send_request(
            "initialize",
            {
                "clientInfo": {
                    "name": "telegram-codex-bridge",
                    "title": "Telegram Codex Bridge",
                    "version": "2.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [],
                },
            },
        )

    async def _stdout_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            buffer = b""
            while True:
                raw = await self.process.stdout.read(65536)
                if not raw:
                    if buffer.strip():
                        self.log.warning("stdout 遗留未解析内容: %s", buffer[:500].decode("utf-8", errors="replace"))
                    break
                buffer += raw
                while b"\n" in buffer:
                    line_raw, buffer = buffer.split(b"\n", 1)
                    line = line_raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        self.log.warning("忽略非 JSON stdout: %s", line[:500])
                        continue
                    if "id" in payload:
                        future = self.pending.pop(int(payload["id"]), None)
                        if not future:
                            continue
                        if "error" in payload:
                            future.set_exception(
                                RuntimeError(payload["error"].get("message", "未知 RPC 错误"))
                            )
                        else:
                            future.set_result(payload["result"])
                        continue
                    if "method" in payload:
                        for handler in self.notification_handlers:
                            await handler(payload)
        finally:
            await self._handle_process_exit()

    async def _stderr_loop(self) -> None:
        assert self.process and self.process.stderr
        self.err_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.err_log_path.open("a", encoding="utf-8", errors="replace") as log_file:
            log_file.write(f"\n[{utc_now()}] app-server started\n")
            while True:
                raw = await self.process.stderr.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace")
                log_file.write(text)
                log_file.flush()

    async def _handle_process_exit(self) -> None:
        self.loaded_threads.clear()
        pending = list(self.pending.values())
        self.pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(RuntimeError("Codex app-server 已断开"))

    async def shutdown(self) -> None:
        process = self.process
        stdout_task = self.stdout_task
        stderr_task = self.stderr_task

        self.process = None
        self.stdout_task = None
        self.stderr_task = None
        self.loaded_threads.clear()

        if process and process.stdin:
            try:
                process.stdin.close()
            except Exception:
                pass

        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass

        for task in (stdout_task, stderr_task):
            if not task:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def request(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        await self.ensure_started()
        timeout_seconds = 20
        try:
            return await asyncio.wait_for(
                self._send_request(method, params),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Codex app-server 请求超时: {method}") from exc

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        assert self.process and self.process.stdin
        async with self.request_lock:
            self.request_id += 1
            request_id = self.request_id
            loop = asyncio.get_running_loop()
            future: asyncio.Future[dict[str, Any]] = loop.create_future()
            self.pending[request_id] = future
            message: dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
            self.process.stdin.write(
                (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            )
            await self.process.stdin.drain()
        return await future

    async def start_thread(self, repo_path: Path, runtime_config: dict[str, Any]) -> str:
        response = await self.request(
            "thread/start",
            {
                "cwd": str(repo_path),
                "experimentalRawEvents": False,
                **runtime_config,
            },
        )
        thread_id = response["thread"]["id"]
        self.loaded_threads.add(thread_id)
        return thread_id

    async def resume_thread(
        self,
        thread_id: str,
        repo_path: Path,
        runtime_config: dict[str, Any],
    ) -> str:
        response = await self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(repo_path),
                **runtime_config,
            },
        )
        resumed_id = response["thread"]["id"]
        self.loaded_threads.add(resumed_id)
        return resumed_id

    async def start_turn(self, thread_id: str, prompt: str) -> str:
        response = await self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
        )
        return response["turn"]["id"]

    async def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        await self.request(
            "turn/interrupt",
            {
                "threadId": thread_id,
                "turnId": turn_id,
            },
        )

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        return await self.request(
            "thread/read",
            {
                "threadId": thread_id,
                "includeTurns": True,
            },
        )

    async def archive_thread(self, thread_id: str) -> None:
        await self.request(
            "thread/archive",
            {
                "threadId": thread_id,
            },
        )
        self.loaded_threads.discard(thread_id)

    async def unarchive_thread(self, thread_id: str) -> None:
        await self.request(
            "thread/unarchive",
            {
                "threadId": thread_id,
            },
        )

    async def list_threads(
        self,
        *,
        cwd: Path | None = None,
        archived: bool | None = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        response = await self.request(
            "thread/list",
            {
                "limit": limit,
                "cwd": str(cwd) if cwd else None,
                "archived": archived,
            },
        )
        return response.get("data", [])


def extract_runtime_info_from_session_file(path: Path) -> tuple[str | None, str | None]:
    if not path.exists():
        return None, None
    model: str | None = None
    effort: str | None = None
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "turn_context":
                    continue
                body = payload.get("payload", {})
                model = body.get("model") or model
                effort = body.get("effort") or effort
    except OSError:
        return None, None
    return model, effort


async def resolve_effective_runtime_info(
    settings: Settings,
    store: SessionStore,
    client: CodexAppServerClient,
    chat_id: int,
    repo_key: str | None,
) -> tuple[str | None, str | None]:
    thread_id = store.get_thread_id(chat_id)
    if not thread_id or not repo_key:
        return (
            store.get_model(chat_id) or settings.codex_model,
            store.get_reasoning_effort(chat_id) or settings.codex_reasoning_effort,
        )

    repo_path = settings.repos[repo_key]
    local_threads = list_local_threads(repo_path, store.get_thread_history(chat_id), limit=200)
    thread = next((item for item in local_threads if item.get("id") == thread_id), None)
    if not thread:
        try:
            thread_items = [
                *await client.list_threads(cwd=repo_path, archived=False, limit=200),
                *await client.list_threads(cwd=repo_path, archived=True, limit=200),
            ]
        except Exception:
            thread_items = []
        thread = next((item for item in thread_items if item.get("id") == thread_id), None)
    if thread:
        session_path = thread.get("path")
        if isinstance(session_path, str) and session_path:
            model, effort = extract_runtime_info_from_session_file(Path(session_path))
            if model or effort:
                return model, effort

    return (
        store.get_model(chat_id) or settings.codex_model,
        store.get_reasoning_effort(chat_id) or settings.codex_reasoning_effort,
    )


class ConversationManager:
    def __init__(
        self,
        settings: Settings,
        store: SessionStore,
        client: CodexAppServerClient,
    ) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.active_by_chat: dict[int, ActiveTurn] = {}
        self.active_by_turn: dict[str, ActiveTurn] = {}
        self.client.add_notification_handler(self._handle_notification)

    def get_active(self, chat_id: int) -> ActiveTurn | None:
        return self.active_by_chat.get(chat_id)

    async def archive_current_thread(self, chat_id: int) -> bool:
        active = self.get_active(chat_id)
        if active:
            raise RuntimeError("当前还在回复，先 /stop。")
        thread_id = self.store.get_thread_id(chat_id)
        if not thread_id:
            return False
        await self.client.archive_thread(thread_id)
        self.store.clear_thread_id(chat_id)
        return True

    async def _handle_notification(self, payload: dict[str, Any]) -> None:
        method = payload.get("method")
        params = payload.get("params", {})
        turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        turn = self.active_by_turn.get(turn_id) if turn_id else None
        if not turn:
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if not delta:
                return
            if turn.stage in {"准备中", "理解需求"}:
                turn.stage = "整理结果"
            turn.text += delta
            await turn.push({"type": "delta", "text": turn.text})
            return

        if method == "item/completed":
            item = params.get("item", {})
            await self._handle_completed_item(turn, item)
            if item.get("type") == "agentMessage":
                turn.final_text = item.get("text", "") or turn.text
            return

        if method == "turn/completed":
            turn.stage = "完成"
            text = turn.final_text or turn.text
            await turn.push({"type": "done", "text": text})
            self._finish_turn(turn)
            return

        if method == "error":
            error = params.get("error", {})
            details = error.get("additionalDetails")
            message = error.get("message", "执行失败")
            if details:
                message = f"{message}\n{details}"
            turn.error_text = message
            await turn.push({"type": "error", "text": message})
            self._finish_turn(turn)

    async def _handle_completed_item(self, turn: ActiveTurn, item: dict[str, Any]) -> None:
        item_type = item.get("type")

        if item_type == "function_call":
            name = item.get("name") or ""
            if name == "shell_command":
                command = extract_shell_command(item.get("arguments"))
                if command:
                    turn.stage = "执行命令"
                    turn.running_command = command
                    append_unique(turn.recent_commands, command, limit=10)
                    append_unique(turn.tool_notes, "已开始执行命令", limit=6)
                    await turn.push({"type": "tool", "kind": "shell", "command": command})
                    await turn.push({"type": "status"})
                return

            if name in {"read_mcp_resource", "read_thread_terminal"}:
                turn.stage = "读取上下文"
                append_unique(turn.tool_notes, f"读取 {name}", limit=6)
                await turn.push({"type": "tool", "kind": "read", "name": name})
                await turn.push({"type": "status"})
                return

        if item_type == "function_call_output":
            if turn.running_command:
                completed_command = turn.running_command
                append_unique(turn.tool_notes, "命令执行完成", limit=6)
                turn.running_command = None
                if turn.stage == "执行命令":
                    turn.stage = "整理结果"
                await turn.push(
                    {"type": "tool", "kind": "shell_done", "command": completed_command}
                )
                await turn.push({"type": "status"})
            return

        if item_type == "custom_tool_call":
            name = item.get("name") or ""
            if name == "apply_patch":
                turn.stage = "修改文件"
                append_unique(turn.tool_notes, "正在应用补丁", limit=6)
                await turn.push({"type": "tool", "kind": "patch"})
                await turn.push({"type": "status"})
            return

        if item_type == "custom_tool_call_output":
            output = item.get("output") or ""
            if isinstance(output, str):
                changed_files = extract_updated_files(output)
                for path in changed_files:
                    append_unique(turn.modified_files, path, limit=12)
                if changed_files:
                    turn.stage = "修改文件"
                    append_unique(turn.tool_notes, "补丁已写入", limit=6)
                    await turn.push(
                        {"type": "tool", "kind": "patch_output", "files": changed_files}
                    )
                    await turn.push({"type": "status"})
            return

    def _finish_turn(self, turn: ActiveTurn) -> None:
        self.active_by_turn.pop(turn.turn_id, None)
        self.active_by_chat.pop(turn.chat_id, None)

    async def ensure_thread(
        self,
        chat_id: int,
        repo_key: str,
        *,
        force_new: bool = False,
    ) -> tuple[str, Path]:
        repo_path = self.settings.repos[repo_key]
        runtime_config = build_thread_runtime_config(self.settings, self.store, chat_id)
        if force_new:
            old_thread_id = self.store.get_thread_id(chat_id)
            if old_thread_id and self.settings.auto_archive_on_new:
                try:
                    await self.client.archive_thread(old_thread_id)
                except Exception:
                    pass
            self.store.clear_thread_id(chat_id)

        thread_id = self.store.get_thread_id(chat_id)
        if thread_id:
            try:
                if thread_id not in self.client.loaded_threads:
                    thread_id = await self.client.resume_thread(
                        thread_id,
                        repo_path,
                        runtime_config,
                    )
                # 旧版 sessions.json 里可能只有 thread_id 没有 history。
                # 这里每次命中当前线程时都回填一次，保证 /threads 能看到。
                self.store.set_thread_id(chat_id, thread_id)
                return thread_id, repo_path
            except Exception:
                self.store.clear_thread_id(chat_id)

        thread_id = await self.client.start_thread(repo_path, runtime_config)
        self.store.set_thread_id(chat_id, thread_id)
        return thread_id, repo_path

    async def switch_thread(
        self,
        chat_id: int,
        repo_key: str,
        thread_id: str,
    ) -> str:
        active = self.get_active(chat_id)
        if active:
            raise RuntimeError("当前还在回复，先 /stop。")
        repo_path = self.settings.repos[repo_key]
        runtime_config = build_thread_runtime_config(self.settings, self.store, chat_id)
        try:
            await self.client.unarchive_thread(thread_id)
        except Exception:
            pass
        resumed_id = await self.client.resume_thread(
            thread_id,
            repo_path,
            runtime_config,
        )
        self.store.set_thread_id(chat_id, resumed_id)
        return resumed_id

    async def start_chat_turn(
        self,
        chat_id: int,
        repo_key: str,
        prompt: str,
        *,
        force_new: bool = False,
    ) -> ActiveTurn:
        active = self.get_active(chat_id)
        if active:
            raise RuntimeError("上一条消息还没结束，先等它回完或发 /stop。")

        thread_id, repo_path = await self.ensure_thread(
            chat_id,
            repo_key,
            force_new=force_new,
        )
        turn_id = await self.client.start_turn(thread_id, prompt)
        turn = ActiveTurn(
            chat_id=chat_id,
            repo_key=repo_key,
            repo_path=repo_path,
            thread_id=thread_id,
            turn_id=turn_id,
            prompt=prompt,
            stage="理解需求",
        )
        self.active_by_chat[chat_id] = turn
        self.active_by_turn[turn_id] = turn
        await turn.push({"type": "status"})
        return turn

    async def stop_chat_turn(self, chat_id: int) -> bool:
        active = self.get_active(chat_id)
        if not active:
            return False
        await self.client.interrupt_turn(active.thread_id, active.turn_id)
        await active.push({"type": "interrupted", "text": "已中断当前回复。"})
        self._finish_turn(active)
        return True


def ensure_authorized(update: Update, settings: Settings) -> bool:
    chat = update.effective_chat
    if not chat:
        return False
    if not settings.allowed_chat_ids:
        return True
    return chat.id in settings.allowed_chat_ids


def resolve_repo_key(
    chat_id: int,
    settings: Settings,
    store: SessionStore,
    explicit: str | None = None,
) -> str | None:
    if explicit:
        return explicit if explicit in settings.repos else None
    saved = store.get_repo_key(chat_id)
    if saved in settings.repos:
        return saved
    if len(settings.repos) == 1:
        return next(iter(settings.repos))
    return None


def format_thread_preview(thread: dict[str, Any], fallback_id: str) -> str:
    preview = (thread.get("preview") or "").strip()
    if not preview:
        preview = thread.get("name") or fallback_id
    preview = preview.replace("\n", " ").strip()
    if len(preview) > 40:
        preview = preview[:39] + "…"
    return preview


def load_session_index_map() -> dict[str, dict[str, str]]:
    index_path = Path.home() / ".codex" / "session_index.jsonl"
    session_map: dict[str, dict[str, str]] = {}
    if not index_path.exists():
        return session_map
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                thread_id = payload.get("id")
                if not isinstance(thread_id, str) or not thread_id:
                    continue
                session_map[thread_id] = {
                    "name": str(payload.get("thread_name") or "").strip(),
                    "updated_at": str(payload.get("updated_at") or "").strip(),
                }
    except OSError:
        return {}
    return session_map


def extract_session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(10):
                line = handle.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") == "session_meta":
                    body = payload.get("payload")
                    return body if isinstance(body, dict) else None
    except OSError:
        return None
    return None


def list_local_threads(repo_path: Path, history_ids: list[str], limit: int = 200) -> list[dict[str, Any]]:
    codex_root = Path.home() / ".codex"
    index_map = load_session_index_map()
    files = [
        *sorted((codex_root / "sessions").glob("**/*.jsonl"), reverse=True),
        *sorted((codex_root / "archived_sessions").glob("*.jsonl"), reverse=True),
    ]
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    repo_str = str(repo_path.resolve())
    for path in files:
        meta = extract_session_meta(path)
        if not meta:
            continue
        cwd = str(meta.get("cwd") or "").strip()
        if cwd != repo_str:
            continue
        thread_id = str(meta.get("id") or "").strip()
        if not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        index_item = index_map.get(thread_id, {})
        updated_at = index_item.get("updated_at") or datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        preview = index_item.get("name") or thread_id
        status = {"type": "archived"} if "archived_sessions" in str(path) else {}
        items.append(
            {
                "id": thread_id,
                "preview": preview,
                "name": preview,
                "path": str(path),
                "updated_at": updated_at,
                "status": status,
            }
        )
        if len(items) >= limit:
            break

    history_rank = {thread_id: index for index, thread_id in enumerate(history_ids)}
    def sort_timestamp(raw: str | None) -> float:
        if not raw:
            return 0.0
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    items.sort(
        key=lambda item: (
            history_rank.get(item["id"], 10_000),
            -sort_timestamp(item.get("updated_at")),
        )
    )
    return items


async def get_repo_thread_list(
    client: CodexAppServerClient,
    repo_path: Path,
    history_ids: list[str],
) -> list[dict[str, Any]]:
    local_threads = list_local_threads(repo_path, history_ids)
    if local_threads:
        return local_threads
    return build_ordered_thread_list(await list_repo_threads(client, repo_path), history_ids)


def get_chat_model(settings: Settings, store: SessionStore, chat_id: int) -> str | None:
    return store.get_model(chat_id) or settings.codex_model


def get_chat_reasoning_effort(
    settings: Settings,
    store: SessionStore,
    chat_id: int,
) -> str | None:
    return store.get_reasoning_effort(chat_id) or settings.codex_reasoning_effort


def get_chat_sandbox(settings: Settings, store: SessionStore, chat_id: int) -> str:
    return store.get_sandbox(chat_id) or settings.codex_sandbox


def get_chat_verbose(store: SessionStore, chat_id: int) -> str:
    return store.get_verbose_level(chat_id) or "thinking"


def describe_access_mode(settings: Settings, sandbox: str) -> str:
    if sandbox == "danger-full-access":
        return "完全访问权限"
    if sandbox == settings.codex_default_sandbox:
        return "默认访问权限"
    return sandbox


def format_polling_health(health: PollingHealth) -> str:
    if health.last_conflict_at and (
        not health.last_ok_at or health.last_conflict_at >= health.last_ok_at
    ):
        return "冲突中"
    if health.last_ok_at:
        age = int(max(0, time.time() - health.last_ok_at))
        return f"正常 ({age}s)"
    return "初始化中"


def build_thread_keyboard(index: int, thread_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("使用 Use", callback_data=f"{THREAD_CALLBACK_PREFIX}:use:{thread_id}"),
                InlineKeyboardButton("摘要 Summary", callback_data=f"{THREAD_CALLBACK_PREFIX}:summary:{thread_id}"),
                InlineKeyboardButton("全文 History", callback_data=f"{THREAD_CALLBACK_PREFIX}:history:{thread_id}"),
            ],
            [
                InlineKeyboardButton("归档 Archive", callback_data=f"{THREAD_CALLBACK_PREFIX}:archive:{thread_id}")
            ],
        ]
    )


def build_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("状态 Status", callback_data="control:status"),
                InlineKeyboardButton("停止 Stop", callback_data="control:stop"),
                InlineKeyboardButton("新线程 New", callback_data="control:new"),
            ],
            [
                InlineKeyboardButton("默认 Default", callback_data="control:access:default"),
                InlineKeyboardButton("完全 Full", callback_data="control:access:full"),
            ],
            [
                InlineKeyboardButton("GPT-5.4", callback_data="control:model:gpt-5.4"),
                InlineKeyboardButton("5.4 Mini", callback_data="control:model:gpt-5.4-mini"),
                InlineKeyboardButton("5.3 Codex", callback_data="control:model:gpt-5.3-codex"),
            ],
            [
                InlineKeyboardButton("低 Low", callback_data="control:effort:low"),
                InlineKeyboardButton("中 Medium", callback_data="control:effort:medium"),
                InlineKeyboardButton("高 High", callback_data="control:effort:high"),
                InlineKeyboardButton("超高 XHigh", callback_data="control:effort:xhigh"),
            ],
        ]
    )


async def list_repo_threads(
    client: CodexAppServerClient,
    repo_path: Path,
) -> list[dict[str, Any]]:
    active_threads = await client.list_threads(
        cwd=repo_path,
        archived=False,
        limit=200,
    )
    archived_threads = await client.list_threads(
        cwd=repo_path,
        archived=True,
        limit=200,
    )
    return [*active_threads, *archived_threads]


def build_ordered_thread_list(
    all_threads: list[dict[str, Any]],
    history_ids: list[str],
) -> list[dict[str, Any]]:
    thread_map = {item["id"]: item for item in all_threads if item.get("id")}
    ordered_ids = history_ids[:]
    for item in all_threads:
        thread_id = item.get("id")
        if isinstance(thread_id, str) and thread_id and thread_id not in ordered_ids:
            ordered_ids.append(thread_id)
    return [thread_map[thread_id] for thread_id in ordered_ids if thread_id in thread_map]


def split_thread_sections(
    ordered_threads: list[dict[str, Any]],
    history_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    history_set = set(history_ids)
    mine = [item for item in ordered_threads if item.get("id") in history_set]
    repo_recent = [item for item in ordered_threads if item.get("id") not in history_set]
    return mine, repo_recent


def extract_message_text_parts(content: list[Any]) -> str:
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type not in {"input_text", "output_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "".join(parts)


def extract_thread_transcript(session_path: Path) -> list[tuple[str, str]]:
    transcript: list[tuple[str, str]] = []
    if not session_path.exists():
        return transcript
    try:
        with session_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if payload.get("type") != "response_item":
                    continue
                body = payload.get("payload", {})
                if body.get("type") != "message":
                    continue
                role = body.get("role")
                if role not in {"user", "assistant"}:
                    continue
                content = body.get("content")
                if not isinstance(content, list):
                    continue
                text = extract_message_text_parts(content)
                if not text:
                    continue
                speaker = "用户" if role == "user" else "助手"
                transcript.append((speaker, text))
    except OSError:
        return []
    return transcript


def format_thread_transcript(
    transcript: list[tuple[str, str]],
    thread: dict[str, Any],
) -> str:
    lines = [
        f"线程: {format_thread_preview(thread, thread.get('id', '未知线程'))}",
        thread.get("id", "未知线程"),
        "",
    ]
    for speaker, text in transcript:
        lines.append(speaker)
        lines.append(text.rstrip())
        lines.append("")
    return "\n".join(lines).strip()


def resolve_thread_selector(
    selector: str,
    ordered_threads: list[dict[str, Any]],
) -> dict[str, Any] | None:
    raw = selector.strip()
    if not raw:
        return None
    if raw.isdigit():
        index = int(raw) - 1
        if 0 <= index < len(ordered_threads):
            return ordered_threads[index]
        return None
    return next((item for item in ordered_threads if item.get("id") == raw), None)


def find_session_path_by_thread_id(thread_id: str) -> Path | None:
    codex_root = Path.home() / ".codex"
    patterns = [
        codex_root / "sessions" / "**" / f"*-{thread_id}.jsonl",
        codex_root / "archived_sessions" / f"*-{thread_id}.jsonl",
    ]
    for pattern in patterns:
        matches = list(glob.glob(str(pattern), recursive=True))
        if matches:
            return Path(matches[0])
    return None


def build_thread_stub(thread_id: str) -> dict[str, Any]:
    session_path = find_session_path_by_thread_id(thread_id)
    return {
        "id": thread_id,
        "preview": thread_id,
        "path": str(session_path) if session_path else "",
        "status": {},
    }


def format_thread_selection_error(
    selector: str,
    ordered_threads: list[dict[str, Any]],
) -> str:
    if selector.isdigit():
        count = len(ordered_threads)
        if count == 0:
            return "当前没有可选线程。先用 /threads 看列表。"
        return f"序号不存在。当前可用范围: 1 到 {count}。"
    return "线程不存在。先用 /threads 看列表。"


def resolve_thread_session_path(thread: dict[str, Any]) -> tuple[Path | None, str | None]:
    session_path_raw = thread.get("path")
    if not isinstance(session_path_raw, str) or not session_path_raw:
        return None, "这个线程当前没有可读取的本地 session 文件。"
    session_path = Path(session_path_raw)
    if session_path.exists():
        return session_path, None

    # 线程归档后，Codex 会把 jsonl 从 sessions 移到 archived_sessions。
    # app-server 返回的 path 有时还是旧路径，这里按同名文件补一次回退查找。
    archived_candidate = (
        session_path.parents[3] / "archived_sessions" / session_path.name
        if len(session_path.parents) >= 4
        else None
    )
    if archived_candidate and archived_candidate.exists():
        return archived_candidate, None

    archived_root = Path.home() / ".codex" / "archived_sessions" / session_path.name
    if archived_root.exists():
        return archived_root, None

    return None, "这个线程的本地 session 文件已经移动或清理了。"


def format_switch_thread_error(exc: Exception, thread_id: str) -> str:
    details = str(exc).strip()
    lowered = details.lower()
    if "not found" in lowered or "no such" in lowered:
        return f"线程恢复失败。\nthread_id: {thread_id}\n当前索引里已经找不到这个线程。"
    if "disconnect" in lowered or "app-server" in lowered:
        return f"线程恢复失败。\nthread_id: {thread_id}\n当前 Codex 连接已经重置。稍后重试一次。"
    if details:
        return f"线程恢复失败。\nthread_id: {thread_id}\n{type(exc).__name__}: {details}"
    return f"线程恢复失败。\nthread_id: {thread_id}"


def build_transcript_plaintext(transcript: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for speaker, text in transcript:
        lines.append(f"{speaker}:")
        lines.append(text.rstrip())
        lines.append("")
    return "\n".join(lines).strip()


def trim_prompt_text(text: str, limit: int = 40000) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= limit:
        return text, False
    head = min(12000, limit // 3)
    tail = limit - head - 32
    trimmed = f"{text[:head].rstrip()}\n\n[中间内容已省略]\n\n{text[-tail:].lstrip()}"
    return trimmed, True


async def run_single_prompt(
    client: CodexAppServerClient,
    thread_id: str,
    prompt: str,
    *,
    timeout_seconds: int = 240,
) -> str:
    loop = asyncio.get_running_loop()
    result_future: asyncio.Future[str] = loop.create_future()
    state: dict[str, Any] = {"text": ""}

    async def handler(payload: dict[str, Any]) -> None:
        method = payload.get("method")
        params = payload.get("params", {})
        turn_id = params.get("turnId") or params.get("turn", {}).get("id")
        if turn_id != state.get("turn_id"):
            return

        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                state["text"] += delta
            return

        if method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                state["final_text"] = item.get("text", "") or state["text"]
            return

        if method == "turn/completed":
            if not result_future.done():
                result_future.set_result(state.get("final_text") or state["text"])
            return

        if method == "error":
            error = params.get("error", {})
            details = error.get("additionalDetails")
            message = error.get("message", "执行失败")
            if details:
                message = f"{message}\n{details}"
            if not result_future.done():
                result_future.set_exception(RuntimeError(message))

    client.add_notification_handler(handler)
    try:
        turn_id = await client.start_turn(thread_id, prompt)
        state["turn_id"] = turn_id
        return await asyncio.wait_for(result_future, timeout=timeout_seconds)
    finally:
        try:
            client.notification_handlers.remove(handler)
        except ValueError:
            pass


async def summarize_transcript_with_model(
    client: CodexAppServerClient,
    repo_path: Path,
    runtime_config: dict[str, Any],
    transcript: list[tuple[str, str]],
) -> str:
    transcript_text, trimmed = trim_prompt_text(build_transcript_plaintext(transcript))
    prompt_lines = [
        "你在做历史线程压缩。",
        "根据下面的聊天记录，用中文输出一份给人类看的上下文摘要。",
        "目标是让人类快速接手这个线程。",
        "",
        "输出格式必须严格固定成下面四段，按这个顺序逐段输出：",
        "目标:",
        "已完成:",
        "当前状态:",
        "下一步:",
        "",
        "写作要求：",
        "1. 每个标题只出现一次。",
        "2. 每个标题下面写 1 到 4 行短句。",
        "3. 不要寒暄，不要额外加标题，不要补总结收尾。",
        "4. 如果聊天里出现关键命令、路径、线程号、配置项，保留它们。",
        "5. 信息不足时，把缺失内容写进“当前状态”里，格式用“当前缺失信息：...”。",
    ]
    if trimmed:
        prompt_lines.append(
            "6. 原始记录过长时，要在“当前状态”里明确写“当前缺失信息：摘要基于截断后的记录”。"
        )
    prompt_lines.extend(
        [
            "",
            "聊天记录：",
            transcript_text,
        ]
    )
    temp_thread_id = await client.start_thread(repo_path, runtime_config)
    try:
        summary = await run_single_prompt(client, temp_thread_id, "\n".join(prompt_lines))
    finally:
        try:
            await client.archive_thread(temp_thread_id)
        except Exception:
            pass
    return summary.strip()


def build_fallback_summary(
    transcript: list[tuple[str, str]],
    thread: dict[str, Any],
) -> str:
    user_messages = [text for speaker, text in transcript if speaker == "用户" and text.strip()]
    assistant_messages = [
        text for speaker, text in transcript if speaker == "助手" and text.strip()
    ]
    first_goal = shorten_middle(user_messages[0], 120) if user_messages else "当前缺失信息：没有找到明确目标"
    last_user = shorten_middle(user_messages[-1], 140) if user_messages else "当前缺失信息：没有用户收尾消息"
    last_assistant = (
        shorten_middle(assistant_messages[-1], 160)
        if assistant_messages
        else "当前缺失信息：没有助手输出"
    )
    preview = format_thread_preview(thread, thread.get("id", "未知线程"))
    return "\n".join(
        [
            "目标:",
            f"- {preview}",
            f"- {first_goal}",
            "",
            "已完成:",
            f"- 已读取 {len(transcript)} 条用户/助手消息",
            f"- 已定位线程 {thread.get('id', '未知线程')}",
            "",
            "当前状态:",
            f"- 最近用户消息：{last_user}",
            f"- 最近助手输出：{last_assistant}",
            "",
            "下一步:",
            "- 继续追问当前线程，或先用 /history 看全文",
            "- 如果需要更精确摘要，再重试 /summary",
        ]
    )


def inject_current_status_note(summary: str, note: str) -> str:
    marker = "当前状态:\n"
    if marker not in summary:
        return summary
    return summary.replace(marker, f"{marker}- {note}\n", 1)


async def summarize_with_progress(
    application: Application,
    chat_id: int,
    message_id: int,
    work: asyncio.Future[str] | asyncio.Task[str],
) -> str:
    start = time.time()
    step = 0
    labels = [
        "正在压缩上下文…",
        "正在压缩上下文…\n已读取历史消息",
        "正在压缩上下文…\n正在生成结构化摘要",
        "正在压缩上下文…\n仍在等待模型返回",
    ]
    while not work.done():
        await asyncio.sleep(4)
        if work.done():
            break
        step = min(step + 1, len(labels) - 1)
        elapsed = int(time.time() - start)
        text = f"{labels[step]}\n已等待 {elapsed}s"
        try:
            await edit_message_safe(application, chat_id, message_id, text)
        except Exception:
            pass
    return await work


def build_thread_runtime_config(
    settings: Settings,
    store: SessionStore,
    chat_id: int,
) -> dict[str, Any]:
    # 线程启动/恢复时，把每个 chat 的模型和思考强度偏好一起带上。
    payload: dict[str, Any] = {
        "approvalPolicy": settings.codex_approval,
        "sandbox": get_chat_sandbox(settings, store, chat_id),
        "persistExtendedHistory": True,
    }
    model = get_chat_model(settings, store, chat_id)
    effort = get_chat_reasoning_effort(settings, store, chat_id)
    if model:
        payload["model"] = model
    if effort:
        payload["reasoningEffort"] = effort
    return payload


def telegram_menu_commands() -> list[tuple[str, str]]:
    # 跟 Hermes 一样，启动时一次性注册 Telegram 的 slash 菜单。
    return [
        ("start", "帮助 / Help"),
        ("status", "查看状态 / Status"),
        ("new", "新开线程 / New thread"),
        ("repos", "仓库列表 / Repos"),
        ("repo", "切换仓库 / Switch repo"),
        ("threads", "历史线程 / Threads"),
        ("use", "切回线程 / Use thread"),
        ("history", "导出全文 / Export history"),
        ("summary", "压缩摘要 / Summary"),
        ("archive", "归档线程 / Archive"),
        ("cleanup_threads", "清理旧线程 / Cleanup"),
        ("verbose", "进度显示 / Verbose"),
        ("access", "访问权限 / Access"),
        ("model", "模型设置 / Model"),
        ("effort", "思考强度 / Effort"),
        ("stop", "停止回复 / Stop"),
        ("task", "新线程提问 / New task"),
        ("continue", "当前线程继续 / Continue"),
    ]


VALID_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
EFFORT_LABELS = {
    "minimal": "极低",
    "low": "低",
    "medium": "中",
    "high": "高",
    "xhigh": "超高",
}

# 这里只放常见、稳定、好解释的候选项。
# Codex 实际上能接收 Responses API 可用模型名，但 Telegram 菜单里需要给用户一份短清单。
SUGGESTED_CODEX_MODELS = (
    "gpt-5.4",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
    "gpt-5.1-codex-mini",
)


def format_effort_name(effort: str | None) -> str:
    if not effort:
        return "medium (中)"
    label = EFFORT_LABELS.get(effort)
    return f"{effort} ({label})" if label else effort


async def reject_unauthorized(update: Update) -> None:
    chat = update.effective_chat
    if not chat or not update.effective_message:
        return
    await update.effective_message.reply_text(
        f"未授权的 chat_id: {chat.id}\n把这个 ID 加到 ALLOWED_CHAT_IDS 再重启。"
    )


async def bootstrap_notice(update: Update, settings: Settings) -> None:
    if settings.allowed_chat_ids:
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    await message.reply_text(
        "\n".join(
            [
                "当前处于启动期开放模式。",
                f"你的 chat_id: {chat.id}",
                "把它写进 ALLOWED_CHAT_IDS 后重启，机器人就只接受白名单消息。",
            ]
        )
    )


def mark_poll_ok(application: Application) -> None:
    health: PollingHealth | None = application.bot_data.get("health")
    if not health:
        return
    health.last_ok_at = time.time()
    health.last_error_text = None


def build_attachment_prompt(
    *,
    user_text: str,
    attachments: list[tuple[str, Path, str | None]],
) -> str:
    lines = [user_text.strip() or "请根据附件继续处理。", "", "附件"]
    for kind, path, preview in attachments:
        lines.append(f"- 类型: {kind}")
        lines.append(f"  路径: {path}")
        lines.append("  说明: 你可以直接读取这个本地路径。")
        if preview:
            lines.append("  预览:")
            lines.append(preview)
    return "\n".join(lines).strip()


def maybe_inline_file_preview(path: Path, max_chars: int = 6000) -> str | None:
    if path.suffix.lower() not in {
        ".txt",
        ".md",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".log",
        ".csv",
    }:
        return None
    try:
        if path.stat().st_size > 64 * 1024:
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...[已截断]"
    return text


async def build_status_text(
    settings: Settings,
    store: SessionStore,
    client: CodexAppServerClient,
    conversations: ConversationManager,
    health: PollingHealth,
    chat_id: int,
) -> str:
    repo_key = resolve_repo_key(chat_id, settings, store) or "未绑定"
    thread_id = store.get_thread_id(chat_id) or "未创建"
    active = conversations.get_active(chat_id)
    effective_model, effective_effort = await resolve_effective_runtime_info(
        settings,
        store,
        client,
        chat_id,
        repo_key if repo_key in settings.repos else None,
    )
    effective_sandbox = get_chat_sandbox(settings, store, chat_id)
    lines = [
        f"repo: {repo_key}",
        f"thread: {thread_id}",
        f"model: {effective_model or settings.codex_model or 'gpt-5.4'}",
        f"effort: {format_effort_name(effective_effort or settings.codex_reasoning_effort)}",
        f"verbose: {get_chat_verbose(store, chat_id)}",
        f"access: {describe_access_mode(settings, effective_sandbox)}",
        f"sandbox: {effective_sandbox}",
        f"polling: {format_polling_health(health)}",
        f"approval: {settings.codex_approval}",
    ]
    if active:
        lines.extend(
            [
                f"turn: {active.turn_id}",
                f"运行中: {int(time.time() - active.started_at)}s",
            ]
        )
    else:
        lines.append("当前空闲")
    return "\n".join(lines)


async def save_telegram_file(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
    target_dir: Path,
    filename: str,
) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", filename).strip("._") or "attachment.bin"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"{timestamp}-{safe_name}"
    telegram_file = await context.bot.get_file(file_id)
    await telegram_file.download_to_drive(custom_path=str(target))
    return target.resolve()


async def edit_message_safe(
    application: Application,
    chat_id: int,
    message_id: int,
    text: str,
) -> None:
    attempts = 0
    while True:
        attempts += 1
        try:
            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
            return
        except RetryAfter as exc:
            await asyncio.sleep(float(exc.retry_after) + 0.2)
            if attempts >= 3:
                raise
        except (TimedOut, NetworkError):
            if attempts >= 3:
                raise
            await asyncio.sleep(0.8 * attempts)
        except BadRequest as exc:
            lowered = str(exc).lower()
            if "message is not modified" in lowered:
                return
            raise


async def update_stream_message(
    application: Application,
    chat_id: int,
    message_id: int,
    text: str,
) -> int:
    try:
        await edit_message_safe(application, chat_id, message_id, text)
        return message_id
    except BadRequest as exc:
        lowered = str(exc).lower()
        recoverable = (
            "message to edit not found" in lowered
            or "message can't be edited" in lowered
            or "message identifier is not specified" in lowered
        )
        if not recoverable:
            raise
    except (TimedOut, NetworkError):
        pass
    message = await application.bot.send_message(chat_id=chat_id, text=text)
    return message.message_id


async def stream_turn_to_telegram(
    application: Application,
    turn: ActiveTurn,
    message_id: int | None,
    settings: Settings,
) -> None:
    last_status_edit_at = 0.0
    last_status_text = ""
    last_answer_flush_at = 0.0
    last_answer_length = 0
    status_message_id = message_id
    answer_message_ids: list[int] = []
    answer_chunks: list[str] = []
    store: SessionStore = application.bot_data["store"]
    conversations: ConversationManager = application.bot_data["conversations"]
    verbose = get_chat_verbose(store, turn.chat_id)

    try:
        while True:
            try:
                event = await asyncio.wait_for(turn.queue.get(), timeout=4.0)
            except asyncio.TimeoutError:
                if conversations.get_active(turn.chat_id) is not turn:
                    return
                if not status_message_id or verbose == "off":
                    continue
                now = time.time()
                text = render_run_status(turn, verbose, state="running")
                if now - last_status_edit_at < settings.stream_edit_interval or text == last_status_text:
                    continue
                status_message_id = await update_stream_message(
                    application,
                    turn.chat_id,
                    status_message_id,
                    text,
                )
                last_status_edit_at = now
                last_status_text = text
                continue
            event_type = event["type"]

            if event_type == "tool":
                tool_text = format_tool_event(event, verbose)
                if tool_text:
                    for chunk in split_message(tool_text, settings.max_message_chars):
                        await application.bot.send_message(chat_id=turn.chat_id, text=chunk)
                continue

            if event_type == "status":
                if not status_message_id or verbose == "off":
                    continue
                now = time.time()
                text = render_run_status(turn, verbose, state="running")
                if now - last_status_edit_at < settings.stream_edit_interval or text == last_status_text:
                    continue
                status_message_id = await update_stream_message(
                    application,
                    turn.chat_id,
                    status_message_id,
                    text,
                )
                last_status_edit_at = now
                last_status_text = text
                continue

            if event_type == "delta":
                if status_message_id and verbose != "off":
                    now = time.time()
                    text = render_run_status(turn, verbose, state="running")
                    if now - last_status_edit_at >= settings.stream_edit_interval and text != last_status_text:
                        status_message_id = await update_stream_message(
                            application,
                            turn.chat_id,
                            status_message_id,
                            text,
                        )
                        last_status_edit_at = now
                        last_status_text = text

                now = time.time()
                should_flush = (
                    not answer_message_ids
                    or len(turn.text) - last_answer_length >= STREAM_BUFFER_THRESHOLD
                    or now - last_answer_flush_at >= max(1.2, settings.stream_edit_interval * 2)
                )
                if not should_flush:
                    continue
                answer_message_ids, answer_chunks = await sync_text_bubbles(
                    application,
                    turn.chat_id,
                    answer_message_ids,
                    answer_chunks,
                    turn.text,
                    settings.max_message_chars,
                )
                last_answer_flush_at = now
                last_answer_length = len(turn.text)
                continue

            if event_type == "done":
                final_text = event["text"].strip() or "已完成。"
                answer_message_ids, answer_chunks = await sync_text_bubbles(
                    application,
                    turn.chat_id,
                    answer_message_ids,
                    answer_chunks,
                    final_text,
                    settings.max_message_chars,
                )
                if status_message_id and verbose != "off":
                    done_text = render_run_status(turn, verbose, state="done")
                    if done_text:
                        await update_stream_message(
                            application,
                            turn.chat_id,
                            status_message_id,
                            done_text,
                        )
                if not answer_message_ids:
                    for chunk in split_message(final_text, settings.max_message_chars):
                        await application.bot.send_message(chat_id=turn.chat_id, text=chunk)
                return

            if event_type == "error":
                error_text = trim_for_stream(f"执行失败\n\n{event['text']}", settings.max_message_chars)
                if status_message_id and verbose != "off":
                    fail_text = render_run_status(turn, verbose, state="error")
                    if fail_text:
                        await update_stream_message(
                            application,
                            turn.chat_id,
                            status_message_id,
                            fail_text,
                        )
                await application.bot.send_message(chat_id=turn.chat_id, text=error_text)
                return

            if event_type == "interrupted":
                if status_message_id and verbose != "off":
                    stop_text = render_run_status(turn, verbose, state="interrupted")
                    if stop_text:
                        await update_stream_message(
                            application,
                            turn.chat_id,
                            status_message_id,
                            stop_text,
                        )
                await application.bot.send_message(chat_id=turn.chat_id, text=event["text"])
                return
    except Exception as exc:
        await application.bot.send_message(
            chat_id=turn.chat_id,
            text=f"桥接失败\n\n{type(exc).__name__}: {exc}",
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    health: PollingHealth = context.application.bot_data["health"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    current_repo = resolve_repo_key(chat_id, settings, store)
    repo_lines = []
    for name, path in settings.repos.items():
        marker = " [当前]" if name == current_repo else ""
        repo_lines.append(f"{name}{marker} -> {path}")
    effective_model, effective_effort = await resolve_effective_runtime_info(
        settings,
        store,
        client,
        chat_id,
        current_repo,
    )
    current_model = effective_model or settings.codex_model or "gpt-5.4"
    current_effort = format_effort_name(effective_effort or settings.codex_reasoning_effort)
    current_sandbox = get_chat_sandbox(settings, store, chat_id)
    current_verbose = get_chat_verbose(store, chat_id)

    text = "\n".join(
        [
            "现在是会话式模式。",
            "直接发普通文本，就会在同一个 Codex 线程里继续聊。",
            "",
            "命令",
            "/repos 查看仓库 / List repos",
            "/repo <名称> 切仓库并新开线程 / Switch repo",
            "/new 丢掉当前上下文，重新开聊 / New thread",
            "/threads 查看这个 chat 的历史线程 / Show chat threads",
            "/threads all 查看同仓库更多线程 / Show repo threads",
            "/use <序号或 thread_id> 切回旧线程 / Resume thread",
            "/history [序号或 thread_id] 导出线程全文 / Export history",
            "/summary [序号或 thread_id] 压缩线程上下文 / Summarize thread",
            "/archive 归档当前线程 / Archive current thread",
            "/cleanup_threads 批量归档这个 chat 的旧线程 / Cleanup old threads",
            "/verbose [off|thinking|new|all|verbose] 设置消息显示档位 / Display mode",
            "/access [default|full] 切换访问权限 / Access mode",
            "/model [模型名] 查看或切换模型 / Model",
            "/effort [minimal|low|medium|high|xhigh] 查看或切换思考强度 / Effort",
            "/status 看当前线程 / Status",
            "/stop 中断当前回复 / Stop",
            "",
            "仓库",
            *repo_lines,
            "",
            f"model: {current_model}",
            f"effort: {current_effort}",
            f"verbose: {current_verbose}",
            f"access: {describe_access_mode(settings, current_sandbox)}",
            f"sandbox: {current_sandbox}",
            f"polling: {format_polling_health(health)}",
            f"approval: {settings.codex_approval}",
        ]
    )
    await update.effective_message.reply_text(text, reply_markup=build_control_keyboard())


async def repos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    current = resolve_repo_key(update.effective_chat.id, settings, store)
    lines = ["仓库列表"]
    for name, path in settings.repos.items():
        marker = " [当前]" if name == current else ""
        lines.append(f"{name}{marker} -> {path}")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=build_control_keyboard(),
    )


async def repo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    if conversations.get_active(update.effective_chat.id):
        await update.effective_message.reply_text("当前还在回复，先 /stop。")
        return
    if not context.args:
        await update.effective_message.reply_text("用法: /repo <名称>")
        return
    repo_key = context.args[0].strip()
    if repo_key not in settings.repos:
        await update.effective_message.reply_text(
            "仓库不存在。\n" + "\n".join(settings.repos.keys())
        )
        return

    old_thread_id = store.get_thread_id(update.effective_chat.id)
    if old_thread_id and settings.auto_archive_on_new:
        try:
            await conversations.archive_current_thread(update.effective_chat.id)
        except RuntimeError as exc:
            await update.effective_message.reply_text(str(exc))
            return

    store.set_repo_key(update.effective_chat.id, repo_key)
    await update.effective_message.reply_text(
        f"已切到 {repo_key}\n{settings.repos[repo_key]}\n上下文已重置。"
    )


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    try:
        archived = await conversations.archive_current_thread(update.effective_chat.id)
    except RuntimeError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if archived:
        await update.effective_message.reply_text("已新开一个空线程，旧线程已归档。")
        return
    await update.effective_message.reply_text("已新开一个空线程。")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    health: PollingHealth = context.application.bot_data["health"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    text = await build_status_text(
        settings,
        store,
        client,
        conversations,
        health,
        chat_id,
    )
    await update.effective_message.reply_text(text, reply_markup=build_control_keyboard())


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    stopped = await conversations.stop_chat_turn(update.effective_chat.id)
    if not stopped:
        await update.effective_message.reply_text("当前没有可中断的回复。")
        return
    await update.effective_message.reply_text("已中断。")


async def archive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    try:
        archived = await conversations.archive_current_thread(update.effective_chat.id)
    except RuntimeError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    if not archived:
        await update.effective_message.reply_text("当前没有可归档的线程。")
        return
    await update.effective_message.reply_text("已归档当前线程。")


async def access_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    if conversations.get_active(chat_id):
        await update.effective_message.reply_text("当前还在回复，先 /stop。")
        return

    current_sandbox = get_chat_sandbox(settings, store, chat_id)
    if not context.args:
        await update.effective_message.reply_text(
            "\n".join(
                [
                    f"当前访问权限: {describe_access_mode(settings, current_sandbox)}",
                    f"sandbox: {current_sandbox}",
                    "用法: /access <default|full>",
                    "default = 默认访问权限",
                    "full = 完全访问权限",
                ]
            )
        )
        return

    raw = context.args[0].strip().lower()
    if raw in {"default", "normal"}:
        sandbox = settings.codex_default_sandbox
    elif raw in {"full", "danger", "full-access"}:
        sandbox = "danger-full-access"
    else:
        await update.effective_message.reply_text(
            "访问权限无效。\n可用值: default, full"
        )
        return

    old_thread_id = store.get_thread_id(chat_id)
    if old_thread_id and settings.auto_archive_on_new:
        try:
            await conversations.archive_current_thread(chat_id)
        except RuntimeError as exc:
            await update.effective_message.reply_text(str(exc))
            return
    else:
        store.clear_thread_id(chat_id)
    store.set_sandbox(chat_id, sandbox)
    await update.effective_message.reply_text(
        f"已切换访问权限: {describe_access_mode(settings, sandbox)}\n"
        f"sandbox: {sandbox}\n"
        "下一条消息会用新权限开线程。"
    )


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    if conversations.get_active(chat_id):
        await update.effective_message.reply_text("当前还在回复，先 /stop。")
        return

    if not context.args:
        repo_key = resolve_repo_key(chat_id, settings, store)
        effective_model, _ = await resolve_effective_runtime_info(
            settings,
            store,
            client,
            chat_id,
            repo_key,
        )
        current = effective_model or "默认"
        suggested = "\n".join(SUGGESTED_CODEX_MODELS)
        await update.effective_message.reply_text(
            "\n".join(
                [
                    f"当前模型: {current}",
                    "用法: /model <模型名>",
                    "恢复默认: /model default",
                    f"默认模型: {settings.codex_model or 'gpt-5.4'}",
                    "",
                    "常用模型",
                    suggested,
                ]
            )
        )
        return

    raw = context.args[0].strip()
    if raw.lower() == "list":
        await update.effective_message.reply_text(
            "常用模型\n"
            + "\n".join(SUGGESTED_CODEX_MODELS)
            + "\n\n说明: Codex 还能接收 Responses API 可用的其他模型名。"
        )
        return
    model = None if raw.lower() in {"default", "reset", "auto"} else raw
    old_thread_id = store.get_thread_id(chat_id)
    if old_thread_id and settings.auto_archive_on_new:
        try:
            await conversations.archive_current_thread(chat_id)
        except RuntimeError as exc:
            await update.effective_message.reply_text(str(exc))
            return
    else:
        store.clear_thread_id(chat_id)
    store.set_model(chat_id, model)
    target_model = model or settings.codex_model or "gpt-5.4"
    await update.effective_message.reply_text(
        f"已切换模型: {target_model}\n下一条消息会用新配置开线程。"
    )


async def effort_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    if conversations.get_active(chat_id):
        await update.effective_message.reply_text("当前还在回复，先 /stop。")
        return

    if not context.args:
        repo_key = resolve_repo_key(chat_id, settings, store)
        _, effective_effort = await resolve_effective_runtime_info(
            settings,
            store,
            client,
            chat_id,
            repo_key,
        )
        current = format_effort_name(effective_effort or settings.codex_reasoning_effort)
        allowed = "|".join(VALID_REASONING_EFFORTS)
        await update.effective_message.reply_text(
            "\n".join(
                [
                    f"当前思考强度: {current}",
                    f"用法: /effort <{allowed}>",
                    "恢复默认: /effort default",
                    "常用档位: low(低), medium(中), high(高), xhigh(超高)",
                ]
            )
        )
        return

    raw = context.args[0].strip().lower()
    if raw in {"default", "reset", "auto"}:
        effort = None
    elif raw in VALID_REASONING_EFFORTS:
        effort = raw
    else:
        await update.effective_message.reply_text(
            "思考强度无效。\n可用值: minimal, low, medium, high, xhigh"
        )
        return

    old_thread_id = store.get_thread_id(chat_id)
    if old_thread_id and settings.auto_archive_on_new:
        try:
            await conversations.archive_current_thread(chat_id)
        except RuntimeError as exc:
            await update.effective_message.reply_text(str(exc))
            return
    else:
        store.clear_thread_id(chat_id)
    store.set_reasoning_effort(chat_id, effort)
    target_effort = format_effort_name(effort or settings.codex_reasoning_effort)
    await update.effective_message.reply_text(
        f"已切换思考强度: {target_effort}\n下一条消息会用新配置开线程。"
    )


async def verbose_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    current = get_chat_verbose(store, chat_id)
    if not context.args:
        await update.effective_message.reply_text(
            "\n".join(
                [
                    f"当前进度显示: {current}",
                    "用法: /verbose <off|thinking|new|all|verbose>",
                    "off = 只看最终答案",
                    "thinking = thinking 气泡 + 回答气泡",
                    "new = 再加命令和改文件气泡",
                    "all = 再加上下文工具气泡",
                    "verbose = 最细工具进度",
                ]
            )
        )
        return

    raw = context.args[0].strip().lower()
    if raw not in VALID_VERBOSE_LEVELS:
        await update.effective_message.reply_text(
            "显示档位无效。\n可用值: off, thinking, new, all, verbose"
        )
        return
    store.set_verbose_level(chat_id, raw)
    await update.effective_message.reply_text(
        f"已切换进度显示: {raw}\n下一条消息开始生效。"
    )


async def cleanup_threads_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    repo_key = resolve_repo_key(chat_id, settings, store)
    if not repo_key:
        await update.effective_message.reply_text("先用 /repo <名称> 选一个仓库。")
        return

    history_ids = store.get_thread_history(chat_id)
    if not history_ids:
        await update.effective_message.reply_text("这个 chat 还没有可清理的历史线程。")
        return

    try:
        active_threads = await client.list_threads(
            cwd=settings.repos[repo_key],
            archived=False,
            limit=200,
        )
        archived_threads = await client.list_threads(
            cwd=settings.repos[repo_key],
            archived=True,
            limit=200,
        )
    except Exception as exc:
        await update.effective_message.reply_text(
            f"清理失败\n{type(exc).__name__}: {exc}"
        )
        return
    active_ids = {item["id"] for item in active_threads}
    archived_ids = {item["id"] for item in archived_threads}

    current_thread_id = store.get_thread_id(chat_id)
    current_active = conversations.get_active(chat_id)
    archived_count = 0
    already_archived = 0
    skipped_current = 0
    missing_count = 0
    failed: list[str] = []

    for thread_id in history_ids:
        if thread_id == current_thread_id:
            skipped_current += 1
            continue
        if thread_id in archived_ids:
            already_archived += 1
            continue
        if thread_id not in active_ids:
            missing_count += 1
            continue
        try:
            await client.archive_thread(thread_id)
            archived_count += 1
        except Exception:
            failed.append(thread_id)

    lines = ["清理完成", f"已归档: {archived_count}"]
    if already_archived:
        lines.append(f"本来就已归档: {already_archived}")
    if skipped_current:
        suffix = "（当前线程正在回复）" if current_active else "（当前线程保留）"
        lines.append(f"跳过当前线程: {skipped_current}{suffix}")
    if missing_count:
        lines.append(f"未找到: {missing_count}")
    if failed:
        lines.append(f"归档失败: {len(failed)}")
        lines.extend(failed[:5])
    await update.effective_message.reply_text("\n".join(lines))


async def threads_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    repo_key = resolve_repo_key(chat_id, settings, store)
    if not repo_key:
        await update.effective_message.reply_text("先用 /repo <名称> 选一个仓库。")
        return

    history_ids = store.get_thread_history(chat_id)
    mode = (context.args[0].strip().lower() if context.args else "")
    if mode not in {"", "all", "repo"}:
        await update.effective_message.reply_text("用法: /threads [all|repo]")
        return

    lines = ["历史线程"]
    current_thread_id = store.get_thread_id(chat_id)
    shown = 0
    ordered_threads = await get_repo_thread_list(client, settings.repos[repo_key], history_ids)
    mine_threads, repo_recent_threads = split_thread_sections(ordered_threads, history_ids)
    display_threads: list[dict[str, Any]] = []

    def append_threads(title: str, items: list[dict[str, Any]]) -> None:
        nonlocal shown
        if not items or shown >= 15:
            return
        lines.append(title)
        for thread in items:
            if shown >= 15:
                break
            thread_id = thread["id"]
            shown += 1
            display_threads.append(thread)
            status = thread.get("status", {})
            marker_parts: list[str] = []
            if thread_id == current_thread_id:
                marker_parts.append("当前")
            if status.get("type") == "archived":
                marker_parts.append("已归档")
            marker = f" [{' / '.join(marker_parts)}]" if marker_parts else ""
            preview = format_thread_preview(thread, thread_id)
            lines.append(f"{shown}. {preview}{marker}")
            lines.append(f"{thread_id}")

    if mode == "repo":
        append_threads("当前仓库最近线程", repo_recent_threads)
    elif mode == "all":
        append_threads("这个 chat 的历史", mine_threads)
        append_threads("当前仓库最近线程", repo_recent_threads)
    else:
        append_threads("这个 chat 的历史", mine_threads)
        if not mine_threads:
            append_threads("当前仓库最近线程", repo_recent_threads)

    if shown == 0:
        await update.effective_message.reply_text("这个 chat 还没有可切回的线程。")
        return
    if mode == "" and mine_threads and repo_recent_threads:
        lines.append("")
        lines.append(f"同仓库还有 {len(repo_recent_threads)} 条线程。用 /threads all 查看。")
    lines.append("")
    lines.append("用法: /use <序号或 thread_id>")
    lines.append("/history <序号或 thread_id> 导出全文")
    lines.append("/summary <序号或 thread_id> 压缩上下文")
    await update.effective_message.reply_text("\n".join(lines))
    for index, thread in enumerate(display_threads[:5], start=1):
        thread_id = thread["id"]
        marker = "当前线程" if thread_id == current_thread_id else "历史线程"
        preview = format_thread_preview(thread, thread_id)
        await update.effective_message.reply_text(
            f"{index}. {preview}\n{thread_id}\n{marker}",
            reply_markup=build_thread_keyboard(index, thread_id),
        )


async def use_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    repo_key = resolve_repo_key(chat_id, settings, store)
    if not repo_key:
        await update.effective_message.reply_text("先用 /repo <名称> 选一个仓库。")
        return
    if not context.args:
        await update.effective_message.reply_text("用法: /use <序号或 thread_id>")
        return

    raw = context.args[0].strip()
    thread_id = raw
    if raw.isdigit():
        client: CodexAppServerClient = context.application.bot_data["client"]
        ordered_threads = await get_repo_thread_list(
            client,
            settings.repos[repo_key],
            store.get_thread_history(chat_id),
        )
        thread = resolve_thread_selector(raw, ordered_threads)
        if not thread:
            await update.effective_message.reply_text(
                format_thread_selection_error(raw, ordered_threads)
            )
            return
        thread_id = thread["id"]

    try:
        resumed_id = await conversations.switch_thread(chat_id, repo_key, thread_id)
    except RuntimeError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception as exc:
        store.remove_thread_from_history(chat_id, thread_id)
        await update.effective_message.reply_text(
            format_switch_thread_error(exc, thread_id)
        )
        return

    await update.effective_message.reply_text(f"已切回线程\n{resumed_id}")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    repo_key = resolve_repo_key(chat_id, settings, store)
    if not repo_key:
        await update.effective_message.reply_text("先用 /repo <名称> 选一个仓库。")
        return

    selector = " ".join(context.args).strip()
    if selector and not selector.isdigit():
        thread = build_thread_stub(selector)
    else:
        ordered_threads = await get_repo_thread_list(
            client,
            settings.repos[repo_key],
            store.get_thread_history(chat_id),
        )
        if not ordered_threads:
            await update.effective_message.reply_text("这个 chat 还没有可导出的线程。")
            return
        if selector:
            thread = resolve_thread_selector(selector, ordered_threads)
            if not thread:
                await update.effective_message.reply_text(
                    format_thread_selection_error(selector, ordered_threads)
                )
                return
        else:
            current_thread_id = store.get_thread_id(chat_id)
            thread = resolve_thread_selector(current_thread_id or "", ordered_threads)
            if not thread and current_thread_id:
                thread = build_thread_stub(current_thread_id)
            if not thread:
                await update.effective_message.reply_text("当前没有已绑定线程。先聊一句，或用 /threads 选一个。")
                return

    session_path, path_error = resolve_thread_session_path(thread)
    if path_error:
        await update.effective_message.reply_text(path_error)
        return

    transcript = extract_thread_transcript(session_path)
    if not transcript:
        await update.effective_message.reply_text("这个线程里还没有可导出的用户/助手消息。")
        return

    text = format_thread_transcript(transcript, thread)
    for chunk in split_message(text, settings.max_message_chars):
        await update.effective_message.reply_text(chunk)


async def summary_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    repo_key = resolve_repo_key(chat_id, settings, store)
    if not repo_key:
        await update.effective_message.reply_text("先用 /repo <名称> 选一个仓库。")
        return
    if conversations.get_active(chat_id):
        await update.effective_message.reply_text("当前还在回复，先 /stop。")
        return

    selector = " ".join(context.args).strip()
    if selector and not selector.isdigit():
        thread = build_thread_stub(selector)
    else:
        ordered_threads = await get_repo_thread_list(
            client,
            settings.repos[repo_key],
            store.get_thread_history(chat_id),
        )
        if not ordered_threads:
            await update.effective_message.reply_text("这个 chat 还没有可总结的线程。")
            return
        if selector:
            thread = resolve_thread_selector(selector, ordered_threads)
            if not thread:
                await update.effective_message.reply_text(
                    format_thread_selection_error(selector, ordered_threads)
                )
                return
        else:
            current_thread_id = store.get_thread_id(chat_id)
            thread = resolve_thread_selector(current_thread_id or "", ordered_threads)
            if not thread and current_thread_id:
                thread = build_thread_stub(current_thread_id)
            if not thread:
                await update.effective_message.reply_text("当前没有已绑定线程。先聊一句，或用 /threads 选一个。")
                return

    session_path, path_error = resolve_thread_session_path(thread)
    if path_error:
        await update.effective_message.reply_text(path_error)
        return

    transcript = extract_thread_transcript(session_path)
    if not transcript:
        await update.effective_message.reply_text("这个线程里还没有可总结的用户/助手消息。")
        return

    placeholder = await update.effective_message.reply_text("正在压缩上下文…")
    fallback_summary = inject_current_status_note(
        build_fallback_summary(transcript, thread),
        "当前缺失信息：正在等待模型精炼",
    )
    initial_chunks = split_message(fallback_summary, settings.max_message_chars)
    if initial_chunks:
        await edit_message_safe(
            context.application,
            chat_id,
            placeholder.message_id,
            initial_chunks[0],
        )
        for chunk in initial_chunks[1:]:
            await context.application.bot.send_message(chat_id=chat_id, text=chunk)
    try:
        summary = await asyncio.wait_for(
            summarize_transcript_with_model(
                client,
                settings.repos[repo_key],
                build_thread_runtime_config(settings, store, chat_id),
                transcript,
            ),
            timeout=SUMMARY_MODEL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await edit_message_safe(
            context.application,
            chat_id,
            placeholder.message_id,
            inject_current_status_note(
                build_fallback_summary(transcript, thread),
                "当前缺失信息：模型摘要超时，当前显示本地摘要",
            ),
        )
        return
    except Exception as exc:
        await edit_message_safe(
            context.application,
            chat_id,
            placeholder.message_id,
            inject_current_status_note(
                build_fallback_summary(transcript, thread),
                f"当前缺失信息：模型摘要失败，{type(exc).__name__}",
            ),
        )
        return

    chunks = split_message(summary or "没有拿到总结结果。", settings.max_message_chars)
    if chunks:
        await edit_message_safe(
            context.application,
            chat_id,
            placeholder.message_id,
            chunks[0],
        )
        for chunk in chunks[1:]:
            await context.application.bot.send_message(chat_id=chat_id, text=chunk)


async def dispatch_chat_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    prompt: str,
    force_new: bool = False,
) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    conversations: ConversationManager = context.application.bot_data["conversations"]

    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)

    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    prompt = prompt.strip()
    if not prompt:
        await update.effective_message.reply_text("发点实际内容。")
        return
    if len(prompt) > settings.max_prompt_chars:
        await update.effective_message.reply_text(
            f"消息太长，当前限制 {settings.max_prompt_chars} 字。"
        )
        return

    repo_key = resolve_repo_key(chat_id, settings, store)
    if not repo_key:
        await update.effective_message.reply_text("先用 /repo <名称> 选一个仓库。")
        return

    active = conversations.get_active(chat_id)
    if active and not force_new:
        stopped = await conversations.stop_chat_turn(chat_id)
        if stopped:
            await asyncio.sleep(0.2)

    try:
        turn = await conversations.start_chat_turn(
            chat_id,
            repo_key,
            prompt,
            force_new=force_new,
        )
    except RuntimeError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception as exc:
        await update.effective_message.reply_text(f"启动失败\n{type(exc).__name__}: {exc}")
        return

    placeholder_id: int | None = None
    if get_chat_verbose(store, chat_id) != "off":
        placeholder = await update.effective_message.reply_text("思考中…")
        placeholder_id = placeholder.message_id
    asyncio.create_task(
        stream_turn_to_telegram(
            context.application,
            turn,
            placeholder_id,
            settings,
        )
    )


async def task_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await dispatch_chat_message(
        update,
        context,
        prompt=" ".join(context.args),
        force_new=True,
    )


async def continue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await dispatch_chat_message(
        update,
        context,
        prompt=" ".join(context.args),
        force_new=False,
    )


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return
    await dispatch_chat_message(update, context, prompt=message.text)


async def media_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not ensure_authorized(update, settings):
        await reject_unauthorized(update)
        return
    mark_poll_ok(context.application)
    await bootstrap_notice(update, settings)

    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    upload_dir = settings.state_dir / "uploads" / str(chat.id)
    attachments: list[tuple[str, Path, str | None]] = []
    caption = (message.caption or "").strip()

    if message.photo:
        photo = message.photo[-1]
        saved = await save_telegram_file(
            context,
            photo.file_id,
            upload_dir,
            f"{photo.file_unique_id}.jpg",
        )
        attachments.append(("图片", saved, None))

    if message.document:
        document = message.document
        saved = await save_telegram_file(
            context,
            document.file_id,
            upload_dir,
            document.file_name or f"{document.file_unique_id}.bin",
        )
        attachments.append(("文件", saved, maybe_inline_file_preview(saved)))

    if not attachments:
        await message.reply_text("这类附件我还没识别到。")
        return

    await dispatch_chat_message(
        update,
        context,
        prompt=build_attachment_prompt(user_text=caption, attachments=attachments),
        force_new=False,
    )


async def thread_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    query = update.callback_query
    if not query or not update.effective_chat:
        return
    if not ensure_authorized(update, settings):
        await query.answer("未授权", show_alert=True)
        return
    mark_poll_ok(context.application)
    await query.answer()

    data = (query.data or "").split(":")
    if len(data) < 3 or data[0] != THREAD_CALLBACK_PREFIX:
        return

    action = data[1]
    selector = data[2]
    context.args = [selector]
    if action == "use":
        await use_command(update, context)
        return
    if action == "summary":
        await summary_command(update, context)
        return
    if action == "history":
        await history_command(update, context)
        return
    if action == "archive":
        if conversations.get_active(update.effective_chat.id):
            await query.message.reply_text("当前还在回复，先 /stop。")
            return
        repo_key = resolve_repo_key(update.effective_chat.id, settings, store)
        if not repo_key:
            await query.message.reply_text("先用 /repo <名称> 选一个仓库。")
            return
        client: CodexAppServerClient = context.application.bot_data["client"]
        try:
            await client.archive_thread(selector)
            if store.get_thread_id(update.effective_chat.id) == selector:
                store.clear_thread_id(update.effective_chat.id)
            await query.message.reply_text(f"已归档线程\n{selector}")
        except Exception as exc:
            await query.message.reply_text(f"归档失败\n{type(exc).__name__}: {exc}")


async def control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    store: SessionStore = context.application.bot_data["store"]
    client: CodexAppServerClient = context.application.bot_data["client"]
    conversations: ConversationManager = context.application.bot_data["conversations"]
    health: PollingHealth = context.application.bot_data["health"]
    query = update.callback_query
    if not query or not update.effective_chat:
        return
    if not ensure_authorized(update, settings):
        await query.answer("未授权", show_alert=True)
        return
    mark_poll_ok(context.application)
    await query.answer()

    parts = (query.data or "").split(":")
    if len(parts) < 2 or parts[0] != "control":
        return

    action = parts[1]
    chat_id = update.effective_chat.id

    if action == "status":
        text = await build_status_text(
            settings,
            store,
            client,
            conversations,
            health,
            chat_id,
        )
        try:
            await query.edit_message_text(text=text, reply_markup=build_control_keyboard())
        except BadRequest:
            pass
        return

    if action == "stop":
        context.args = []
        await stop_command(update, context)
        return

    if action == "new":
        context.args = []
        await new_command(update, context)
        return

    if action == "access" and len(parts) >= 3:
        context.args = [parts[2]]
        await access_command(update, context)
        return

    if action == "model" and len(parts) >= 3:
        context.args = [parts[2]]
        await model_command(update, context)
        return

    if action == "effort" and len(parts) >= 3:
        context.args = [parts[2]]
        await effort_command(update, context)
        return


async def register_telegram_menu(application: Application) -> None:
    # 让 Telegram 客户端左下角的 Menu 和 "/" 提示都显示这份命令表。
    commands = [BotCommand(name, desc) for name, desc in telegram_menu_commands()]
    try:
        await application.bot.set_my_commands(commands)
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except Exception:
        logging.getLogger("telegram.menu").exception("注册 Telegram 菜单失败")
    health: PollingHealth | None = application.bot_data.get("health")
    if health:
        health.last_ok_at = time.time()
    if application.job_queue:
        application.job_queue.run_repeating(
            monitor_polling_health,
            interval=30,
            first=30,
            name="polling-health",
        )


async def monitor_polling_health(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    settings: Settings = application.bot_data["settings"]
    health: PollingHealth = application.bot_data["health"]
    if not health.last_conflict_at:
        return
    if health.alerted_conflict:
        return
    if health.last_ok_at and health.last_ok_at > health.last_conflict_at:
        return
    text = (
        "检测到 Telegram 轮询冲突。\n"
        "当前 token 可能同时被别的实例占用。\n"
        "建议检查其他机器、测试脚本或旧进程。"
    )
    for chat_id in settings.allowed_chat_ids:
        try:
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            continue
    health.alerted_conflict = True


async def telegram_error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    application = context.application
    health: PollingHealth = application.bot_data["health"]
    error = context.error
    if isinstance(error, Conflict):
        health.last_conflict_at = time.time()
        health.last_error_text = str(error)
        return
    if error:
        health.last_error_text = f"{type(error).__name__}: {error}"


def build_application(settings: Settings) -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(register_telegram_menu)
        .build()
    )
    store = SessionStore(settings.state_dir / "sessions.json")
    client = CodexAppServerClient(settings)
    conversations = ConversationManager(settings, store, client)
    health = PollingHealth()

    application.bot_data["settings"] = settings
    application.bot_data["store"] = store
    application.bot_data["client"] = client
    application.bot_data["conversations"] = conversations
    application.bot_data["health"] = health

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("repos", repos_command))
    application.add_handler(CommandHandler("repo", repo_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("threads", threads_command))
    application.add_handler(CommandHandler("use", use_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("summary", summary_command))
    application.add_handler(CommandHandler("archive", archive_command))
    application.add_handler(CommandHandler("cleanup_threads", cleanup_threads_command))
    application.add_handler(CommandHandler("verbose", verbose_command))
    application.add_handler(CommandHandler("access", access_command))
    application.add_handler(CommandHandler("model", model_command))
    application.add_handler(CommandHandler("effort", effort_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("task", task_command))
    application.add_handler(CommandHandler("continue", continue_command))
    application.add_handler(CallbackQueryHandler(thread_callback, pattern=r"^thread:"))
    application.add_handler(CallbackQueryHandler(control_callback, pattern=r"^control:"))
    application.add_handler(
        MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, media_message)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_message)
    )
    application.add_error_handler(telegram_error_handler)
    return application


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    acquire_single_instance_lock()
    settings = Settings.load()
    application = build_application(settings)
    application.run_polling()


if __name__ == "__main__":
    main()
