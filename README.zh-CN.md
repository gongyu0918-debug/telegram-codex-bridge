# Telegram Codex Bridge

[English README](./README.md)

这是一个把本地 Codex 会话接到 Telegram 的桥接器。每个 Telegram chat 会绑定一个持久 Codex 线程，回复通过编辑消息持续流式更新，也支持历史线程、摘要、全文导出和权限切换。

这份项目是非官方社区桥接层，底层依赖 Telegram Bot API 和你本机的 Codex CLI / `app-server`。它和 OpenAI、Telegram 都没有官方从属关系。

## 功能

- 一个 chat 对应一个持久 `thread_id`
- `thinking` 状态气泡，加上独立的工具气泡和回答气泡
- 历史线程、切回、归档、全文导出、摘要压缩
- 按 chat 保存模型、思考强度、访问权限、显示档位
- 支持图片和文件附件
- slash 菜单和内联按钮采用中英双语

## 命令

- `/start` 帮助和当前配置
- `/status` 当前线程状态
- `/repos` 仓库列表
- `/repo <name>` 切仓库并重置线程
- `/new` 新开线程
- `/threads` 查看这个 chat 的历史线程
- `/threads all` 查看同仓库更多线程
- `/use <序号|thread_id>` 切回线程
- `/history [序号|thread_id]` 导出全文
- `/summary [序号|thread_id]` 压缩摘要
- `/archive` 归档当前线程
- `/cleanup_threads` 清理旧线程
- `/verbose [off|thinking|new|all|verbose]` 显示档位
- `/access [default|full]` 访问权限
- `/model [name]` 模型设置
- `/effort [minimal|low|medium|high|xhigh]` 思考强度
- `/stop` 中断当前回复
- `/task <text>` 以新线程发送
- `/continue <text>` 在当前线程继续

## 快速开始

```powershell
cd telegram_codex_bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python bot.py
```

`.env` 至少要填：

- `TELEGRAM_BOT_TOKEN`
- `CODEX_COMMAND`

示例：

```env
TELEGRAM_BOT_TOKEN=123456:replace_me
ALLOWED_CHAT_IDS=
REPOS=default=.
STATE_DIR=./state
CODEX_COMMAND=cmd /c npx @openai/codex@0.122.0
CODEX_APPROVAL=never
CODEX_SANDBOX=danger-full-access
CODEX_DEFAULT_SANDBOX=workspace-write
CODEX_MODEL=gpt-5.4
CODEX_REASONING_EFFORT=medium
MAX_PROMPT_CHARS=12000
STREAM_EDIT_INTERVAL=0.8
MAX_MESSAGE_CHARS=3800
AUTO_ARCHIVE_ON_NEW=true
```

## 安全边界

这份桥接层等于把本地 Codex 工作流暴露给 Telegram，所以边界要收紧：

- 用 `ALLOWED_CHAT_IDS`
- `REPOS` 尽量收窄
- `danger-full-access` 视为完全本地执行
- token 泄露后立即轮换
- 不要把本地会话、授权态、日志、`.env` 提交到 Git

## 版本建议

`CODEX_COMMAND` 最好固定到已经验证过的 Codex CLI 版本。

- 推荐：`cmd /c npx @openai/codex@0.122.0`
- `@latest` 适合本机追新版
- 固定版本可以减少桥接层和 Codex 协议一起漂移时的静默兼容问题

## 许可证

这份桥接代码使用 MIT License。项目本身没有打包 OpenAI Codex 的源代码。如果你后续复制 `openai/codex` 仓库里的代码片段，那个上游仓库使用 Apache 2.0，你需要保留对应声明。

## 参考

- [OpenAI Codex CLI – Getting Started](https://help.openai.com/en/articles/11096431-openai-codex-ci-getting-started)
- [Using Codex with your ChatGPT plan](https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan)
- [openai/codex](https://github.com/openai/codex)
- [telecodex](https://github.com/Headcrab/telecodex)
- [CCGram](https://github.com/alexei-led/ccgram)
- [CCBot](https://github.com/alexei-led/ccbot)
