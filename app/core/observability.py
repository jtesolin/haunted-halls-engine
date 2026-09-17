"""OpenTelemetry tracing and trace-correlated application logging."""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

import google.auth
import grpc
from fastapi import FastAPI
from google.auth.transport.grpc import AuthMetadataPlugin
from google.auth.transport.requests import Request
from opentelemetry import trace
from opentelemetry.trace import Span
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.core.config import Settings, settings

ExporterFactory = Callable[[Settings], SpanExporter]

_LOGGER_NAME = "app"
_MAX_STACK_TRACE_CHARS = 8000
_STACK_TRACE_TRUNCATION_MARKER = "...[truncated]"
_REDACTED_SPAN_ATTRIBUTE_VALUE = "[redacted]"
_HTTP_SPAN_ATTRIBUTES_WITH_SENSITIVE_REQUEST_VALUES = frozenset(
    {
        "url.query",
        "url.full",
        "http.target",
        "http.url",
        "http.user_agent",
        "user_agent.original",
        "http.client_ip",
        "client.address",
        "network.peer.address",
        "net.peer.ip",
    }
)


class _JsonFormatter(logging.Formatter):
    def __init__(self, settings_: Settings) -> None:
        super().__init__()
        self._settings = settings_

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["logging.googleapis.com/trace"] = (
                f"projects/{self._settings.OTEL_GCP_PROJECT_ID}/traces/{context.trace_id:032x}"
                if self._settings.OTEL_GCP_PROJECT_ID
                else f"traces/{context.trace_id:032x}"
            )
            payload["logging.googleapis.com/spanId"] = f"{context.span_id:016x}"
            payload["logging.googleapis.com/trace_sampled"] = bool(
                context.trace_flags.sampled
            )
        if record.exc_info:
            exc_type, exc_value, _exc_tb = record.exc_info
            if exc_type is not None:
                payload["exception.type"] = exc_type.__name__
            if exc_value is not None:
                payload["exception.message"] = str(exc_value)
            stack_trace = "".join(traceback.format_exception(*record.exc_info))
            if len(stack_trace) > _MAX_STACK_TRACE_CHARS:
                stack_trace = (
                    stack_trace[:_MAX_STACK_TRACE_CHARS]
                    + _STACK_TRACE_TRUNCATION_MARKER
                )
            payload["stack_trace"] = stack_trace
        return json.dumps(payload, separators=(",", ":"))


class _LoggingConfiguration:
    """Result of newly applying D7A's owned logging setup.

    Only returned when this call is the sole owner: an already-active D7A
    handler is never returned here so a second instance cannot later mutate
    or release state it does not own.
    """

    def __init__(
        self,
        handler: logging.Handler,
        prior_level: int,
        prior_propagate: bool,
    ) -> None:
        self.handler = handler
        self.prior_level = prior_level
        self.prior_propagate = prior_propagate


def _logging_owned_elsewhere() -> bool:
    """True if another D7A instance already owns the shared ``app`` logger handler."""
    logger = logging.getLogger(_LOGGER_NAME)
    return any(
        getattr(handler, "_haunted_halls_observability", False)
        for handler in logger.handlers
    )


def _fastapi_already_instrumented(app: FastAPI) -> bool:
    """True if FastAPI instrumentation is already applied, by this process or another owner."""
    return bool(getattr(app, "_is_instrumented_by_opentelemetry", False))


def _configure_logging(settings_: Settings) -> _LoggingConfiguration:
    """Apply D7A's owned logging setup.

    Callers must first confirm logging is not already owned elsewhere via
    :func:`_logging_owned_elsewhere`; this function always claims ownership.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    prior_level = logger.level
    prior_propagate = logger.propagate
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(settings_))
    handler._haunted_halls_observability = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return _LoggingConfiguration(handler, prior_level, prior_propagate)


def _google_exporter(settings_: Settings) -> SpanExporter:
    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        quota_project_id=settings_.OTEL_GCP_PROJECT_ID,
    )
    call_credentials = grpc.metadata_call_credentials(
        AuthMetadataPlugin(credentials, Request())
    )
    channel_credentials = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(), call_credentials
    )
    return OTLPSpanExporter(
        endpoint=settings_.OTEL_EXPORTER_OTLP_ENDPOINT,
        credentials=channel_credentials,
        insecure=False,
    )


def _sanitized_http_target(scope: Mapping[str, Any]) -> str:
    path = scope.get("path")
    return path if isinstance(path, str) and path else _REDACTED_SPAN_ATTRIBUTE_VALUE


def _sanitized_http_url(scope: Mapping[str, Any]) -> str:
    scheme = scope.get("scheme")
    server = scope.get("server")
    path = _sanitized_http_target(scope)
    if (
        isinstance(scheme, str)
        and isinstance(server, tuple)
        and len(server) >= 1
        and isinstance(server[0], str)
    ):
        host = server[0]
        return f"{scheme}://{host}{path}"
    return path


def _sanitize_server_request_span(span: Span, scope: Mapping[str, Any]) -> None:
    """Overwrite sensitive built-in HTTP request values before span export."""
    if not span.is_recording():
        return

    attributes = getattr(span, "attributes", None)
    keys_to_sanitize = (
        _HTTP_SPAN_ATTRIBUTES_WITH_SENSITIVE_REQUEST_VALUES
        if not isinstance(attributes, Mapping)
        else _HTTP_SPAN_ATTRIBUTES_WITH_SENSITIVE_REQUEST_VALUES.intersection(
            attributes.keys()
        )
    )
    for key in keys_to_sanitize:
        if key == "http.target":
            span.set_attribute(key, _sanitized_http_target(scope))
        elif key in {"http.url", "url.full"}:
            span.set_attribute(key, _sanitized_http_url(scope))
        elif key == "url.query":
            span.set_attribute(key, "")
        else:
            span.set_attribute(key, _REDACTED_SPAN_ATTRIBUTE_VALUE)


def _uninstrument_owned_fastapi_app(app: FastAPI) -> None:
    """Release instrumentation installed by this owner on this application."""
    FastAPIInstrumentor.uninstrument_app(app)


class Observability:
    """Own the engine trace provider, processor, exporter, and instrumentation.

    D7A keeps FastAPI instrumentation bound to this explicitly owned
    ``TracerProvider`` instead of the process-global OpenTelemetry provider
    registry, which is write-once and cannot be safely replaced across
    ``shutdown()``/``initialize()`` cycles in a single process.
    """

    def __init__(self) -> None:
        self._provider: TracerProvider | None = None
        self._processor: BatchSpanProcessor | None = None
        self._instrumented = False
        self._app: FastAPI | None = None
        self._logging_handler: logging.Handler | None = None
        self._prior_logger_level: int | None = None
        self._prior_logger_propagate: bool | None = None

    def initialize(
        self,
        app: FastAPI,
        settings_: Settings = settings,
        exporter_factory: ExporterFactory | None = None,
    ) -> None:
        if not settings_.OTEL_ENABLED or self._instrumented or self._provider is not None:
            return
        if _logging_owned_elsewhere() or _fastapi_already_instrumented(app):
            # Another D7A instance (or pre-existing instrumentation) already
            # owns logging and/or FastAPI instrumentation. D7A is a single-owner
            # model: degrade this instance to disabled rather than sharing or
            # later tearing down state it does not own.
            return
        try:
            self._app = app
            logging_configuration = _configure_logging(settings_)
            self._logging_handler = logging_configuration.handler
            self._prior_logger_level = logging_configuration.prior_level
            self._prior_logger_propagate = logging_configuration.prior_propagate

            attributes: dict[str, str] = {"service.name": settings_.OTEL_SERVICE_NAME}
            optional_attributes = {
                "service.version": settings_.OTEL_SERVICE_VERSION,
                "deployment.environment.name": settings_.OTEL_DEPLOYMENT_ENVIRONMENT,
                "cloud.run.service": os.getenv("K_SERVICE"),
                "cloud.run.revision": os.getenv("K_REVISION"),
                "gcp.project_id": settings_.OTEL_GCP_PROJECT_ID,
            }
            attributes.update(
                {key: value for key, value in optional_attributes.items() if value}
            )
            self._provider = TracerProvider(
                resource=Resource.create(attributes),
                sampler=ParentBased(
                    TraceIdRatioBased(settings_.OTEL_TRACES_SAMPLE_RATIO)
                ),
            )
            exporter = (exporter_factory or _google_exporter)(settings_)
            self._processor = BatchSpanProcessor(exporter)
            self._provider.add_span_processor(self._processor)
            FastAPIInstrumentor.instrument_app(
                app,
                tracer_provider=self._provider,
                excluded_urls=(
                    r"^https?://[^/]+/health(?:/.*)?(?:\?.*)?$"
                    r"|^/health(?:/.*)?(?:\?.*)?$"
                ),
                server_request_hook=_sanitize_server_request_span,
                # Empty lists fall back to ambient OTel env vars; match no headers instead.
                http_capture_headers_server_request=[r"(?!)"],
                http_capture_headers_server_response=[r"(?!)"],
            )
            # Starlette caches a built middleware stack on the app instance and
            # only rebuilds it lazily when unset. Force a rebuild so a
            # reinitialize on a previously (un)instrumented app picks up the
            # newly patched middleware chain instead of a stale cached one.
            app.middleware_stack = None
            self._instrumented = True
        except Exception:
            self._release_owned_state()
            logging.getLogger(_LOGGER_NAME).warning(
                "Observability initialization failed; continuing without telemetry",
                exc_info=True,
            )

    def shutdown(self) -> None:
        self._release_owned_state()

    def _release_owned_state(self) -> None:
        """Undo every resource this instance may have created, in either the
        normal shutdown path or after a partial-initialization failure.

        This only ever releases state this instance itself acquired:
        ``initialize()`` refuses to claim ownership of logging or FastAPI
        instrumentation already held elsewhere, so ``self._logging_handler``
        and ``self._instrumented`` are only ever set when this instance is
        the sole owner.
        """
        if self._instrumented and self._app is not None:
            _uninstrument_owned_fastapi_app(self._app)
        if self._provider is not None:
            self._provider.shutdown()
        if self._logging_handler is not None:
            logger = logging.getLogger(_LOGGER_NAME)
            logger.removeHandler(self._logging_handler)
            self._logging_handler.close()
            assert self._prior_logger_level is not None
            assert self._prior_logger_propagate is not None
            logger.setLevel(self._prior_logger_level)
            logger.propagate = self._prior_logger_propagate
        self._app = None
        self._processor = None
        self._provider = None
        self._instrumented = False
        self._logging_handler = None
        self._prior_logger_level = None
        self._prior_logger_propagate = None


observability = Observability()
