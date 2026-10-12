"""Lab service — a small HTTP service to exercise monitoring.

The point of this service is NOT the code itself: it exists so you can stand up
full monitoring around it (metrics, logs, traces) and make the graphs react by
calling the endpoints (FastAPI's Swagger UI at / lets you trigger them).

It provides:
  GET /            -> Swagger UI to trigger the endpoints by hand
  GET /error       -> returns HTTP 500 and increments the error counter
  GET /delay       -> sleeps 1-3 s (a "slow" request)
  GET /load        -> fires a burst of requests at this service itself
  GET /metrics     -> Prometheus RED metrics
  GET /health      -> simple liveness probe

It is instrumented with OpenTelemetry: every incoming request gets a root span
(auto-instrumented via FastAPIInstrumentor), the /delay handler adds a nested
"slow-dependency" span, and the /error handler marks its span as errored.
It writes structured JSON logs that include the current request's trace_id.
Uvicorn's own logs are disabled; instead every request is logged once with a
level chosen from the HTTP status code (2xx/3xx -> INFO, 4xx -> WARNING,
5xx -> ERROR).
"""
import logging
import os
import time

import httpx
import uvicorn
from fastapi import APIRouter, FastAPI, Response
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.routing import APIRoute
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from pythonjsonlogger import jsonlogger

# --- Telemetry ---------------------------------------------------------------
# Tag all signals with a service name, then export traces via OTLP.
# OTEL_EXPORTER_OTLP_ENDPOINT points at Jaeger (default: its gRPC port 4317).
resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "lab-service")})
tracer_provider = TracerProvider(resource=resource)
otlp_exporter = OTLPSpanExporter(
    endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
    insecure=True,
)
tracer_provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer(__name__)  # used for the manual / nested spans

# --- Structured (JSON) logging ------------------------------------------------
class TraceIdFilter(logging.Filter):
    """Attach the current OpenTelemetry trace_id to every log record."""

    def filter(self, record):
        span = trace.get_current_span()
        context = span.get_span_context()
        record.trace_id = trace.format_trace_id(context.trace_id) if context.is_valid else None
        return True


class JsonFormatter(jsonlogger.JsonFormatter):
    """Render logs as one JSON object per line: level, message, trace_id, ..."""

    def add_fields(self, log_record, record, message_dict):
        super().add_fields(log_record, record, message_dict)
        log_record["level"] = record.levelname
        log_record["message"] = record.getMessage()
        log_record["logger"] = record.name


handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter(fmt="%(level)s %(message)s %(trace_id)s %(logger)s"))
handler.addFilter(TraceIdFilter())

# Everything we emit goes through the JSON handler on the root logger.
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [handler]

# Disable Uvicorn's own loggers (access + startup). We emit our own request log
# from the `log_requests` middleware, with the level derived from the HTTP
# status code, so Uvicorn's plain-text logs are dropped entirely.
for _name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
    _lg = logging.getLogger(_name)
    _lg.handlers = []
    _lg.propagate = False
    _lg.setLevel(logging.CRITICAL)

log = logging.getLogger("lab.service")   # application events
access_log = logging.getLogger("lab.http")  # one line per HTTP request


def status_to_level(status: int) -> int:
    """Pick a log level from the HTTP status code.

    2xx/3xx -> INFO, 4xx -> WARNING, 5xx -> ERROR.
    """
    if status >= 500:
        return logging.ERROR
    if status >= 400:
        return logging.WARNING
    return logging.INFO

# --- Prometheus RED metrics ---------------------------------------------------
# RED = Rate, Errors, Duration. We expose ONLY these service metrics: instead of
# using the default REGISTRY (which also carries python_gc_*, python_info,
# process_* ...), we put them in their own registry so /metrics stays clean.
RED_REGISTRY = CollectorRegistry()

REQUEST_COUNTER = Counter(
    "http_requests_total",
    "Total HTTP requests (all endpoints and statuses combined).",
    registry=RED_REGISTRY,
)
ERROR_COUNTER = Counter(
    "http_errors_total",
    "Total HTTP 5xx responses (all endpoints combined).",
    registry=RED_REGISTRY,
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request handling time in seconds (RED: latency).",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=RED_REGISTRY,
)

# --- The app ------------------------------------------------------------------
# Swagger UI at "/" (instead of /docs); openapi_tags groups the endpoints.
app = FastAPI(
    title="Lab Service",
    docs_url=None,
    openapi_tags=[
        {"name": "Testing", "description": "Endpoints that trigger monitoring situations (error, delay, load)."},
        {"name": "Metrics", "description": "Prometheus metrics and health probe."},
    ],
)

# Request logging. Registered BEFORE the OTel instrumentation below so it sits
# inside the request's server span and can pick up the current trace_id.
@app.middleware("http")
async def log_requests(request, call_next):
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        access_log.error(
            "%s %s -> unhandled exception (%.1fms)",
            request.method, request.url.path,
            (time.perf_counter() - start) * 1000,
        )
        raise
    access_log.log(
        status_to_level(response.status_code),
        "%s %s -> %d (%.1fms)",
        request.method, request.url.path, response.status_code,
        (time.perf_counter() - start) * 1000,
    )
    return response


# OpenTelemetry auto-instrumentation: root span per request, plus trace the
# self-requests fired by the /load endpoint.
# /metrics and /health are excluded: Prometheus scrapes and health probes are
# infrastructure traffic and would only flood Jaeger with useless spans.
FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")
HTTPXClientInstrumentor().instrument()


class InstrumentedRoute(APIRoute):
    """APIRoute that records the RED metrics around its handler.

    Instrumentation is opt-in: only routes registered on a router that uses
    this route_class are counted. Service routes such as /health and /metrics
    stay on the plain app, so probe and scrape traffic never pollutes the
    RED metrics — no path denylist needed.
    """

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            start = time.perf_counter()
            response = await original(request)

            # One single counter series (no endpoint/status label):
            # http_requests_total IS the total. Errors are tracked separately by
            # ERROR_COUNTER, so nothing is double-counted.
            REQUEST_COUNTER.inc()
            REQUEST_DURATION.observe(time.perf_counter() - start)
            if response.status_code >= 500:
                ERROR_COUNTER.inc()
            return response

        return handler


# Business endpoints live on this router and are the only ones measured.
business = APIRouter(route_class=InstrumentedRoute)


@business.get("/", include_in_schema=False)
def home():
    # Serve the Swagger UI at "/" ourselves (docs_url=None), so the page is a
    # regular instrumented business route: loading it counts in the RED metrics.
    return get_swagger_ui_html(openapi_url=app.openapi_url, title=f"{app.title} - Swagger UI")


@app.get("/health", tags=["Metrics"])
def health():
    return {"status": "ok"}


@business.get("/error", tags=["Testing"])
def cause_error():
    # Mark the current (auto-created root) span as errored so Jaeger highlights it.
    span = trace.get_current_span()
    span.set_status(Status(StatusCode.ERROR, description="requested error"))
    log.error("a 500 was requested on /error")
    return Response(status_code=500, content='{"error": "boom"}')

@business.get("/delay", tags=["Testing"])
def cause_delay():
    # Wrap the slow operation in a nested span so the trace waterfall shows
    # where the time went (the "slow-dependency" span).
    with tracer.start_as_current_span("slow-dependency"):
        log.info("sleeping on /delay")
        time.sleep(1 + (time.time() % 3))  # 1-3 seconds
    return {"message": "that took a while"}


@business.get("/load", tags=["Testing"])
def load():
    """Fire a burst of requests at ourselves to spike request rate (RPS)."""
    burst = 50
    log.info("firing a burst of %d requests at /", burst)
    with tracer.start_as_current_span("load-burst"):
        # trust_env=False connects directly, ignoring any HTTP(S)_PROXY env var
        # (a machine-wide proxy would otherwise turn every burst into 503s).
        with httpx.Client(
            base_url=os.environ.get("SELF_BASE_URL", "http://localhost:8000"),
            trust_env=False,
        ) as client:
            for i in range(burst):
                client.get("/")
    return {"message": f"fired {burst} requests"}


app.include_router(business)


@app.get("/metrics", tags=["Metrics"])
def metrics():
    # Render only our RED metrics (not python_gc_*, python_info, process_*),
    # and strip the *_created timestamp series. We filter the text output here
    # rather than relying on a prometheus_client private kwarg (_use_created) or
    # newer helper (disable_created_metrics), so this works on any version.
    raw = generate_latest(RED_REGISTRY).decode()
    payload = "\n".join(
        line for line in raw.splitlines() if "_created" not in line
    ).encode()
    return Response(content=payload, media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    # Pass the in-memory app (not the "main:app" string) so the module isn't
    # imported twice; log_config=None keeps our JSON logging setup intact.
    uvicorn.run(app, host="0.0.0.0", port=8000, log_config=None, access_log=False)