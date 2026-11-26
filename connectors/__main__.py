"""Local operator entry points. All task actions use registered identities."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from . import clients, coordinator


ROOT = clients.ROOT
SECRET = "MEMORY_CONNECTION_SECRET"


def client_config(root, client, name):
    coordinator.authenticate(root, name)
    python = root / "runtime/python/Scripts/python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    args = [str(root / "memory_service.py"), "--connection", name]
    if client == "codex":
        return "\n".join((
            "[mcp_servers.project_memory]",
            f"command = {json.dumps(str(python), ensure_ascii=False)}",
            f"args = {json.dumps(args, ensure_ascii=False)}",
            f'env_vars = ["{SECRET}"]',
        ))
    variable = "${env:" + SECRET + "}" if client == "cursor" else "${" + SECRET + "}"
    return json.dumps({"mcpServers": {"project_memory": {
        "type": "stdio", "command": str(python), "args": args, "env": {SECRET: variable},
    }}}, ensure_ascii=False, indent=2)


def explicit_capture(root, name, path):
    import memory_core

    project, _, _ = coordinator.authenticate(root, name)
    args = json.loads(path.read_text(encoding="utf-8-sig"))
    return memory_core.call("memory_capture", args, root=root, project=project)


def capabilities(root):
    report = {}
    for client in ("codex", "claude"):
        item = {
            "surface": "Codex CLI" if client == "codex" else "Claude Code",
            "resume": "确切的 CLI 会话 UUID",
            "notification": "任务簿拉取",
        }
        try:
            item["binary"] = clients.executable(client)
            item.update(clients.check_account(client, item["binary"], root))
        except (OSError, subprocess.SubprocessError) as exc:
            item.update(ready=False, account=type(exc).__name__)
        report[client] = item
    report.update({
        "claude_desktop": {
            "push": False, "resume": False, "collection": "显式选择文件后本机提交",
        },
        "cursor": {
            "executable": shutil.which("cursor"), "push": False, "resume": False,
            "collection": "本地项目 MCP 或显式选择文件",
        },
        "obsidian": {"collection": "直接打开 vault/ 阅读和编辑 Markdown"},
    })
    if os.name == "nt":
        ps = shutil.which("powershell.exe")
        if ps:
            try:
                process_names = subprocess.run(
                    [ps, "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-Process -Name Claude,Cursor,Obsidian -ErrorAction SilentlyContinue).Name"],
                    capture_output=True, text=True, errors="replace", timeout=8, check=False,
                ).stdout.casefold().split()
            except (OSError, subprocess.SubprocessError):
                process_names = []
            for key, process in (("claude_desktop", "claude"), ("cursor", "cursor"), ("obsidian", "obsidian")):
                report[key]["process_seen"] = process in process_names
    return report


def main():
    parser = argparse.ArgumentParser(description="本机项目连接、派工、恢复和显式采集")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("capabilities")
    start = actions.add_parser("start", help="启动任务簿中已经登记的推送轮次")
    start.add_argument("owner_connection")
    start.add_argument("executor_connection")
    start.add_argument("task_id")
    start.add_argument("dispatch_id")
    start.add_argument("client", choices=("codex", "claude"))
    start.add_argument("--cwd", type=Path, required=True)
    start.add_argument("--resume-run")
    for action in ("status", "recover"):
        sub = actions.add_parser(action)
        sub.add_argument("connection")
        sub.add_argument("run_id")
    find = actions.add_parser("find", help="按任务编号找本机派工记录")
    find.add_argument("connection")
    find.add_argument("task_id")
    config = actions.add_parser("config", help="打印单个选定项目的 MCP 配置片段")
    config.add_argument("client", choices=("codex", "claude", "cursor"))
    config.add_argument("connection")
    capture = actions.add_parser("capture", help="提交一个由用户明确选定的 JSON 文件")
    capture.add_argument("connection")
    capture.add_argument("selected_json", type=Path)
    worker = actions.add_parser("_worker")
    worker.add_argument("run_id")
    worker.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "_worker":
        coordinator.worker(args.root, args.run_id)
        return
    if args.action == "capabilities":
        result = capabilities(ROOT)
    elif args.action == "start":
        result = coordinator.enqueue(
            ROOT, args.owner_connection, args.executor_connection, args.task_id, args.dispatch_id,
            args.client, args.cwd, args.resume_run,
        )
    elif args.action == "capture":
        result = explicit_capture(ROOT, args.connection, args.selected_json)
    elif args.action == "find":
        result = coordinator.find(ROOT, args.connection, args.task_id)
    elif args.action == "config":
        print(client_config(ROOT, args.client, args.connection))
        return
    else:
        result = coordinator.status(ROOT, args.connection, args.run_id, args.action == "recover")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
