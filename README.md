# Telegram Codex Bridge

[中文说明](./README.zh-CN.md)

Telegram bridge for local Codex sessions. It keeps one persistent Codex thread per Telegram chat, streams progress back by editing messages, supports thread history, and can run with full local access when you explicitly allow it.

This project is an unofficial community bridge. It uses the Telegram Bot API plus your local Codex CLI / `app-server`. It is not affiliated with OpenAI or Telegram.

## Features

- Persistent chat-to-thread mapping
- Streaming message edits with status, command, and file-change previews
- Thread history, resume, archive, full transcript export, and summary
- Per-chat model, reasoning effort, access mode, and verbosity settings
- Attachment intake for images and documents
- Local usage snapshot from `~/.codex/state_5.sqlite`
- Inline buttons and Telegram command menu with bilingual labels

## Commands

- `/start` help and current config
- `/status` current thread status
- `/usage` local usage snapshot
- `/repos` list repos
- `/repo <name>` switch repo and reset thread
- `/new` start a fresh thread
- `/threads` list this chat's history
- `/threads all` include more repo threads
- `/use <index|thread_id>` switch back to a thread
- `/history [index|thread_id]` export full transcript
- `/summary [index|thread_id]` compress thread context
- `/archive` archive current thread
- `/cleanup_threads` archive old threads from this chat
- `/verbose [off|new|all|verbose]` progress display mode
- `/access [default|full]` sandbox mode
- `/model [name]` set or inspect model
- `/effort [minimal|low|medium|high|xhigh]` set or inspect reasoning effort
- `/stop` interrupt the running turn
- `/task <text>` send as a new thread
- `/continue <text>` continue in the current thread

## Quick start

```powershell
cd telegram_codex_bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

Fill `.env` with at least:

- `TELEGRAM_BOT_TOKEN`
- `CODEX_COMMAND`

Example:

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
ALLOWED_CHAT_IDS=
REPOS=default=.
STATE_DIR=./state
CODEX_COMMAND=cmd /c npx @openai/codex@latest
CODEX_APPROVAL=never
CODEX_SANDBOX=danger-full-access
CODEX_DEFAULT_SANDBOX=workspace-write
CODEX_MODEL=
CODEX_REASONING_EFFORT=
MAX_PROMPT_CHARS=12000
STREAM_EDIT_INTERVAL=0.8
MAX_MESSAGE_CHARS=3800
AUTO_ARCHIVE_ON_NEW=true
```

## Usage data

`/usage` shows a local estimate only. It reads thread totals from `~/.codex/state_5.sqlite` and reports:

- current thread tokens
- this chat's thread history totals
- current repo totals
- recent 5-hour active-thread totals
- all local thread totals

It does not show the official remaining quota of your ChatGPT Codex plan. OpenAI documents Codex plan limits, but there is currently no public exact remaining-quota API for ChatGPT plan usage.

## Security

This bridge exposes your local Codex workflow to Telegram. Keep these boundaries tight:

- use `ALLOWED_CHAT_IDS`
- keep `REPOS` narrow
- treat `danger-full-access` as full local execution
- rotate leaked bot tokens immediately
- never commit local session files, auth state, logs, or `.env`

## Open source safety

Files that must stay out of Git:

- `.env`
- `state/`
- `*.log`
- `bot.lock`
- `~/.codex/auth.json`
- `~/.codex/sessions`
- `~/.codex/archived_sessions`
- any exported chat history, screenshots, or credentials

## License

This bridge is released under the MIT License. The project does not bundle OpenAI Codex source code. If you copy code from `openai/codex`, that upstream project is licensed under Apache 2.0 and you must keep its notices for copied portions.

## References

- [OpenAI Codex CLI – Getting Started](https://help.openai.com/en/articles/11096431-openai-codex-ci-getting-started)
- [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
- [openai/codex](https://github.com/openai/codex)
- [telecodex](https://github.com/Headcrab/telecodex)
- [CCGram](https://github.com/alexei-led/ccgram)
- [CCBot](https://github.com/alexei-led/ccbot)
