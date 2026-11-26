"""Opt-in local Stop hook for one explicitly selected conversation transcript."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import hashlib
from pathlib import Path
import sqlite3
import sys

import memory_core
from connectors.coordinator import authenticate

ROOT = Path(__file__).resolve().parent


def database(root):
    db = sqlite3.connect(root / "runtime/hook-selections.sqlite3")
    db.execute("CREATE TABLE IF NOT EXISTS selections(connection TEXT, client TEXT, session TEXT,"
               "workspace TEXT, transcript TEXT, last_result TEXT, updated TEXT,"
               "PRIMARY KEY(connection,client,session))")
    db.execute("CREATE TABLE IF NOT EXISTS checkpoints(connection TEXT,client TEXT,session TEXT,count INTEGER,"
               "digest TEXT,PRIMARY KEY(connection,client,session))")
    return db


def authorize(root, name, client, session, workspace, transcript):
    authenticate(root, name)
    workspace = Path(workspace).resolve(strict=True)
    transcript = Path(transcript).resolve(strict=True)
    if not workspace.is_dir() or transcript.suffix.lower() != ".jsonl" or not transcript.is_file():
        raise ValueError("请选择实际工作目录和明确选中的会话 JSONL 文件")
    with closing(database(root)) as db, db:
        db.execute("INSERT INTO selections VALUES (?,?,?,?,?,NULL,NULL)",
                   (name, client, session, str(workspace), str(transcript)))


def visible_turns(path, client, session):
    turns = []
    identified = False
    turn_id = None
    formats = {}
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            event = json.loads(line)
            if client == "codex":
                data = event.get("payload") or {}
                if event.get("type") == "session_meta":
                    if data.get("id") != session:
                        raise ValueError("会话编号不匹配")
                    identified = True
                if event.get("type") != "event_msg":
                    continue
                form = data.get("type")
                if form == "task_started":
                    turn_id = data.get("turn_id")
                if form == "item_completed":
                    item = data.get("item") or {}
                    if item.get("type") not in ("UserMessage", "AgentMessage"):
                        continue
                    role = "user" if item["type"] == "UserMessage" else "assistant"
                    content = "\n".join(b["text"] for b in item.get("content", [])
                                        if str(b.get("type")).lower()=="text" and isinstance(b.get("text"),str))
                elif form in ("user_message", "agent_message"):
                    role = "user" if form == "user_message" else "assistant"
                    content = data.get("message")
                else:
                    continue
                # Some releases record the same visible message in both forms.
                if isinstance(content, str):
                    key = (data.get("turn_id") or turn_id, role, content)
                    if formats.setdefault(key,form) != form:
                        continue
            else:
                if event.get("sessionId") and event["sessionId"] != session:
                    raise ValueError("会话编号不匹配")
                if event.get("sessionId") != session or event.get("isMeta") or event.get("isSidechain"):
                    continue
                identified = True
                role = event.get("type")
                if role not in ("user", "assistant") or event.get("isCompactSummary"):
                    continue
                content = (event.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = "\n".join(b["text"] for b in content
                                        if b.get("type") == "text" and isinstance(b.get("text"), str))
            if isinstance(content, str) and content:
                turns.append(dict(turn_id=f"L{number}", ordinal=number, role=role, text=content))
    if not identified or not turns:
        raise ValueError("未能核对会话或提取可见正文")
    return turns


def on_stop(root, name, client, event):
    session = event.get("session_id")
    if event.get("hook_event_name") != "Stop" or not session:
        return None
    with closing(database(root)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        key = (name, client, session)
        row = db.execute("SELECT workspace,transcript FROM selections WHERE connection=? AND client=? AND session=?", key).fetchone()
        if not row:
            return None
        try:
            if not event.get("cwd") or not event.get("transcript_path"):
                raise ValueError("客户端缺少会话文件或项目路径")
            workspace, path = (Path(value).resolve(strict=True) for value in (event["cwd"], event["transcript_path"]))
            # Windows clients sometimes report the same file with a long-path prefix.
            if not (workspace.samefile(row[0]) and path.samefile(row[1])):
                raise ValueError("事件与已选择的工作目录或会话文件不匹配")
            project, _, _ = authenticate(root, name)
            turns = visible_turns(path, client, session)
            checkpoint = db.execute("SELECT count,digest FROM checkpoints WHERE connection=? AND client=? AND session=?", key).fetchone()
            def fingerprint(items):
                return hashlib.sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            start = checkpoint[0] if checkpoint else 0
            if checkpoint and (len(turns) < start or fingerprint(turns[:start]) != checkpoint[1]):
                raise ValueError("已收录的对话前段发生变化，需要核查")
            if len(turns) == start:
                return None
            result = memory_core.call("memory_capture", dict(
                client=client, source_key=session, kind="conversation", scope="excerpt",
                selection=f"已选择会话中第 {turns[start]['ordinal']} 至 {turns[-1]['ordinal']} 行的可见文字",
                turns=turns[start:], gaps=["附件、工具输出和非正文内容不在自动采集范围"]), root=root, project=project)
            status = result["capture_id"]
            db.execute("INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)", (*key,len(turns),fingerprint(turns)))
        except Exception as exc:
            status = "失败：" + type(exc).__name__
            result = None
            print(status + "。请核对会话路径和连接后重试。", file=sys.stderr)
        db.execute("UPDATE selections SET last_result=?,updated=? WHERE connection=? AND client=? AND session=?",
                   (status, datetime.now(timezone.utc).isoformat(), *key))
    if result is None:
        raise RuntimeError("选定会话采集失败，已登记状态")
    return result


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8-sig")
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    cmds = p.add_subparsers(dest="action", required=True)
    for action in ("select", "stop", "unselect", "status"):
        sub = cmds.add_parser(action)
        sub.add_argument("connection")
        sub.add_argument("client", choices=("codex","claude"))
        if action in ("select", "unselect"):
            sub.add_argument("session")
        if action == "select":
            sub.add_argument("workspace")
            sub.add_argument("transcript")
    a = p.parse_args()
    if a.action == "select":
        authorize(ROOT, a.connection, a.client, a.session, a.workspace, a.transcript)
    elif a.action == "stop":
        on_stop(ROOT, a.connection, a.client, json.load(sys.stdin))
    else:
        with closing(database(ROOT)) as connection, connection:
            if a.action == "unselect":
                connection.execute("DELETE FROM selections WHERE connection=? AND client=? AND session=?",
                                   (a.connection, a.client, a.session))
            else:
                for record in connection.execute("SELECT session,last_result,updated FROM selections WHERE connection=? AND client=?",
                                                 (a.connection, a.client)):
                    print(*record)
