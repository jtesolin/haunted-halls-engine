# D7A observability

D7A provides the engine's OpenTelemetry tracing foundation. It is deliberately
small: normal FastAPI requests can produce one inbound HTTP server span, while
gameplay, orchestration, agents, memory, and persistence remain uninstrumented
until later D7 slices.

## Tracing

Observability is disabled by default (`OTEL_ENABLED=false`): initialization does
not change application logging, instrument requests, or create an exporter.
When enabled, the engine
uses the official FastAPI instrumentation, accepts and honors W3C
`traceparent`, and applies parent-aware ratio sampling:

```text
ParentBased(TraceIdRatioBased(OTEL_TRACES_SAMPLE_RATIO))
```

Health/probe requests under `/health` are excluded. Request and response bodies,
chat text, prompts, model output, memories, summaries, authorization headers,
cookies, service tokens, API keys, Google credentials, and provider payloads are
never recorded. HTTP request/response header capture is explicitly disabled,
even when ambient OpenTelemetry header-capture environment variables are set.

The resource always contains `service.name=haunted-halls-engine` by default.
Version, deployment environment, Cloud Run service/revision, and project
attributes are added only when supplied by configuration or Cloud Run.

D7A uses a simple single-owner model, not reference counting: the first
`Observability` instance to see an unclaimed `app.*` logging handler and an
uninstrumented FastAPI app claims both. A second overlapping instance, or an
app that is already instrumented elsewhere, degrades that instance to
disabled telemetry instead of sharing or later tearing down state it does not
own; its `shutdown()` is then a no-op with respect to the other owner's
handler and instrumentation.

The OpenTelemetry packages are pinned to the coherent `1.41.1`/`0.62b1`
release family. FastAPI instrumentation `0.63b0` introduced a process-wide
Starlette `BackgroundTask` patch whose per-app teardown is unsafe when multiple
FastAPI applications share a process. The pinned family keeps instrumentation
and teardown application-local while D7A uses only the supported public API.

## Google export

The production exporter is direct OTLP/gRPC to the Google Cloud Telemetry API
(`telemetry.googleapis.com:443`) using Application Default Credentials and
Google-authenticated gRPC channel credentials. There is no collector sidecar
and no hard-coded project ID. Cloud Trace is the eventual trace storage and
viewing location. Export is batched and exporter failures do not fail API
requests. `OTEL_GCP_PROJECT_ID` is passed to ADC as the quota project, applied
by Google Auth to credential types supporting quota projects (including user
ADC), as well as identifying the project in trace correlation and resources.
ADC requests the `cloud-platform` scope; export uses TLS with composed Google
call credentials and never an insecure gRPC channel.

Production activation is intentionally pending the companion infrastructure
work for Telemetry API enablement, least-privilege runtime IAM, and Cloud Run
environment settings.

## Logging and privacy

When observability is enabled, application records under the `app.*` namespace
are emitted as structured JSON
to stdout/stderr for Cloud Logging. While a valid span is active, records also
contain Cloud Logging trace, span ID, and sampled correlation fields. Outside a
trace those fields are omitted. Logs are not exported through OTLP, so Cloud
Logging does not receive duplicate records. Existing application messages are
preserved; arbitrary LogRecord extras are not serialized or added to
correlation metadata. When a log call includes exception information
(`exc_info=True`), the exception type, message, and a bounded stack trace are
retained in dedicated structured fields so existing `logger.error(...,
exc_info=True)` call sites keep useful diagnostics; no other `LogRecord`
attributes are added.

D7A's privacy contract governs what the new OpenTelemetry span and
trace-correlation metadata capture: neither ever records request/response
bodies, headers, gameplay text, prompts, model output, memories, credentials,
or provider payloads. D7A deliberately does not rewrite, redact, or otherwise
sanitize the content of pre-existing `app.*` log call sites; those messages
already exist in Cloud Run stdout/stderr today, and any broad sanitization or
redesign of that historical logging content is a separate, dedicated slice
of work.

## Future boundaries

* D7B: BFF-to-engine distributed propagation.
* D7C: internal parser, Director, Narrator, memory, and persistence spans.
* D7D: production dashboards, sampling tuning, and operational verification.
