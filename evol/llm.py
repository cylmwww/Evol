"""Small OpenAI-compatible JSON client with auditable usage accounting."""
import json
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any, Dict, List, Optional

from .telemetry import ExecutionLedger
from .context_manager import estimate_tokens
from .runtime import RuntimeBudgetExceeded


JSON_PROTOCOL = """Output exactly ONE JSON object per response. No Markdown, prose, wrappers,
or concatenated JSON objects. Never output multiple actions. For a tool action, choose ONE tool
and wait for its result before choosing the next action. For a final action, use only the schema
for the current phase. Do not wrap the result in type/content or response_format fields.
Never emit DSML/XML or native tool-call markup. Use this exact tool envelope:
{"action":"tool","tool":"read_file","arguments":{"path":"example.py"},"reason":"brief"}.
All final responses must include "action":"final". Keep explanations concise. Prefer evidence
already visible in the diff; request a tool only for genuinely missing evidence."""


class OutputProtocolError(ValueError):
    pass


def decode_output(choice, role, ledger=None):
    if choice.get("finish_reason") == "length":
        raise OutputProtocolError("output truncated at token limit; produce a concise complete object")
    result = json.loads(choice["message"]["content"])
    changes = []
    if isinstance(result, dict) and role in {"lead", "security", "correctness-reliability", "critic"}:
        result = dict(result)
        finals = {"lead": ("delegations", "revision_requests", "accepted_finding_indices"),
                  "security": ("findings",), "correctness-reliability": ("findings",),
                  "critic": ("decisions",)}[role]
        # Normalize only unambiguous aliases; never select one of multiple actions.
        if "tool" in result and not any(k in result for k in finals):
            if "action" not in result:
                result["action"] = "tool"; changes.append("inferred_tool_action")
            if result.get("action") == "tool":
                if "parameters" in result:
                    if "arguments" in result:
                        raise OutputProtocolError("ambiguous arguments and parameters")
                    result["arguments"] = result.pop("parameters"); changes.append("parameters_alias")
                elif "arguments" not in result:
                    flat = set(result) - {"action", "tool", "reason"}
                    if flat and flat <= {"path", "start_line", "end_line"} and result.get("tool") == "read_file":
                        result["arguments"] = {k: result.pop(k) for k in flat}
                        changes.append("read_file_flat_arguments")
        elif "action" not in result and "tool" not in result and sum(k in result for k in finals) == 1:
            result["action"] = "final"; changes.append("inferred_final_action")
    validate_output(result, role)
    if changes and ledger:
        ledger.trace(role, "output_protocol_normalized", changes=changes)
    return result


def validate_output(result, role):
    if not isinstance(result, dict):
        raise OutputProtocolError("JSON root must be an object")
    if role not in {"lead", "security", "correctness-reliability", "critic"}:
        return
    action = result.get("action")
    if action == "tool":
        if any(k in result for k in ("findings", "decisions", "delegations", "revision_requests", "accepted_finding_indices")):
            raise OutputProtocolError("mixed tool and final actions")
        if not isinstance(result.get("tool"), str) or not result["tool"].strip() or not isinstance(result.get("arguments"), dict):
            raise OutputProtocolError("tool action requires one tool name and an arguments object")
    elif action == "final":
        if any(k in result for k in ("tool", "arguments", "parameters")):
            raise OutputProtocolError("mixed final and tool actions")
        fields = {"security": ("findings",), "correctness-reliability": ("findings",),
                  "critic": ("decisions",), "lead": ("delegations", "revision_requests", "accepted_finding_indices")}[role]
        if not any(isinstance(result.get(k), list) for k in fields):
            raise OutputProtocolError("final action requires the current phase array: " + ", ".join(fields))
    else:
        raise OutputProtocolError("action must be tool or final")


class JsonChatClient:
    def __init__(
        self, base_url: str, api_key: str, model: str,
        provider: str = "openai-compatible", timeout: int = 60,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.timeout = timeout
        self.extra_headers = dict(extra_headers or {})

    def request_options(self, role):
        # The bounded JSON-action workflow needs its output budget for the action,
        # not the official DeepSeek endpoint's default high-effort reasoning.
        if (urlsplit(self.base_url).hostname == "api.deepseek.com"
                and self.model == "deepseek-flash"
                and role in {"lead", "security", "correctness-reliability", "critic"}):
            return {"thinking": {"type": "disabled"}}
        return {}

    def complete_json(self, role, system, user, ledger=None, max_tokens=None):
        system = system + "\n\n" + JSON_PROTOCOL
        try:
            managed = json.loads(user)
        except (ValueError, TypeError):
            managed = {}
        remaining = managed.get("remaining_token_budget") if isinstance(managed, dict) else None
        seconds = managed.get("remaining_time_seconds") if isinstance(managed, dict) else None
        started = time.monotonic()
        initial_used = sum(x.input_tokens + x.output_tokens for x in ledger.model_calls if x.role == role) if ledger else 0
        for attempt in range(2):
            allowance = max_tokens
            if isinstance(seconds, (int, float)) and time.monotonic() - started >= seconds:
                raise RuntimeBudgetExceeded("%s time budget exhausted before request" % role)
            if isinstance(remaining, (int, float)):
                used = (sum(x.input_tokens + x.output_tokens for x in ledger.model_calls if x.role == role) - initial_used) if ledger else 0
                if attempt and ledger is None:
                    raise RuntimeBudgetExceeded("retry requires usage accounting")
                available = int(remaining) - used - estimate_tokens(system) - estimate_tokens(user) - 64
                allowance = min(max_tokens or 4000, available)
                if allowance < 1024:
                    raise RuntimeBudgetExceeded("%s insufficient budget for input and a complete output" % role)
            try:
                return self._complete_json_once(role, system, user, ledger, allowance)
            except OutputProtocolError as exc:
                if attempt:
                    raise
                if ledger:
                    ledger.trace(role, "output_protocol_retry", attempt=1, error=str(exc))
                system += ("\nYour previous response failed output validation: " + str(exc)
                           + ". Regenerate the entire response as one valid JSON object. "
                           "Do not repeat the invalid output. Follow the current phase schema.")

    def _complete_json_once(
        self, role: str, system: str, user: str,
        ledger: Optional[ExecutionLedger] = None,
        max_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        payload.update(self.request_options(role))
        headers = {
            "Authorization": "Bearer " + self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        headers.update(self.extra_headers)
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        started = time.monotonic()
        usage = {}
        protocol_error = False
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            usage = body.get("usage") or {}
            try:
                result = decode_output(body["choices"][0], role, ledger)
            except (ValueError, TypeError) as exc:
                protocol_error = True
                raise OutputProtocolError(str(exc)) from None
            if ledger:
                ledger.record_model(
                    role, self.provider, self.model, body.get("usage") or {},
                    int((time.monotonic() - started) * 1000), True,
                )
            return result
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            message = "%s API returned HTTP %d: %s" % (
                self.provider, exc.code, detail,
            )
        except (urllib.error.URLError, socket.timeout, ValueError, KeyError,
                IndexError, TypeError, json.JSONDecodeError) as exc:
            message = "%s JSON request failed: %s" % (self.provider, exc)
        if ledger:
            ledger.record_model(
                role, self.provider, self.model, usage,
                int((time.monotonic() - started) * 1000), False, message,
            )
        if protocol_error:
            raise OutputProtocolError(message)
        raise RuntimeError(message)
