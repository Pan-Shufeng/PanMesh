from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from uuid import uuid4

from ledger_core import LedgerError, TOOLS, call, db_path


class TaskLedgerTests(unittest.TestCase):
    owner = "codex-main"
    executor = "claude-executor"
    session = "original-session"

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def act(self, name, *, actor=None, project="alpha", **args):
        tool = "task_" + name
        if "request_id" in TOOLS[tool]["inputSchema"]["required"]:
            args.setdefault("request_id", uuid4().hex)
        return call(tool, args, root=self.root, project=project, actor=actor or self.owner)

    def error(self, code, name, **kwargs):
        with self.assertRaises(LedgerError) as caught:
            self.act(name, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def create(self, **kwargs):
        return self.act("create", **{
            "title": "核对原文", "goal": "逐项比对样品和条件", "deliverable": "带页码的复核表",
            "criteria": [{"id": "sample", "requirement": "核对样品编号"},
                         {"id": "potential", "requirement": "核对原文电位"}],
            "methods": [{"method_id": "no3rr", "version": "1", "summary": "区分工作点"}],
            "materials": [{"ref": "source:authorized-paper"}], **kwargs,
        })["task_id"]

    def dispatch(self, task_id, **kwargs):
        return self.act("dispatch", **{
            "task_id": task_id, "previous_dispatch_id": None, "executor": self.executor,
            "client": "claude", "channel": "pull", **kwargs,
        })

    def claim(self, round_args, **kwargs):
        return self.act("claim", **{
            **round_args, "actor": self.executor, "session_id": self.session, **kwargs,
        })

    def submit(self, round_args, **kwargs):
        return self.act("submit", **{
            **round_args, "actor": self.executor, "session_id": self.session, "self_check": "自检完成",
            "results": [{"criterion_id": "sample", "summary": "样品 A3", "artifacts": ["sample.xlsx"]},
                        {"criterion_id": "potential", "summary": "电位 -0.3 V", "artifacts": ["potential.csv"]}],
            **kwargs,
        })

    def decide(self, round_args, **decisions):
        return self.act("review", **round_args, decisions=[
            {"criterion_id": key, "decision": decision, "evidence": [f"原文 Fig. 2 / {key}"]}
            for key, decision in decisions.items()
        ])

    def test_handoff_review_and_follow_up_preserve_only_accepted_work(self):
        task_id = self.create()
        first = self.dispatch(task_id)
        old_id = first["dispatch_id"]
        old = {"task_id": task_id, "dispatch_id": old_id}
        self.claim(old)
        self.act("supplement", **old, instructions="补记引用页数")
        self.act("progress", **old, actor=self.executor, session_id=self.session, summary="正在对照图 2")
        self.assertEqual(self.submit(old)["status"], "pending_review")
        self.assertEqual(self.decide(old, sample="accept", potential="defer")["status"], "needs_user")
        self.error("state_conflict", "review", **old, decisions=[
            {"criterion_id": "sample", "decision": "return", "evidence": ["page 4"]}])
        self.assertEqual(self.decide(old, potential="return")["status"], "needs_rework")
        next_round = self.dispatch(
            task_id, previous_dispatch_id=old_id, executor="another",
            instructions="重查电位读数", method_update={
                "methods": [{"method_id": "no3rr", "version": "2", "summary": "限制同样品"}],
                "reason": "补充样品边界",
            },
        )
        new = {"task_id": task_id, "dispatch_id": next_round["dispatch_id"]}
        self.assertEqual([item["id"] for item in next_round["handoff"]["criteria"]], ["potential"])
        pointer, = next_round["handoff"]["accepted_work"]
        self.assertEqual(pointer["criterion_id"], "sample")
        detail = self.act("handoff_item", **new, actor="another", source_task_id=task_id, criterion_id="sample")
        self.assertEqual(detail["item"]["result"]["artifacts"], ["sample.xlsx"])
        self.error("not_found", "handoff_item", **new, actor="another", source_task_id=task_id,
                   criterion_id="potential")
        self.error("not_found", "get", **old, actor="another")
        self.error("not_found", "get", **new, actor=self.executor)
        self.error("state_conflict", "progress", **old, actor=self.executor, session_id=self.session,
                   summary="迟到回报")
        self.claim(new, actor="another", session_id="new-session")
        self.submit(new, actor="another", session_id="new-session",
                    results=[{"criterion_id": "potential", "summary": "电位 -0.35 V"}])
        self.assertEqual(self.decide(new, potential="accept")["status"], "accepted")
        self.assertEqual(self.act("get", **old)["dispatch"]["handoff"]["methods"][0]["version"], "1")
        self.assertEqual(self.act("get", **old)["dispatch"]["classification"][0]["reason"], "verified_quality_gap")
        view = self.act("get", **new)
        self.assertEqual(view["dispatch"]["handoff"]["methods"][0]["version"], "2")
        self.assertEqual(len(view["task"]["accepted_items"]), 2)
        self.assertGreaterEqual(view["elapsed_seconds"], 0)

    def test_identity_project_validation_and_scoped_history(self):
        task_id = self.create()
        self.error("invalid_argument", "dispatch", task_id=task_id, previous_dispatch_id=None,
                   executor=self.owner, client="codex", channel="pull")
        self.error("not_found", "get", task_id=task_id, actor=self.executor)
        dispatch_id = self.dispatch(task_id)["dispatch_id"]
        round_args = {"task_id": task_id, "dispatch_id": dispatch_id}
        for actor in (self.owner, self.executor):
            self.assertEqual(self.act("list", project="beta", actor=actor)["tasks"], [])
            self.error("not_found", "get", **round_args, project="beta", actor=actor)
        self.error("not_found", "claim", **round_args, actor=self.owner, session_id=self.session)
        self.claim(round_args)
        self.error("state_conflict", "submit", **round_args, actor=self.executor, session_id="wrong",
                   self_check="wrong session", results=[])
        self.submit(round_args)
        self.error("not_found", "review", **round_args, actor=self.executor, decisions=[
            {"criterion_id": "sample", "decision": "accept", "evidence": ["self"]}])
        self.error("invalid_argument", "get", task_id=task_id, owner=self.owner)
        self.act("record_metrics", task_id=task_id, source="owner notes", user_seconds=12)
        events = self.act("history", task_id=task_id, actor=self.executor)["events"]
        self.assertEqual([event["kind"] for event in events], ["dispatched", "claimed", "submitted"])

    def test_project_local_pagination(self):
        first = self.create()
        self.create(project="beta")
        second = self.create()
        page = self.act("list", limit=1)
        self.assertEqual(page["tasks"][0]["task_id"], first)
        self.assertEqual(page["next_after"], 1)
        next_page = self.act("list", limit=1, after=page["next_after"])
        self.assertEqual(next_page["tasks"][0]["task_id"], second)
        self.assertEqual(next_page["tasks"][0]["cursor"], 2)
        self.assertIsNone(next_page["next_after"])

    def test_push_unknown_recovery_and_replay(self):
        task_id = self.create()
        dispatch_id = self.dispatch(task_id, channel="push", session_id=self.session)["dispatch_id"]
        round_args = {"task_id": task_id, "dispatch_id": dispatch_id}
        begin = self.act("send_begin", **round_args, request_id="exact-begin")
        self.assertTrue(begin["send_allowed"])
        self.assertFalse(self.act("send_begin", **round_args, request_id="exact-begin")["send_allowed"])
        receipt = {**round_args, "reservation_id": begin["reservation_id"]}
        self.assertEqual(self.act("send_receipt", **receipt, outcome="unknown", evidence="timeout")["status"],
                         "send_unknown")
        self.error("state_conflict", "send_begin", **round_args)
        self.error("state_conflict", "interrupt", **round_args, reason="timeout", evidence="timeout")
        self.error("state_conflict", "dispatch", task_id=task_id, previous_dispatch_id=dispatch_id,
                   executor="another", client="claude", instructions="试一次", channel="pull")
        self.error("invalid_argument", "send_receipt", **receipt, outcome="not_sent", evidence="empty search")
        self.error("state_conflict", "claim", **round_args, actor=self.executor,
                   session_id="wrong", received_evidence="messsage ref")
        self.claim(round_args, received_evidence="original-message")
        self.submit(round_args)
        self.assertEqual(self.act("send_receipt", **receipt, outcome="sent", receipt_ref="msg-1",
                                  evidence="server confirmation")["status"], "pending_review")
        self.assertEqual(self.act("list", role="owner", status="pending_review")["tasks"][0]["task_id"], task_id)
        executor_view = self.act("get", **round_args, actor=self.executor)["dispatch"]
        self.assertNotIn("reservation_id", executor_view)
        self.assertNotIn("receipt", executor_view)

    def test_cli_first_push_binds_actual_session_from_confirmed_receipt(self):
        task_id = self.create()
        dispatch_id = self.dispatch(task_id, channel="push")["dispatch_id"]
        round_args = {"task_id": task_id, "dispatch_id": dispatch_id}
        begin = self.act("send_begin", **round_args)
        self.assertIsNone(begin["session_id"])
        receipt = {**round_args, "reservation_id": begin["reservation_id"], "evidence": "CLI event"}
        self.error("state_conflict", "claim", **round_args, actor=self.executor, session_id=self.session,
                   received_evidence="self declared")
        self.error("invalid_argument", "send_receipt", **receipt, outcome="unknown", session_id="untrusted")
        self.error("invalid_argument", "send_receipt", **receipt, outcome="sent", receipt_ref="turn-1")
        confirmed = self.act("send_receipt", **receipt, outcome="sent", session_id=self.session,
                             receipt_ref="turn-1")
        self.assertEqual(confirmed["receipt"]["session_id"], self.session)
        self.error("state_conflict", "send_receipt", **receipt, outcome="sent", session_id="different",
                   receipt_ref="turn-1")
        self.claim(round_args)
        self.assertEqual(self.act("get", **round_args)["dispatch"]["target_session_id"], self.session)

    def test_proven_unsent_retry_rejects_stale_reservation_and_duplicate_transport_ref(self):
        task_id = self.create()
        dispatch_id = self.dispatch(task_id, channel="push", session_id=self.session)["dispatch_id"]
        round_args = {"task_id": task_id, "dispatch_id": dispatch_id}
        token = self.act("send_begin", **round_args)["reservation_id"]
        self.act("send_receipt", **round_args, reservation_id=token, outcome="not_sent",
                 sender_stopped=True, evidence="transport rejected before start; sender joined")
        current = self.act("send_begin", **round_args)["reservation_id"]
        self.assertNotEqual(token, current)
        self.error("state_conflict", "send_receipt", **round_args, reservation_id=token, outcome="sent",
                   receipt_ref="stale", evidence="old job")
        self.act("send_receipt", **round_args, reservation_id=current, outcome="sent", receipt_ref="msg-3",
                 evidence="turn started")
        self.act("interrupt", **round_args, reason="原进程已关闭", evidence="turn cancelled and joined")
        follow = self.dispatch(task_id, previous_dispatch_id=dispatch_id, channel="push",
                               session_id=self.session, instructions="使用同一会话继续")
        new = {"task_id": task_id, "dispatch_id": follow["dispatch_id"]}
        new_token = self.act("send_begin", **new)["reservation_id"]
        self.error("state_conflict", "send_receipt", **new, reservation_id=new_token, outcome="sent",
                   receipt_ref="msg-3", evidence="not a new message")

    def test_dependencies_only_use_accepted_items_from_same_project_and_owner(self):
        task_id = self.create()
        deps = [{"task_id": task_id, "criterion_ids": ["sample"]}]
        self.error("not_found", "create", title="new", goal="goal", deliverable="result",
                   criteria=[{"id": "r", "requirement": "sample"}], dependencies=deps, project="beta")
        self.error("not_found", "create", title="new", goal="goal", deliverable="result",
                   criteria=[{"id": "r", "requirement": "sample"}], dependencies=deps, actor="other")
        downstream = self.create(dependencies=deps)
        self.error("dependency_pending", "dispatch", task_id=downstream, previous_dispatch_id=None,
                   executor="other", client="codex", channel="pull")
        old = {"task_id": task_id, "dispatch_id": self.dispatch(task_id)["dispatch_id"]}
        self.claim(old)
        self.submit(old)
        self.decide(old, sample="accept", potential="return")
        handoff = self.dispatch(downstream, executor="other")["handoff"]
        item, = handoff["prerequisites"]
        self.assertEqual(item["task_id"], task_id)
        self.assertEqual(item["criterion_id"], "sample")

    def race(self, size, operation):
        gate = Barrier(size, timeout=10)

        def run(index):
            gate.wait()
            try:
                return operation(index)
            except LedgerError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=size) as workers:
            return list(workers.map(run, range(size)))

    def test_concurrent_create_dispatch_claim_and_single_send_authorization(self):
        creates = self.race(6, lambda _: self.create(request_id="create-once"))
        self.assertEqual(len(set(creates)), 1)
        task_id = creates[0]
        dispatches = self.race(6, lambda _: self.dispatch(task_id, channel="push", session_id=self.session))
        winner, = [result for result in dispatches if isinstance(result, dict)]
        self.assertEqual(dispatches.count("state_conflict"), 5)
        round_args = {"task_id": task_id, "dispatch_id": winner["dispatch_id"]}
        begins = self.race(6, lambda _: self.act("send_begin", **round_args, request_id="same-send"))
        self.assertEqual(sum(result["send_allowed"] for result in begins), 1)
        self.act("send_receipt", **round_args, reservation_id=begins[0]["reservation_id"],
                 outcome="sent", receipt_ref="id", evidence="confirmed")
        claims = self.race(6, lambda _: self.claim(round_args))
        self.assertEqual(sum(isinstance(result, dict) for result in claims), 1)
        self.assertEqual(claims.count("state_conflict"), 5)

    def test_submit_replay_and_validation_leave_single_event(self):
        task_id = self.create()
        round_args = {"task_id": task_id, "dispatch_id": self.dispatch(task_id)["dispatch_id"]}
        self.claim(round_args)
        result = self.submit(round_args, request_id="same-submit")
        self.assertTrue(self.submit(round_args, request_id="same-submit")["replayed"])
        self.error("idempotency_conflict", "submit", **round_args, actor=self.executor, session_id=self.session,
                   self_check="changed", results=[], request_id="same-submit")
        events = self.act("history", **round_args, after=result["event_seq"] - 1)["events"]
        self.assertEqual([event["kind"] for event in events], ["submitted"])

    def test_uses_source_based_usage_and_keeps_unknown_metrics(self):
        task_id = self.create(comparison={"arm": "solo", "case_id": "held-out-1", "rubric_version": "r1"})
        round_args = {"task_id": task_id, "dispatch_id": self.dispatch(task_id)["dispatch_id"]}
        self.claim(round_args)
        values = {
            **round_args, "actor": self.executor, "sample_id": "p1", "model": "model-a",
            "source": "usage report", "tokens_kind": "measured", "input_tokens": 21,
            "output_tokens": 14, "total_tokens": 35, "cost_kind": "actual",
            "cost_amount": "0.014", "currency": "USD",
        }
        self.act("record_usage", **values)
        self.assertTrue(self.act("record_usage", **values)["unchanged"])
        self.error("idempotency_conflict", "record_usage", **{**values, "cost_amount": "0.028"})
        self.error("invalid_argument", "record_usage", **{**values, "total_tokens": True})
        self.act("record_usage", task_id=task_id, sample_id="p2", model="owner-model",
                 source="client does not disclose usage", tokens_kind="unknown", cost_kind="unknown")
        self.act("record_metrics", task_id=task_id, source="user diary", rework_count=0,
                 final_missed_errors=None, user_seconds=60)
        metrics = self.act("get", task_id=task_id)["task"]["metrics"]
        self.assertIsNone(metrics["final_missed_errors"])
        self.assertEqual(metrics["rework_count"], 0)

    def comparison_pair(self, **collab_metadata):
        common = {"case_id": "case-1", "rubric_version": "criteria-v1"}
        solo = self.create(comparison={**common, "arm": "solo"})
        collab = self.create(comparison={**common, "arm": "collab", **collab_metadata})
        return {"solo_task_id": solo, "collab_task_id": collab}

    def test_compare_reports_separate_recorded_subtotals_per_model_kind_and_currency(self):
        pair = self.comparison_pair()
        solo, collab = pair.values()

        def record(task_id, sample_id, model, **fields):
            self.act("record_usage", **{
                "task_id": task_id, "sample_id": sample_id, "model": model,
                "source": "client usage", "tokens_kind": "unknown", "cost_kind": "unknown", **fields,
            })

        for _ in range(2):
            record(solo, "s1", "model-a", tokens_kind="measured", input_tokens=6, output_tokens=4,
                   cost_kind="actual", cost_amount="0.10", currency="USD")
        record(collab, "c1", "model-a", tokens_kind="measured", total_tokens=7,
               cost_kind="actual", cost_amount="9999999999999999.999999999999", currency="USD")
        record(collab, "c2", "model-a", tokens_kind="estimated", input_tokens=13,
               cost_kind="estimated", cost_amount="0.45", currency="USD")
        record(collab, "c3", "model-b", cost_kind="actual", cost_amount="0", currency="EUR")
        record(collab, "c4", "model-b", cost_kind="actual", cost_amount="0.000000000001", currency="USD")
        record(collab, "c5", "model-c")
        result = self.act("compare", **pair)
        self.assertEqual(result["case_id"], "case-1")
        self.assertEqual(result["rubric_version"], "criteria-v1")
        solo_usage = result["solo"]["usage"]
        self.assertEqual(solo_usage["sample_count"], 1)
        self.assertEqual(solo_usage["token_coverage"], "unverified")
        self.assertEqual(solo_usage["cost_coverage"], "unverified")
        self.assertEqual(solo_usage["tokens"]["measured"]["combined_tokens"]["recorded_subtotal"], 10)
        usage = result["collab"]["usage"]
        self.assertEqual(usage["sample_count"], 5)
        self.assertEqual(usage["token_coverage"], "partial")
        self.assertEqual(usage["cost_coverage"], "partial")
        self.assertEqual(usage["tokens"]["unknown"]["sample_count"], 3)
        self.assertEqual(usage["costs"]["unknown"]["sample_count"], 1)
        self.assertEqual(usage["costs"]["actual"]["USD"],
                         {"recorded_subtotal": "10000000000000000.000000000000", "sample_count": 2})
        self.assertEqual(usage["costs"]["actual"]["EUR"]["recorded_subtotal"], "0")
        self.assertEqual(usage["costs"]["estimated"]["USD"]["recorded_subtotal"], "0.45")
        first_model = usage["models"]["model-a"]
        self.assertEqual(first_model["tokens"]["measured"]["combined_tokens"]["recorded_subtotal"], 7)
        self.assertIsNone(first_model["tokens"]["estimated"]["combined_tokens"]["recorded_subtotal"])
        self.assertEqual(first_model["tokens"]["estimated"]["input_tokens"]["recorded_subtotal"], 13)
        self.assertEqual(usage["models"]["model-c"]["cost_coverage"], "missing")

    def test_compare_preserves_metric_sources_and_distinguishes_missing_from_zero(self):
        pair = self.comparison_pair()
        solo, collab = pair.values()
        self.act("record_metrics", task_id=solo, source="baseline measurement",
                 user_seconds=90, rework_count=0, final_missed_errors=2)
        self.act("record_metrics", task_id=solo, source="revised review", final_missed_errors=None)
        self.act("record_metrics", task_id=collab, source="collaboration measurement",
                 user_seconds=24, elapsed_seconds=100, rework_count=1, final_missed_errors=0)
        self.act("record_usage", task_id=solo, sample_id="input-only", model="model-a",
                 source="visible input", tokens_kind="measured", input_tokens=0, cost_kind="unknown")
        result = self.act("compare", **pair)
        baseline = result["solo"]
        self.assertEqual(baseline["status"], "open")
        self.assertEqual(baseline["metrics"]["user_seconds"], 90)
        self.assertEqual(baseline["metrics"]["rework_count"], 0)
        self.assertIsNone(baseline["metrics"]["final_missed_errors"])
        self.assertEqual(baseline["metric_sources"]["rework_count"]["source"], "baseline measurement")
        self.assertEqual(baseline["metric_sources"]["final_missed_errors"]["source"], "revised review")
        self.assertNotIn("elapsed_seconds", baseline["metric_sources"])
        self.assertEqual(baseline["usage"]["token_coverage"], "partial")
        self.assertIsNone(baseline["ledger_elapsed_seconds"])
        other = result["collab"]
        self.assertEqual(other["usage"]["token_coverage"], "missing")
        self.assertEqual(other["usage"]["cost_coverage"], "missing")
        self.assertEqual(other["metrics"]["user_seconds"], 24)
        self.assertEqual(other["metrics"]["rework_count"], 1)
        self.assertEqual(other["metrics"]["final_missed_errors"], 0)
        self.assertEqual(other["metrics"]["elapsed_seconds"], 100)

    def test_compare_requires_both_owners_project_case_rubric_and_correct_arms(self):
        pair = self.comparison_pair()
        self.error("not_found", "compare", **pair, actor=self.executor)
        self.error("not_found", "compare", **pair, project="beta")
        self.error("invalid_argument", "compare", solo_task_id=pair["collab_task_id"],
                   collab_task_id=pair["solo_task_id"])
        self.error("invalid_argument", "compare", **self.comparison_pair(case_id="different-case"))
        self.error("invalid_argument", "compare", **self.comparison_pair(rubric_version="criteria-v2"))
        without_rubric = self.create(comparison={"arm": "collab", "case_id": "case-1"})
        self.error("invalid_argument", "compare", solo_task_id=pair["solo_task_id"],
                   collab_task_id=without_rubric)
        different_project = self.create(project="beta", comparison={
            "arm": "collab", "case_id": "case-1", "rubric_version": "criteria-v1",
        })
        self.error("not_found", "compare", solo_task_id=pair["solo_task_id"],
                   collab_task_id=different_project)
        different_owner = self.create(actor=self.executor, comparison={
            "arm": "collab", "case_id": "case-1", "rubric_version": "criteria-v1",
        })
        self.error("not_found", "compare", solo_task_id=pair["solo_task_id"],
                   collab_task_id=different_owner)

    def test_failure_without_work_does_not_count_as_quality(self):
        task_id = self.create()
        round_args = {"task_id": task_id, "dispatch_id": self.dispatch(task_id)["dispatch_id"]}
        self.claim(round_args)
        self.submit(round_args, results=[], failure="Connection reset")
        self.error("invalid_argument", "review", **round_args, decisions=[
            {"criterion_id": "sample", "decision": "accept", "evidence": ["nothing to inspect"]}])
        self.decide(round_args, sample="return", potential="return")
        findings = self.act("get", **round_args)["dispatch"]["classification"]
        self.assertEqual({item["outcome"] for item in findings}, {"system"})
        self.assertFalse(any(item["counts_as_model_quality"] for item in findings))

    def test_persistence_after_process_exit_and_sqlite_restore(self):
        task_id = self.create()
        round_args = {"task_id": task_id, "dispatch_id": self.dispatch(task_id)["dispatch_id"]}
        self.claim(round_args)
        code = (
            "from ledger_core import call; import json,sys; from pathlib import Path; "
            "args=json.loads(sys.argv[2]); "
            "print(json.dumps(call('task_submit',args,root=Path(sys.argv[1]),"
            "project='alpha',actor=sys.argv[3])))"
        )
        args = {**round_args, "request_id": "remote-submit", "session_id": self.session,
                "results": [{"criterion_id": "sample", "summary": "Checked original"}], "self_check": "done"}
        process = subprocess.run(
            [sys.executable, "-B", "-c", code, str(self.root), json.dumps(args), self.executor],
            cwd=Path(__file__).resolve().parents[1], check=True, capture_output=True, text=True,
        )
        self.assertEqual(json.loads(process.stdout)["status"], "pending_review")
        with TemporaryDirectory() as restored:
            restored_root = Path(restored)
            db_path(restored_root).parent.mkdir()
            with closing(sqlite3.connect(db_path(self.root))) as source, closing(
                sqlite3.connect(db_path(restored_root))
            ) as target:
                source.backup(target)
                self.assertEqual(target.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            received = call("task_get", round_args, root=restored_root, project="alpha", actor=self.owner)
            self.assertEqual(received["dispatch"]["submission"]["results"][0]["summary"], "Checked original")
            replay = call("task_submit", args, root=restored_root, project="alpha", actor=self.executor)
            self.assertTrue(replay["replayed"])


if __name__ == "__main__":
    unittest.main()
