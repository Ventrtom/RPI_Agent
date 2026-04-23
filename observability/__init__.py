from dataclasses import dataclass

from observability.feedback import FeedbackRecorder
from observability.reader import ObservabilityReader
from observability.snapshots import SessionSnapshotManager
from observability.telemetry import TelemetryLogger, _current_session_id

__all__ = [
    "TelemetryLogger",
    "FeedbackRecorder",
    "SessionSnapshotManager",
    "ObservabilityReader",
    "ObservabilityBundle",
    "_current_session_id",
]


@dataclass
class ObservabilityBundle:
    telemetry: TelemetryLogger
    feedback: FeedbackRecorder
    snapshots: SessionSnapshotManager
    reader: ObservabilityReader
