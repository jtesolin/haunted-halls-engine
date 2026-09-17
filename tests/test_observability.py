import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.core.config import Settings
from app.core.observability import Observability, _JsonFormatter


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


def test_disabled_does_not_construct_exporter_or_instrument() -> None:
    app = _app()
    observability = Observability()
    constructed = False

    def factory(_: Settings) -> InMemorySpanExporter:
        nonlocal constructed
        constructed = True
        return InMemorySpanExporter()

    observability.initialize(app, Settings(), factory)
    with TestClient(app) as client:
        assert client.get("/hello").json() == {"ok": True}
    assert not constructed
    observability.shutdown()


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
    context = request_spans[0].context
    assert context is not None
    assert f"{context.trace_id:032x}" == trace_id
    assert not any(span.name == "GET /health" for span in spans)
    observability.shutdown()


def test_sensitive_request_values_are_not_recorded() -> None:
    app = _app()
    exporter = InMemorySpanExporter()
    observability = Observability()
    observability.initialize(
        app,
        Settings(OTEL_ENABLED=True, OTEL_GCP_PROJECT_ID="test-project"),
        lambda _: exporter,
    )
    marker = "distinctive-private-marker"
    with TestClient(app) as client:
        assert client.post(
            "/echo",
            json={"message": marker},
            headers={"Authorization": f"Bearer {marker}"},
        ).status_code == 200
    assert observability._processor is not None
    observability._processor.force_flush()
    serialized_spans = repr(exporter.get_finished_spans())
    assert marker not in serialized_spans
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
    outside = json.loads(formatter.format(logging.LogRecord(
        "app.test", 20, "", 0, "outside", (), None
    )))
    assert "logging.googleapis.com/trace" not in outside
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("test-span"):
        inside = json.loads(formatter.format(logging.LogRecord(
            "app.test", 20, "", 0, "inside", (), None
        )))
    assert inside["logging.googleapis.com/spanId"]
    assert inside["logging.googleapis.com/trace"].startswith("projects/test-project/")
    assert "logging.googleapis.com/trace_sampled" in inside


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
    observability.initialize(app, settings, lambda _: exporter)
    processor = observability._processor
    observability.initialize(app, settings, lambda _: exporter)
    assert observability._processor is processor
    assert len([h for h in logging.getLogger("app").handlers if getattr(
        h, "_haunted_halls_observability", False
    )]) == 1
    observability.shutdown()
