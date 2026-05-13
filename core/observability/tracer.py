"""core/observability/tracer.py — CDP tracing (placeholder seguro).

FASE 2: placeholder mínimo con context manager.

FASE 3 conectará al `_trace_auth.AuthTracer` real preservando la API:
    with TraceSession(ctx, page, label="auth") as trace:
        ...  # listeners CDP activos
    # __exit__ → detach + exports garantizado

Por ahora context manager NO-OP — no toca CDP ni crea recursos.
"""


class TraceSession:
    """Placeholder seguro de tracing CDP.

    Context manager NO-OP que permite escribir código futuro como:

        with TraceSession() as trace:
            ...

    sin que el placeholder cause efectos secundarios.
    """

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        pass
