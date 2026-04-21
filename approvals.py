from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


APPROVAL_TIMEOUT_SECONDS = 900


@dataclass
class PendingApproval:
    token: str
    request_id: int | str
    method: str
    chat_id: int
    thread_id: str
    turn_id: str
    item_id: str
    prompt_text: str
    result_future: asyncio.Future[dict[str, Any]] | None
    requested_permissions: dict[str, Any] | None = None
    user_input_questions: list[dict[str, Any]] = field(default_factory=list)
    dynamic_tool_name: str | None = None
    dynamic_tool_arguments: Any = None
    message_id: int | None = None
    created_at: float = field(default_factory=time.time)
    stale: bool = False


@dataclass(frozen=True)
class ApprovalDependencies:
    build_request_keyboard: Callable[[PendingApproval], Any]
    build_mcp_elicitation_result: Callable[[str], dict[str, Any]]
    build_dynamic_tool_result: Callable[..., dict[str, Any]]
    resolve_repo_key_for_thread: Callable[[Any, Any, int, str], str | None]
    append_unique: Callable[[list[str], str, int], None]
    get_chat_verbose: Callable[[Any, int], str]
    stream_turn_to_telegram: Callable[[Any, Any, int | None, Any], Awaitable[None]]


class ApprovalManager:
    def __init__(
        self,
        settings: Any,
        store: Any,
        conversations: Any,
        dependencies: ApprovalDependencies,
    ) -> None:
        self.settings = settings
        self.store = store
        self.conversations = conversations
        self.dependencies = dependencies
        self.application: Any | None = None
        self.pending: dict[str, PendingApproval] = {}
        self._restore_pending_requests()

    def bind_application(self, application: Any) -> None:
        self.application = application

    def pending_count(self) -> int:
        self._purge_expired_pending()
        return len(self.pending)

    def _is_pending_expired(self, pending: PendingApproval, now: float | None = None) -> bool:
        current = now if now is not None else time.time()
        return pending.created_at + APPROVAL_TIMEOUT_SECONDS <= current

    def _purge_expired_pending(self) -> None:
        now = time.time()
        expired_tokens = [
            token for token, pending in self.pending.items() if self._is_pending_expired(pending, now)
        ]
        for token in expired_tokens:
            self._drop_pending(token)

    def _serialize_pending(self, pending: PendingApproval) -> dict[str, Any]:
        return {
            "token": pending.token,
            "request_id": pending.request_id,
            "method": pending.method,
            "chat_id": pending.chat_id,
            "thread_id": pending.thread_id,
            "turn_id": pending.turn_id,
            "item_id": pending.item_id,
            "prompt_text": pending.prompt_text,
            "requested_permissions": pending.requested_permissions,
            "user_input_questions": pending.user_input_questions,
            "dynamic_tool_name": pending.dynamic_tool_name,
            "dynamic_tool_arguments": pending.dynamic_tool_arguments,
            "message_id": pending.message_id,
            "created_at": pending.created_at,
        }

    def _persist_pending(self, pending: PendingApproval) -> None:
        self.store.save_pending_request(self._serialize_pending(pending))

    def _drop_pending(self, token: str) -> None:
        self.pending.pop(token, None)
        self.store.delete_pending_request(token)

    def _restore_pending_requests(self) -> None:
        now = time.time()
        for record in self.store.list_pending_requests():
            token = str(record.get("token") or "").strip()
            if not token:
                continue
            created_at_raw = record.get("created_at")
            created_at = float(created_at_raw) if isinstance(created_at_raw, (int, float)) else now
            if created_at + APPROVAL_TIMEOUT_SECONDS <= now:
                self.store.delete_pending_request(token)
                continue
            pending = PendingApproval(
                token=token,
                request_id=record.get("request_id") or token,
                method=str(record.get("method") or "").strip(),
                chat_id=int(record.get("chat_id") or 0),
                thread_id=str(record.get("thread_id") or "").strip(),
                turn_id=str(record.get("turn_id") or "").strip(),
                item_id=str(record.get("item_id") or "").strip(),
                prompt_text=str(record.get("prompt_text") or "").strip() or "待处理请求",
                result_future=None,
                requested_permissions=record.get("requested_permissions")
                if isinstance(record.get("requested_permissions"), dict)
                else None,
                user_input_questions=record.get("user_input_questions")
                if isinstance(record.get("user_input_questions"), list)
                else [],
                dynamic_tool_name=str(record.get("dynamic_tool_name") or "").strip() or None,
                dynamic_tool_arguments=record.get("dynamic_tool_arguments"),
                message_id=int(record.get("message_id")) if isinstance(record.get("message_id"), int) else None,
                created_at=created_at,
                stale=True,
            )
            self.pending[token] = pending

    async def handle_notification(self, payload: dict[str, Any]) -> None:
        if payload.get("method") != "serverRequest/resolved":
            return
        params = payload.get("params", {})
        request_id = params.get("requestId")
        thread_id = str(params.get("threadId") or "").strip()
        if request_id is None:
            return
        for token, pending in list(self.pending.items()):
            if pending.request_id != request_id:
                continue
            if thread_id and pending.thread_id and pending.thread_id != thread_id:
                continue
            self._drop_pending(token)

    def _format_result_for_message(
        self,
        pending: PendingApproval,
        action: str,
        result: dict[str, Any],
    ) -> str:
        if pending.method == "item/tool/requestUserInput":
            answers = result.get("answers") or {}
            if answers:
                return f"{action}: {json.dumps(answers, ensure_ascii=False)}"
            return action
        if pending.method == "mcpServer/elicitation/request":
            return f"{action}: {json.dumps(result, ensure_ascii=False)}"
        if pending.method == "item/tool/call":
            return f"{action}: {pending.dynamic_tool_name or 'dynamic-tool'}"
        return action

    def _build_recovery_prompt(
        self,
        pending: PendingApproval,
        action: str,
        result: dict[str, Any],
    ) -> str:
        lines = [
            "系统恢复说明",
            "上一个 Telegram bridge 进程在处理中途重启了。",
            "下面是用户刚刚对旧请求做出的决定，请你在当前线程继续处理，并把它视为最新约束。",
            "",
            f"请求类型: {pending.method}",
            f"thread: {pending.thread_id}",
            f"turn: {pending.turn_id}",
        ]
        if pending.dynamic_tool_name:
            lines.append(f"tool: {pending.dynamic_tool_name}")
        lines.extend(
            [
                "",
                "原始请求",
                pending.prompt_text,
                "",
                f"用户选择: {action}",
                "结构化结果",
                json.dumps(result, ensure_ascii=False, indent=2),
            ]
        )
        return "\n".join(lines)

    async def recover_stale_request(
        self,
        application: Any,
        pending: PendingApproval,
        action: str,
        result: dict[str, Any],
    ) -> Any | None:
        repo_key = self.dependencies.resolve_repo_key_for_thread(
            self.settings,
            self.store,
            pending.chat_id,
            pending.thread_id,
        )
        if not repo_key:
            return None

        active = self.conversations.get_active(pending.chat_id)
        recovery_prompt = self._build_recovery_prompt(pending, action, result)
        if active and active.thread_id == pending.thread_id:
            return await self.conversations.steer_chat_turn(
                pending.chat_id,
                recovery_prompt,
            )
        if active:
            return None

        if pending.thread_id:
            try:
                await self.conversations.switch_thread(
                    pending.chat_id,
                    repo_key,
                    pending.thread_id,
                )
            except Exception:
                current_thread_id = self.store.get_thread_id(pending.chat_id)
                if current_thread_id != pending.thread_id:
                    return None

        return await self.conversations.start_chat_turn(
            pending.chat_id,
            repo_key,
            recovery_prompt,
            force_new=False,
        )

    def _resolve_chat_id(self, thread_id: str, turn_id: str) -> int | None:
        active = self.conversations.active_by_turn.get(turn_id)
        if active:
            return active.chat_id
        return self.store.find_chat_id_by_thread(thread_id)

    def _normalize_method(self, method: str) -> str:
        if method == "applyPatchApproval":
            return "item/fileChange/requestApproval"
        if method == "execCommandApproval":
            return "item/commandExecution/requestApproval"
        return method

    def _default_result_for_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        normalized_method = self._normalize_method(method)
        if normalized_method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "turn"}
        if normalized_method == "item/tool/requestUserInput":
            return {"answers": {}}
        if normalized_method == "mcpServer/elicitation/request":
            return self.dependencies.build_mcp_elicitation_result("decline")
        if normalized_method == "item/tool/call":
            return self.dependencies.build_dynamic_tool_result(
                str(params.get("tool") or "dynamic-tool"),
                params.get("arguments"),
                success=False,
                mode="cancel",
            )
        if normalized_method == "account/chatgptAuthTokens/refresh":
            return {}
        return {"decision": "cancel"}

    def _build_prompt_text(self, method: str, params: dict[str, Any]) -> str:
        normalized_method = self._normalize_method(method)
        if normalized_method == "item/tool/requestUserInput":
            lines = ["需要补充输入", f"thread: {params.get('threadId')}", f"turn: {params.get('turnId')}"]
            questions = params.get("questions") or []
            if isinstance(questions, list):
                for index, question in enumerate(questions[:3], start=1):
                    if not isinstance(question, dict):
                        continue
                    header = str(question.get("header") or "").strip()
                    body = str(question.get("question") or "").strip()
                    if header:
                        lines.extend(["", f"{index}. {header}"])
                    elif body:
                        lines.extend(["", f"{index}."])
                    if body:
                        lines.append(body)
                    options = question.get("options") or []
                    if isinstance(options, list):
                        for option in options[:4]:
                            if not isinstance(option, dict):
                                continue
                            label = str(option.get("label") or "").strip()
                            description = str(option.get("description") or "").strip()
                            if label and description:
                                lines.append(f"- {label}: {description}")
                            elif label:
                                lines.append(f"- {label}")
            return "\n".join(lines)

        if normalized_method == "mcpServer/elicitation/request":
            lines = ["MCP 交互请求", f"thread: {params.get('threadId')}", f"turn: {params.get('turnId')}"]
            server_name = str(params.get("serverName") or "").strip()
            message = str(params.get("message") or "").strip()
            if server_name:
                lines.append(f"server: {server_name}")
            if message:
                lines.extend(["", message])
            return "\n".join(lines)

        if normalized_method == "item/tool/call":
            tool_name = str(params.get("tool") or "").strip() or "dynamic-tool"
            lines = [
                "动态工具调用",
                f"thread: {params.get('threadId')}",
                f"turn: {params.get('turnId')}",
                f"tool: {tool_name}",
            ]
            arguments = params.get("arguments")
            if arguments is not None:
                try:
                    rendered = json.dumps(arguments, ensure_ascii=False, indent=2)
                except TypeError:
                    rendered = str(arguments)
                lines.extend(["", "arguments", rendered])
            return "\n".join(lines)

        if method == "item/commandExecution/requestApproval":
            lines = ["命令审批", f"thread: {params.get('threadId')}", f"turn: {params.get('turnId')}"]
            command = str(params.get("command") or "").strip()
            cwd = str(params.get("cwd") or "").strip()
            reason = str(params.get("reason") or "").strip()
            if command:
                lines.extend(["", "命令", command])
            if cwd:
                lines.append(f"cwd: {cwd}")
            if reason:
                lines.extend(["", "原因", reason])
            return "\n".join(lines)

        if method == "item/fileChange/requestApproval":
            lines = ["文件变更审批", f"thread: {params.get('threadId')}", f"turn: {params.get('turnId')}"]
            grant_root = str(params.get("grantRoot") or "").strip()
            reason = str(params.get("reason") or "").strip()
            if grant_root:
                lines.extend(["", "写入范围", grant_root])
            if reason:
                lines.extend(["", "原因", reason])
            return "\n".join(lines)

        if method == "item/permissions/requestApproval":
            permissions = params.get("permissions") or {}
            lines = ["权限提升审批", f"thread: {params.get('threadId')}", f"turn: {params.get('turnId')}"]
            reason = str(params.get("reason") or "").strip()
            if reason:
                lines.extend(["", "原因", reason])
            file_system = permissions.get("fileSystem") if isinstance(permissions, dict) else None
            network = permissions.get("network") if isinstance(permissions, dict) else None
            if file_system:
                lines.append("")
                lines.append("文件系统")
                read_roots = file_system.get("read") or []
                write_roots = file_system.get("write") or []
                if read_roots:
                    lines.extend(f"- 读: {path}" for path in read_roots[:4])
                if write_roots:
                    lines.extend(f"- 写: {path}" for path in write_roots[:4])
            if network:
                lines.append("")
                lines.append(f"网络: {json.dumps(network, ensure_ascii=False)}")
            return "\n".join(lines)

        return f"待审批请求\n{method}"

    async def handle_server_request(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        self._purge_expired_pending()
        raw_method = str(payload.get("method") or "")
        method = self._normalize_method(raw_method)
        if method not in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
            "item/tool/call",
            "mcpServer/elicitation/request",
            "account/chatgptAuthTokens/refresh",
        }:
            return None
        if method == "account/chatgptAuthTokens/refresh":
            logging.getLogger("codex.approvals").info("忽略 chatgptAuthTokens/refresh 请求")
            return {}
        if not self.application:
            return self._default_result_for_method(method, payload.get("params", {}))

        params = payload.get("params", {})
        if not isinstance(params, dict):
            return self._default_result_for_method(method, {})

        thread_id = str(params.get("threadId") or "").strip()
        turn_id = str(params.get("turnId") or "").strip()
        item_id = str(params.get("itemId") or "").strip()
        chat_id = self._resolve_chat_id(thread_id, turn_id)
        if not chat_id:
            return self._default_result_for_method(method, params)
        active = self.conversations.active_by_turn.get(turn_id)
        if active:
            if method == "item/tool/requestUserInput":
                active.stage = "等待输入"
                self.dependencies.append_unique(active.tool_notes, "等待用户补充输入", limit=6)
            elif method == "mcpServer/elicitation/request":
                active.stage = "等待 MCP 决策"
                self.dependencies.append_unique(active.tool_notes, "等待 MCP 决策", limit=6)
            elif method == "item/tool/call":
                active.stage = "等待动态工具"
                self.dependencies.append_unique(active.tool_notes, "等待动态工具响应", limit=6)
            else:
                active.stage = "等待审批"
                self.dependencies.append_unique(active.tool_notes, "等待用户审批", limit=6)
            await active.push({"type": "status"})

        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[dict[str, Any]] = loop.create_future()
        token = uuid.uuid4().hex[:16]
        pending = PendingApproval(
            token=token,
            request_id=payload["id"],
            method=method,
            chat_id=chat_id,
            thread_id=thread_id,
            turn_id=turn_id,
            item_id=item_id,
            prompt_text=self._build_prompt_text(method, params),
            result_future=result_future,
            requested_permissions=params.get("permissions") if method == "item/permissions/requestApproval" else None,
            user_input_questions=params.get("questions") if method == "item/tool/requestUserInput" and isinstance(params.get("questions"), list) else [],
            dynamic_tool_name=str(params.get("tool") or "").strip() or None,
            dynamic_tool_arguments=params.get("arguments") if method == "item/tool/call" else None,
        )
        self.pending[token] = pending
        self._persist_pending(pending)
        try:
            sent = await self.application.bot.send_message(
                chat_id=chat_id,
                text=pending.prompt_text,
                reply_markup=self.dependencies.build_request_keyboard(pending),
            )
        except Exception:
            self._drop_pending(token)
            raise
        pending.message_id = sent.message_id
        self._persist_pending(pending)
        try:
            return await asyncio.wait_for(result_future, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            if pending.message_id:
                try:
                    await self.application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=pending.message_id,
                        text=pending.prompt_text + "\n\n审批超时，已自动取消。",
                    )
                except Exception:
                    pass
            return self._default_result_for_method(method, params)
        finally:
            if active:
                active.stage = "整理结果"
                await active.push({"type": "status"})
            self._drop_pending(token)

    def resolve_callback(
        self,
        token: str,
        action: str,
        extra: list[str] | None = None,
    ) -> tuple[PendingApproval | None, dict[str, Any] | None]:
        self._purge_expired_pending()
        pending = self.pending.get(token)
        if not pending:
            return None, None
        extra = extra or []
        if pending.method == "item/tool/requestUserInput":
            if action in {"skip", "cancel", "decline"}:
                return pending, {"answers": {}}
            if action == "pick" and len(extra) >= 2:
                try:
                    question_index = int(extra[0])
                    option_index = int(extra[1])
                except ValueError:
                    return pending, None
                if question_index < 0 or question_index >= len(pending.user_input_questions):
                    return pending, None
                question = pending.user_input_questions[question_index]
                if not isinstance(question, dict):
                    return pending, None
                options = question.get("options") or []
                if not isinstance(options, list) or option_index < 0 or option_index >= len(options):
                    return pending, None
                option = options[option_index]
                if not isinstance(option, dict):
                    return pending, None
                question_id = str(question.get("id") or f"question_{question_index}").strip()
                answer = str(option.get("label") or "").strip()
                if not question_id or not answer:
                    return pending, None
                return pending, {"answers": {question_id: {"answers": [answer]}}}
            return pending, None
        if pending.method == "mcpServer/elicitation/request":
            if action in {"decline", "cancel"}:
                return pending, self.dependencies.build_mcp_elicitation_result(action)
            return pending, None
        if pending.method == "item/tool/call":
            if action == "echo":
                return pending, self.dependencies.build_dynamic_tool_result(
                    pending.dynamic_tool_name or "dynamic-tool",
                    pending.dynamic_tool_arguments,
                    success=True,
                    mode="echo",
                )
            if action in {"decline", "cancel"}:
                return pending, self.dependencies.build_dynamic_tool_result(
                    pending.dynamic_tool_name or "dynamic-tool",
                    pending.dynamic_tool_arguments,
                    success=False,
                    mode=action,
                )
            return pending, None
        if pending.method == "item/permissions/requestApproval":
            if action == "session":
                return pending, {"permissions": pending.requested_permissions or {}, "scope": "session"}
            if action == "once":
                return pending, {"permissions": pending.requested_permissions or {}, "scope": "turn"}
            return pending, {"permissions": {}, "scope": "turn"}
        decision_map = {
            "once": "accept",
            "session": "acceptForSession",
            "decline": "decline",
            "cancel": "cancel",
        }
        decision = decision_map.get(action)
        if not decision:
            return pending, None
        return pending, {"decision": decision}

