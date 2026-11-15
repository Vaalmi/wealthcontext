"""Local OpenTelemetry JSONL export. Durable audit records live in SQLite."""

import json
import threading
from collections.abc import Sequence
from pathlib import Path

from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult


class JsonlExporter(SpanExporter):
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        try:
            with self.lock, self.path.open("a", encoding="utf-8") as stream:
                for span in spans:
                    # One JSON object per line; export no bearer tokens or source payloads.
                    stream.write(json.dumps(json.loads(span.to_json()), separators=(",", ":")))
                    stream.write("\n")
            return SpanExportResult.SUCCESS
        except OSError:
            return SpanExportResult.FAILURE


def create_provider(path: Path) -> TracerProvider:
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(JsonlExporter(path)))
    return provider
