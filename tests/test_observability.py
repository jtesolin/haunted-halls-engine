import json
import logging
import threading
from unittest.mock import Mock, patch, sentinel

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.background import BackgroundTask

from app.core import observability as observability_module
from app.core.config import Settings, settings
from app.core.observability import (
    Observability,
    _google_exporter,
    _JsonFormatter,
    _MAX_STACK_TRACE_CHARS,
    _STACK_TRACE_TRUNCATION_MARKER,
)


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

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/healthcheck")
    async def healthcheck() -> dict[str, str]:
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


def test_otel_gcp_project_id_trims_surrounding_whitespace() -> None:
    assert Settings(OTEL_GCP_PROJECT_ID="  test-project  ").OTEL_GCP_PROJECT_ID == (
        "test-project"
    )


@pytest.mark.parametrize("project_id", ["", "   ", "\t\n"])
def test_enabled_export_rejects_whitespace_only_project(project_id: str) -> None:
    with pytest.raises(ValueError, match="OTEL_GCP_PROJECT_ID"):
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID=project_id)


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
        assert client.get("/health?probe=1").status_code == 200
        assert client.get("/health/live?probe=1").status_code == 200
        assert client.get("/healthcheck?probe=1").status_code == 200
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
    assert not any(span.name == "GET /health/live" for span in spans)
    assert any(span.name == "GET /healthcheck" for span in spans)
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
        "query": "distinctive-private-query-marker",
        "user_agent": "distinctive-private-user-agent-marker",
        "forwarded": "distinctive-private-forwarded-marker",
        "client": "distinctive-private-client-marker",
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
        with TestClient(app, client=(markers["client"], 45678)) as client:
            response = client.post(
                f"/private?probe={markers['query']}",
                json={"message": markers["body"]},
                headers={
                    "Authorization": f"Bearer {markers['authorization']}",
                    "Cookie": f"session={markers['cookie']}",
                    "User-Agent": markers["user_agent"],
                    "Forwarded": f"for={markers['forwarded']}",
                    "X-Forwarded-For": markers["forwarded"],
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
        server_span = next(span for span in spans if span.kind == trace.SpanKind.SERVER)
        assert server_span.attributes is not None
        # Raw request target/URL are redacted rather than rebuilt from the
        # ASGI scope path; only the low-cardinality route template survives.
        assert server_span.attributes["http.target"] == "[redacted]"
        assert server_span.attributes["http.route"] == "/private"
    finally:
        observability.shutdown()


def test_dynamic_route_identifier_is_not_recorded_but_route_template_is() -> None:
    """Regression for identifier-bearing dynamic routes (e.g. campaign/
    character IDs): the raw path segment must never be rebuilt into
    http.target/http.url/url.full/url.path, while http.route keeps the
    low-cardinality route template."""
    app = _app()
    campaign_id_marker = "distinctive-campaign-id-marker"

    @app.get("/private/{campaign_id}")
    async def private_campaign(campaign_id: str) -> JSONResponse:
        return JSONResponse({"campaign_id": campaign_id})

    exporter = InMemorySpanExporter()
    observability = Observability()
    observability.initialize(
        app,
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: exporter,
    )
    try:
        with TestClient(app) as client:
            assert client.get(f"/private/{campaign_id_marker}").status_code == 200
        assert observability._processor is not None
        assert observability._processor.force_flush()
        spans = exporter.get_finished_spans()
        server_spans = [span for span in spans if span.kind == trace.SpanKind.SERVER]
        assert len(server_spans) == 1
        serialized_spans = "\n".join(span.to_json() for span in spans)
        assert campaign_id_marker not in serialized_spans
        attributes = server_spans[0].attributes
        assert attributes is not None
        assert attributes["http.route"] == "/private/{campaign_id}"
        raw_path_bearing_keys = ("http.target", "http.url", "url.full", "url.path")
        present_keys = [key for key in raw_path_bearing_keys if key in attributes]
        assert present_keys
        for key in present_keys:
            assert attributes[key] == "[redacted]"
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


def test_enabled_logging_restores_prior_disabled_level_handlers_and_propagation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    logger = logging.getLogger("app")
    original_disabled = logger.disabled
    logger.disabled = True
    original_level = logger.level
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)
    logger.setLevel(logging.DEBUG)
    logger.propagate = True
    try:
        prior_level = logger.level
        prior_propagate = logger.propagate
        prior_handlers = list(logger.handlers)

        observability = Observability()
        observability.initialize(
            _app(),
            Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
            lambda _: InMemorySpanExporter(),
        )
        assert logger.level == logging.INFO
        assert logger.propagate is False
        assert logger.disabled is False

        logger.info("disabled-state-structured-log")
        log_line = next(
            line
            for line in capsys.readouterr().out.splitlines()
            if "disabled-state-structured-log" in line
        )
        payload = json.loads(log_line)
        assert payload["logger"] == "app"
        assert payload["message"] == "disabled-state-structured-log"

        observability.shutdown()
        assert logger.level == prior_level
        assert logger.propagate == prior_propagate
        assert logger.disabled is True
        assert logger.handlers == prior_handlers
    finally:
        logger.disabled = original_disabled
        logger.setLevel(original_level)
        logger.propagate = original_propagate
        logger.handlers = original_handlers


def test_provider_shutdown_failure_still_restores_logger_and_clears_state() -> None:
    """Provider/exporter shutdown failure must not skip safe logger cleanup."""
    app = _app()
    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    logger = logging.getLogger("app")
    logger.disabled = False
    prior_level = logger.level
    prior_propagate = logger.propagate
    prior_handlers = list(logger.handlers)

    observability.initialize(app, settings, lambda _: InMemorySpanExporter())
    assert observability._instrumented is True
    provider = observability._provider
    assert provider is not None

    with patch.object(
        provider,
        "shutdown",
        side_effect=RuntimeError("simulated provider shutdown failure"),
    ):
        observability.shutdown()

    assert observability._instrumented is False
    assert observability._provider is None
    assert observability._processor is None
    assert observability._app is None
    assert observability._logging_handler is None
    assert observability._prior_logger_level is None
    assert observability._prior_logger_propagate is None
    assert observability._prior_logger_disabled is None
    assert logger.level == prior_level
    assert logger.propagate == prior_propagate
    assert logger.handlers == prior_handlers

    retry_app = _app()
    exporter = InMemorySpanExporter()
    observability.initialize(retry_app, settings, lambda _: exporter)
    try:
        assert observability._instrumented is True
        with TestClient(retry_app) as client:
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


def test_uninstrumentation_failure_retains_live_ownership_until_retry() -> None:
    app = _app()
    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    logger = logging.getLogger("app")
    logger.disabled = False
    prior_level = logger.level
    prior_propagate = logger.propagate
    prior_handlers = list(logger.handlers)
    first_exporter = InMemorySpanExporter()

    observability.initialize(app, settings, lambda _: first_exporter)
    provider = observability._provider
    processor = observability._processor
    handler = observability._logging_handler
    assert provider is not None
    assert processor is not None
    assert handler is not None

    with patch(
        "app.core.observability.FastAPIInstrumentor.uninstrument_app",
        side_effect=RuntimeError("simulated uninstrument failure"),
    ), patch.object(provider, "shutdown", wraps=provider.shutdown) as shutdown_provider:
        observability.shutdown()
        shutdown_provider.assert_not_called()

    # The middleware is still live, so its provider and atomic ownership
    # claim must remain live too. In particular, logger cleanup cannot make a
    # second owner believe the process-local D7A resources are unclaimed.
    assert observability._instrumented is True
    assert observability._app is app
    assert observability._provider is provider
    assert observability._processor is processor
    assert observability._logging_handler is handler
    assert handler in logger.handlers
    assert app._is_instrumented_by_opentelemetry is True  # type: ignore[attr-defined]

    competing_owner = Observability()
    competing_owner.initialize(_app(), settings, lambda _: InMemorySpanExporter())
    assert competing_owner._provider is None
    assert competing_owner._instrumented is False
    competing_owner.shutdown()

    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200
    assert processor.force_flush()
    assert len(
        [
            span
            for span in first_exporter.get_finished_spans()
            if span.kind == trace.SpanKind.SERVER
        ]
    ) == 1

    # Public uninstrumentation is retried on the same owner before its
    # provider and logger claim are released.
    observability.shutdown()
    assert observability._instrumented is False
    assert observability._app is None
    assert observability._provider is None
    assert observability._processor is None
    assert observability._logging_handler is None
    assert logger.level == prior_level
    assert logger.propagate == prior_propagate
    assert logger.handlers == prior_handlers

    # The same app can now be initialized again, with exactly one valid server
    # span rather than stale/duplicate middleware tied to the old provider.
    retry_exporter = InMemorySpanExporter()
    observability.initialize(app, settings, lambda _: retry_exporter)
    try:
        with TestClient(app) as client:
            assert client.get("/hello").status_code == 200
        assert observability._processor is not None
        assert observability._processor.force_flush()
        server_spans = [
            span
            for span in retry_exporter.get_finished_spans()
            if span.kind == trace.SpanKind.SERVER
        ]
        assert len(server_spans) == 1
    finally:
        observability.shutdown()


def test_initialization_failure_is_isolated_and_allows_retry() -> None:
    app = _app()
    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    logger = logging.getLogger("app")
    original_disabled = logger.disabled
    logger.disabled = True
    prior_level = logger.level
    prior_propagate = logger.propagate
    prior_handlers = list(logger.handlers)
    prior_disabled = logger.disabled

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
    assert logger.disabled == prior_disabled

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
    assert logger.disabled == prior_disabled
    logger.disabled = original_disabled


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


def test_partial_fastapi_instrumentation_is_rolled_back_and_retry_succeeds() -> None:
    """If instrument_app() mutates the app (setting
    _is_instrumented_by_opentelemetry and patching its middleware stack)
    before raising, D7A must detect that partial mutation from the
    uninstrumented starting state and roll it back through the supported
    public uninstrument API, then allow a clean retry on the same app."""
    app = _app()
    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    logger = logging.getLogger("app")
    logger.disabled = False
    prior_level = logger.level
    prior_propagate = logger.propagate
    prior_handlers = list(logger.handlers)

    original_instrument_app = FastAPIInstrumentor.instrument_app

    def partially_mutate_then_fail(target_app, **kwargs):  # type: ignore[no-untyped-def]
        # Simulate the real instrumentor completing its mutation of the app
        # before a later failure (e.g. in caller-side bookkeeping) surfaces.
        original_instrument_app(target_app, **kwargs)
        raise RuntimeError("simulated failure after instrumentation applied")

    with patch(
        "app.core.observability.FastAPIInstrumentor.instrument_app",
        side_effect=partially_mutate_then_fail,
    ):
        observability.initialize(app, settings, lambda _: InMemorySpanExporter())

    assert observability._instrumented is False
    assert observability._provider is None
    assert observability._processor is None
    assert observability._app is None
    assert observability._logging_handler is None
    assert app._is_instrumented_by_opentelemetry is False  # type: ignore[attr-defined]
    assert logger.level == prior_level
    assert logger.propagate == prior_propagate
    assert logger.handlers == prior_handlers

    with TestClient(app) as client:
        assert client.get("/hello").status_code == 200

    exporter = InMemorySpanExporter()
    observability.initialize(app, settings, lambda _: exporter)
    try:
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


def test_oversized_exception_diagnostics_are_bounded_and_valid_json() -> None:
    formatter = _JsonFormatter(Settings(OTEL_GCP_PROJECT_ID="test-project"))
    oversized_message = "distinctive-oversized-message-" + (
        "x" * (_MAX_STACK_TRACE_CHARS * 2)
    )
    try:
        raise ValueError(oversized_message)
    except ValueError:
        import sys

        record = logging.LogRecord(
            "app.test", logging.ERROR, "", 0, "operation failed", (), sys.exc_info()
        )
    record.__dict__["private_extra"] = "must-not-be-serialized"

    serialized = formatter.format(record)
    payload = json.loads(serialized)

    assert set(payload) == {
        "severity",
        "message",
        "logger",
        "timestamp",
        "exception.type",
        "exception.message",
        "stack_trace",
    }
    assert payload["exception.type"] == "ValueError"
    assert len(payload["exception.message"]) == _MAX_STACK_TRACE_CHARS
    assert payload["exception.message"].endswith(_STACK_TRACE_TRUNCATION_MARKER)
    assert len(payload["stack_trace"]) == _MAX_STACK_TRACE_CHARS
    assert payload["stack_trace"].endswith(_STACK_TRACE_TRUNCATION_MARKER)
    assert "must-not-be-serialized" not in serialized


def test_log_without_exc_info_omits_exception_fields() -> None:
    formatter = _JsonFormatter(Settings(OTEL_GCP_PROJECT_ID="test-project"))
    record = logging.LogRecord(
        "app.test", logging.INFO, "", 0, "no failure here", (), None
    )
    payload = json.loads(formatter.format(record))
    assert "exception.type" not in payload
    assert "exception.message" not in payload
    assert "stack_trace" not in payload


def test_second_logging_owner_degrades_and_cannot_disturb_first_owner() -> None:
    """A second overlapping Observability instance must not claim, mutate, or
    later release the first instance's active D7A logging handler/state."""
    logger = logging.getLogger("app")
    logger.disabled = False
    original_level = logger.level
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)

    owner1 = Observability()
    owner2 = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")

    try:
        owner1.initialize(_app(), settings, lambda _: InMemorySpanExporter())
        assert owner1._logging_handler is not None
        assert owner1._instrumented is True
        owner1_handler = owner1._logging_handler
        level_after_owner1 = logger.level
        propagate_after_owner1 = logger.propagate
        handlers_after_owner1 = list(logger.handlers)

        # A second instance targeting a distinct app must degrade to disabled
        # rather than sharing or claiming the already-active handler.
        owner2.initialize(_app(), settings, lambda _: InMemorySpanExporter())
        assert owner2._logging_handler is None
        assert owner2._instrumented is False
        assert owner2._provider is None
        assert logger.handlers == handlers_after_owner1
        assert logger.level == level_after_owner1
        assert logger.propagate == propagate_after_owner1

        # Shutting down the degraded second instance must not disturb the
        # first owner's handler or logger state at all.
        owner2.shutdown()
        assert logger.handlers == handlers_after_owner1
        assert owner1_handler in logger.handlers
        assert logger.level == level_after_owner1
        assert logger.propagate == propagate_after_owner1

        owner1.shutdown()
        assert owner1._logging_handler is None
        assert logger.level == original_level
        assert logger.propagate == original_propagate
        assert logger.handlers == original_handlers
    finally:
        owner1.shutdown()
        owner2.shutdown()
        logger.setLevel(original_level)
        logger.propagate = original_propagate


def test_distinct_fastapi_app_instrumentation_remains_application_local() -> None:
    external_app = _app()
    external_exporter = InMemorySpanExporter()
    external_provider = TracerProvider()
    external_provider.add_span_processor(SimpleSpanProcessor(external_exporter))

    owned_app = _app()
    owner = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    original_background_call = BackgroundTask.__call__

    FastAPIInstrumentor.instrument_app(
        external_app,
        tracer_provider=external_provider,
        http_capture_headers_server_request=[r"(?!)"],
        http_capture_headers_server_response=[r"(?!)"],
    )
    assert BackgroundTask.__call__ is original_background_call

    try:
        owner.initialize(owned_app, settings, lambda _: InMemorySpanExporter())
        assert owner._instrumented is True
        assert BackgroundTask.__call__ is original_background_call

        owner.shutdown()
        assert owner._instrumented is False
        assert BackgroundTask.__call__ is original_background_call

        with TestClient(external_app) as client:
            assert client.get("/hello").status_code == 200
        external_provider.force_flush()
        span_names = {span.name for span in external_exporter.get_finished_spans()}
        assert "GET /hello" in span_names
    finally:
        owner.shutdown()
        FastAPIInstrumentor.uninstrument_app(external_app)
        external_provider.shutdown()
        assert BackgroundTask.__call__ is original_background_call


def test_auth_startup_failure_does_not_claim_observability_and_retry_is_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as main_module

    app = main_module.app
    observability = Observability()
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(main_module, "observability", observability)
    monkeypatch.setattr(settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(settings, "OTEL_GCP_PROJECT_ID", "test-project")
    monkeypatch.setattr(settings, "INTERNAL_ENGINE_SERVICE_TOKEN", None)
    monkeypatch.setattr(observability_module, "_google_exporter", lambda _: exporter)

    with pytest.raises(RuntimeError, match="INTERNAL_ENGINE_SERVICE_TOKEN"):
        with TestClient(app):
            pass

    assert observability._provider is None
    assert observability._processor is None
    assert observability._instrumented is False
    assert observability._logging_handler is None

    monkeypatch.setattr(
        settings,
        "INTERNAL_ENGINE_SERVICE_TOKEN",
        "test-internal-engine-service-token-0000000000000000000000000000000000",
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 200

    assert observability._provider is None
    assert observability._processor is None
    assert observability._instrumented is False
    assert observability._logging_handler is None
    server_spans = [
        span for span in exporter.get_finished_spans()
        if span.kind == trace.SpanKind.SERVER
    ]
    assert len(server_spans) == 1
    assert server_spans[0].name == "GET /"


def test_fastapi_instrumentation_does_not_claim_pre_instrumented_app() -> None:
    """D7A must not claim ownership of, or later uninstrument, FastAPI
    instrumentation that already exists on the app before initialize()."""
    app = _app()
    prior_middleware = list(app.user_middleware)

    # Simulate pre-existing instrumentation (e.g. auto-instrumentation or a
    # prior owner) without invoking real OTel instrumentation machinery.
    app._is_instrumented_by_opentelemetry = True  # type: ignore[attr-defined]

    observability = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")

    with patch(
        "app.core.observability.FastAPIInstrumentor.instrument_app"
    ) as instrument, patch(
        "app.core.observability.FastAPIInstrumentor.uninstrument_app"
    ) as uninstrument:
        observability.initialize(app, settings, lambda _: InMemorySpanExporter())
        instrument.assert_not_called()

        assert observability._instrumented is False
        assert observability._provider is None
        assert observability._logging_handler is None

        observability.shutdown()
        uninstrument.assert_not_called()

    assert app.user_middleware == prior_middleware
    assert app._is_instrumented_by_opentelemetry is True  # type: ignore[attr-defined]


def test_overlapping_instrumentation_owners_cannot_uninstrument_each_other() -> None:
    """Two Observability instances targeting the same app: only the first
    claims ownership; the second's shutdown must not uninstrument the app."""
    app = _app()
    owner1 = Observability()
    owner2 = Observability()
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")

    try:
        owner1.initialize(app, settings, lambda _: InMemorySpanExporter())
        assert owner1._instrumented is True
        assert app._is_instrumented_by_opentelemetry is True  # type: ignore[attr-defined]

        owner2.initialize(app, settings, lambda _: InMemorySpanExporter())
        assert owner2._instrumented is False
        assert owner2._provider is None
        assert owner2._logging_handler is None
        # Owner1's instrumentation must remain intact.
        assert app._is_instrumented_by_opentelemetry is True  # type: ignore[attr-defined]

        with patch(
            "app.core.observability.FastAPIInstrumentor.uninstrument_app"
        ) as uninstrument:
            owner2.shutdown()
            uninstrument.assert_not_called()

        assert app._is_instrumented_by_opentelemetry is True  # type: ignore[attr-defined]
        with TestClient(app) as client:
            assert client.get("/hello").status_code == 200
    finally:
        owner1.shutdown()
        owner2.shutdown()


def test_concurrent_initialize_has_exactly_one_owner() -> None:
    """Two concurrent same-process initialize() calls must not both claim
    ownership: the check-and-claim lifecycle is serialized by a small
    process-local lock so exactly one instance wins, the loser is fully
    uninitialized, the loser's shutdown is harmless, and the winner still
    traces/logs and can later restore state cleanly."""
    logger = logging.getLogger("app")
    logger.disabled = False
    original_level = logger.level
    original_propagate = logger.propagate
    original_handlers = list(logger.handlers)

    apps = [_app(), _app()]
    owners = [Observability(), Observability()]
    exporters = [InMemorySpanExporter(), InMemorySpanExporter()]
    settings = Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project")
    barrier = threading.Barrier(2)

    def run(index: int) -> None:
        barrier.wait()
        owners[index].initialize(
            apps[index], settings, lambda _, exporter=exporters[index]: exporter
        )

    threads = [threading.Thread(target=run, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    try:
        instrumented_flags = [owner._instrumented for owner in owners]
        assert instrumented_flags.count(True) == 1
        assert instrumented_flags.count(False) == 1

        winner_index = instrumented_flags.index(True)
        loser_index = instrumented_flags.index(False)
        winner = owners[winner_index]
        loser = owners[loser_index]
        winner_app = apps[winner_index]

        # The loser must be fully uninitialized: it never claimed logging,
        # instrumentation, or a tracer provider.
        assert loser._provider is None
        assert loser._processor is None
        assert loser._app is None
        assert loser._logging_handler is None

        handlers_after_race = list(logger.handlers)
        level_after_race = logger.level
        propagate_after_race = logger.propagate

        # The loser's shutdown must be harmless and must not disturb the
        # winner's active handler or logger state.
        loser.shutdown()
        assert logger.handlers == handlers_after_race
        assert logger.level == level_after_race
        assert logger.propagate == propagate_after_race
        assert winner._instrumented is True

        with TestClient(winner_app) as client:
            assert client.get("/hello").status_code == 200
        assert winner._processor is not None
        winner._processor.force_flush()
        winner_exporter = exporters[winner_index]
        server_spans = [
            span for span in winner_exporter.get_finished_spans()
            if span.kind == trace.SpanKind.SERVER
        ]
        assert len(server_spans) == 1

        winner.shutdown()
        assert winner._instrumented is False
        assert logger.handlers == original_handlers
        assert logger.level == original_level
        assert logger.propagate == original_propagate
    finally:
        for owner in owners:
            owner.shutdown()
        logger.setLevel(original_level)
        logger.propagate = original_propagate
