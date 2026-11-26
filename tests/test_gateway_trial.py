from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import Mock, patch

import gateway_trial as trial


_ENV = {"ANTHROPIC_BASE_URL": "https://gateway.example/relay",
        "ANTHROPIC_AUTH_TOKEN": "private-mock-bearer-token", "ANTHROPIC_API_KEY": ""}


def _response(**overrides):
    return json.dumps({
        "type": "message", "id": "msg-01", "model": "served-model",
        "content": [{"type": "text", "text": "answer"}], "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 4}, **overrides,
    }).encode("utf-8")


def _parameters(root, **overrides):
    return {
        "root": root, "gateway_host": "gateway.example", "request_id": "round-1",
        "model": "requested-model", "messages": [{"role": "user", "content": "hello"}],
        "max_output_tokens": 8, "input_token_upper_bound": 2048, "currency": "USD",
        "total_budget": "0.012", "input_per_million": "3", "output_per_million": "15", **overrides,
    }


def _budget_worker(item):
    root, number = item
    with patch.dict("os.environ", _ENV), patch.object(
        trial, "_post", return_value=(200, "req-01", _response()),
    ) as send:
        try:
            trial.run_trial(**_parameters(root, request_id=f"request-{number}",
                                          messages=[{"role": "user", "content": str(number)}]))
        except trial.GatewayTrialError as error:
            return error.code, send.call_count
        return "confirmed", send.call_count


class GatewayTrialTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict("os.environ", _ENV)
        env.start()
        self.addCleanup(env.stop)
        # Any exercise of the transport goes through an explicit fake connection.
        guard = patch.object(trial.http.client, "HTTPSConnection",
                             side_effect=AssertionError("Unexpected network access"))
        guard.start()
        self.addCleanup(guard.stop)

    def run_trial(self, **overrides):
        return trial.run_trial(**_parameters(self.root, **overrides))

    def assert_error(self, code, **overrides):
        with self.assertRaises(trial.GatewayTrialError) as caught:
            self.run_trial(**overrides)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def rows(self):
        with closing(sqlite3.connect(self.root / "runtime" / "gateway_trial.sqlite3")) as conn:
            return conn.execute("SELECT state, reserved, receipt FROM requests ORDER BY id").fetchall()

    def test_multiturn_messages_schema_auth_and_receipt(self):
        messages = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "继续"},
                    {"role": "user", "content": "第二轮"}]
        with patch.object(trial, "_post", return_value=(200, "req-123", _response(usage={
            "input_tokens": 5, "cache_read_input_tokens": 7, "cache_creation_input_tokens": 2,
            "output_tokens": 4,
        }))) as send:
            result = self.run_trial(messages=messages, system="Answer briefly.")
            replay = self.run_trial(messages=messages, system="Answer briefly.")
        send.assert_called_once()
        host, port, path, headers, body, timeout = send.call_args.args
        self.assertEqual((host, port, path, timeout), ("gateway.example", 443, "/relay/v1/messages", 45))
        self.assertEqual(headers["Authorization"], "Bearer " + _ENV["ANTHROPIC_AUTH_TOKEN"])
        self.assertNotIn("x-api-key", headers)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(json.loads(body), {"model": "requested-model", "messages": messages,
                                           "max_tokens": 8, "system": "Answer briefly."})
        self.assertEqual(result["text"], "answer")
        self.assertEqual(result["provider_model"], "served-model")
        self.assertEqual(result["message_id"], "msg-01")
        self.assertEqual(result["gateway_request_id"], "req-123")
        self.assertEqual(result["usage"]["total_input_tokens"], 14)
        self.assertEqual(result["estimated_cost"], "0.000102")
        self.assertEqual(result["reserved_budget"], "0.006264")
        self.assertIsNone(result["billed_cost"])
        self.assertGreaterEqual(result["elapsed_seconds"], 0)
        self.assertEqual(replay, {**result, "replayed": True})

    def test_gateway_host_https_and_input_floor_are_checked_before_sending(self):
        with patch.object(trial, "_post") as send:
            self.assert_error("invalid_gateway", gateway_host="elsewhere.example")
            self.assert_error("invalid_gateway", gateway_host="https://gateway.example")
            for malformed in (
                "http://gateway.example", "https://gateway.example@other.example",
                "https://gateway.example?secret=1", "https://gateway.example/v1/../admin",
                "https://gateway.example/%2E/example", "https://gateway.example:0",
            ):
                with self.subTest(malformed=malformed), patch.dict("os.environ", {"ANTHROPIC_BASE_URL": malformed}):
                    self.assert_error("invalid_gateway")
            self.assert_error("input_bound_too_low", input_token_upper_bound=32)
            self.assert_error("invalid_argument", messages=[{"role": "system", "content": "inline"}])
            self.assert_error("credential_in_payload", messages=[
                {"role": "user", "content": _ENV["ANTHROPIC_AUTH_TOKEN"]},
            ])
            self.assert_error("invalid_argument", fixed_per_request="1")
            self.assert_error("budget_exceeded", total_budget="0.000001")
        send.assert_not_called()

    def test_api_key_header_and_fixed_price_with_missing_usage(self):
        values = {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": "private-test-api-key",
                  "ANTHROPIC_BASE_URL": "https://gateway.example:9443/prefix/v1"}
        fixed = {"fixed_per_request": "0.01", "input_per_million": None, "output_per_million": None}
        with patch.dict("os.environ", values), patch.object(
            trial, "_post", return_value=(200, None, _response(usage=None)),
        ) as send:
            result = self.run_trial(**fixed)
            self.assert_error("budget_exceeded", **fixed, request_id="next",
                              messages=[{"role": "user", "content": "next"}])
        send.assert_called_once()
        self.assertEqual(send.call_args.args[1:3], (9443, "/prefix/v1/messages"))
        self.assertEqual(send.call_args.args[3]["x-api-key"], values["ANTHROPIC_API_KEY"])
        self.assertNotIn("Authorization", send.call_args.args[3])
        self.assertIsNone(result["usage"])
        self.assertEqual(result["estimated_cost"], "0.01")
        self.assertEqual(result["reserved_budget"], "0.01")

    def test_redirect_is_not_followed_and_unknown_keeps_full_reservation(self):
        response = Mock(status=307)
        response.getheader.side_effect = lambda name: {
            "location": "https://elsewhere.example", "request-id": "redirect-id",
        }.get(name)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(trial.http.client, "HTTPSConnection", return_value=connection) as transport:
            first = self.assert_error("outcome_unknown")
            replay = self.assert_error("outcome_unknown")
            self.assert_error("duplicate_request", request_id="new-id-same-payload")
            self.assert_error("budget_exceeded", request_id="next",
                              messages=[{"role": "user", "content": "different"}])
        transport.assert_called_once()
        connection.request.assert_called_once()
        response.read.assert_not_called()
        self.assertEqual(first.receipt, replay.receipt)
        self.assertEqual(first.receipt["http_status"], 307)
        self.assertEqual(first.receipt["gateway_request_id"], "redirect-id")
        state, reserved, _ = self.rows()[0]
        self.assertEqual((state, reserved), ("unknown", 6264000000))

    def test_transport_exception_is_sanitized_and_no_retry_is_performed(self):
        with patch.object(trial, "_post", side_effect=TimeoutError(_ENV["ANTHROPIC_AUTH_TOKEN"])) as send:
            error = self.assert_error("outcome_unknown")
            self.assert_error("outcome_unknown")
        send.assert_called_once()
        self.assertNotIn(_ENV["ANTHROPIC_AUTH_TOKEN"], str(error))
        self.assertNotIn(_ENV["ANTHROPIC_AUTH_TOKEN"], self.rows()[0][2])

    def test_http_success_without_message_id_is_unknown(self):
        with patch.object(trial, "_post", return_value=(200, "req-200", _response(id=None))) as send:
            error = self.assert_error("outcome_unknown")
            self.assert_error("outcome_unknown")
        self.assertEqual(error.receipt["http_status"], 200)
        self.assertEqual(error.receipt["gateway_request_id"], "req-200")
        self.assertEqual(self.rows()[0][0], "unknown")
        send.assert_called_once()

    def test_response_secrets_are_redacted_before_saving_and_printing(self):
        secret = _ENV["ANTHROPIC_AUTH_TOKEN"]
        # Splitting the credential across text blocks must not evade redaction.
        raw = _response(id="message-" + secret, model=secret, content=[
            {"type": "text", "text": secret[:8]}, {"type": "text", "text": secret[8:]},
        ])
        output, errors = io.StringIO(), io.StringIO()
        args = [
            "--gateway-host", "gateway.example", "--request-id", "cli-1", "--model", "requested",
            "--prompt", "hello", "--max-output-tokens", "8", "--input-token-upper-bound", "2048",
            "--currency", "USD", "--total-budget", "0.012", "--fixed-per-request", "0.01",
        ]
        run_from_test_root = trial.run_trial
        with patch.object(trial, "run_trial", side_effect=lambda **kw: run_from_test_root(root=self.root, **kw)), (
            patch.object(trial, "_post", return_value=(200, "header-" + secret, raw))
        ), redirect_stdout(output), redirect_stderr(errors):
            exit_code = trial.main(args)
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())["text"], "[REDACTED]")
        self.assertNotIn(secret, output.getvalue() + errors.getvalue() + self.rows()[0][2])

    def test_crash_after_reservation_cannot_resend_and_budget_cannot_be_increased(self):
        with patch.object(trial, "_post", side_effect=KeyboardInterrupt) as send:
            with self.assertRaises(KeyboardInterrupt):
                self.run_trial()
            self.assert_error("outcome_unknown")
            self.assert_error("budget_conflict", total_budget="0.50")
            self.assert_error("budget_conflict", currency="CNY")
        send.assert_called_once()
        self.assertEqual(self.rows()[0][:2], ("reserved", 6264000000))

    def test_reported_output_overrun_freezes_further_requests(self):
        with patch.object(trial, "_post", return_value=(200, "req", _response(usage={
            "input_tokens": 3, "output_tokens": 9,
        }))) as send:
            reply = self.run_trial(total_budget="1")
            self.assertTrue(reply["bounds_exceeded"])
            self.assertIn("message_id", reply)
            self.assert_error("budget_frozen", total_budget="1", request_id="next",
                              messages=[{"role": "user", "content": "different"}])
        send.assert_called_once()

    def test_concurrent_replays_cannot_double_send(self):
        gate = Barrier(4, timeout=10)

        def attempt():
            gate.wait()
            try:
                reply = self.run_trial()
            except trial.GatewayTrialError as error:
                return error.code
            return "replayed" if reply["replayed"] else "confirmed"

        with patch.object(trial, "_post", return_value=(200, "req", _response())) as send:
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(lambda _: attempt(), range(4)))
        self.assertEqual(results.count("confirmed"), 1)
        self.assertTrue(set(results) <= {"confirmed", "replayed", "outcome_unknown"})
        send.assert_called_once()

    def test_distinct_processes_reserve_against_one_cap(self):
        with ProcessPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(_budget_worker, [(self.root, index) for index in range(5)]))
        self.assertEqual(results.count(("confirmed", 1)), 1)
        self.assertEqual(results.count(("budget_exceeded", 0)), 4)
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()
