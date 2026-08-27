# Architecture

> Stub — expanded in later phases. This document will hold the detailed
> architecture write-up (request flow diagrams, data model rationale, caching
> and rate-limiting design, and observability wiring) for interview review.

## Overview

A production-style, observable, rate-limited URL shortener.

```
Client --HTTP--> FastAPI app --> Redis (cache + rate limit)
                            --> PostgreSQL (urls, click_events)
FastAPI also exposes GET /metrics (Prometheus format) and emits
structured JSON logs to stdout.
```

## Redirect hot path (`GET /{short_code}`)

1. Rate-limit check (Redis token bucket) → `429` if exceeded.
2. Redis cache lookup `short_code -> long_url` → hit: log click, `302`.
3. Miss: query Postgres, populate cache with TTL, log click, `302`.
4. Every branch increments the relevant Prometheus metric.

_Detailed sections (schema, indexing choices, Lua rate limiter, metrics, alerts)
are added as each phase lands._
