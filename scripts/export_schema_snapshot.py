from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


def parse_command(raw: str) -> list[str]:
    parts = shlex.split(raw, posix=os.name != "nt")
    if not parts:
        raise ValueError("空命令")
    return parts


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--command",
        default=os.getenv("CODEX_COMMAND", "cmd /c npx @openai/codex@0.122.0"),
        help="用于调用 codex CLI 的命令",
    )
    parser.add_argument(
        "--out",
        default=str(Path("schema") / "app-server-0.122.0.snapshot.json"),
        help="快照输出路径",
    )
    args = parser.parse_args()

    command = parse_command(args.command)
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="codex-schema-") as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        subprocess.run(
            [*command, "app-server", "generate-json-schema", "--out", str(temp_dir)],
            check=True,
        )
        client_request = read_json(temp_dir / "ClientRequest.json")
        client_notification = read_json(temp_dir / "ClientNotification.json")
        server_notification = read_json(temp_dir / "ServerNotification.json")
        server_request = read_json(temp_dir / "ServerRequest.json")
        steer_params = read_json(temp_dir / "v2" / "TurnSteerParams.json")

        request_methods = sorted(
            item["properties"]["method"]["enum"][0]
            for item in client_request.get("oneOf", [])
            if isinstance(item, dict)
            and item.get("properties", {}).get("method", {}).get("enum")
        )
        notification_methods = sorted(
            item["properties"]["method"]["enum"][0]
            for item in client_notification.get("oneOf", [])
            if isinstance(item, dict)
            and item.get("properties", {}).get("method", {}).get("enum")
        )
        server_notification_methods = sorted(
            item["properties"]["method"]["enum"][0]
            for item in server_notification.get("oneOf", [])
            if isinstance(item, dict)
            and item.get("properties", {}).get("method", {}).get("enum")
        )
        server_request_methods = sorted(
            item["properties"]["method"]["enum"][0]
            for item in server_request.get("oneOf", [])
            if isinstance(item, dict)
            and item.get("properties", {}).get("method", {}).get("enum")
        )

        version_text = subprocess.run(
            [*command, "--version"],
            check=True,
            capture_output=True,
            text=True,
        )
        version = (version_text.stdout or version_text.stderr).strip().splitlines()[0]
        snapshot = {
            "codex_version": version,
            "request_methods": request_methods,
            "notification_methods": notification_methods,
            "server_notification_methods": server_notification_methods,
            "server_request_methods": server_request_methods,
            "turn_steer_required": steer_params.get("required", []),
        }
        out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
