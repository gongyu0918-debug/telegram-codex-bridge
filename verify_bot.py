from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, os.getcwd())

import bot


class FakeSentMessage:
    def __init__(self, fake_bot: "FakeBot", chat_id: int, message_id: int, text: str = "") -> None:
        self._fake_bot = fake_bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.text = text
        self.caption = ""

    async def reply_text(self, text: str, reply_markup=None) -> "FakeSentMessage":
        return await self._fake_bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_markup=reply_markup,
        )


class FakeBot:
    def __init__(self) -> None:
        self._next_message_id = 1
        self.sent_records: list[tuple[int, str]] = []
        self.edited_records: list[tuple[int, str]] = []
        self.messages: dict[int, str] = {}

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> FakeSentMessage:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.sent_records.append((message_id, text))
        self.messages[message_id] = text
        return FakeSentMessage(self, chat_id, message_id, text)

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup=None,
    ) -> FakeSentMessage:
        self.edited_records.append((message_id, text))
        self.messages[message_id] = text
        return FakeSentMessage(self, chat_id, message_id, text)

    def all_text(self) -> str:
        sent = [text for _, text in self.sent_records]
        edited = [text for _, text in self.edited_records]
        return "\n\n".join(sent + edited)


class FakeCallbackQuery:
    def __init__(self, fake_bot: FakeBot, data: str, message: FakeSentMessage) -> None:
        self._fake_bot = fake_bot
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text: str, reply_markup=None) -> FakeSentMessage:
        return await self._fake_bot.edit_message_text(
            chat_id=self.message.chat_id,
            message_id=self.message.message_id,
            text=text,
            reply_markup=reply_markup,
        )


def make_application(
    settings: bot.Settings,
    store: bot.SessionStore,
    client: bot.CodexAppServerClient,
    conversations: bot.ConversationManager,
) -> SimpleNamespace:
    return SimpleNamespace(
        bot_data={
            "settings": settings,
            "store": store,
            "client": client,
            "conversations": conversations,
            "health": bot.PollingHealth(last_ok_at=time.time()),
        },
        bot=None,
        job_queue=None,
    )


async def run_command(
    handler,
    application: SimpleNamespace,
    chat_id: int,
    *,
    args: list[str] | None = None,
    text: str = "/verify",
) -> FakeBot:
    fake_bot = FakeBot()
    application.bot = fake_bot
    message = FakeSentMessage(fake_bot, chat_id, 0, text)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
        callback_query=None,
    )
    context = SimpleNamespace(application=application, args=list(args or []))
    await handler(update, context)
    return fake_bot


async def run_callback(
    handler,
    application: SimpleNamespace,
    chat_id: int,
    *,
    data: str,
) -> tuple[FakeBot, FakeCallbackQuery]:
    fake_bot = FakeBot()
    application.bot = fake_bot
    base_message = await fake_bot.send_message(chat_id=chat_id, text="callback")
    query = FakeCallbackQuery(fake_bot, data, base_message)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=base_message,
        callback_query=query,
    )
    context = SimpleNamespace(application=application, args=[])
    await handler(update, context)
    return fake_bot, query


async def verify() -> int:
    settings = bot.Settings.load()
    original_store = bot.SessionStore(settings.state_dir / "sessions.json")
    chat_ids = sorted(settings.allowed_chat_ids)
    if chat_ids:
        chat_id = chat_ids[0]
    else:
        chat_prefs = original_store.data.get("chat_prefs", {})
        if not chat_prefs:
            print("FAIL")
            print("- verify: 没有可用 chat_id，先配置 ALLOWED_CHAT_IDS 或至少聊过一次")
            return 1
        chat_id = int(next(iter(chat_prefs.keys())))

    with tempfile.TemporaryDirectory(prefix="telegram_codex_bridge_verify_") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        temp_store_path = temp_dir / "sessions.json"
        temp_store_path.write_text(
            json.dumps(original_store.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        store = bot.SessionStore(temp_store_path)
        client = bot.CodexAppServerClient(settings)
        conversations = bot.ConversationManager(settings, store, client)
        application = make_application(settings, store, client, conversations)

        repo_key = store.get_repo_key(chat_id)
        if repo_key not in settings.repos:
            repo_key = next(iter(settings.repos))
        repo_path = settings.repos[repo_key]

        failures: list[str] = []

        local_threads = bot.list_local_threads(repo_path, store.get_thread_history(chat_id), limit=15)
        if not local_threads:
            failures.append("threads: 当前仓库没有本地线程")
            target_thread = None
        else:
            print(f"threads: ok ({len(local_threads)})")
            mine_threads, repo_recent_threads = bot.split_thread_sections(
                local_threads,
                store.get_thread_history(chat_id),
            )
            if store.get_thread_history(chat_id) and not mine_threads:
                failures.append("threads(split): 没有命中这个 chat 的历史线程")
            else:
                print(f"threads(split): ok mine={len(mine_threads)} repo={len(repo_recent_threads)}")
            target_thread = local_threads[0]
            if bot.resolve_thread_selector("2", local_threads[:2]) is None and len(local_threads) >= 2:
                failures.append("use(selector): 数字选择器失败")
            else:
                print("use(selector): ok")

        if target_thread:
            session_path, path_error = bot.resolve_thread_session_path(target_thread)
            if path_error or not session_path:
                failures.append(f"history(path): {path_error or '未知错误'}")
            else:
                print("history(path): ok")
                transcript = bot.extract_thread_transcript(session_path)
                if not transcript:
                    failures.append("history(transcript): 没有用户/助手消息")
                else:
                    print(f"history(transcript): ok ({len(transcript)})")
                    summary = bot.build_fallback_summary(transcript, target_thread)
                    if "目标:" not in summary or "下一步:" not in summary:
                        failures.append("summary(fallback): 模板缺失")
                    else:
                        print("summary(fallback): ok")

            try:
                resumed = await conversations.switch_thread(chat_id, repo_key, target_thread["id"])
            except Exception as exc:
                failures.append(f"use(resume): {type(exc).__name__}: {exc}")
            else:
                if resumed != target_thread["id"]:
                    failures.append("use(resume): 恢复结果不匹配")
                else:
                    print("use(resume): ok")

            try:
                first = await conversations.start_chat_turn(
                    chat_id,
                    repo_key,
                    "请连续输出 1 到 120 的数字，每行一个。",
                )
                await asyncio.sleep(0.5)
                stopped = await conversations.stop_chat_turn(chat_id)
                second = await conversations.start_chat_turn(
                    chat_id,
                    repo_key,
                    "只回复 INTERRUPT_OK",
                )
            except Exception as exc:
                failures.append(f"interrupt(chain): {type(exc).__name__}: {exc}")
            else:
                if not stopped:
                    failures.append("interrupt(chain): stop_chat_turn 返回 False")
                elif first.thread_id != second.thread_id:
                    failures.append("interrupt(chain): 插话后线程发生变化")
                else:
                    print("interrupt(chain): ok")
                await conversations.stop_chat_turn(chat_id)

        try:
            model, effort = await bot.resolve_effective_runtime_info(
                settings,
                store,
                client,
                chat_id,
                repo_key,
            )
        except Exception as exc:
            failures.append(f"status(runtime): {type(exc).__name__}: {exc}")
        else:
            print(f"status(runtime): ok model={model or 'default'} effort={effort or 'default'}")

        if settings.codex_model != "gpt-5.4":
            failures.append(f"default(model): 当前默认模型是 {settings.codex_model!r}")
        else:
            print("default(model): ok")
        if settings.codex_reasoning_effort != "medium":
            failures.append(
                f"default(effort): 当前默认思考强度是 {settings.codex_reasoning_effort!r}"
            )
        else:
            print("default(effort): ok")
        available_models = bot.get_available_models()
        if not available_models:
            failures.append("models(sync): 没有读到本机模型列表")
        else:
            print(f"models(sync): ok ({len(available_models)})")
        if "medium" not in bot.get_supported_efforts_for_model("gpt-5.4"):
            failures.append("efforts(sync): gpt-5.4 没有 medium")
        else:
            print("efforts(sync): ok")

        if target_thread:
            original_archive_current_thread = conversations.archive_current_thread
            original_start_chat_turn = conversations.start_chat_turn
            original_stop_chat_turn = conversations.stop_chat_turn
            original_stream_turn = bot.stream_turn_to_telegram
            original_client_list_threads = client.list_threads
            original_client_archive_thread = client.archive_thread
            original_summarizer = bot.summarize_transcript_with_model

            async def fake_archive_current_thread(chat_id_arg: int) -> bool:
                store.clear_thread_id(chat_id_arg)
                return True

            async def fake_start_chat_turn(
                chat_id_arg: int,
                repo_key_arg: str,
                prompt: str,
                force_new: bool = False,
            ) -> bot.ActiveTurn:
                return bot.ActiveTurn(
                    chat_id=chat_id_arg,
                    repo_key=repo_key_arg,
                    repo_path=settings.repos[repo_key_arg],
                    thread_id="verify-thread",
                    turn_id="verify-turn",
                    prompt=prompt,
                )

            async def fake_stop_chat_turn(chat_id_arg: int) -> bool:
                return True

            async def fake_stream_turn_to_telegram(application_arg, turn_arg, message_id_arg, settings_arg) -> None:
                return

            async def fake_list_threads(cwd: Path, archived: bool = False, limit: int = 200):
                history_ids = store.get_thread_history(chat_id)
                if archived:
                    return []
                return [{"id": item} for item in history_ids]

            async def fake_archive_thread(thread_id: str) -> None:
                return None

            async def fake_summarizer(client_arg, repo_path_arg, runtime_config_arg, transcript_arg):
                return "\n".join(
                    [
                        "目标:",
                        "- 验证 /summary 命令",
                        "",
                        "已完成:",
                        "- 已读取线程消息",
                        "",
                        "当前状态:",
                        "- 验证脚本替换了模型摘要",
                        "",
                        "下一步:",
                        "- 继续回到原线程聊天",
                    ]
                )

            conversations.archive_current_thread = fake_archive_current_thread  # type: ignore[assignment]
            conversations.start_chat_turn = fake_start_chat_turn  # type: ignore[assignment]
            conversations.stop_chat_turn = fake_stop_chat_turn  # type: ignore[assignment]
            client.list_threads = fake_list_threads  # type: ignore[assignment]
            client.archive_thread = fake_archive_thread  # type: ignore[assignment]
            bot.stream_turn_to_telegram = fake_stream_turn_to_telegram  # type: ignore[assignment]
            bot.summarize_transcript_with_model = fake_summarizer  # type: ignore[assignment]

            try:
                checks: list[tuple[str, str]] = []

                start_bot = await run_command(bot.start_command, application, chat_id, text="/start")
                if "/summary" in start_bot.all_text():
                    checks.append(("command(start)", "ok"))
                else:
                    failures.append("command(start): 帮助文本缺少 /summary")

                repos_bot = await run_command(bot.repos_command, application, chat_id, text="/repos")
                if "仓库列表" in repos_bot.all_text():
                    checks.append(("command(repos)", "ok"))
                else:
                    failures.append("command(repos): 没有返回仓库列表")

                status_bot = await run_command(bot.status_command, application, chat_id, text="/status")
                if "model:" in status_bot.all_text() and "polling:" in status_bot.all_text():
                    checks.append(("command(status)", "ok"))
                else:
                    failures.append("command(status): 缺少 model 或 polling 字段")

                verbose_info_bot = await run_command(bot.verbose_command, application, chat_id, text="/verbose")
                if "thinking" in verbose_info_bot.all_text():
                    checks.append(("command(verbose-info)", "ok"))
                else:
                    failures.append("command(verbose-info): 没有显示 thinking 档位")

                verbose_set_bot = await run_command(
                    bot.verbose_command,
                    application,
                    chat_id,
                    args=["new"],
                    text="/verbose new",
                )
                if store.get_verbose_level(chat_id) == "new" and "已切换进度显示" in verbose_set_bot.all_text():
                    checks.append(("command(verbose-set)", "ok"))
                else:
                    failures.append("command(verbose-set): 切换到 new 失败")
                store.set_verbose_level(chat_id, "thinking")

                access_info_bot = await run_command(bot.access_command, application, chat_id, text="/access")
                if "当前访问权限" in access_info_bot.all_text():
                    checks.append(("command(access-info)", "ok"))
                else:
                    failures.append("command(access-info): 没有返回访问权限")

                access_set_bot = await run_command(
                    bot.access_command,
                    application,
                    chat_id,
                    args=["full"],
                    text="/access full",
                )
                if store.get_sandbox(chat_id) == "danger-full-access" and "已切换访问权限" in access_set_bot.all_text():
                    checks.append(("command(access-set)", "ok"))
                else:
                    failures.append("command(access-set): 切换 full 失败")

                model_info_bot = await run_command(bot.model_command, application, chat_id, text="/model")
                if "当前模型" in model_info_bot.all_text():
                    checks.append(("command(model-info)", "ok"))
                else:
                    failures.append("command(model-info): 没有返回当前模型")

                model_set_bot = await run_command(
                    bot.model_command,
                    application,
                    chat_id,
                    args=["gpt-5.3-codex"],
                    text="/model gpt-5.3-codex",
                )
                if store.get_model(chat_id) == "gpt-5.3-codex" and "已切换模型" in model_set_bot.all_text():
                    checks.append(("command(model-set)", "ok"))
                else:
                    failures.append("command(model-set): 模型切换失败")

                effort_info_bot = await run_command(bot.effort_command, application, chat_id, text="/effort")
                if "当前思考强度" in effort_info_bot.all_text():
                    checks.append(("command(effort-info)", "ok"))
                else:
                    failures.append("command(effort-info): 没有返回当前思考强度")

                effort_set_bot = await run_command(
                    bot.effort_command,
                    application,
                    chat_id,
                    args=["high"],
                    text="/effort high",
                )
                if store.get_reasoning_effort(chat_id) == "high" and "已切换思考强度" in effort_set_bot.all_text():
                    checks.append(("command(effort-set)", "ok"))
                else:
                    failures.append("command(effort-set): 思考强度切换失败")

                repo_bot = await run_command(
                    bot.repo_command,
                    application,
                    chat_id,
                    args=[repo_key],
                    text=f"/repo {repo_key}",
                )
                if f"已切到 {repo_key}" in repo_bot.all_text():
                    checks.append(("command(repo)", "ok"))
                else:
                    failures.append("command(repo): 切仓库失败")
                store.set_repo_key(chat_id, repo_key)
                store.set_thread_id(chat_id, target_thread["id"])

                new_bot = await run_command(bot.new_command, application, chat_id, text="/new")
                if "已新开一个空线程" in new_bot.all_text():
                    checks.append(("command(new)", "ok"))
                else:
                    failures.append("command(new): 新线程命令失败")
                store.set_thread_id(chat_id, target_thread["id"])

                archive_bot = await run_command(bot.archive_command, application, chat_id, text="/archive")
                if "已归档当前线程" in archive_bot.all_text():
                    checks.append(("command(archive)", "ok"))
                else:
                    failures.append("command(archive): 归档命令失败")
                store.set_thread_id(chat_id, target_thread["id"])

                cleanup_bot = await run_command(
                    bot.cleanup_threads_command,
                    application,
                    chat_id,
                    text="/cleanup_threads",
                )
                if "清理完成" in cleanup_bot.all_text():
                    checks.append(("command(cleanup_threads)", "ok"))
                else:
                    failures.append("command(cleanup_threads): 清理命令失败")
                store.set_thread_id(chat_id, target_thread["id"])

                threads_bot = await run_command(bot.threads_command, application, chat_id, text="/threads")
                if "历史线程" in threads_bot.all_text():
                    checks.append(("command(threads)", "ok"))
                else:
                    failures.append("command(threads): 没有返回历史线程")

                use_bot = await run_command(bot.use_command, application, chat_id, args=["1"], text="/use 1")
                if "已切回线程" in use_bot.all_text():
                    checks.append(("command(use)", "ok"))
                else:
                    failures.append("command(use): 切回线程失败")

                history_bot = await run_command(
                    bot.history_command,
                    application,
                    chat_id,
                    args=["1"],
                    text="/history 1",
                )
                if "\n用户\n" in history_bot.all_text() or "\n助手\n" in history_bot.all_text():
                    checks.append(("command(history)", "ok"))
                else:
                    failures.append("command(history): 没有导出对话")

                summary_bot = await run_command(
                    bot.summary_command,
                    application,
                    chat_id,
                    args=["1"],
                    text="/summary 1",
                )
                if "目标:" in summary_bot.all_text() and "下一步:" in summary_bot.all_text():
                    checks.append(("command(summary)", "ok"))
                else:
                    failures.append("command(summary): 没有返回四段摘要")

                task_bot = await run_command(
                    bot.task_command,
                    application,
                    chat_id,
                    args=["测试任务"],
                    text="/task 测试任务",
                )
                if "思考中…" in task_bot.all_text():
                    checks.append(("command(task)", "ok"))
                else:
                    failures.append("command(task): 没有启动思考气泡")

                continue_bot = await run_command(
                    bot.continue_command,
                    application,
                    chat_id,
                    args=["继续"],
                    text="/continue 继续",
                )
                if "思考中…" in continue_bot.all_text():
                    checks.append(("command(continue)", "ok"))
                else:
                    failures.append("command(continue): 没有启动继续消息")

                text_bot = await run_command(
                    bot.text_message,
                    application,
                    chat_id,
                    text="普通文本继续",
                )
                if "思考中…" in text_bot.all_text():
                    checks.append(("command(text)", "ok"))
                else:
                    failures.append("command(text): 普通文本入口没有启动")

                original_save_file = bot.save_telegram_file
                original_dispatch_message = bot.dispatch_chat_message
                media_prompts: list[str] = []

                async def fake_save_file(context_arg, file_id: str, target_dir: Path, filename: str) -> Path:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / filename
                    if target.suffix.lower() == ".txt":
                        target.write_text("hello from attachment", encoding="utf-8")
                    else:
                        target.write_bytes(b"fake-image")
                    return target

                async def fake_dispatch_message(update_arg, context_arg, *, prompt: str, force_new: bool = False) -> None:
                    media_prompts.append(prompt)

                bot.save_telegram_file = fake_save_file  # type: ignore[assignment]
                bot.dispatch_chat_message = fake_dispatch_message  # type: ignore[assignment]
                try:
                    media_bot = FakeBot()
                    application.bot = media_bot

                    photo_message = FakeSentMessage(media_bot, chat_id, 0, "")
                    photo_message.caption = "看这张图"
                    photo_message.photo = [SimpleNamespace(file_id="photo-1", file_unique_id="photo-1")]
                    photo_message.document = None
                    photo_update = SimpleNamespace(
                        effective_chat=SimpleNamespace(id=chat_id),
                        effective_message=photo_message,
                        callback_query=None,
                    )
                    photo_context = SimpleNamespace(application=application, args=[], bot=media_bot)
                    await bot.media_message(photo_update, photo_context)

                    doc_message = FakeSentMessage(media_bot, chat_id, 0, "")
                    doc_message.caption = "读这个文件"
                    doc_message.photo = None
                    doc_message.document = SimpleNamespace(
                        file_id="doc-1",
                        file_unique_id="doc-1",
                        file_name="note.txt",
                    )
                    doc_update = SimpleNamespace(
                        effective_chat=SimpleNamespace(id=chat_id),
                        effective_message=doc_message,
                        callback_query=None,
                    )
                    doc_context = SimpleNamespace(application=application, args=[], bot=media_bot)
                    await bot.media_message(doc_update, doc_context)
                finally:
                    bot.save_telegram_file = original_save_file  # type: ignore[assignment]
                    bot.dispatch_chat_message = original_dispatch_message  # type: ignore[assignment]

                joined_media = "\n".join(media_prompts)
                if "附件" in joined_media and "hello from attachment" in joined_media and "photo-1.jpg" in joined_media:
                    checks.append(("command(media)", "ok"))
                else:
                    failures.append("command(media): 图片或文件附件提示没有生成")

                stop_bot = await run_command(bot.stop_command, application, chat_id, text="/stop")
                if "已中断。" in stop_bot.all_text():
                    checks.append(("command(stop)", "ok"))
                else:
                    failures.append("command(stop): 中断命令失败")

                control_bot, _ = await run_callback(
                    bot.control_callback,
                    application,
                    chat_id,
                    data="control:effort:medium",
                )
                if "已切换思考强度" in control_bot.all_text():
                    checks.append(("callback(control:effort)", "ok"))
                else:
                    failures.append("callback(control:effort): 没有切换思考强度")

                thread_summary_bot, _ = await run_callback(
                    bot.thread_callback,
                    application,
                    chat_id,
                    data=f"{bot.THREAD_CALLBACK_PREFIX}:summary:{target_thread['id']}",
                )
                if "目标:" in thread_summary_bot.all_text():
                    checks.append(("callback(thread:summary)", "ok"))
                else:
                    failures.append("callback(thread:summary): 没有返回摘要")

                for name, status in checks:
                    print(f"{name}: {status}")
            finally:
                conversations.archive_current_thread = original_archive_current_thread  # type: ignore[assignment]
                conversations.start_chat_turn = original_start_chat_turn  # type: ignore[assignment]
                conversations.stop_chat_turn = original_stop_chat_turn  # type: ignore[assignment]
                client.list_threads = original_client_list_threads  # type: ignore[assignment]
                client.archive_thread = original_client_archive_thread  # type: ignore[assignment]
                bot.stream_turn_to_telegram = original_stream_turn  # type: ignore[assignment]
                bot.summarize_transcript_with_model = original_summarizer  # type: ignore[assignment]

            fake_bot = FakeBot()
            stream_turn = bot.ActiveTurn(
                chat_id=chat_id,
                repo_key=repo_key,
                repo_path=repo_path,
                thread_id="stream-thread",
                turn_id="stream-turn",
                prompt="stream verify",
            )
            stream_store = bot.SessionStore(temp_dir / "stream_sessions.json")
            stream_store.data = json.loads(json.dumps(store.data, ensure_ascii=False))
            stream_store.set_verbose_level(chat_id, "thinking")
            stream_application = SimpleNamespace(
                bot=fake_bot,
                bot_data={
                    "store": stream_store,
                    "conversations": SimpleNamespace(get_active=lambda chat_id_arg: stream_turn),
                },
            )
            placeholder = await fake_bot.send_message(chat_id=chat_id, text="思考中…")
            stream_task = asyncio.create_task(
                bot.stream_turn_to_telegram(
                    stream_application,
                    stream_turn,
                    placeholder.message_id,
                    settings,
                )
            )
            await asyncio.sleep(0)
            stream_turn.stage = "执行命令"
            stream_turn.running_command = 'rg "verbose" bot.py'
            await stream_turn.push({"type": "tool", "kind": "shell", "command": 'rg "verbose" bot.py'})
            stream_turn.text = "先看了当前实现。"
            stream_turn.stage = "整理结果"
            await stream_turn.push({"type": "delta", "text": stream_turn.text})
            await asyncio.sleep(0)
            await stream_turn.push({"type": "tool", "kind": "patch_output", "files": ["bot.py", "README.md"]})
            stream_turn.text = "先看了当前实现。\n已经改了 bot.py 和 README.md。"
            await stream_turn.push({"type": "delta", "text": stream_turn.text})
            await asyncio.sleep(0)
            await stream_turn.push({"type": "done", "text": "最终答案\n- bot.py\n- README.md"})
            await stream_task
            stream_text = fake_bot.all_text()
            if "已运行命令" not in stream_text or "已修改文件" not in stream_text:
                failures.append("stream(layout): 没有分出工具气泡")
            elif "最终答案" not in stream_text:
                failures.append("stream(layout): 没有分出回答气泡")
            elif not any("已完成" in text for _, text in fake_bot.edited_records):
                failures.append("stream(layout): thinking 气泡没有落到完成态")
            else:
                print("stream(layout): ok")

        await client.shutdown()

        if failures:
            print("\nFAIL")
            for item in failures:
                print(f"- {item}")
            return 1

        print("\nPASS")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
