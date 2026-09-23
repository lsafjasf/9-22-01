"""Causally ordered event stamping and multi-source merging (stdlib only)."""

from eventorder.clock import HybridClock, is_stamped, now_ms, order_key
from eventorder.merge import external_merge, merge, merge_streams

__all__ = [
    "HybridClock",
    "external_merge",
    "is_stamped",
    "merge",
    "merge_streams",
    "now_ms",
    "order_key",
]
