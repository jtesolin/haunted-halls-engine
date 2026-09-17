import json
import logging
from unittest.mock import Mock, patch, sentinel

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.core.observability import Observability, _google_exporter, _JsonFormatter


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/hello")
    async def hello() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/echo")
    async def echo() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def test_disabled_does_not_construct_exporter_or_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    observability = Observability()
    logger = logging.getLogger("app")
    handler = logging.NullHandler()
    formatter = logging.Formatter("%(levelname)s: %(message)s")
    handler.setFormatter(formatter)
    monkeypatch.setattr(logger, "handlers", [handler])
    monkeypatch.setattr(logger, "propagate", True)
    handlers = logger.handlers
    level = logger.level
    provider = trace.get_tracer_provider()
    middleware = list(app.user_middleware)

    with (
        patch("app.core.observability._configure_logging") as configure_logging,
        patch("app.core.observability._google_exporter") as google_exporter,
        patch("app.core.observability.TracerProvider") as create_provider,
        patch("app.core.observability.FastAPIInstrumentor.instrument_app") as instrument,
        patch("app.core.observability.FastAPIInstrumentor.uninstrument_app") as uninstrument,
    ):
        factory = Mock()
        settings = Settings(OTEL_ENABLED=False)
        observability.initialize(app, settings)
        observability.initialize(app, settings, factory)
        with TestClient(app) as client:
            assert client.get("/hello").json() == {"ok": True}
        observability.shutdown()
        configure_logging.assert_not_called()
        google_exporter.assert_not_called()
        factory.assert_not_called()
        create_provider.assert_not_called()
        instrument.assert_not_called()
        uninstrument.assert_not_called()

    assert trace.get_tracer_provider() is provider
    assert app.user_middleware == middleware
    assert logger.handlers is handlers
    assert logger.handlers == [handler]
    assert handler.formatter is formatter
    assert logger.level == level
    assert logger.propagate is True
    assert observability._app is None
    assert observability._processor is None
    assert observability._logging_handler is None


@pytest.mark.parametrize("ratio", [0.0, 1.0])
def test_sample_ratio_boundaries_are_valid(ratio: float) -> None:
    assert Settings(OTEL_TRACES_SAMPLE_RATIO=ratio).OTEL_TRACES_SAMPLE_RATIO == ratio


@pytest.mark.parametrize("ratio", [-0.01, 1.01])
def test_sample_ratio_out_of_range_is_rejected(ratio: float) -> None:
    with pytest.raises(ValueError):
        Settings(OTEL_TRACES_SAMPLE_RATIO=ratio)


def test_enabled_export_requires_project() -> None:
    with pytest.raises(ValueError, match="OTEL_GCP_PROJECT_ID"):
        Settings(OTEL_ENABLED=True)


@pytest.mark.parametrize("ratio", [0.0, 1.0])
@pytest.mark.parametrize("parent_flags", [None, "01", "00"])
def test_parent_based_sampling_behavior(ratio: float, parent_flags: str | None) -> None:
    app = _app()
    exporter = InMemorySpanExporter()
    observability = Observability()
    observability.initialize(
        app,
        Settings(
            OTEL_ENABLED=True,
            OTEL_GCP_PROJECT_ID="test-project",
            OTEL_TRACES_SAMPLE_RATIO=ratio,
        ),
        lambda _: exporter,
    )
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    parent_id = "b7ad6b7169203331"
    headers = (
        {"traceparent": f"00-{trace_id}-{parent_id}-{parent_flags}"}
        if parent_flags is not None
        else {}
    )
    try:
        with TestClient(app) as client:
            assert client.get("/hello", headers=headers).status_code == 200
        assert observability._processor is not None
        assert observability._processor.force_flush()
        spans = exporter.get_finished_spans()
        sampled = ratio == 1.0 if parent_flags is None else parent_flags == "01"
        if not sampled:
            assert not spans
        else:
            server_spans = [span for span in spans if span.kind == trace.SpanKind.SERVER]
            assert len(server_spans) == 1
            span = server_spans[0]
            assert span.context is not None
            assert span.context.trace_flags.sampled
            if parent_flags is None:
                assert span.parent is None
            else:
                assert f"{span.context.trace_id:032x}" == trace_id
                assert span.parent is not None
                assert span.parent.is_remote
                assert f"{span.parent.span_id:016x}" == parent_id
    finally:
        observability.shutdown()


def test_google_exporter_uses_adc_quota_project_and_secure_grpc() -> None:
    settings = Settings(
        OTEL_ENABLED=True,
        OTEL_GCP_PROJECT_ID="test-quota-project",
        OTEL_EXPORTER_OTLP_ENDPOINT="configured-telemetry.example:443",
    )
    with (
        patch(
            "app.core.observability.google.auth.default",
            return_value=(sentinel.adc_credentials, "different-adc-project"),
        ) as adc,
        patch("app.core.observability.Request", return_value=sentinel.request) as request,
        patch(
            "app.core.observability.AuthMetadataPlugin",
            return_value=sentinel.auth_plugin,
        ) as auth_plugin,
        patch(
            "app.core.observability.grpc.metadata_call_credentials",
            return_value=sentinel.call_credentials,
        ) as metadata_credentials,
        patch(
            "app.core.observability.grpc.ssl_channel_credentials",
            return_value=sentinel.tls_credentials,
        ) as tls_credentials,
        patch(
            "app.core.observability.grpc.composite_channel_credentials",
            return_value=sentinel.composite_credentials,
        ) as composite_credentials,
        patch(
            "app.core.observability.OTLPSpanExporter", return_value=sentinel.exporter
        ) as exporter,
    ):
        assert _google_exporter(settings) is sentinel.exporter

    adc.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
        quota_project_id="test-quota-project",
    )
    request.assert_called_once_with()
    auth_plugin.assert_called_once_with(sentinel.adc_credentials, sentinel.request)
    metadata_credentials.assert_called_once_with(sentinel.auth_plugin)
    tls_credentials.assert_called_once_with()
    composite_credentials.assert_called_once_with(
        sentinel.tls_credentials, sentinel.call_credentials
    )
    exporter.assert_called_once_with(
        endpoint="configured-telemetry.example:443",
        credentials=sentinel.composite_credentials,
        insecure=False,
    )


def test_request_spans_health_exclusion_and_w3c_propagation() -> None:
    app = _app()
    exporter = InMemorySpanExporter()
    observability = Observability()
    observability.initialize(
        app,
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: exporter,
    )
    trace_id = "0af7651916cd43dd8448eb211c80319c"
    with TestClient(app) as client:
        assert client.get(
            "/hello",
            headers={"traceparent": f"00-{trace_id}-b7ad6b7169203331-01"},
        ).status_code == 200
        assert client.get("/health").status_code == 200
    assert observability._processor is not None
    observability._processor.force_flush()
    spans = exporter.get_finished_spans()
    request_spans = [span for span in spans if span.name == "GET /hello"]
    assert len(request_spans) == 1
    assert request_spans[0].kind == trace.SpanKind.SERVER
    attributes = request_spans[0].attributes
    assert attributes is not None
    assert attributes["http.method"] == "GET"
    assert attributes["http.route"] == "/hello"
    assert attributes["http.status_code"] == 200
    context = request_spans[0].context
    assert context is not None
    assert f"{context.trace_id:032x}" == trace_id
    assert not any(span.name == "GET /health" for span in spans)
    observability.shutdown()


@pytest.mark.parametrize(
    "capture_headers", [".*", "authorization,cookie,set-cookie,x-private-response"]
)
def test_sensitive_request_values_are_not_recorded(
    monkeypatch: pytest.MonkeyPatch, capture_headers: str,
) -> None:
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", capture_headers
    )
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE", capture_headers
    )
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS", "")
    app = _app()
    markers = {
        "body": "distinctive-private-body-marker",
        "authorization": "distinctive-private-auth-marker",
        "cookie": "distinctive-private-cookie-marker",
        "response": "distinctive-private-response-marker",
        "set_cookie": "distinctive-private-set-cookie-marker",
    }

    @app.post("/private")
    async def private_response() -> JSONResponse:
        return JSONResponse(
            {"message": markers["body"]},
            headers={
                "X-Private-Response": markers["response"],
                "Set-Cookie": f"session={markers['set_cookie']}",
            },
        )

    exporter = InMemorySpanExporter()
    observability = Observability()
    observability.initialize(
        app,
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: exporter,
    )
    try:
        with TestClient(app) as client:
            response = client.post(
                "/private",
                json={"message": markers["body"]},
                headers={
                    "Authorization": f"Bearer {markers['authorization']}",
                    "Cookie": f"session={markers['cookie']}",
                },
            )
            assert response.status_code == 200
            assert response.json() == {"message": markers["body"]}
            assert response.headers["x-private-response"] == markers["response"]
            assert markers["set_cookie"] in response.headers["set-cookie"]
        assert observability._processor is not None
        assert observability._processor.force_flush()
        spans = exporter.get_finished_spans()
        assert any(span.kind == trace.SpanKind.SERVER for span in spans)
        serialized_spans = "\n".join(span.to_json() for span in spans)
        assert all(marker not in serialized_spans for marker in markers.values())
        assert "http.request.header." not in serialized_spans
        assert "http.response.header." not in serialized_spans
    finally:
        observability.shutdown()


def test_resource_contains_service_name_without_identity() -> None:
    app = _app()
    exporter = InMemorySpanExporter()
    observability = Observability()
    observability.initialize(
        app,
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: exporter,
    )
    assert observability._provider is not None
    attributes = dict(observability._provider.resource.attributes)
    assert attributes["service.name"] == "haunted-halls-engine"
    assert not any(
        key in attributes
        for key in ("user.id", "user.email", "campaign.id", "turn.id", "chat.text")
    )
    observability.shutdown()


def test_structured_logging_correlates_only_inside_span() -> None:
    formatter = _JsonFormatter(Settings(OTEL_GCP_PROJECT_ID="test-project"))
    record = logging.LogRecord(
        "app.test", logging.INFO, "", 0, "existing message %s", ("unchanged",), None
    )
    record.__dict__["private_extra"] = "distinctive-private-log-extra"
    base_keys = {"severity", "message", "logger", "timestamp"}
    provider = TracerProvider()
    try:
        tracer = provider.get_tracer("test")
        with trace.use_span(trace.INVALID_SPAN):
            outside = json.loads(formatter.format(record))
            assert set(outside) == base_keys
            with tracer.start_as_current_span("test-span") as span:
                inside = json.loads(formatter.format(record))
                context = span.get_span_context()
            after = json.loads(formatter.format(record))
        assert set(after) == base_keys
        assert set(inside) == base_keys | {
            "logging.googleapis.com/spanId",
            "logging.googleapis.com/trace",
            "logging.googleapis.com/trace_sampled",
        }
        assert inside["logging.googleapis.com/spanId"] == f"{context.span_id:016x}"
        assert inside["logging.googleapis.com/trace"] == (
            f"projects/test-project/traces/{context.trace_id:032x}"
        )
        assert inside["logging.googleapis.com/trace_sampled"] is True
        for payload in (outside, inside, after):
            assert payload["message"] == "existing message unchanged"
            assert payload["severity"] == "INFO"
            assert payload["logger"] == "app.test"
            assert "distinctive-private-log-extra" not in json.dumps(payload)
    finally:
        provider.shutdown()


def test_exporter_failure_does_not_fail_request() -> None:
    class FailingExporter(InMemorySpanExporter):
        def export(self, spans):  # type: ignore[no-untyped-def]
            raise RuntimeError("simulated exporter failure")

    app = _app()
    observability = Observability()
    observability.initialize(
        app,
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: FailingExporter(),
    )
    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200
    observability.shutdown()


def test_shutdown_closes_owned_exporter() -> None:
    class ClosableExporter(InMemorySpanExporter):
        closed = False

        def shutdown(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            self.closed = True

    exporter = ClosableExporter()
    observability = Observability()
    observability.initialize(
        _app(),
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: exporter,
    )
    observability.shutdown()
    assert exporter.closed


def test_initialization_is_idempotent() -> None:
    app = _app()
    observability = Observability()
    exporter = InMemorySpanExporter()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    factory = Mock(return_value=exporter)
    observability.initialize(app, settings, factory)
    processor = observability._processor
    observability.initialize(app, settings, factory)
    factory.assert_called_once_with(settings)
    assert observability._processor is processor
    assert len([h for h in logging.getLogger("app").handlers if getattr(
        h, "_haunted_halls_observability", False
    )]) == 1
    try:
        with TestClient(app) as client:
            for expected_count in (1, 2):
                assert client.get("/hello").status_code == 200
                assert processor is not None
                assert processor.force_flush()
                server_spans = [
                    span for span in exporter.get_finished_spans()
                    if span.kind == trace.SpanKind.SERVER
                ]
                assert len(server_spans) == expected_count
                assert all(span.name == "GET /hello" for span in server_spans)
        assert server_spans[0].context != server_spans[1].context
    finally:
        observability.shutdown()


def test_reinitialize_after_shutdown_preserves_trace_log_correlation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Alembic's fileConfig (triggered by the per-test isolated_database fixture)
    # disables previously registered loggers; re-enable "app" as other tests do.
    logging.getLogger("app").disabled = False
    app = FastAPI()

    @app.get("/traced")
    async def traced() -> dict[str, bool]:
        logging.getLogger("app").info("traced-request-log")
        return {"ok": True}

    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")

    def run_traced_cycle(exporter: InMemorySpanExporter) -> tuple[str, dict[str, object]]:
        observability.initialize(app, settings, lambda _: exporter)
        with TestClient(app) as client:
            assert client.get("/traced").status_code == 200
        assert observability._processor is not None
        observability._processor.force_flush()
        server_spans = [
            span for span in exporter.get_finished_spans()
            if span.kind == trace.SpanKind.SERVER
        ]
        assert len(server_spans) == 1
        captured = capsys.readouterr().out
        log_line = next(
            line for line in captured.splitlines() if "traced-request-log" in line
        )
        payload = json.loads(log_line)
        context = server_spans[0].context
        assert context is not None
        trace_id = f"{context.trace_id:032x}"
        return trace_id, payload

    try:
        trace_id_1, payload_1 = run_traced_cycle(InMemorySpanExporter())
        assert observability._provider is not None
        provider_1 = observability._provider
        observability.shutdown()
        assert observability._provider is None
        assert observability._instrumented is False

        trace_id_2, payload_2 = run_traced_cycle(InMemorySpanExporter())
        assert observability._provider is not None
        assert observability._provider is not provider_1
    finally:
        observability.shutdown()

    assert trace_id_1 != trace_id_2
    assert payload_1["logging.googleapis.com/trace"] == (
        f"projects/test-project/traces/{trace_id_1}"
    )
    assert payload_2["logging.googleapis.com/trace"] == (
        f"projects/test-project/traces/{trace_id_2}"
    )
    for payload in (payload_1, payload_2):
        assert payload["logging.googleapis.com/trace_sampled"] is True
        assert "logging.googleapis.com/spanId" in payload


def test_shutdown_restores_prior_logger_level_and_propagation() -> None:
    logger = logging.getLogger("app")
    logger.disabled = False
    original_level = logger.level
    original_propagate = logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    try:
        prior_level = logger.level
        prior_propagate = logger.propagate

        observability = Observability()
        observability.initialize(
            _app(),
            Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
            lambda _: InMemorySpanExporter(),
        )
        assert logger.level == logging.INFO
        assert logger.propagate is False

        observability.shutdown()
        assert logger.level == prior_level
        assert logger.propagate == prior_propagate
    finally:
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_initialization_failure_is_isolated_and_allows_retry() -> None:
    app = _app()
    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    logger = logging.getLogger("app")
    logger.disabled = False
    prior_level = logger.level
    prior_propagate = logger.propagate
    prior_handlers = list(logger.handlers)

    def failing_factory(_: Settings) -> SpanExporter:
        raise RuntimeError("simulated ADC/exporter failure")

    observability.initialize(app, settings, failing_factory)

    assert observability._provider is None
    assert observability._processor is None
    assert observability._instrumented is False
    assert observability._logging_handler is None
    assert logger.level == prior_level
    assert logger.propagate == prior_propagate
    assert logger.handlers == prior_handlers

    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200

    exporter = InMemorySpanExporter()
    observability.initialize(app, settings, lambda _: exporter)
    try:
        assert observability._provider is not None
        assert observability._instrumented is True
        with TestClient(app) as client:
            assert client.get("/hello").status_code == 200
        assert observability._processor is not None
        observability._processor.force_flush()
        server_spans = [
            span for span in exporter.get_finished_spans()
            if span.kind == trace.SpanKind.SERVER
        ]
        assert len(server_spans) == 1
    finally:
        observability.shutdown()

    assert logger.level == prior_level
    assert logger.propagate == prior_propagate
    assert logger.handlers == prior_handlers


def test_instrumentation_failure_during_initialize_cleans_up_state() -> None:
    app = _app()
    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    logger = logging.getLogger("app")
    logger.disabled = False
    prior_level = logger.level
    prior_propagate = logger.propagate
    prior_handlers = list(logger.handlers)

    with patch(
        "app.core.observability.FastAPIInstrumentor.instrument_app",
        side_effect=RuntimeError("simulated instrumentation failure"),
    ) as instrument, patch(
        "app.core.observability.FastAPIInstrumentor.uninstrument_app"
    ) as uninstrument:
        observability.initialize(app, settings, lambda _: InMemorySpanExporter())
        instrument.assert_called_once()
        uninstrument.assert_not_called()

    assert observability._provider is None
    assert observability._processor is None
    assert observability._instrumented is False
    assert observability._app is None
    assert observability._logging_handler is None
    assert logger.level == prior_level
    assert logger.propagate == prior_propagate
    assert logger.handlers == prior_handlers


def test_exception_log_preserves_type_message_and_traceback() -> None:
    formatter = _JsonFormatter(Settings(OTEL_GCP_PROJECT_ID="test-project"))
    try:
        raise ValueError("distinctive-failure-message")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "app.test", logging.ERROR, "", 0, "operation failed", (), sys.exc_info()
        )
    payload = json.loads(formatter.format(record))
    assert payload["exception.type"] == "ValueError"
    assert payload["exception.message"] == "distinctive-failure-message"
    assert "ValueError: distinctive-failure-message" in payload["stack_trace"]
    assert "Traceback" in payload["stack_trace"]


def test_log_without_exc_info_omits_exception_fields() -> None:
    formatter = _JsonFormatter(Settings(OTEL_GCP_PROJECT_ID="test-project"))
    record = logging.LogRecord(
        "app.test", logging.INFO, "", 0, "no failure here", (), None
    )
    payload = json.loads(formatter.format(record))
    assert "exception.type" not in payload
    assert "exception.message" not in payload
    assert "stack_trace" not in payload
