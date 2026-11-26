import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import ledger_core
import manage
from connectors import clients, coordinator, handoff
from connectors.__main__ import client_config, explicit_capture


class ClientTests(unittest.TestCase):
    def test_exact_session_and_terminal_event_are_required(self):
        session = str(uuid4())
        events = [
            {"type": "thread.started", "thread_id": session},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Answer"}},
            {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}},
        ]
        lines = b"".join(json.dumps(row).encode() + b"\n" for row in events)
        self.assertEqual(clients.read_turn("codex", io.BytesIO(lines), session).result, "Answer")
        self.assertIsNone(clients.read_turn("codex", io.BytesIO(lines[:-1]), session).result)
        self.assertIsNone(clients.read_turn("codex", io.BytesIO(lines), str(uuid4())).result)

    def test_claude_rejects_error_and_uses_session_resume(self):
        session = str(uuid4())
        result = {"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": "Answer"}
        raw = json.dumps(result).encode() + b"\n"
        self.assertEqual(clients.read_turn("claude", io.BytesIO(raw), session).result, "Answer")
        result["is_error"] = True
        bad = json.dumps(result).encode() + b"\n"
        self.assertIsNone(clients.read_turn("claude", io.BytesIO(bad), session).result)
        self.assertEqual(clients.command("claude", "claude.exe", session)[-2:], ["--resume", session])
        self.assertIn("resume", clients.command("codex", "codex.exe", session))

    def test_no_gateway_or_connection_secret_reaches_model_process(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "fixture", "ANTHROPIC_BASE_URL": "fixture",
            "CLAUDE_CODE_USE_BEDROCK": "1", "MEMORY_CONNECTION_SECRET": "fixture",
        }):
            env = clients.environment()
        for key in ("OPENAI_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                    "MEMORY_CONNECTION_SECRET"):
            self.assertNotIn(key, env)

    def test_claude_rejects_non_subscription_auth(self):
        auth = {"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(auth), stderr="")
        with patch.object(clients.subprocess, "run", return_value=result):
            self.assertFalse(clients.check_account("claude", "claude.exe", Path.cwd())["ready"])

    def test_resumed_codex_usage_is_counted_once(self):
        start = {"input_tokens": 100, "output_tokens": 7}
        end = {"input_tokens": 230, "output_tokens": 20}
        self.assertEqual(clients.token_counts("codex", end, continued=True, previous=start),
                         {"input_tokens": 130, "output_tokens": 13})
        self.assertEqual(clients.token_counts("codex", end, continued=True), {})


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        manage.enroll(self.root, "lead", "prj", "lead", "collab")
        manage.enroll(self.root, "executor", "prj", "executor", "collab")
        manage.enroll(self.root, "capture", "prj", "capture", "capture")
        manage.enroll(self.root, "other", "other", "lead", "collab")
        self.task = self.ledger(
            "task_create", title="Task", goal="Check one value", deliverable="Answer",
            criteria=[{"id": "evidence", "requirement": "Show the value"}],
        )["task_id"]
        self.dispatch = self.ledger(
            "task_dispatch", task_id=self.task, previous_dispatch_id=None,
            executor="executor", channel="push", client="codex",
        )["dispatch_id"]

    def ledger(self, name, **args):
        if name not in {"task_get", "task_history"}:
            args["request_id"] = uuid4().hex
        return ledger_core.call(name, args, root=self.root, project="prj", actor="lead")

    def enqueue(self):
        return coordinator.enqueue(
            self.root, "lead", "executor", self.task, self.dispatch, "codex", self.root,
        )

    def reserve(self):
        with patch.object(coordinator.subprocess, "Popen") as launch:
            job = self.enqueue()
            self.assertEqual(self.enqueue()["id"], job["id"])
            self.assertEqual(launch.call_count, 1)
        stored = coordinator.lookup(self.root, job["id"])
        binding = coordinator.authenticate(self.root, "lead")
        grant = coordinator.step(self.root, binding, stored, "task_send_begin")
        coordinator.update(self.root, job["id"], state="sending",
                           reservation=grant["reservation_id"], account="ChatGPT")
        return job["id"]

    def test_unknown_send_recovers_later_receipt_without_resend(self):
        run = self.reserve()
        session = str(uuid4())
        directory = coordinator.files(self.root, run)
        events = directory / "events.jsonl"
        events.write_text(json.dumps({"type": "thread.started", "thread_id": session}) + "\n",
                          encoding="utf-8")
        with patch.object(coordinator.subprocess, "Popen") as launch:
            pending = coordinator.status(self.root, "lead", run, recover=True)
            self.assertEqual((pending["state"], pending["session"]), ("unconfirmed", session))
            self.assertEqual(self.enqueue()["id"], run)
            launch.assert_not_called()
        reply = json.dumps({
            "results": [{"criterion_id": "evidence", "summary": "Value = 11"}],
            "self_check": "Checked", "deliverable_markdown": "The value is 11.",
        })
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "item.completed",
                                     "item": {"type": "agent_message", "text": reply}}) + "\n")
            stream.write(json.dumps({"type": "turn.completed", "usage": {"output_tokens": 3}}) + "\n")
        completed = coordinator.status(self.root, "lead", run, recover=True)
        self.assertEqual(completed["state"], "submitted")
        ledger_round = self.ledger("task_get", task_id=self.task, dispatch_id=self.dispatch)
        self.assertEqual(ledger_round["status"], "pending_review")
        result = ledger_round["dispatch"]["submission"]["results"][0]
        self.assertIn("The value is 11.", Path(result["artifacts"][0]).read_text(encoding="utf-8"))
        self.assertEqual(coordinator.status(self.root, "lead", run, recover=True), completed)
        self.assertEqual(self.enqueue()["id"], run)
        self.assertEqual(coordinator.find(self.root, "lead", self.task)[0]["id"], run)
        for invalid in ("other", "capture"):
            with self.assertRaises(PermissionError):
                coordinator.status(self.root, invalid, run)

    def test_reservation_recovery_never_launches_a_model(self):
        with patch.object(coordinator.subprocess, "Popen") as launch:
            queued = self.enqueue()
            coordinator.update(self.root, queued["id"], state="reserving")
            settled = coordinator.status(self.root, "lead", queued["id"], recover=True)
            self.assertEqual(settled["state"], "not_sent")
            self.assertEqual(launch.call_count, 1)
        row = self.ledger("task_get", task_id=self.task, dispatch_id=self.dispatch)
        self.assertEqual(row["dispatch"]["send_state"], "not_sent")

    def test_verified_interruption_can_resume_the_original_session(self):
        run = self.reserve()
        session = str(uuid4())
        events = coordinator.files(self.root, run) / "events.jsonl"
        events.write_text(json.dumps({"type": "thread.started", "thread_id": session}) + "\n",
                          encoding="utf-8")
        coordinator.status(self.root, "lead", run, recover=True)
        stored = coordinator.lookup(self.root, run)
        self.ledger("task_send_receipt", task_id=self.task, dispatch_id=self.dispatch,
                    reservation_id=stored["reservation"], outcome="sent", receipt_ref="native-proof",
                    session_id=session, evidence="Owner verified original message in the native session")
        self.ledger("task_interrupt", task_id=self.task, dispatch_id=self.dispatch,
                    reason="Old turn stopped", evidence="Owner verified the native turn has stopped")
        follow = self.ledger("task_dispatch", task_id=self.task, previous_dispatch_id=self.dispatch,
                             executor="executor", channel="push", client="codex", session_id=session,
                             instructions="Continue from the last confirmed progress")["dispatch_id"]
        with patch.object(coordinator.subprocess, "Popen") as launch:
            queued = coordinator.enqueue(self.root, "lead", "executor", self.task, follow,
                                         "codex", self.root, resume_run=run)
        launch.assert_called_once()
        self.assertEqual(coordinator.lookup(self.root, queued["id"])["previous"], session)

    def test_explicit_capture_and_config_contain_no_secret(self):
        selected = self.root / "selected.json"
        selected.write_text(json.dumps({
            "client": "cursor", "source_key": "explicit-fixture", "kind": "conversation",
            "scope": "excerpt", "selection": "turn 2 only", "gaps": ["original attachments unavailable"],
            "turns": [{"turn_id": "turn-2", "ordinal": 2, "role": "user", "text": "Only this turn"}],
        }), encoding="utf-8")
        first = explicit_capture(self.root, "capture", selected)
        self.assertEqual(first["source_id"], explicit_capture(self.root, "capture", selected)["source_id"])
        profile = self.root / "runtime/credentials/capture.json"
        secret = json.loads(profile.read_text(encoding="utf-8"))["env"]["MEMORY_CONNECTION_SECRET"]
        for client in ("codex", "claude", "cursor"):
            snippet = client_config(self.root, client, "capture")
            self.assertIn("MEMORY_CONNECTION_SECRET", snippet)
            self.assertNotIn(secret, snippet)

    def test_only_declared_in_scope_text_is_loaded(self):
        allowed = self.root / "allowed"
        allowed.mkdir()
        (allowed / "read.txt").write_text("Selected text", encoding="utf-8")
        (allowed / "unselected.txt").write_text("Unselected text", encoding="utf-8")
        (self.root / "private.txt").write_text("Private outside workspace", encoding="utf-8")
        materials = [{"ref": ref} for ref in ("read.txt", "../private.txt", "https://example.org/article")]
        report = handoff.collect_materials({"materials": materials}, allowed)
        self.assertEqual(report[0]["text"], "Selected text")
        self.assertNotIn("text", report[1])
        self.assertNotIn("text", report[2])
        self.assertEqual(len(report), 3)


if __name__ == "__main__":
    unittest.main()
