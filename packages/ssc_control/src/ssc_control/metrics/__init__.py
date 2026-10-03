"""Product metrics (SSC-028): pseudonymous events and the report that reads them.

``Metrics(keys).record_event(...)`` implements ``ports.MetricsPort``; the report is
``python -m ssc_control.metrics.report``.
"""

from ssc_control.metrics.events import Metrics, MetricsPropertyError, metrics_port, record_once
from ssc_control.metrics.pseudonym import (
    DerivedKeys,
    MetricsKeyError,
    PseudonymKeys,
    parse_master_key,
    pseudonym,
)
from ssc_control.metrics.source_tool import SOURCE_TOOL_HEADER, source_tool_of

__all__ = [
    "SOURCE_TOOL_HEADER",
    "DerivedKeys",
    "Metrics",
    "MetricsKeyError",
    "MetricsPropertyError",
    "PseudonymKeys",
    "metrics_port",
    "parse_master_key",
    "pseudonym",
    "record_once",
    "source_tool_of",
]
