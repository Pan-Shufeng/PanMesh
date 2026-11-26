"""Operator-only Claude Messages trial, with an immutable local budget.

Uses the Python standard library and the existing ANTHROPIC_BASE_URL plus
ANTHROPIC_AUTH_TOKEN (Bearer) or ANTHROPIC_API_KEY (x-api-key). Direct TLS;
the only outbound operation is one POST per newly reserved request.

Budget values are operator-supplied in a single currency. A reservation is
never released against an estimated bill. Store results only in the local
runtime directory and submit only material authorized for the gateway.
"""

import argparse
from contextlib import closing, contextmanager
from hashlib import sha256
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import ssl
import sys
from time import monotonic
from urllib.parse import urlsplit


_ROOT = Path(__file__).resolve().parent
_MICRO = 10**6
_UNIT = 10**12
_SQLITE_MAX = 2**63 - 1
_INPUT_PADDING = 1024
_RESPONSE_LIMIT = 4 * 1024 * 1024
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                 "cache_read_input_tokens")


class GatewayTrialError(ValueError):
    """A sanitized error; raw provider exceptions and response bodies are private."""

    def __init__(self, code, message, *, receipt=None):
        super().__init__(message)
        self.code = code
        self.receipt = receipt


def _require(condition, code, message):
    if not condition:
        raise GatewayTrialError(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _digest(value):
    return sha256(value).hexdigest()


def _hostname(value):
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    return (isinstance(value, str) and len(value) <= 253
            and re.fullmatch(rf"{label}(?:\.{label})*", value.lower(), re.ASCII))


def _settings(allowed_hostname):
    _require(_hostname(allowed_hostname), "invalid_gateway", "Provide the approved HTTPS hostname.")
    raw_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    try:
        base = urlsplit(raw_base)
        host = base.hostname
        port = base.port or 443
    except ValueError:
        raise GatewayTrialError("invalid_gateway", "The configured gateway URL is invalid.") from None
    _require(
        all(33 <= ord(char) <= 126 for char in raw_base)
        and not any(char in raw_base for char in "\\?#")
        and base.scheme == "https" and host == allowed_hostname.lower()
        and base.username is None and base.password is None and base.port != 0,
        "invalid_gateway", "The approved hostname must match the configured HTTPS gateway.",
    )
    prefix = base.path.rstrip("/")
    _require(
        not prefix or (re.fullmatch(r"(?:/[a-zA-Z0-9._~-]+)+", prefix)
                       and not any(part in (".", "..") for part in prefix.split("/"))),
        "invalid_gateway", "The configured gateway path is invalid.",
    )
    if prefix.endswith("/v1/messages"):
        path = prefix
    else:
        path = prefix + ("/messages" if prefix.endswith("/v1") else "/v1/messages")

    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    secret = token or api_key
    _require(secret and all(33 <= ord(char) <= 126 for char in secret),
             "invalid_credentials", "A valid gateway credential is required in the environment.")
    auth = {"Authorization": "Bearer " + token} if token else {"x-api-key": api_key}
    headers = {"anthropic-version": "2023-06-01", "Content-Type": "application/json",
               "Accept": "application/json", **auth}
    return host, port, path, headers, tuple(value for value in (token, api_key) if value)


def _natural(value, name):
    _require(type(value) is int and 0 < value <= _SQLITE_MAX,
             "invalid_argument", f"{name} must be a positive integer.")
    return value


def _amount(value, name, *, allow_zero=False):
    """Parse decimal currency or rates as millionths without binary rounding."""
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}(\.[0-9]{1,6})?", value),
             "invalid_argument", f"{name} must be a decimal string with up to six fractional digits.")
    whole, _, fraction = value.partition(".")
    result = int(whole) * _MICRO + int(fraction.ljust(6, "0"))
    _require(allow_zero or result > 0, "invalid_argument", f"{name} must be positive.")
    return result


def _money(units):
    whole, fraction = divmod(units, _UNIT)
    return (f"{whole}.{fraction:012d}".rstrip("0").rstrip(".")) if fraction else str(whole)


def _prices(total_budget, currency, input_per_million, output_per_million, fixed_per_request):
    _require(isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency),
             "invalid_argument", "Provide a three-letter currency code.")
    cap = _amount(total_budget, "total_budget") * _MICRO
    _require(cap <= _SQLITE_MAX, "invalid_argument", "The total budget is out of range.")
    if fixed_per_request is not None:
        _require(input_per_million is None and output_per_million is None,
                 "invalid_argument", "Choose fixed or per-token pricing.")
        rates = {"fixed": _amount(fixed_per_request, "fixed_per_request") * _MICRO}
    else:
        rates = {"input": _amount(input_per_million, "input_per_million", allow_zero=True),
                 "output": _amount(output_per_million, "output_per_million", allow_zero=True)}
        _require(any(rates.values()), "invalid_argument", "At least one per-token rate must be positive.")
    return cap, rates


def _charge(rates, input_tokens, output_tokens):
    if "fixed" in rates:
        return rates["fixed"]
    if input_tokens is None or output_tokens is None:
        return None
    return rates["input"] * input_tokens + rates["output"] * output_tokens


def _body(model, messages, max_output_tokens, system):
    _require(isinstance(model, str) and model.strip(), "invalid_argument", "Provide the gateway model ID.")
    _require(isinstance(messages, list) and messages, "invalid_argument", "Provide a nonempty messages list.")
    for message in messages:
        _require(isinstance(message, dict) and message.keys() == {"role", "content"}
                 and message["role"] in ("user", "assistant") and isinstance(message["content"], str)
                 and message["content"].strip(),
                 "invalid_argument", "Messages require a user or assistant role and nonempty text content.")
    data = {"model": model, "messages": messages, "max_tokens": max_output_tokens}
    if system is not None:
        _require(isinstance(system, str) and system.strip(), "invalid_argument", "System must be nonempty text.")
        data["system"] = system
    try:
        return _json(data).encode("utf-8")
    except UnicodeError:
        raise GatewayTrialError("invalid_argument", "Messages contain invalid Unicode.") from None


@contextmanager
def _db(root):
    path = Path(root) / "runtime" / "gateway_trial.sqlite3"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=15)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("BEGIN IMMEDIATE")
            yield conn
    except (OSError, sqlite3.Error):
        raise GatewayTrialError("store_unavailable", "The local trial ledger could not be accessed or committed.") from None


def _reserve(root, request_id, fingerprint, terms, scope, currency, cap, reserved):
    with _db(root) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS budget (
            id INTEGER PRIMARY KEY CHECK (id=1), scope TEXT NOT NULL, currency TEXT NOT NULL,
            cap INTEGER NOT NULL, frozen INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS requests (
            id TEXT PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, terms TEXT NOT NULL,
            reserved INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.execute("INSERT OR IGNORE INTO budget(id, scope, currency, cap) VALUES (1, ?, ?, ?)",
                     (scope, currency, cap))
        budget = conn.execute("SELECT * FROM budget WHERE id=1").fetchone()
        _require((budget["scope"], budget["currency"], budget["cap"]) == (scope, currency, cap),
                 "budget_conflict", "This trial ledger is bound to its original gateway, currency and cap.")
        row = conn.execute("SELECT * FROM requests WHERE id=? OR fingerprint=?", (request_id, fingerprint)).fetchone()
        if row:
            _require(row["id"] == request_id and row["fingerprint"] == fingerprint and row["terms"] == terms,
                     "duplicate_request", "The request ID or exact Messages payload is already registered.")
            if row["state"] != "confirmed":
                evidence = json.loads(row["receipt"]) if row["receipt"] is not None else None
                raise GatewayTrialError("outcome_unknown",
                                        "A send may already have occurred. The request will not be sent again.",
                                        receipt=evidence)
            return {**json.loads(row["receipt"]), "replayed": True}
        _require(not budget["frozen"], "budget_frozen", "Reported token usage exceeded a request bound.")
        spent = conn.execute("SELECT COALESCE(SUM(reserved), 0) FROM requests").fetchone()[0]
        _require(reserved <= cap - spent, "budget_exceeded", "The request does not fit the unreserved local budget.")
        conn.execute("INSERT INTO requests(id, fingerprint, terms, reserved, state) VALUES (?, ?, ?, ?, 'reserved')",
                     (request_id, fingerprint, terms, reserved))
        return None


def _receipt(root, request_id, state, data, *, frozen=False):
    with _db(root) as conn:
        receipt = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        conn.execute("UPDATE requests SET state=?, receipt=? WHERE id=?", (state, receipt, request_id))
        if frozen:
            conn.execute("UPDATE budget SET frozen=1 WHERE id=1")


def _post(host, port, path, headers, body, timeout):
    """Read exactly one HTTPS response. Ignore redirect locations and error bodies."""
    with closing(http.client.HTTPSConnection(
        host, port=port, timeout=timeout, context=ssl.create_default_context()
    )) as conn:
        conn.request("POST", path, body=body, headers=headers)
        with closing(conn.getresponse()) as response:
            response_id = response.getheader("request-id") or response.getheader("x-request-id")
            if response.status != 200:
                return response.status, response_id, b""
            raw = response.read(_RESPONSE_LIMIT + 1)
            if response.length and len(raw) <= _RESPONSE_LIMIT:
                raise http.client.IncompleteRead(raw)
            return response.status, response_id, raw


def _redact(value, secrets):
    if not isinstance(value, str):
        return None
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def _redact_receipt(receipt, secrets):
    for key in ("text", "provider_model", "message_id", "gateway_request_id", "stop_reason", "request_id"):
        if key in receipt:
            receipt[key] = _redact(receipt[key], secrets)
    return receipt


def _parse(raw, secrets):
    _require(len(raw) <= _RESPONSE_LIMIT, "unconfirmed_response", "Gateway response exceeded the local size limit.")
    try:
        message = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise GatewayTrialError("unconfirmed_response", "The gateway response was not a valid Messages object.") from None
    _require(isinstance(message, dict) and message.get("type") == "message"
             and isinstance(message.get("id"), str) and message["id"]
             and isinstance(message.get("model"), str) and message["model"]
             and isinstance(message.get("content"), list),
             "unconfirmed_response", "The gateway returned no identifiable Messages receipt.")
    texts = []
    for block in message["content"]:
        _require(isinstance(block, dict) and isinstance(block.get("type"), str),
                 "unconfirmed_response", "The gateway returned an invalid Messages content block.")
        if block["type"] == "text":
            _require(isinstance(block.get("text"), str), "unconfirmed_response", "The gateway text block is invalid.")
            texts.append(block["text"])
    text = _redact("".join(texts), secrets)
    usage = message.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    counters = {
        field: usage[field] for field in _TOKEN_FIELDS
        if type(usage.get(field)) is int and 0 <= usage[field] <= _SQLITE_MAX
    }
    input_fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    valid_input = "input_tokens" in counters and not any(
        field in usage and field not in counters for field in input_fields
    )
    input_tokens = sum(counters.get(field, 0) for field in input_fields) if valid_input else None
    if input_tokens is not None:
        counters["total_input_tokens"] = input_tokens
    return {
        "text": text, "provider_model": _redact(message["model"], secrets),
        "message_id": _redact(message["id"], secrets), "usage": counters or None,
        "stop_reason": _redact(message.get("stop_reason"), secrets),
    }


def run_trial(*, gateway_host, request_id, model, messages, max_output_tokens,
              input_token_upper_bound, total_budget, currency, input_per_million=None,
              output_per_million=None, fixed_per_request=None, system=None, timeout=45, root=_ROOT):
    """Reserve then make one text-only Messages request, or replay its receipt.

    Monetary arguments are decimal strings. For per-token pricing, the input
    rate must cover any category of input traffic the gateway can bill,
    including cache creation. The input upper bound must account for any
    gateway-added tokens as well as meet the local serialized-bytes floor.
    """
    _require(isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", request_id),
             "invalid_argument", "Use a stable alphanumeric request ID with optional dots, dashes or underscores.")
    _natural(max_output_tokens, "max_output_tokens")
    _natural(input_token_upper_bound, "input_token_upper_bound")
    _require(type(timeout) in (int, float) and 0 < timeout <= 600,
             "invalid_argument", "Timeout must be positive and at most 600 seconds.")
    host, port, path, headers, secrets = _settings(gateway_host)
    cap, rates = _prices(total_budget, currency, input_per_million, output_per_million, fixed_per_request)
    body = _body(model, messages, max_output_tokens, system)
    _require(not any(secret in request_id or secret.encode("ascii") in body for secret in secrets
                     if secret.isascii()),
             "credential_in_payload", "Remove the gateway credential from the request ID or Messages payload.")
    floor = len(body) + _INPUT_PADDING
    _require(input_token_upper_bound >= floor, "input_bound_too_low",
             f"Use an input upper bound of at least {floor} tokens plus any necessary gateway overhead.")
    reserve = _charge(rates, input_token_upper_bound, max_output_tokens)
    fingerprint = _digest(body)
    terms = _json({"input_token_upper_bound": input_token_upper_bound,
                   "max_output_tokens": max_output_tokens, "rates": rates})
    scope = _digest(_json([host, port, path]).encode("utf-8"))
    try:
        replay = _reserve(root, request_id, fingerprint, terms, scope, currency, cap, reserve)
    except GatewayTrialError as error:
        if error.receipt is not None:
            _redact_receipt(error.receipt, secrets)
        raise
    if replay is not None:
        return _redact_receipt(replay, secrets)

    started = monotonic()
    evidence = {}
    try:
        status, header_id, raw = _post(host, port, path, headers, body, timeout)
        evidence = {"http_status": status, "gateway_request_id": _redact(header_id, secrets)}
        _require(status == 200, "unconfirmed_response", "The gateway returned no successful Messages receipt.")
        result = _parse(raw, secrets)
    except (OSError, http.client.HTTPException, GatewayTrialError) as error:
        evidence["failure"] = error.code if isinstance(error, GatewayTrialError) else "transport_failed"
        _receipt(root, request_id, "unknown", evidence)
        raise GatewayTrialError(
            "outcome_unknown", "The gateway outcome is unknown. The reservation remains held.",
            receipt=evidence,
        ) from None

    usage = result["usage"] or {}
    actual_input = usage.get("total_input_tokens")
    actual_output = usage.get("output_tokens")
    estimate = _charge(rates, actual_input, actual_output)
    lower_input = sum(usage.get(field, 0) for field in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
    ))
    exceeded = (lower_input > input_token_upper_bound
                or (actual_output is not None and actual_output > max_output_tokens))
    result.update(
        request_id=request_id, gateway_request_id=evidence["gateway_request_id"],
        elapsed_seconds=monotonic() - started,
        estimated_cost=_money(estimate) if estimate is not None else None, currency=currency,
        reserved_budget=_money(reserve), billed_cost=None, bounds_exceeded=exceeded,
    )
    _receipt(root, request_id, "confirmed", result, frozen=exceeded)
    return {**result, "replayed": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one budgeted Claude Messages trial.")
    parser.add_argument("--gateway-host", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--model", required=True)
    prompts = parser.add_mutually_exclusive_group(required=True)
    prompts.add_argument("--prompt", help="Single user turn.")
    prompts.add_argument("--messages-file", type=Path, help="UTF-8 JSON array of text turns.")
    parser.add_argument("--system")
    parser.add_argument("--max-output-tokens", type=int, required=True)
    parser.add_argument("--input-token-upper-bound", type=int, required=True)
    parser.add_argument("--total-budget", required=True)
    parser.add_argument("--currency", required=True)
    parser.add_argument("--input-per-million")
    parser.add_argument("--output-per-million")
    parser.add_argument("--fixed-per-request")
    parser.add_argument("--timeout", type=float, default=45)
    args = vars(parser.parse_args(argv))
    prompt = args.pop("prompt")
    source = args.pop("messages_file")
    try:
        if source is None:
            messages = [{"role": "user", "content": prompt}]
        else:
            try:
                messages = json.loads(source.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, RecursionError):
                raise GatewayTrialError("invalid_argument", "Could not read a UTF-8 JSON messages array.") from None
        result = run_trial(messages=messages, **args)
    except GatewayTrialError as error:
        failure = {"error": error.code, "message": str(error)}
        if error.receipt is not None:
            failure["receipt"] = error.receipt
        print(json.dumps(failure, ensure_ascii=True, allow_nan=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
