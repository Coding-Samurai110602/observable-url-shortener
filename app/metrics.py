"""Prometheus metric definitions — one module-level object per metric.

Defining every metric here (imported wherever needed) prevents the
``prometheus_client`` duplicate-registration error that would arise if any
metric were instantiated more than once in the same process.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram

# Buckets tuned for a web service targeting sub-100 ms p99 on redirects and
# sub-500 ms on writes. The 10 s upper bucket captures catastrophic outliers
# without inflating the histogram's memory footprint.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 10.0)

# End-to-end latency per HTTP request. The (route, method, status) triple is the
# minimal label set to pinpoint which endpoint is slow or error-prone without
# blowing up cardinality with per-path-param variants (e.g. every unique short
# code becoming its own label value).
http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "End-to-end HTTP request latency, from first byte received to response sent.",
    labelnames=["route", "method", "status"],
    buckets=_DURATION_BUCKETS,
)

# Separate counters (not a single counter with a hit/miss label) so a cache
# hit-rate alert can be written as hit/(hit+miss) in a single PromQL expression
# without needing label filtering on both sides of the fraction.
cache_hit_total = Counter(
    "cache_hit_total",
    "Short-code lookups served from Redis (no Postgres round-trip incurred).",
    labelnames=["route"],
)

cache_miss_total = Counter(
    "cache_miss_total",
    "Short-code lookups that fell through to Postgres because the cache was cold or absent.",
    labelnames=["route"],
)

# A spike here signals traffic is saturating a token bucket — useful for
# capacity planning and detecting amplification attacks before they show up in
# overall latency.
rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total",
    "Requests rejected by the token-bucket rate limiter, by route class.",
    labelnames=["route_class"],
)

# Three statuses because 'not_found' and 'expired' warrant separate alert
# thresholds: not_found links are client-side broken links; expired spikes may
# mean a campaign's TTL was misconfigured and short codes aged out early.
redirect_total = Counter(
    "redirect_total",
    "Redirect attempts by outcome: success, not_found, or expired.",
    labelnames=["status"],
)

# Per-query-type histogram so you can identify which query shape (create vs.
# fetch vs. stats aggregation) is driving DB tail latency, without APM tooling.
db_query_duration_seconds = Histogram(
    "db_query_duration_seconds",
    "Latency of individual database operations, labeled by query type.",
    labelnames=["query_type"],
    buckets=_DURATION_BUCKETS,
)

# Redis operation histogram per operation so cache GET latency is visible
# separately from SET and the rate-limiter's atomic Lua EVAL. A degrading Redis
# cluster typically shows p99 drift here before overall HTTP tail latency moves.
# NOTE: this histogram only records completed operations — it is structurally
# blind to hard connection failures (ConnectionError), which never reach the
# observation point. Use redis_fallback_total for that signal instead.
redis_operation_duration_seconds = Histogram(
    "redis_operation_duration_seconds",
    "Latency of individual Redis operations, labeled by operation type.",
    labelnames=["operation"],
    buckets=_DURATION_BUCKETS,
)

# Incremented every time the app degrades gracefully because Redis was unreachable —
# the one signal redis_operation_duration_seconds cannot provide, since it only
# records operations that complete. A non-zero rate here means Redis is down and
# the app is running without rate limiting or caching (still serving traffic).
redis_fallback_total = Counter(
    "redis_fallback_total",
    "Redis operations that fell back to fail-open/fail-safe because Redis was unreachable.",
    labelnames=["component", "operation"],
)
