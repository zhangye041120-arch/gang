from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import re
import secrets
import sys
import time
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram


_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        try:
            fields = json.loads(message)
            if not isinstance(fields, dict):
                fields = {"message": message}
        except (TypeError, ValueError):
            fields = {"message": message}
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            **fields,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(environment: str) -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(logging.INFO if environment.strip().lower() == "production" else logging.DEBUG)


class Metrics:
    def __init__(self, registry: CollectorRegistry):
        self.registry = registry
        self.http_requests = Counter(
            "xiaoliao_http_requests_total",
            "Completed HTTP requests",
            ("method", "route", "status"),
            registry=registry,
        )
        self.http_duration = Histogram(
            "xiaoliao_http_request_duration_seconds",
            "Complete HTTP response duration",
            ("method", "route"),
            registry=registry,
        )
        self.agent_stage_duration = Histogram(
            "xiaoliao_agent_stage_duration_seconds",
            "Agent stage duration",
            ("stage",),
            registry=registry,
        )
        self.model_errors = Counter(
            "xiaoliao_model_errors_total",
            "Model errors",
            ("model_role", "error_code"),
            registry=registry,
        )
        self.idempotency = Counter(
            "xiaoliao_idempotency_total",
            "Idempotency decisions",
            ("outcome",),
            registry=registry,
        )
        self.dependency_unavailable = Counter(
            "xiaoliao_dependency_unavailable_total",
            "Unavailable dependencies",
            ("dependency",),
            registry=registry,
        )
        self.memory_operations = Counter(
            "xiaoliao_memory_operations_total",
            "Memory operations",
            ("operation", "outcome"),
            registry=registry,
        )
        self.memory_cache = Counter(
            "xiaoliao_memory_cache_total",
            "Memory cache outcomes",
            ("outcome",),
            registry=registry,
        )
        self.crisis_events = Counter(
            "xiaoliao_crisis_events_total",
            "Crisis event outcomes",
            ("outcome",),
            registry=registry,
        )

    def observe_http(
        self,
        method: str,
        route: str,
        status: int,
        duration_seconds: float,
    ) -> None:
        self.http_requests.labels(method, route, str(status)).inc()
        self.http_duration.labels(method, route).observe(max(0.0, duration_seconds))

    def observe_agent_stage(self, stage: str, duration_seconds: float) -> None:
        self.agent_stage_duration.labels(stage).observe(max(0.0, duration_seconds))

    def increment(self, metric: str, **labels: str) -> None:
        mapping = {
            "model_errors": self.model_errors,
            "idempotency": self.idempotency,
            "dependency_unavailable": self.dependency_unavailable,
            "memory_operations": self.memory_operations,
            "memory_cache": self.memory_cache,
            "crisis_events": self.crisis_events,
        }
        try:
            mapping[metric].labels(**labels).inc()
        except KeyError as exc:
            raise ValueError("unknown metric") from exc

    def snapshot(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        for metric in self.registry.collect():
            for sample in metric.samples:
                if sample.name.endswith(("_total", "_count")):
                    snapshot[sample.name] = (
                        snapshot.get(sample.name, 0.0) + float(sample.value)
                    )
        return snapshot


class Telemetry:
    def __init__(
        self,
        registry: CollectorRegistry | None = None,
        *,
        logger: logging.Logger | None = None,
    ):
        self.registry = registry or CollectorRegistry()
        self.metrics = Metrics(self.registry)
        self.logger = logger or logging.getLogger("xiaoliao.http")


class ObservabilityMiddleware:
    def __init__(self, app: Any, *, telemetry: Telemetry):
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500
        response_headers: list[tuple[bytes, bytes]] = []
        response_body = bytearray()
        recorded = False

        async def observed_send(message):
            nonlocal status, response_headers, recorded
            if message["type"] == "http.response.start":
                status = int(message["status"])
                response_headers = list(message.get("headers", []))
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                if status >= 400 and len(response_body) + len(chunk) <= 4096:
                    response_body.extend(chunk)
                if not message.get("more_body", False) and not recorded:
                    self._record(
                        scope,
                        status,
                        response_headers,
                        bytes(response_body),
                        time.perf_counter() - started,
                    )
                    recorded = True
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except Exception:
            if not recorded:
                self._record(
                    scope, 500, response_headers, b"", time.perf_counter() - started
                )
            raise

    def _record(
        self,
        scope: dict[str, Any],
        status: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
        elapsed: float,
    ) -> None:
        route_object = scope.get("route")
        route = getattr(route_object, "path", None) or "unmatched"
        method = str(scope.get("method", "UNKNOWN"))
        request_id = ""
        for name, value in (*scope.get("headers", []), *headers):
            if name.lower() == b"x-request-id":
                candidate = value.decode("ascii", errors="ignore")
                if _REQUEST_ID.fullmatch(candidate):
                    request_id = candidate
        error_code = ""
        request_id = request_id or secrets.token_hex(16)
        if body.startswith(b"{"):
            try:
                error_code = str(json.loads(body).get("error_code", ""))
            except (UnicodeError, ValueError, AttributeError):
                pass
        self.telemetry.metrics.observe_http(method, route, status, elapsed)
        self.telemetry.logger.info(json.dumps({
            "event": "http_request",
            "request_id": request_id,
            "route": route,
            "method": method,
            "status": status,
            "latency_ms": max(0, int(elapsed * 1000)),
            "error_code": error_code,
        }, separators=(",", ":")))
