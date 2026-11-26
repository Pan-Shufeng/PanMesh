"""Durable local task ledger. Authentication and transport belong to the caller."""

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from ledger_vendor.failure_classification import classify


class LedgerError(ValueError):
    """A stable application error with a machine-readable code."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _check(condition, code, message):
    if not condition:
        raise LedgerError(code, message)


def _object(required, **properties):
    return {
        "type": "object", "properties": properties,
        "required": required.split(), "additionalProperties": False,
    }


def _string(limit):
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _choice(*values):
    return {"enum": list(values)}


def _array(item, limit=24, minimum=0):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": limit}


def _integer(low=0, high=10**12):
    return {"type": "integer", "minimum": low, "maximum": high}


def _nullable(spec):
    return {"anyOf": [spec, {"type": "null"}]}


def _tool(description, required, **properties):
    return {
        "description": description, "mode": "collab",
        "inputSchema": _object(required, **properties),
    }


ID = _string(128)
TEXT = _string(1200)
REF = _string(512)
SHORT = _string(240)
DECIMAL = {**_string(32), "pattern": r"^[0-9]{1,16}(\.[0-9]{1,12})?$"}
CURRENCY = {**_string(3), "pattern": r"^[A-Z]{3}$"}
TASK = {"task_id": ID}
ROUND = {"task_id": ID, "dispatch_id": ID}
OP = {"request_id": ID}
METHOD = _object("method_id version summary", method_id=ID, version=ID, summary=SHORT, source=REF)
MATERIAL = _object("ref", ref=REF, label=SHORT)
CRITERION = _object("id requirement", id=ID, requirement=SHORT)
DEPENDENCY = _object("task_id criterion_ids", task_id=ID, criterion_ids=_array(ID, minimum=1))
RESULT = _object("criterion_id summary", criterion_id=ID, summary=TEXT, artifacts=_array(REF, 8),
                 evidence=_array(REF, 8))
DECISION = _object("criterion_id decision evidence", criterion_id=ID,
                   decision=_choice("accept", "return", "defer"), evidence=_array(REF, 8, 1),
                   note=TEXT)

TOOLS = {
    "task_create": _tool("创建任务并固定开工所用的材料、做法版本和验收条件。",
                         "request_id title goal deliverable criteria", **OP, title=SHORT,
                         goal=TEXT, deliverable=TEXT, criteria=_array(CRITERION, minimum=1),
                         materials=_array(MATERIAL), methods=_array(METHOD),
                         dependencies=_array(DEPENDENCY), comparison=_object(
                             "case_id arm", case_id=ID, arm=_choice("solo", "collab"),
                             rubric_version=ID, trial_id=ID)),
    "task_list": _tool("查找负责或受派的任务，包括待验收和发送待核对的任务。", "",
                       role=_choice("owner", "executor", "all"),
                       status=_choice("open", "queued", "sending", "send_unknown", "sent",
                                      "running", "pending_review", "needs_user", "needs_rework",
                                      "accepted", "interrupted"),
                       after=_integer(), limit=_integer(1, 100)),
    "task_get": _tool("读取任务和当前可访问的轮次。指定派工编号可读取旧轮次。",
                      "task_id", **ROUND),
    "task_history": _tool("分页读取你有权查看的任务事件。", "task_id", **ROUND,
                          after=_integer(), limit=_integer(1, 100)),
    "task_handoff_item": _tool("按需展开本轮交接中已验收的继承项及其来源。",
                               "task_id dispatch_id source_task_id criterion_id",
                               **ROUND, source_task_id=ID, criterion_id=ID),
    "task_dispatch": _tool("负责人先登记派工意图。每次补派必须声明上一轮编号。",
                           "request_id task_id previous_dispatch_id executor channel client",
                           **TASK, **OP, previous_dispatch_id=_nullable(ID), executor=ID,
                           channel=_choice("pull", "push"), client=ID, session_id=ID,
                           instructions=TEXT, questions=_array(SHORT), method_update=_object(
                               "methods reason", methods=_array(METHOD), reason=TEXT)),
    "task_send_begin": _tool("手动投递时，在你实际发送之前取得一次性许可。由 run_start 投递的轮次由协调器自己领取许可。重试响应的 send_allowed 为 false。",
                             "request_id task_id dispatch_id", **ROUND, **OP),
    "task_send_receipt": _tool("记录或核对推送的回执。确认未送达需要能够证明未执行的证据。",
                               "request_id task_id dispatch_id reservation_id outcome evidence",
                               **ROUND, **OP, reservation_id=ID,
                               outcome=_choice("sent", "unknown", "not_sent"), evidence=REF,
                               receipt_ref=REF, session_id=ID, sender_stopped={"type": "boolean"}),
    "task_claim": _tool("执行方登记实际会话并领取派工。推送回执待核对时可提供收到原消息的证据。",
                        "request_id task_id dispatch_id session_id", **ROUND, **OP,
                        session_id=ID, received_evidence=REF),
    "task_supplement": _tool("负责人追加具体要求，固定的材料与做法版本沿用本轮交接。",
                             "request_id task_id dispatch_id instructions", **ROUND, **OP,
                             instructions=TEXT),
    "task_progress": _tool("执行方回报当前进展及阻塞。", "request_id task_id dispatch_id session_id summary",
                           **ROUND, **OP, session_id=ID, summary=TEXT, evidence=_array(REF, 8),
                           blocked=_array(SHORT, 8)),
    "task_submit": _tool("执行方提交可验收成果、自检和未决项，进入待验收。",
                         "request_id task_id dispatch_id session_id results self_check",
                         **ROUND, **OP, session_id=ID, results=_array(RESULT), self_check=TEXT,
                         unresolved=_array(SHORT), read_originals=_nullable({"type": "boolean"}),
                         sources_read=_array(REF), failure=TEXT),
    "task_review": _tool("负责人按条件独立验收，记录证据并选择接受、退回或待用户判断。",
                         "request_id task_id dispatch_id decisions", **ROUND, **OP,
                         decisions=_array(DECISION, minimum=1), next_step=TEXT),
    "task_interrupt": _tool("负责人确认已停止的会话及证据，保留旧轮次以便补派。",
                            "request_id task_id dispatch_id reason evidence", **ROUND, **OP,
                            reason=TEXT, evidence=REF),
    "task_record_usage": _tool("记录单个模型的分段用量与费用，注明来源及实测或估算口径。",
                               "request_id task_id sample_id model source tokens_kind cost_kind",
                               **ROUND, **OP, sample_id=ID, model=ID, client=ID, account_type=SHORT,
                               source=REF, tokens_kind=_choice("measured", "estimated", "unknown"),
                               input_tokens=_integer(), output_tokens=_integer(), total_tokens=_integer(),
                               cost_kind=_choice("actual", "estimated", "unknown"),
                               cost_amount=DECIMAL, currency=CURRENCY),
    "task_record_metrics": _tool("负责人追加一组试验评估数据。未知项填 null。",
                                 "request_id task_id source", **TASK, **OP, source=REF,
                                 elapsed_seconds=_nullable(_integer()),
                                 user_seconds=_nullable(_integer()),
                                 repeated_explanations=_nullable(_integer()),
                                 rework_count=_nullable(_integer()),
                                 final_missed_errors=_nullable(_integer()),
                                 read_originals=_nullable({"type": "boolean"}),
                                 methods_used=_nullable(_array(METHOD)), notes=TEXT),
    "task_compare": _tool("负责人对照同一案例和验收标准的单 AI 与协作任务，查看已登记用量、费用及质量观测。",
                          "solo_task_id collab_task_id", solo_task_id=ID, collab_task_id=ID),
}

_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS ledger_tasks (
        project TEXT NOT NULL, task_id TEXT NOT NULL, owner TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (project, task_id))""",
    """CREATE TABLE IF NOT EXISTS ledger_rounds (
        project TEXT NOT NULL, task_id TEXT NOT NULL, dispatch_id TEXT NOT NULL,
        number INTEGER NOT NULL, executor TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (project, task_id, dispatch_id), UNIQUE (project, task_id, number),
        FOREIGN KEY (project, task_id) REFERENCES ledger_tasks (project, task_id))""",
    """CREATE TABLE IF NOT EXISTS ledger_events (
        project TEXT NOT NULL, task_id TEXT NOT NULL, seq INTEGER NOT NULL,
        dispatch_id TEXT, actor TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL,
        visibility TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY (project, task_id, seq),
        FOREIGN KEY (project, task_id) REFERENCES ledger_tasks (project, task_id))""",
    """CREATE TABLE IF NOT EXISTS ledger_requests (
        project TEXT NOT NULL, actor TEXT NOT NULL, request_id TEXT NOT NULL,
        tool TEXT NOT NULL, payload TEXT NOT NULL, response TEXT NOT NULL,
        PRIMARY KEY (project, actor, request_id))""",
    """CREATE TABLE IF NOT EXISTS ledger_usage (
        project TEXT NOT NULL, task_id TEXT NOT NULL, actor TEXT NOT NULL, sample_id TEXT NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY (project, task_id, actor, sample_id),
        FOREIGN KEY (project, task_id) REFERENCES ledger_tasks (project, task_id))""",
    """CREATE TABLE IF NOT EXISTS ledger_delivery_refs (
        project TEXT NOT NULL, client TEXT NOT NULL, session_id TEXT NOT NULL,
        receipt_ref TEXT NOT NULL, dispatch_id TEXT NOT NULL,
        PRIMARY KEY (project, client, session_id, receipt_ref))""",
    """CREATE INDEX IF NOT EXISTS ledger_tasks_owner ON ledger_tasks (project, owner)""",
    """CREATE INDEX IF NOT EXISTS ledger_rounds_executor ON ledger_rounds
        (project, executor, task_id, number)""",
)


def _validate(value, spec, field):
    if "anyOf" in spec:
        for option in spec["anyOf"]:
            try:
                _validate(value, option, field)
                return
            except LedgerError:
                continue
        raise LedgerError("invalid_argument", f"{field}: invalid value")
    if "enum" in spec:
        _check(value in spec["enum"], "invalid_argument", f"{field}: invalid choice")
        return
    kind = spec["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "null": type(None), "boolean": bool}
    _check(type(value) is types[kind], "invalid_argument", f"{field}: expected {kind}")
    if kind == "object":
        props = spec["properties"]
        _check(all(key in props for key in value), "invalid_argument", f"{field}: unknown field")
        for key in spec["required"]:
            _check(key in value, "invalid_argument", f"{field}.{key}: required")
        for key, item in value.items():
            _validate(item, props[key], f"{field}.{key}")
    elif kind == "array":
        _check(spec["minItems"] <= len(value) <= spec["maxItems"],
               "invalid_argument", f"{field}: invalid item count")
        for index, item in enumerate(value):
            _validate(item, spec["items"], f"{field}[{index}]")
    elif kind == "string":
        valid = value.strip() and len(value) <= spec["maxLength"]
        valid = valid and not any(ord(char) < 32 and char not in "\n\t\r" for char in value)
        valid = valid and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        _check(valid, "invalid_argument", f"{field}: invalid text")
        if "pattern" in spec:
            _check(re.fullmatch(spec["pattern"], value), "invalid_argument", f"{field}: invalid format")
    elif kind == "integer":
        _check(spec["minimum"] <= value <= spec["maximum"],
               "invalid_argument", f"{field}: out of range")


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id(prefix):
    return prefix + "_" + uuid4().hex


def db_path(root: Path) -> Path:
    """Location of the persistent database. Use SQLite's backup API for live copies."""
    return Path(root) / "runtime" / "task_ledger.sqlite3"


_READS = {"task_get", "task_list", "task_history", "task_handoff_item", "task_compare"}


def call(name, args, *, root: Path, project: str, actor: str):
    """Call a task tool under a trusted, already authorized project and actor."""
    _check(isinstance(name, str) and name in TOOLS, "unknown_tool", "Unknown task tool")
    _validate(project, ID, "project")
    _validate(actor, ID, "actor")
    _validate(args, TOOLS[name]["inputSchema"], "args")
    path = db_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=15)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        initialized = conn.execute("PRAGMA user_version").fetchone()[0]
        _check(initialized in (0, 1), "schema_version", "Unsupported ledger database version")
        if not initialized:
            conn.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        conn.execute("BEGIN" if name in _READS else "BEGIN IMMEDIATE")
        ledger = _Ledger(conn, project, actor)
        handler = getattr(ledger, name)
        if name in _READS:
            return handler(args)

        request_id = args["request_id"]
        key = (project, actor, request_id)
        payload = _dump(args)
        row = conn.execute(
            "SELECT tool, payload, response FROM ledger_requests WHERE project=? AND actor=? AND request_id=?",
            key,
        ).fetchone()
        if row:
            _check((row["tool"], row["payload"]) == (name, payload), "idempotency_conflict",
                   "Request ID was used with a different tool or payload")
            response = json.loads(row["response"])
            if name == "task_send_begin":
                response["send_allowed"] = False
            return {**response, "replayed": True}
        response = handler(args)
        stored = {**response, "send_allowed": False} if name == "task_send_begin" else response
        conn.execute("INSERT INTO ledger_requests VALUES (?, ?, ?, ?, ?, ?)",
                     (*key, name, payload, _dump(stored)))
        return {**response, "replayed": False}


def _fields(args, *names):
    return {name: args[name] for name in names if name in args}


def _index(items, key):
    indexed = {item[key]: item for item in items}
    _check(len(indexed) == len(items), "invalid_argument", f"Duplicate {key}")
    return indexed


def _method_versions(methods):
    _index(methods, "method_id")
    return methods


def _handoff_pointer(item):
    result = item["result"]
    return {**_fields(item, "task_id", "dispatch_id"),
            "criterion_id": item["criterion"]["id"], "summary": result["summary"][:240],
            "artifact": next(iter(result.get("artifacts", [])), None)}


def _status(round_data):
    if not round_data:
        return "open"
    if round_data["state"] != "queued":
        return round_data["state"]
    return {"sending": "sending", "unknown": "send_unknown", "sent": "sent"}.get(
        round_data["send_state"], "queued"
    )


def _recovery(round_data):
    return {
        "open": "dispatch", "queued": "claim" if not round_data or round_data["channel"] == "pull" else "begin_send",
        "sending": "verify_original_session", "send_unknown": "verify_original_session",
        "sent": "check_original_session", "running": "resume_original_session",
        "pending_review": "review", "needs_user": "await_user",
        "needs_rework": "dispatch_next_round", "interrupted": "dispatch_next_round",
        "accepted": "complete",
    }[_status(round_data)]


def _usage_quality(args):
    token_names = {"input_tokens", "output_tokens", "total_tokens"}
    tokens = token_names & args.keys()
    _check(bool(tokens) == (args["tokens_kind"] != "unknown"),
           "invalid_argument", "Known token usage needs a token count; unknown usage omits counts")
    if len(tokens) == 3:
        _check(args["total_tokens"] == args["input_tokens"] + args["output_tokens"],
               "invalid_argument", "Token total must equal input plus output when all are recorded")
    cost = {"cost_amount", "currency"} & args.keys()
    _check(len(cost) == (0 if args["cost_kind"] == "unknown" else 2),
           "invalid_argument", "Known cost requires decimal amount and currency; unknown cost omits both")


_COMPARE_METRICS = (
    "elapsed_seconds", "user_seconds", "repeated_explanations", "rework_count",
    "final_missed_errors", "read_originals", "methods_used",
)


def _combined_tokens(sample):
    if "total_tokens" in sample:
        return sample["total_tokens"]
    if "input_tokens" in sample and "output_tokens" in sample:
        return sample["input_tokens"] + sample["output_tokens"]
    return None


def _subtotal(values):
    return {"sample_count": len(values), "recorded_subtotal": sum(values) if values else None}


def _coverage(available, count):
    if available == 0:
        return "missing"
    return "partial" if available < count else "unverified"


def _usage_summary(samples):
    tokens = {}
    for kind in ("measured", "estimated", "unknown"):
        selected = [sample for sample in samples if sample["tokens_kind"] == kind]
        tokens[kind] = {"sample_count": len(selected)}
        if kind != "unknown":
            tokens[kind].update({
                key: _subtotal([sample[key] for sample in selected if key in sample])
                for key in ("input_tokens", "output_tokens")
            })
            combined = [_combined_tokens(sample) for sample in selected]
            tokens[kind]["combined_tokens"] = _subtotal([count for count in combined if count is not None])

    costs = {"actual": {}, "estimated": {}, "unknown": {"sample_count": 0}}
    amounts = {}
    for sample in samples:
        kind = sample["cost_kind"]
        if kind == "unknown":
            costs[kind]["sample_count"] += 1
        else:
            amounts.setdefault((kind, sample["currency"]), []).append(Decimal(sample["cost_amount"]))
    with localcontext() as context:
        # Each input amount has at most 28 digits; allow carry for all samples.
        context.prec = 28 + len(str(len(samples)))
        for (kind, currency), values in sorted(amounts.items()):
            costs[kind][currency] = {
                "sample_count": len(values),
                "recorded_subtotal": format(sum(values, Decimal(0)), "f"),
            }
    token_count = sum(_combined_tokens(sample) is not None for sample in samples)
    token_coverage = _coverage(token_count, len(samples))
    if token_coverage == "missing" and any(sample["tokens_kind"] != "unknown" for sample in samples):
        token_coverage = "partial"
    cost_count = len(samples) - costs["unknown"]["sample_count"]
    return {
        "sample_count": len(samples),
        "token_coverage": token_coverage,
        "cost_coverage": _coverage(cost_count, len(samples)),
        "tokens": tokens, "costs": costs,
    }


class _Ledger:
    def __init__(self, conn, project, actor):
        self.conn = conn
        self.project = project
        self.actor = actor
        self.at = _now()

    def _task(self, task_id):
        row = self.conn.execute(
            "SELECT data FROM ledger_tasks WHERE project=? AND task_id=?",
            (self.project, task_id),
        ).fetchone()
        _check(row is not None, "not_found", "Task or dispatch is unavailable")
        return json.loads(row["data"])

    def _round(self, task_id, dispatch_id=None, *, executor=None):
        sql = "SELECT data FROM ledger_rounds WHERE project=? AND task_id=?"
        params = [self.project, task_id]
        if dispatch_id is not None:
            sql += " AND dispatch_id=?"
            params.append(dispatch_id)
        if executor is not None:
            sql += " AND executor=?"
            params.append(executor)
        row = self.conn.execute(sql + " ORDER BY number DESC LIMIT 1", params).fetchone()
        return json.loads(row["data"]) if row else None

    def _save_task(self, task):
        self.conn.execute("UPDATE ledger_tasks SET data=? WHERE project=? AND task_id=?",
                          (_dump(task), self.project, task["task_id"]))

    def _save_round(self, round_data):
        self.conn.execute(
            "UPDATE ledger_rounds SET data=? WHERE project=? AND task_id=? AND dispatch_id=?",
            (_dump(round_data), self.project, round_data["task_id"], round_data["dispatch_id"]),
        )

    def _owned(self, args):
        task = self._task(args["task_id"])
        _check(task["owner"] == self.actor, "not_found", "Task or dispatch is unavailable")
        return task

    def _current(self, args, *, owner):
        task = self._owned(args) if owner else self._task(args["task_id"])
        round_data = self._round(args["task_id"], args["dispatch_id"],
                                 executor=None if owner else self.actor)
        _check(round_data is not None, "not_found", "Task or dispatch is unavailable")
        _check(task["current_dispatch_id"] == round_data["dispatch_id"],
               "state_conflict", "This is a historical dispatch")
        return task, round_data

    def _event(self, task_id, kind, data, *, dispatch_id=None, visibility="owner"):
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM ledger_events WHERE project=? AND task_id=?",
            (self.project, task_id),
        ).fetchone()[0]
        self.conn.execute("INSERT INTO ledger_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (self.project, task_id, seq, dispatch_id, self.actor, kind,
                           self.at, visibility, _dump(data)))
        return seq

    def _round_event(self, round_data, kind, data, *, visibility="round"):
        round_data["updated_at"] = self.at
        self._save_round(round_data)
        seq = self._event(round_data["task_id"], kind, data,
                          dispatch_id=round_data["dispatch_id"], visibility=visibility)
        return {**_fields(round_data, "task_id", "dispatch_id", "send_state"),
                "status": _status(round_data), "event_seq": seq}

    def _accepted_dependencies(self, task):
        accepted = []
        for dependency in task["dependencies"]:
            source = self._owned(dependency)
            for criterion_id in dependency["criterion_ids"]:
                item = source["accepted_items"].get(criterion_id)
                _check(item, "dependency_pending", "A prerequisite criterion has not been accepted")
                accepted.append(item)
        return accepted

    def _view(self, task, round_data, owner):
        view = {"task_id": task["task_id"], "title": task["title"], "owner": task["owner"],
                "role": "owner" if owner else "executor", "status": _status(round_data),
                "next_action": _recovery(round_data), "dispatch": None}
        if round_data:
            view["dispatch"] = dict(round_data)
            view["dispatch"].pop("handoff_items", None)
            if not owner:
                for field in ("reservation_id", "receipt", "classification"):
                    view["dispatch"].pop(field, None)
        if owner:
            view["task"] = task
            if task["accepted_at"] and task["started_at"]:
                view["elapsed_seconds"] = (
                    datetime.fromisoformat(task["accepted_at"]) - datetime.fromisoformat(task["started_at"])
                ).total_seconds()
        return view

    def task_create(self, args):
        _index(args["criteria"], "id")
        dependencies = args.get("dependencies", [])
        _index(dependencies, "task_id")
        for dependency in dependencies:
            source = self._owned(dependency)
            ids = set(dependency["criterion_ids"])
            permitted = {criterion["id"] for criterion in source["criteria"]}
            _check(len(ids) == len(dependency["criterion_ids"]) and ids <= permitted,
                   "invalid_argument", "Invalid dependency criteria")
        task_id = _id("task")
        task = {
            "task_id": task_id, "owner": self.actor, "created_at": self.at,
            **_fields(args, "title", "goal", "deliverable", "criteria", "comparison"),
            "materials": args.get("materials", []), "methods": _method_versions(args.get("methods", [])),
            "dependencies": dependencies, "accepted_items": {}, "current_dispatch_id": None,
            "started_at": None, "accepted_at": None, "metrics": None,
        }
        self.conn.execute("INSERT INTO ledger_tasks VALUES (?, ?, ?, ?)",
                          (self.project, task_id, self.actor, _dump(task)))
        seq = self._event(task_id, "created", task)
        return {"task_id": task_id, "status": "open", "event_seq": seq}

    def task_list(self, args):
        role = args.get("role", "all")
        sql = """WITH scoped AS (
                   SELECT ROW_NUMBER() OVER (ORDER BY rowid) AS cursor, project, task_id, owner, data
                   FROM ledger_tasks WHERE project=?
                 )
                 SELECT cursor, data FROM scoped AS task WHERE cursor>?
                 AND ((? != 'executor' AND owner=?) OR (? != 'owner' AND EXISTS (
                     SELECT 1 FROM ledger_rounds AS round WHERE round.project=task.project
                     AND round.task_id=task.task_id AND round.executor=?)))
                 ORDER BY cursor"""
        rows = self.conn.execute(sql, (self.project, args.get("after", 0), role, self.actor,
                                       role, self.actor))
        result = []
        limit = args.get("limit", 50)
        after = None
        for row in rows:
            task = json.loads(row["data"])
            owner = task["owner"] == self.actor
            round_data = self._round(task["task_id"], executor=None if owner else self.actor)
            status = _status(round_data)
            if args.get("status", status) != status:
                continue
            if len(result) == limit:
                after = result[-1]["cursor"]
                break
            result.append({
                **_fields(task, "task_id", "title", "owner"),
                "role": "owner" if owner else "executor", "status": status,
                "next_action": _recovery(round_data), "cursor": row["cursor"],
                "dispatch_id": round_data["dispatch_id"] if round_data else None,
            })
        return {"tasks": result, "next_after": after}

    def task_get(self, args):
        task = self._task(args["task_id"])
        owner = task["owner"] == self.actor
        round_data = self._round(task["task_id"], args.get("dispatch_id"),
                                 executor=None if owner else self.actor)
        _check(round_data is not None or (owner and "dispatch_id" not in args),
               "not_found", "Task or dispatch is unavailable")
        return self._view(task, round_data, owner)

    def task_history(self, args):
        access = self.task_get(args)
        sql = ("SELECT seq, dispatch_id, actor, kind, at, data FROM ledger_events "
               "WHERE project=? AND task_id=? AND seq>?")
        params = [self.project, args["task_id"], args.get("after", 0)]
        if "dispatch_id" in args:
            sql += " AND dispatch_id=?"
            params.append(args["dispatch_id"])
        if access["role"] == "executor":
            sql += (" AND visibility='round' AND dispatch_id IN "
                    "(SELECT dispatch_id FROM ledger_rounds WHERE project=? AND task_id=? AND executor=?)")
            params.extend([self.project, args["task_id"], self.actor])
        limit = args.get("limit", 50)
        rows = self.conn.execute(sql + " ORDER BY seq LIMIT ?", (*params, limit + 1)).fetchall()
        events = [{**dict(row), "data": json.loads(row["data"])} for row in rows[:limit]]
        return {"events": events, "next_after": events[-1]["seq"] if len(rows) > limit else None}

    def task_handoff_item(self, args):
        access = self.task_get(_fields(args, "task_id", "dispatch_id"))
        round_data = self._round(args["task_id"], args["dispatch_id"],
                                 executor=self.actor if access["role"] == "executor" else None)
        for item in round_data["handoff_items"]:
            if item["task_id"] == args["source_task_id"] and item["criterion"]["id"] == args["criterion_id"]:
                return {"item": item}
        raise LedgerError("not_found", "Handoff item is unavailable")

    def task_dispatch(self, args):
        task = self._owned(args)
        previous_id = task["current_dispatch_id"]
        _check(args["previous_dispatch_id"] == previous_id, "state_conflict", "Dispatch head has changed")
        previous = self._round(task["task_id"], previous_id) if previous_id else None
        if previous:
            _check(previous["state"] in ("needs_rework", "interrupted"), "state_conflict",
                   "The previous dispatch must be returned or confirmed stopped")
            _check("instructions" in args, "invalid_argument", "A follow-up requires specific instructions")
        _check(args["executor"] != task["owner"], "invalid_argument",
               "The independent reviewer and executor must be distinct actors")
        deps = self._accepted_dependencies(task)
        methods = previous["handoff"]["methods"] if previous else task["methods"]
        if "method_update" in args:
            methods = _method_versions(args["method_update"]["methods"])
        dispatch_id = _id("dispatch")
        outstanding = [item for item in task["criteria"] if item["id"] not in task["accepted_items"]]
        handoff = {
            **_fields(task, "task_id", "title", "goal", "deliverable", "materials"),
            "dispatch_id": dispatch_id, "criteria": outstanding, "methods": methods,
            "questions": args.get("questions", []), "instructions": args.get("instructions", ""),
            "accepted_work": [_handoff_pointer(item) for item in task["accepted_items"].values()],
            "prerequisites": [_handoff_pointer(item) for item in deps],
        }
        round_data = {
            **_fields(args, "executor", "channel", "client"),
            "dispatch_id": dispatch_id, "task_id": task["task_id"],
            "number": previous["number"] + 1 if previous else 1,
            "previous_dispatch_id": previous_id, "target_session_id": args.get("session_id"),
            "session_id": None, "state": "queued",
            "send_state": "queued" if args["channel"] == "push" else "not_required",
            "handoff": handoff, "handoff_items": [*task["accepted_items"].values(), *deps],
            "latest_progress": None, "submission": None, "review": {},
            "created_at": self.at, "updated_at": self.at,
        }
        self.conn.execute("INSERT INTO ledger_rounds VALUES (?, ?, ?, ?, ?, ?)",
                          (self.project, task["task_id"], dispatch_id, round_data["number"],
                           args["executor"], _dump(round_data)))
        task["current_dispatch_id"] = dispatch_id
        self._save_task(task)
        info = _fields(round_data, "dispatch_id", "number", "executor", "channel", "client",
                       "target_session_id", "handoff", "previous_dispatch_id")
        if "method_update" in args:
            info["method_update_reason"] = args["method_update"]["reason"]
        seq = self._event(task["task_id"], "dispatched", info, dispatch_id=dispatch_id, visibility="round")
        return {"task_id": task["task_id"], "dispatch_id": dispatch_id, "status": "queued",
                "handoff": handoff, "event_seq": seq}

    def task_send_begin(self, args):
        _, round_data = self._current(args, owner=True)
        _check(round_data["state"] == "queued" and round_data["send_state"] in ("queued", "not_sent"),
               "state_conflict", "Send is not available; inspect the original dispatch")
        reservation_id = _id("send")
        round_data.update(send_state="sending", reservation_id=reservation_id, receipt=None)
        event = self._round_event(round_data, "send_begun", {"reservation_id": reservation_id},
                                  visibility="owner")
        return {**event, "send_allowed": True, "reservation_id": reservation_id,
                "client": round_data["client"], "session_id": round_data["target_session_id"],
                "handoff": round_data["handoff"], **_fields(round_data, "latest_supplement")}

    def task_send_receipt(self, args):
        task = self._owned(args)
        round_data = self._round(task["task_id"], args["dispatch_id"])
        _check(round_data is not None, "not_found", "Task or dispatch is unavailable")
        _check(round_data.get("reservation_id") == args["reservation_id"],
               "state_conflict", "The sending reservation does not match")
        outcome = args["outcome"]
        _check(("receipt_ref" in args) == (outcome == "sent"), "invalid_argument",
               "Confirmed delivery requires an external receipt reference")
        _check(outcome != "not_sent" or args.get("sender_stopped") is True, "invalid_argument",
               "Confirm the previous sender is stopped before certifying that the dispatch was not sent")
        target = round_data["target_session_id"]
        supplied_session = args.get("session_id")
        if outcome == "sent":
            _check(target or supplied_session, "invalid_argument", "Confirmed delivery must bind its session")
            _check(target is None or supplied_session in (None, target),
                   "state_conflict", "Confirmed delivery must match the original target session")
            target = target or supplied_session
        else:
            _check(supplied_session is None, "invalid_argument", "Only confirmed delivery may bind a session")
        receipt = _fields(args, "reservation_id", "outcome", "evidence", "receipt_ref", "sender_stopped")
        if outcome == "sent":
            receipt["session_id"] = target
        previous = round_data["send_state"]
        if round_data.get("receipt") == receipt:
            return {**_fields(round_data, "task_id", "dispatch_id", "send_state"),
                    "status": _status(round_data), "receipt": receipt, "unchanged": True}
        may_record = (previous in ("sending", "unknown") or (
            previous == "sent" and outcome == "sent" and round_data.get("receipt") is None
        ))
        _check(may_record, "state_conflict", "A confirmed receipt cannot be overwritten")
        if outcome == "sent":
            key = (self.project, round_data["client"], target, args["receipt_ref"])
            row = self.conn.execute(
                "SELECT dispatch_id FROM ledger_delivery_refs WHERE project=? AND client=? "
                "AND session_id=? AND receipt_ref=?", key,
            ).fetchone()
            _check(row is None or row["dispatch_id"] == round_data["dispatch_id"],
                   "state_conflict", "Receipt already belongs to a different dispatch")
            if row is None:
                self.conn.execute("INSERT INTO ledger_delivery_refs VALUES (?, ?, ?, ?, ?)",
                                  (*key, round_data["dispatch_id"]))
        round_data.update(send_state=outcome, receipt=receipt, target_session_id=target)
        event = self._round_event(round_data, "send_receipt", receipt, visibility="owner")
        return {**event, "receipt": receipt}

    def task_claim(self, args):
        task, round_data = self._current(args, owner=False)
        target = round_data["target_session_id"]
        _check(target is None or args["session_id"] == target,
               "state_conflict", "Session does not match the target session")
        _check(round_data["state"] == "queued", "state_conflict", "Dispatch is already claimed or closed")
        send_state = round_data["send_state"]
        info = _fields(args, "session_id", "received_evidence")
        if target and send_state in ("sending", "unknown") and "received_evidence" in args:
            round_data["send_state"] = "sent"
            # Preserve any earlier unknown receipt in the event log. The external
            # confirmed receipt can still arrive after the executor starts work.
            round_data["receipt"] = None
            info["corroborated_delivery"] = True
        else:
            _check(send_state in ("sent", "not_required"), "state_conflict",
                   "Wait for delivery or provide evidence of the original push")
        round_data.update(state="running", session_id=args["session_id"], claimed_at=self.at)
        if task["started_at"] is None:
            task["started_at"] = self.at
            self._save_task(task)
        event = self._round_event(round_data, "claimed", info)
        return {**event, "session_id": round_data["session_id"], "handoff": round_data["handoff"],
                **_fields(round_data, "latest_supplement")}

    def task_supplement(self, args):
        _, round_data = self._current(args, owner=True)
        _check(round_data["state"] in ("queued", "running"), "state_conflict",
               "Supplement instructions on an active dispatch or add them to the follow-up")
        info = {"instructions": args["instructions"], "at": self.at}
        round_data["latest_supplement"] = info
        return self._round_event(round_data, "supplemented", info)

    def _working(self, args):
        task, round_data = self._current(args, owner=False)
        _check(round_data["state"] == "running" and args["session_id"] == round_data["session_id"],
               "state_conflict", "Reporting requires the claimed, active session")
        return task, round_data

    def task_progress(self, args):
        _, round_data = self._working(args)
        progress = {**_fields(args, "summary", "evidence", "blocked"), "at": self.at}
        round_data["latest_progress"] = progress
        return self._round_event(round_data, "progress", progress)

    def task_submit(self, args):
        _, round_data = self._working(args)
        criteria = {item["id"] for item in round_data["handoff"]["criteria"]}
        results = _index(args["results"], "criterion_id")
        _check(results.keys() <= criteria, "invalid_argument", "Result refers to an out-of-scope criterion")
        _check(bool(results) or "failure" in args, "invalid_argument",
               "Submit work or explain why no results are available")
        submission = {
            **_fields(args, "results", "self_check", "unresolved", "read_originals", "sources_read", "failure"),
            "at": self.at,
        }
        round_data.update(state="pending_review", submission=submission)
        return self._round_event(round_data, "submitted", submission)

    def task_review(self, args):
        task, round_data = self._current(args, owner=True)
        _check(round_data["state"] in ("pending_review", "needs_user"), "state_conflict",
               "Review requires a submitted dispatch")
        decisions = _index(args["decisions"], "criterion_id")
        submission = round_data["submission"]
        results = _index(submission["results"], "criterion_id")
        criteria = _index(round_data["handoff"]["criteria"], "id")
        _check(decisions.keys() <= criteria.keys(), "invalid_argument", "Review item is outside this dispatch")
        for criterion_id, item in decisions.items():
            previous = round_data["review"].get(criterion_id)
            _check(not previous or previous["decision"] == "defer", "state_conflict",
                   "An accepted or returned criterion cannot be overwritten")
            decision = {**item, "reviewer": self.actor, "at": self.at}
            if item["decision"] == "accept":
                _check(criterion_id in results, "invalid_argument", "Acceptance requires a submitted result")
                task["accepted_items"][criterion_id] = {
                    "task_id": task["task_id"], "dispatch_id": round_data["dispatch_id"],
                    "criterion": criteria[criterion_id], "result": results[criterion_id],
                    "review": decision,
                }
            round_data["review"][criterion_id] = decision
        if "next_step" in args:
            round_data["next_step"] = args["next_step"]
        review = round_data["review"]
        deferred = any(item["decision"] == "defer" for item in review.values())
        if deferred:
            round_data["state"] = "needs_user"
        elif review.keys() == criteria.keys():
            returned = [item for item in review.values() if item["decision"] == "return"]
            if returned:
                round_data["state"] = "needs_rework"
                round_data["classification"] = [
                    classify({
                        "id": round_data["dispatch_id"],
                        "status": "failed" if item["criterion_id"] not in results else "reviewed",
                        "error": submission.get("failure", "") if item["criterion_id"] not in results else "",
                        "review": {"state": "returned", "returned_criteria": [item["criterion_id"]],
                                   "executor_status": "completed" if item["criterion_id"] in results else "failed"},
                    }) for item in returned
                ]
            else:
                round_data["state"] = "accepted"
                task["accepted_at"] = self.at
                round_data["classification"] = [classify({
                    "id": round_data["dispatch_id"], "review": {"state": "accepted",
                                                                "executor_status": "completed"},
                })]
        else:
            round_data["state"] = "pending_review"
        self._save_task(task)
        info = {"decisions": [review[key] for key in decisions], "state": round_data["state"],
                **_fields(args, "next_step")}
        return self._round_event(round_data, "reviewed", info)

    def task_interrupt(self, args):
        _, round_data = self._current(args, owner=True)
        _check(round_data["state"] in ("queued", "running")
               and round_data["send_state"] not in ("sending", "unknown"),
               "state_conflict", "Reconcile unknown delivery or review submitted work before closing this dispatch")
        info = {**_fields(args, "reason", "evidence"), "at": self.at}
        round_data.update(state="interrupted", interruption=info)
        return self._round_event(round_data, "interrupted", info)

    def task_record_usage(self, args):
        task = self._task(args["task_id"])
        is_owner = task["owner"] == self.actor
        dispatch_id = args.get("dispatch_id")
        if dispatch_id or not is_owner:
            round_data = self._round(task["task_id"], dispatch_id,
                                     executor=None if is_owner else self.actor) if dispatch_id else None
            _check(round_data is not None, "not_found", "Task or dispatch is unavailable")
            _check(is_owner or round_data["session_id"] is not None,
                   "state_conflict", "Usage requires a claimed dispatch")
        _usage_quality(args)
        data = {key: value for key, value in args.items() if key != "request_id"}
        key = (self.project, task["task_id"], self.actor, args["sample_id"])
        row = self.conn.execute(
            "SELECT data FROM ledger_usage WHERE project=? AND task_id=? AND actor=? AND sample_id=?", key,
        ).fetchone()
        if row:
            _check(json.loads(row["data"]) == data, "idempotency_conflict", "Usage sample conflicts")
            return {"task_id": task["task_id"], "sample_id": args["sample_id"], "unchanged": True}
        self.conn.execute("INSERT INTO ledger_usage VALUES (?, ?, ?, ?, ?)", (*key, _dump(data)))
        seq = self._event(task["task_id"], "usage_recorded", data, dispatch_id=dispatch_id,
                          visibility="owner" if is_owner else "round")
        return {"task_id": task["task_id"], "sample_id": args["sample_id"], "event_seq": seq}

    def task_record_metrics(self, args):
        task = self._owned(args)
        metrics = {key: value for key, value in args.items() if key not in ("task_id", "request_id")}
        _check(len(metrics) > 1, "invalid_argument", "Provide at least one measurement or note")
        task["metrics"] = {**metrics, "at": self.at}
        self._save_task(task)
        seq = self._event(task["task_id"], "metrics_recorded", metrics)
        return {"task_id": task["task_id"], "event_seq": seq}

    def _comparison_arm(self, task):
        key = (self.project, task["task_id"])
        rows = self.conn.execute(
            "SELECT data FROM ledger_usage WHERE project=? AND task_id=?",
            key,
        )
        samples = [json.loads(row["data"]) for row in rows]
        models = {}
        for sample in samples:
            models.setdefault(sample["model"], []).append(sample)
        usage = _usage_summary(samples)
        usage["models"] = {model: _usage_summary(values) for model, values in sorted(models.items())}

        metrics = dict.fromkeys(_COMPARE_METRICS)
        sources = {}
        rows = self.conn.execute(
            "SELECT at, data FROM ledger_events WHERE project=? AND task_id=? "
            "AND kind='metrics_recorded' ORDER BY seq DESC", key,
        )
        for row in rows:
            recorded = json.loads(row["data"])
            for key_name in (metrics.keys() & recorded.keys()) - sources.keys():
                metrics[key_name] = recorded[key_name]
                sources[key_name] = {"source": recorded["source"], "at": row["at"]}
            if len(sources) == len(metrics):
                break
        current = self._round(task["task_id"], task["current_dispatch_id"])
        ledger_elapsed = None
        if task["accepted_at"] and task["started_at"]:
            ledger_elapsed = (
                datetime.fromisoformat(task["accepted_at"]) - datetime.fromisoformat(task["started_at"])
            ).total_seconds()
        return {
            **_fields(task, "task_id", "title"), **_fields(task["comparison"], "trial_id"),
            "status": _status(current), "ledger_elapsed_seconds": ledger_elapsed,
            "usage": usage, "metrics": metrics, "metric_sources": sources,
        }

    def task_compare(self, args):
        solo = self._owned({"task_id": args["solo_task_id"]})
        collab = self._owned({"task_id": args["collab_task_id"]})
        left = solo.get("comparison", {})
        right = collab.get("comparison", {})
        _check(left.get("arm") == "solo" and right.get("arm") == "collab",
               "invalid_argument", "Compare one solo task and one collaboration task in that order")
        _check(left["case_id"] == right["case_id"] and left.get("rubric_version")
               and left["rubric_version"] == right.get("rubric_version"),
               "invalid_argument", "Both tasks must have the same case and an explicit matching rubric version")
        return {**_fields(left, "case_id", "rubric_version"),
                "solo": self._comparison_arm(solo), "collab": self._comparison_arm(collab)}
