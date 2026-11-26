"""Host-approved execution routes for chats with only an MCP connection."""
from contextlib import closing
from pathlib import Path
import sqlite3


def tool(description, required, fields):
    return {"description":description, "mode":"collab", "inputSchema":{
        "type":"object","additionalProperties":False,"required":required,
        "properties":{key:{"type":"string","minLength":1,"maxLength":128} for key in fields}}}


TOOLS = {
    "run_routes":tool("列出本连接登记的执行方与渠道。pull 由目标客户端主动领取，push 可用 run_start 投递。",[],[]),
    "run_start":tool("task_dispatch 登记 push 后直接调用本工具。协调器负责获取一次性发送许可、回执和回报。调用方无需先执行 task_send_begin 或填回执。此操作启动一个本机模型会话，可能使用订阅额度。返回的 id 用于查运行状态。",
                     ["route","task_id","dispatch_id"],["route","task_id","dispatch_id","resume_run"]),
    "run_status":tool("按运行编号读取投递与回报状态。",["run_id"],["run_id"]),
    "run_recover":tool("核对已保存的会话事件并补登回执，未知发送保持待核对。",["run_id"],["run_id"]),
}


def database(root):
    db = sqlite3.connect(root/"runtime/connections.sqlite3")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS routes(owner TEXT, route TEXT, executor TEXT,client TEXT,"
               "workspace TEXT,channel TEXT NOT NULL DEFAULT 'push',PRIMARY KEY(owner,route))")
    if "channel" not in {col[1] for col in db.execute("PRAGMA table_info(routes)")}:
        with db:
            db.execute("BEGIN IMMEDIATE")
            if "channel" not in {col[1] for col in db.execute("PRAGMA table_info(routes)")}:
                db.execute("ALTER TABLE routes ADD COLUMN channel TEXT NOT NULL DEFAULT 'push'")
                db.execute("UPDATE routes SET channel='pull' WHERE client IN ('claude-desktop','cursor')")
    return db


def register(root, owner, route, executor, client, workspace, channel=None):
    from connectors.coordinator import authenticate
    primary, worker = authenticate(root,owner), authenticate(root,executor)
    if primary[0]!=worker[0] or primary[1]==worker[1] or "capture" in (primary[2],worker[2]):
        raise ValueError("派工双方必须是同项目内不同的协作身份")
    path = Path(workspace).resolve(strict=True)
    if not path.is_dir() or client not in ("codex","claude","claude-desktop","cursor"):
        raise ValueError("请选择实际工作目录和支持的客户端")
    channel = channel or ("pull" if client in ("claude-desktop","cursor") else "push")
    if channel not in ("push","pull") or (channel == "push" and client in ("claude-desktop","cursor")):
        raise ValueError("此执行端只支持已登记的推送或主动领取方式")
    with closing(database(root)) as db, db:
        db.execute("INSERT INTO routes(owner,route,executor,client,workspace,channel) VALUES(?,?,?,?,?,?)",
                   (owner,route,executor,client,str(path),channel))


def call(name,args,*,root,connection_name):
    from connectors import coordinator
    if name in ("run_status","run_recover"):
        return coordinator.status(root,connection_name,args["run_id"],recover=name=="run_recover")
    with closing(database(root)) as db:
        if name=="run_routes":
            rows=db.execute("SELECT r.route,r.client,c.actor AS executor,r.channel FROM routes r JOIN connections c "
                            "ON c.name=r.executor WHERE r.owner=? ORDER BY r.route",(connection_name,))
            return [dict(row) for row in rows]
        row=db.execute("SELECT * FROM routes WHERE owner=? AND route=?",(connection_name,args["route"])).fetchone()
    if not row:
        raise PermissionError("该连接未获准使用这条执行路径")
    if row["channel"] == "pull":
        raise ValueError("该客户端从项目任务簿主动领取。使用 task_dispatch 的 pull 渠道并通知相应项目聊天。")
    return coordinator.enqueue(root,connection_name,row["executor"],args["task_id"],args["dispatch_id"],
                               row["client"],row["workspace"],args.get("resume_run"))
