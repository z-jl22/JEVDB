"""Local HTTP adapter translating JEVDB OpenAI json_schema requests to DeepSeek json_object."""

from __future__ import annotations

import argparse
import json
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from stock_research.config import Settings, get_settings

_PLACEHOLDER_KEYS = {"", "local", "dummy", "test", "none", "placeholder"}
_SHIM_LOCK = threading.Lock()
_GLOBAL_SHIM: "ShimServer | None" = None


@dataclass(frozen=True)
class SchemaSpec:
    kind: str  # "number" | "enum" | "boolean" | "numbered_boolean" | "generic"
    expected_count: int | None
    min_value: float | None = None
    max_value: float | None = None
    enum_values: tuple[str, ...] = ()


def _extract_schema_spec(body: dict[str, Any]) -> SchemaSpec:
    rf = body.get("response_format") or {}
    if rf.get("type") != "json_schema":
        return SchemaSpec(kind="generic", expected_count=None)

    schema = (rf.get("json_schema") or {}).get("schema") or {}
    answers_schema = (schema.get("properties") or {}).get("answers") or {}

    expected_count: int | None = answers_schema.get("minItems")
    if expected_count is None:
        expected_count = answers_schema.get("maxItems")
    if expected_count is None:
        for msg in body.get("messages") or []:
            if msg.get("role") == "user":
                try:
                    payload = json.loads(msg.get("content") or "{}")
                    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                        expected_count = len(payload["items"])
                        break
                except (ValueError, TypeError):
                    pass

    items_schema = answers_schema.get("items") or {}
    item_type = items_schema.get("type")

    if item_type == "boolean":
        return SchemaSpec(kind="boolean", expected_count=expected_count)
    if item_type == "object":
        return SchemaSpec(kind="numbered_boolean", expected_count=expected_count)
    if item_type == "string" and isinstance(items_schema.get("enum"), list):
        return SchemaSpec(
            kind="enum",
            expected_count=expected_count,
            enum_values=tuple(str(v) for v in items_schema["enum"]),
        )
    if item_type in ("number", "integer"):
        min_v = items_schema.get("minimum")
        max_v = items_schema.get("maximum")
        return SchemaSpec(
            kind="number",
            expected_count=expected_count,
            min_value=float(min_v) if min_v is not None else None,
            max_value=float(max_v) if max_v is not None else None,
        )
    return SchemaSpec(kind="generic", expected_count=expected_count)


def _schema_instruction(spec: SchemaSpec) -> str:
    count_rule = (
        f"The `answers` array MUST contain EXACTLY {spec.expected_count} elements in input order."
        if spec.expected_count is not None
        else "The `answers` array MUST match the exact number of input items in order."
    )
    if spec.kind == "number":
        lo = spec.min_value if spec.min_value is not None else 0
        hi = spec.max_value if spec.max_value is not None else 1
        elem_rule = f"Each element MUST be a finite JSON number between {lo} and {hi} (inclusive)."
    elif spec.kind == "enum":
        allowed = json.dumps(list(spec.enum_values), ensure_ascii=False)
        elem_rule = f"Each element MUST be a JSON string strictly chosen from {allowed}."
    elif spec.kind == "boolean":
        elem_rule = "Each element MUST be a JSON boolean (`true` or `false`)."
    elif spec.kind == "numbered_boolean":
        hi_id = (spec.expected_count - 1) if spec.expected_count else "N-1"
        elem_rule = (
            f'Each element MUST be an object {{"id": <integer 0..{hi_id}>, "verdict": <boolean>}}.'
        )
    else:
        elem_rule = "Each element MUST conform to the requested item type."

    return (
        "\n\n[STRICT JSON OUTPUT REQUIREMENT]\n"
        'Return ONLY a single valid JSON object with top-level key `"answers"` (no markdown fences, no extra keys).\n'
        f"{count_rule}\n{elem_rule}"
    )


def translate_request(
    body: dict[str, Any],
    settings: Settings,
    *,
    default_model: str | None = None,
    min_max_tokens: int | None = None,
) -> tuple[dict[str, Any], SchemaSpec]:
    """Translate a JEVDB OpenAI json_schema chat request into DeepSeek json_object format."""
    spec = _extract_schema_spec(body)
    messages = [dict(m) for m in (body.get("messages") or [])]

    instruction = _schema_instruction(spec)
    sys_idx = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
    if sys_idx is not None:
        messages[sys_idx] = {
            **messages[sys_idx],
            "content": f"{messages[sys_idx].get('content', '')}{instruction}",
        }
    else:
        messages.insert(0, {"role": "system", "content": instruction.strip()})

    count_for_budget = spec.expected_count if spec.expected_count is not None else 4
    computed_max_tokens = max(4096, min(32768, count_for_budget * 256))
    if min_max_tokens is not None:
        computed_max_tokens = max(computed_max_tokens, min_max_tokens)
    caller_max = body.get("max_tokens")
    if isinstance(caller_max, int) and caller_max > computed_max_tokens:
        computed_max_tokens = caller_max

    translated = {
        "model": body.get("model") or default_model or settings.deepseek_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.0 if body.get("temperature") is None else body["temperature"],
        "max_tokens": computed_max_tokens,
    }
    return translated, spec


def _strip_json_fences(raw_text: str) -> str:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def validate_and_normalize_answers(raw_content: str, spec: SchemaSpec) -> list[Any]:
    """Parse and validate `{"answers": [...]}` against the incoming JEVDB schema."""
    cleaned = _strip_json_fences(raw_content)
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("answers"), list):
        raise ValueError("Model output must be a JSON object containing an 'answers' list")

    answers = parsed["answers"]
    if spec.expected_count is not None and len(answers) != spec.expected_count:
        raise ValueError(
            f"Expected {spec.expected_count} answers, got {len(answers)}"
        )

    if spec.kind == "generic":
        return answers

    normalized: list[Any] = []
    if spec.kind == "boolean":
        for idx, item in enumerate(answers):
            if isinstance(item, bool):
                normalized.append(item)
            elif isinstance(item, str) and item.strip().lower() in ("true", "false"):
                normalized.append(item.strip().lower() == "true")
            else:
                raise ValueError(f"Answer[{idx}] expected boolean, got {item!r}")
        return normalized

    if spec.kind == "numbered_boolean":
        seen_ids: set[int] = set()
        for idx, item in enumerate(answers):
            if not isinstance(item, dict):
                raise ValueError(f"Answer[{idx}] expected object, got {item!r}")
            raw_id = item.get("id")
            raw_verdict = item.get("verdict")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (int, float)):
                raise ValueError(f"Answer[{idx}].id invalid: {raw_id!r}")
            if isinstance(raw_id, float) and (not math.isfinite(raw_id) or not raw_id.is_integer()):
                raise ValueError(f"Answer[{idx}].id must be an integer, got {raw_id!r}")
            item_id = int(raw_id)
            if spec.expected_count is not None and not (0 <= item_id < spec.expected_count):
                raise ValueError(f"Answer[{idx}].id out of bounds: {item_id}")
            if item_id in seen_ids:
                raise ValueError(f"Duplicate id in numbered answers: {item_id}")
            seen_ids.add(item_id)
            if isinstance(raw_verdict, bool):
                verdict = raw_verdict
            elif isinstance(raw_verdict, str) and raw_verdict.strip().lower() in ("true", "false"):
                verdict = raw_verdict.strip().lower() == "true"
            else:
                raise ValueError(f"Answer[{idx}].verdict expected boolean, got {raw_verdict!r}")
            normalized.append({"id": item_id, "verdict": verdict})
        return normalized

    if spec.kind == "enum":
        exact_set = set(spec.enum_values)
        lower_map = {val.strip().lower(): val for val in spec.enum_values}
        for idx, item in enumerate(answers):
            if not isinstance(item, str):
                raise ValueError(f"Answer[{idx}] expected enum string, got {item!r}")
            if item in exact_set:
                normalized.append(item)
            elif item.strip() in exact_set:
                normalized.append(item.strip())
            elif item.strip().lower() in lower_map:
                normalized.append(lower_map[item.strip().lower()])
            else:
                raise ValueError(
                    f"Answer[{idx}]={item!r} not in allowed choices {list(spec.enum_values)}"
                )
        return normalized

    if spec.kind == "number":
        for idx, item in enumerate(answers):
            if isinstance(item, bool):
                raise ValueError(f"Answer[{idx}] expected number, got bool")
            try:
                num = float(item)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Answer[{idx}] expected number, got {item!r}") from exc
            if not math.isfinite(num):
                raise ValueError(f"Answer[{idx}] is not finite: {num}")
            if spec.min_value is not None:
                if num < spec.min_value - 1e-6:
                    raise ValueError(f"Answer[{idx}]={num} below minimum {spec.min_value}")
                num = max(spec.min_value, num)
            if spec.max_value is not None:
                if num > spec.max_value + 1e-6:
                    raise ValueError(f"Answer[{idx}]={num} above maximum {spec.max_value}")
                num = min(spec.max_value, num)
            normalized.append(num)
        return normalized

    return answers


class ShimServer:
    """Thread-safe local OpenAI-compatible HTTP proxy for JEVDB -> DeepSeek."""

    def __init__(
        self,
        address: tuple[str, int] | None = None,
        *,
        upstream_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        settings = get_settings(ensure_dirs=False)
        self._bind_address = address or (settings.shim_host, settings.shim_port)
        self._requested_port = int(self._bind_address[1])
        self.upstream_url = (upstream_url or settings.deepseek_base_url).rstrip("/")
        self._explicit_api_key = api_key is not None
        self.api_key = api_key if api_key is not None else settings.deepseek_api_key
        self.default_model = default_model or settings.deepseek_model
        self.timeout = timeout if timeout is not None else settings.llm_timeout
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._client = httpx.Client(timeout=self.timeout, trust_env=False)

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self._bind_address[1]
        return int(self._httpd.server_address[1])

    @property
    def url(self) -> str:
        host = self._bind_address[0]
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.port}/v1"

    def _resolve_api_key(self, auth_header: str) -> str:
        bearer = ""
        if auth_header.lower().startswith("bearer "):
            bearer = auth_header[7:].strip()
        if bearer and bearer.lower() not in _PLACEHOLDER_KEYS:
            return bearer
        if self._explicit_api_key and self.api_key:
            return self.api_key
        live_key = get_settings(ensure_dirs=False).deepseek_api_key
        if live_key:
            return live_key
        return self.api_key or bearer

    def _forward_chat_completion(
        self, body: dict[str, Any], auth_header: str
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        settings = get_settings(ensure_dirs=False)
        api_key = self._resolve_api_key(auth_header)
        if not api_key:
            return (
                401,
                {"error": {"message": "DEEPSEEK_API_KEY is missing", "type": "auth_error"}},
                {},
            )

        translated, spec = translate_request(
            body, settings, default_model=self.default_model
        )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        target_url = f"{self.upstream_url}/chat/completions"

        last_err = ""
        for attempt in range(2):
            payload = dict(translated)
            if attempt == 1:
                payload["max_tokens"] = min(32768, int(payload["max_tokens"]) * 2)
                payload["messages"] = list(payload["messages"]) + [
                    {
                        "role": "user",
                        "content": (
                            f"Previous JSON failed schema validation ({last_err}). "
                            f"Re-output ONLY valid JSON {{\"answers\": [...]}} matching the exact schema."
                        ),
                    }
                ]
            try:
                resp = self._client.post(target_url, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                return (
                    502,
                    {"error": {"message": f"Upstream transport error: {exc}", "type": "bad_gateway"}},
                    {},
                )

            if resp.status_code == 429:
                extra = {}
                if "Retry-After" in resp.headers:
                    extra["Retry-After"] = resp.headers["Retry-After"]
                return 429, _safe_json(resp), extra
            if resp.status_code >= 500:
                return 502, _safe_json(resp), {}
            if resp.status_code >= 400:
                return resp.status_code, _safe_json(resp), {}

            upstream_json = _safe_json(resp)
            choices = upstream_json.get("choices") or []
            if not choices:
                last_err = "Empty choices from upstream"
                continue
            msg = choices[0].get("message") or {}
            raw_content = msg.get("content") or ""
            try:
                answers = validate_and_normalize_answers(raw_content, spec)
            except (ValueError, json.JSONDecodeError) as exc:
                last_err = str(exc)
                continue

            usage = upstream_json.get("usage") or {}
            normalized_resp = {
                "id": upstream_json.get("id") or f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": upstream_json.get("created") or int(time.time()),
                "model": upstream_json.get("model") or translated["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"answers": answers}, ensure_ascii=False),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                    "completion_tokens": int(usage.get("completion_tokens") or 0),
                    "total_tokens": int(usage.get("total_tokens") or 0),
                },
            }
            return 200, normalized_resp, {}

        return (
            502,
            {
                "error": {
                    "message": f"Upstream output failed schema validation after retry: {last_err}",
                    "type": "invalid_upstream_json",
                }
            },
            {},
        )

    def start(self) -> str:
        if self._httpd is not None:
            return self.url

        shim = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send_json(
                self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None
            ) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                if extra_headers:
                    for k, v in extra_headers.items():
                        self.send_header(k, v)
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:
                if self.path.rstrip("/") in ("/health", "/v1/health"):
                    self._send_json(200, {"status": "ok", "upstream": shim.upstream_url})
                    return
                self._send_json(404, {"error": {"message": "Not found"}})

            def do_POST(self) -> None:
                if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
                    self._send_json(404, {"error": {"message": f"Unsupported path: {self.path}"}})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._send_json(400, {"error": {"message": f"Invalid JSON body: {exc}"}})
                    return

                auth = self.headers.get("Authorization", "")
                status, payload, extra = shim._forward_chat_completion(body, auth)
                self._send_json(status, payload, extra)

        self._httpd = ThreadingHTTPServer(self._bind_address, Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._client.close()

    def __enter__(self) -> "ShimServer":
        self.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self.stop()


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except Exception:
        return {"error": {"message": resp.text[:500]}}


def ensure_shim_running(settings: Settings | None = None) -> str:
    """Start or reuse a local LLM shim server and return its /v1 base URL."""
    global _GLOBAL_SHIM
    cfg = settings or get_settings(ensure_dirs=False)
    target_url = f"http://{cfg.shim_host}:{cfg.shim_port}/v1"

    with _SHIM_LOCK:
        if _GLOBAL_SHIM is not None and _GLOBAL_SHIM._httpd is not None:
            port_matches = (
                cfg.shim_port <= 0
                or _GLOBAL_SHIM.port == cfg.shim_port
                or _GLOBAL_SHIM._requested_port == cfg.shim_port
            )
            if (
                _GLOBAL_SHIM.upstream_url == cfg.deepseek_base_url
                and _GLOBAL_SHIM.default_model == cfg.deepseek_model
                and _GLOBAL_SHIM.api_key == cfg.deepseek_api_key
                and _GLOBAL_SHIM._bind_address[0] == cfg.shim_host
                and port_matches
            ):
                return _GLOBAL_SHIM.url
            _GLOBAL_SHIM.stop()
            _GLOBAL_SHIM = None

        # Check if an external shim process is already healthy on this port.
        if cfg.shim_port > 0:
            try:
                with httpx.Client(timeout=1.0, trust_env=False) as client:
                    r = client.get(f"{target_url}/health")
                    if r.status_code == 200:
                        return target_url
            except httpx.HTTPError:
                pass

        server = ShimServer(
            (cfg.shim_host, cfg.shim_port),
            upstream_url=cfg.deepseek_base_url,
            api_key=cfg.deepseek_api_key,
            default_model=cfg.deepseek_model,
            timeout=cfg.llm_timeout,
        )
        try:
            url = server.start()
        except OSError:
            # Port already bound (possibly started concurrently) — try ephemeral fallback
            server = ShimServer(
                (cfg.shim_host, 0),
                upstream_url=cfg.deepseek_base_url,
                api_key=cfg.deepseek_api_key,
                default_model=cfg.deepseek_model,
                timeout=cfg.llm_timeout,
            )
            url = server.start()
        _GLOBAL_SHIM = server
        return url


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="JEVDB <-> DeepSeek local OpenAI JSON shim")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = get_settings(ensure_dirs=False)
    host = args.host or cfg.shim_host
    port = args.port if args.port is not None else cfg.shim_port
    server = ShimServer((host, port))
    url = server.start()
    print(f"JEVDB LLM shim listening at {url} -> {server.upstream_url} ({server.default_model})", flush=True)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
