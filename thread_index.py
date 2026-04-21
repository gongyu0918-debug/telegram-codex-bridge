from __future__ import annotations

import glob
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


LOCAL_THREAD_INDEX_CACHE_VERSION = 2
LOCAL_THREAD_INDEX_CACHE_PATH = Path.home() / ".codex" / "thread_index_cache.json"
LOCAL_THREAD_INDEX_WARM_TTL_SECONDS = 15


@dataclass
class LocalThreadIndexState:
    signature: tuple[Any, ...] | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)
    by_thread_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_loaded_at: float = 0.0
    dirty: bool = False


LOCAL_THREAD_INDEX = LocalThreadIndexState()


def path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def path_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def scan_directory_tree_signature(root: Path) -> tuple[int, float, str]:
    if not root.exists():
        return (0, 0.0, "")
    count = 0
    newest_mtime = 0.0
    newest_path = ""
    try:
        for path in root.rglob("*"):
            if not path.is_dir():
                continue
            count += 1
            mtime = path_mtime(path)
            path_str = str(path)
            if mtime > newest_mtime or (mtime == newest_mtime and path_str > newest_path):
                newest_mtime = mtime
                newest_path = path_str
    except OSError:
        return (0, 0.0, "")
    return (count, newest_mtime, newest_path)


def read_local_thread_index_cache() -> dict[str, Any] | None:
    if not LOCAL_THREAD_INDEX_CACHE_PATH.exists():
        return None
    try:
        payload = json.loads(LOCAL_THREAD_INDEX_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != LOCAL_THREAD_INDEX_CACHE_VERSION:
        return None
    return payload


def hydrate_local_thread_index_state(payload: dict[str, Any]) -> LocalThreadIndexState | None:
    signature_raw = payload.get("signature")
    entries_raw = payload.get("entries")
    if not isinstance(signature_raw, list) or not isinstance(entries_raw, list):
        return None
    entries = [item for item in entries_raw if isinstance(item, dict)]
    by_thread_id = {
        str(item.get("id")): item
        for item in entries
        if isinstance(item.get("id"), str) and str(item.get("id")).strip()
    }
    return LocalThreadIndexState(
        signature=tuple(signature_raw),
        entries=entries,
        by_thread_id=by_thread_id,
        last_loaded_at=time.time(),
        dirty=False,
    )


def save_local_thread_index_cache(state: LocalThreadIndexState) -> None:
    LOCAL_THREAD_INDEX_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": LOCAL_THREAD_INDEX_CACHE_VERSION,
        "saved_at": time.time(),
        "signature": list(state.signature or ()),
        "entries": state.entries,
    }
    tmp_path = LOCAL_THREAD_INDEX_CACHE_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(LOCAL_THREAD_INDEX_CACHE_PATH)


def find_session_file_candidates(thread_id: str) -> list[Path]:
    if not thread_id:
        return []
    codex_root = Path.home() / ".codex"
    matches: list[Path] = []
    seen: set[str] = set()
    for base in ("sessions", "archived_sessions"):
        pattern = codex_root / base / "**" / f"*-{thread_id}.jsonl"
        for raw in glob.glob(str(pattern), recursive=True):
            if raw in seen:
                continue
            seen.add(raw)
            matches.append(Path(raw))
    matches.sort(key=lambda item: path_mtime(item), reverse=True)
    return matches


def local_thread_index_signature() -> tuple[float, int, int, float, str, int, float, str]:
    codex_root = Path.home() / ".codex"
    sessions_count, sessions_mtime, sessions_path = scan_directory_tree_signature(codex_root / "sessions")
    archived_count, archived_mtime, archived_path = scan_directory_tree_signature(
        codex_root / "archived_sessions"
    )
    return (
        path_mtime(codex_root / "session_index.jsonl"),
        path_size(codex_root / "session_index.jsonl"),
        sessions_count,
        sessions_mtime,
        sessions_path,
        archived_count,
        archived_mtime,
        archived_path,
    )


def invalidate_local_thread_index_cache() -> None:
    LOCAL_THREAD_INDEX.signature = None
    LOCAL_THREAD_INDEX.entries = []
    LOCAL_THREAD_INDEX.by_thread_id = {}
    LOCAL_THREAD_INDEX.last_loaded_at = 0.0
    LOCAL_THREAD_INDEX.dirty = True


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


def build_local_thread_index_from_files(
    files: list[Path],
    index_map: dict[str, dict[str, str]],
    signature: tuple[Any, ...],
) -> LocalThreadIndexState:
    entries: list[dict[str, Any]] = []
    by_thread_id: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()

    for path in files:
        meta = extract_session_meta(path)
        if not meta:
            continue
        cwd = str(meta.get("cwd") or "").strip()
        thread_id = str(meta.get("id") or "").strip()
        if not cwd or not thread_id or thread_id in seen:
            continue
        seen.add(thread_id)
        index_item = index_map.get(thread_id, {})
        updated_at = index_item.get("updated_at") or datetime.fromtimestamp(path_mtime(path)).isoformat()
        preview = index_item.get("name") or thread_id
        status = {"type": "archived"} if "archived_sessions" in str(path) else {}
        item = {
            "id": thread_id,
            "cwd": cwd,
            "preview": preview,
            "name": preview,
            "path": str(path),
            "updated_at": updated_at,
            "status": status,
        }
        entries.append(item)
        by_thread_id[thread_id] = item

    return LocalThreadIndexState(
        signature=signature,
        entries=entries,
        by_thread_id=by_thread_id,
        last_loaded_at=time.time(),
        dirty=False,
    )


def build_local_thread_index() -> LocalThreadIndexState:
    codex_root = Path.home() / ".codex"
    signature = local_thread_index_signature()
    cached_payload = read_local_thread_index_cache()
    cached_state = hydrate_local_thread_index_state(cached_payload) if cached_payload else None
    if cached_state and cached_state.signature == signature:
        return cached_state

    index_map = load_session_index_map()
    if not cached_state:
        files = [
            *sorted((codex_root / "sessions").glob("**/*.jsonl"), reverse=True),
            *sorted((codex_root / "archived_sessions").glob("**/*.jsonl"), reverse=True),
        ]
        state = build_local_thread_index_from_files(files, index_map, signature)
        save_local_thread_index_cache(state)
        return state

    previous_by_thread = dict(cached_state.by_thread_id)
    entries: list[dict[str, Any]] = []
    by_thread_id: dict[str, dict[str, Any]] = {}
    ordered_ids = sorted(
        index_map.keys(),
        key=lambda thread_id: index_map.get(thread_id, {}).get("updated_at") or "",
        reverse=True,
    )

    for thread_id in ordered_ids:
        cached_item = previous_by_thread.pop(thread_id, None)
        session_path = None
        if cached_item:
            cached_path = Path(str(cached_item.get("path") or ""))
            if cached_path.exists():
                session_path = cached_path
        if not session_path:
            candidates = find_session_file_candidates(thread_id)
            session_path = candidates[0] if candidates else None
        if not session_path:
            continue
        meta = extract_session_meta(session_path)
        cwd = str(meta.get("cwd") or "").strip() if meta else str((cached_item or {}).get("cwd") or "").strip()
        if not cwd:
            continue
        index_item = index_map.get(thread_id, {})
        preview = index_item.get("name") or str((cached_item or {}).get("preview") or thread_id)
        updated_at = index_item.get("updated_at") or str((cached_item or {}).get("updated_at") or "")
        if not updated_at:
            updated_at = datetime.fromtimestamp(path_mtime(session_path)).isoformat()
        status = {"type": "archived"} if "archived_sessions" in str(session_path) else {}
        item = {
            "id": thread_id,
            "cwd": cwd,
            "preview": preview,
            "name": preview,
            "path": str(session_path),
            "updated_at": updated_at,
            "status": status,
        }
        entries.append(item)
        by_thread_id[thread_id] = item

    for thread_id, cached_item in previous_by_thread.items():
        cached_path = Path(str(cached_item.get("path") or ""))
        if not cached_path.exists():
            continue
        item = dict(cached_item)
        entries.append(item)
        by_thread_id[thread_id] = item

    state = LocalThreadIndexState(
        signature=signature,
        entries=entries,
        by_thread_id=by_thread_id,
        last_loaded_at=time.time(),
        dirty=False,
    )
    save_local_thread_index_cache(state)
    return state


def get_local_thread_index() -> LocalThreadIndexState:
    signature = local_thread_index_signature()
    if (
        not LOCAL_THREAD_INDEX.dirty
        and LOCAL_THREAD_INDEX.signature == signature
        and (time.time() - LOCAL_THREAD_INDEX.last_loaded_at) <= LOCAL_THREAD_INDEX_WARM_TTL_SECONDS
    ):
        return LOCAL_THREAD_INDEX
    rebuilt = build_local_thread_index()
    LOCAL_THREAD_INDEX.signature = rebuilt.signature
    LOCAL_THREAD_INDEX.entries = rebuilt.entries
    LOCAL_THREAD_INDEX.by_thread_id = rebuilt.by_thread_id
    LOCAL_THREAD_INDEX.last_loaded_at = rebuilt.last_loaded_at
    LOCAL_THREAD_INDEX.dirty = False
    return LOCAL_THREAD_INDEX


def list_local_threads(repo_path: Path, history_ids: list[str], limit: int = 200) -> list[dict[str, Any]]:
    index = get_local_thread_index()
    repo_str = str(repo_path.resolve())
    items = [item for item in index.entries if item.get("cwd") == repo_str]

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
    return items[:limit]

