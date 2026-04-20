from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).resolve().parent)
sys.path.insert(0, os.getcwd())

import bot


async def verify() -> int:
    settings = bot.Settings.load()
    store = bot.SessionStore(settings.state_dir / "sessions.json")
    client = bot.CodexAppServerClient(settings)
    conversations = bot.ConversationManager(settings, store, client)
    chat_ids = sorted(settings.allowed_chat_ids)
    if chat_ids:
        chat_id = chat_ids[0]
    else:
        chat_prefs = store.data.get("chat_prefs", {})
        if not chat_prefs:
            print("FAIL")
            print("- verify: 没有可用 chat_id，先配置 ALLOWED_CHAT_IDS 或至少聊过一次")
            return 1
        chat_id = int(next(iter(chat_prefs.keys())))

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

    try:
        usage_text = bot.build_usage_text(settings, store, chat_id)
    except Exception as exc:
        failures.append(f"usage(snapshot): {type(exc).__name__}: {exc}")
    else:
        if "官方剩余额度" not in usage_text or "当前线程累计 tokens" not in usage_text:
            failures.append("usage(snapshot): 输出缺少关键字段")
        else:
            print("usage(snapshot): ok")

    if failures:
        print("\nFAIL")
        for item in failures:
            print(f"- {item}")
        return 1

    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
