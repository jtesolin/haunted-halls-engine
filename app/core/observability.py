"""OpenTelemetry tracing and trace-correlated application logging."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import google.auth
import grpc
from fastapi import FastAPI
from google.auth.transport.grpc import AuthMetadataPlugin
from google.auth.transport.requests import Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.core.config import Settings, settings

ExporterFactory = Callable[[Settings], SpanExporter]


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
        return json.dumps(payload, separators=(",", ":"))


def _configure_logging(settings_: Settings) -> logging.Handler:
    logger = logging.getLogger("app")
    for handler in logger.handlers:
        if getattr(handler, "_haunted_halls_observability", False):
            return handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter(settings_))
    handler._haunted_halls_observability = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return handler


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


class Observability:
    """Own the engine trace provider, processor, exporter, and instrumentation."""

    def __init__(self) -> None:
        self._provider: TracerProvider | None = None
        self._processor: BatchSpanProcessor | None = None
        self._instrumented = False
        self._logging_handler: logging.Handler | None = None
        self._app: FastAPI | None = None

    def initialize(
        self,
        app: FastAPI,
        settings_: Settings = settings,
        exporter_factory: ExporterFactory | None = None,
    ) -> None:
        if not settings_.OTEL_ENABLED or self._instrumented or self._provider is not None:
            return
        self._app = app
        self._logging_handler = _configure_logging(settings_)

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
            sampler=ParentBased(TraceIdRatioBased(settings_.OTEL_TRACES_SAMPLE_RATIO)),
        )
        exporter = (exporter_factory or _google_exporter)(settings_)
        self._processor = BatchSpanProcessor(exporter)
        self._provider.add_span_processor(self._processor)
        trace.set_tracer_provider(self._provider)
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self._provider,
            excluded_urls=r".*/health(?:/.*)?$",
            # Empty lists fall back to ambient OTel env vars; match no headers instead.
            http_capture_headers_server_request=[r"(?!)"],
            http_capture_headers_server_response=[r"(?!)"],
        )
        self._instrumented = True

    def shutdown(self) -> None:
        if self._provider is not None:
            self._provider.shutdown()
        if self._instrumented:
            if self._app is not None:
                FastAPIInstrumentor.uninstrument_app(self._app)
        self._app = None
        self._processor = None
        self._provider = None
        self._instrumented = False
        if self._logging_handler is not None:
            logging.getLogger("app").removeHandler(self._logging_handler)
            self._logging_handler.close()
            self._logging_handler = None


observability = Observability()
