from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator


class _NullSpan:
    def set_attribute(self, *_args, **_kwargs) -> None:
        return None

    def record_exception(self, *_args, **_kwargs) -> None:
        return None

    def set_status(self, *_args, **_kwargs) -> None:
        return None


_TRACE_CONFIGURED = False


def configure_tracing(force: bool = False, exporter: Any | None = None) -> bool:
    global _TRACE_CONFIGURED
    if _TRACE_CONFIGURED and not force:
        return True

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception:
        return False

    resource = Resource.create(
        {"service.name": os.getenv("OTEL_SERVICE_NAME", "partyhat-agents")}
    )
    provider = TracerProvider(resource=resource)

    if exporter is None:
        endpoint = (
            os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        )
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=endpoint)
            except Exception:
                exporter = None

    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))

    try:
        trace.set_tracer_provider(provider)
    except Exception:
        pass

    _TRACE_CONFIGURED = True
    return True


def get_tracer(name: str = "partyhat"):
    try:
        from opentelemetry import trace
    except Exception:
        return None

    if not _TRACE_CONFIGURED:
        configure_tracing()
    return trace.get_tracer(name)


@contextmanager
def start_span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
    tracer = get_tracer()
    if tracer is None:
        yield _NullSpan()
        return

    with tracer.start_as_current_span(name) as span:
        for key, value in (attributes or {}).items():
            if value is None:
                continue
            if isinstance(value, (bool, int, float, str)):
                span.set_attribute(key, value)
            else:
                span.set_attribute(key, str(value))
        yield span


def current_trace_id() -> str | None:
    try:
        from opentelemetry import trace
    except Exception:
        return None

    span = trace.get_current_span()
    if span is None:
        return None
    context = span.get_span_context()
    if not getattr(context, "trace_id", 0):
        return None
    return f"{context.trace_id:032x}"
