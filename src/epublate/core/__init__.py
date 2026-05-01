"""Core pipeline modules. Concrete implementations land in M1+ per PRD §10."""

from epublate.core.batch import (
    BatchOptions,
    BatchPaused,
    BatchProgressEvent,
    BatchSummary,
    run_batch,
)
from epublate.core.pipeline import (
    TranslateOptions,
    TranslateOutcome,
    translate_segment,
)
from epublate.core.stats import (
    ALERT_KINDS,
    ProjectStats,
    compute_spend,
    compute_stats,
    flagged_segments,
    pending_segments,
    recent_alerts,
)
from epublate.core.validators import validate_segment_placeholders

__all__ = [
    "ALERT_KINDS",
    "BatchOptions",
    "BatchPaused",
    "BatchProgressEvent",
    "BatchSummary",
    "ProjectStats",
    "TranslateOptions",
    "TranslateOutcome",
    "compute_spend",
    "compute_stats",
    "flagged_segments",
    "pending_segments",
    "recent_alerts",
    "run_batch",
    "translate_segment",
    "validate_segment_placeholders",
]
