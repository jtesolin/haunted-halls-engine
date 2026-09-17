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
correlation metadata.

## Future boundaries

* D7B: BFF-to-engine distributed propagation.
* D7C: internal parser, Director, Narrator, memory, and persistence spans.
* D7D: production dashboards, sampling tuning, and operational verification.
