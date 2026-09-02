# Postmortem 001: Redis Outage Causes Total API Failure

**Status:** Draft — root cause identified, fix not yet implemented.
**Date of incident:** 2026-09-02
**Severity:** Critical (100% of traffic affected during the outage window)
**Type:** Deliberate failure injection (planned exercise, not a production incident)

---

## Summary

A deliberately injected Redis outage caused **every** API request to fail with an HTTP 500, instead of degrading gracefully. The rate limiter's dependency on Redis has no fault handling: a Redis connection failure raises an unhandled exception before a request ever reaches its route handler, so the cache-aside fallback to Postgres (designed to survive exactly this kind of failure) never gets a chance to run. The `HighErrorRate` alert correctly caught the incident in 22 seconds, but the alert specifically built to watch Redis health, `RedisOperationSlow`, never fired — it is a latency-based signal and cannot detect a hard connection failure, which produces no completed operations to measure.

## Impact

- 100% of requests to `GET /{short_code}` and `POST /api/urls` returned HTTP 500 for the duration of the outage (both routes depend on the rate-limiter dependency, which touches Redis first).
- Failures were fast (under 2ms per request) rather than slow — the connection attempt fails immediately rather than timing out, so the user-facing symptom is an instant error, not a hang.
- No data was lost or corrupted. `GET /api/urls/{short_code}/stats` was not exercised during the outage and its exposure to this bug was not directly confirmed in this exercise.

## Timeline (all times UTC)

| Time | Event |
|---|---|
| 14:22:12 | Redis container stopped (`docker stop ous-redis`) — incident start, injected deliberately |
| ~14:22:15 onward | Every request against `GET /{short_code}` begins returning HTTP 500 in under 2ms |
| 14:22:34 | `HighErrorRate` alert transitions to Firing — **22 seconds from injection to detection** |
| ~14:24–14:25 | Structured logs confirm the failure signature: `redis.exceptions.ConnectionError: Error 61 connecting to localhost:6379. Connection refused.`, originating in the rate-limiter's Redis call |
| ~14:33–14:40 | Redis container restarted (`docker start ous-redis`); exact restart timestamp not captured — approximate window only |
| 14:40:31 | First confirmed successful request post-recovery: clean HTTP 302 |
| Shortly after 14:40:31 | `HighErrorRate` alert returns to Inactive, confirming resolution |

## Root Cause

The rate-limiter dependency (`app/api/dependencies.py`, the `_dependency` function used by FastAPI's `Depends`) calls `RateLimiter.check()`, which executes the atomic Lua script against Redis (`app/core/rate_limiter.py:203`, `await self._script(...)`). When Redis is unreachable, this call raises `redis.exceptions.ConnectionError` directly. The exception is not caught anywhere in the dependency chain, so FastAPI's dependency resolution fails before the route handler body ever executes — meaning the cache-aside layer's fallback-to-Postgres behavior, which exists specifically to tolerate a degraded or unavailable cache, is never reached. The rate limiter has no fault tolerance for "Redis is completely unreachable," only an implicit assumption that Redis calls succeed.

## Detection

`HighErrorRate` (`sum(rate(http_request_duration_seconds_count{status=~"5.."}[5m])) > 0`, `for: 1m`) fired 22 seconds after injection — fast, and the correct alert to catch this specific failure, since it is scoped to observable client-facing symptoms rather than any one dependency.

However, `RedisOperationSlow`, the alert designed specifically to monitor Redis health (p95 latency on `redis_operation_duration_seconds`), never fired during the incident — confirmed `0 active` throughout. Direct query of `rate(redis_operation_duration_seconds_count[1m])` during the outage returned `0` across all operations (`token_bucket`, `get`, `set`). This is because the histogram only records a sample when a Redis call *completes* — successfully or with a returned error. A connection-level failure (`ConnectionError` raised while establishing the connection) never reaches the point in the code where the timer records a sample, so the metric is structurally blind to this failure mode. The incident was caught by a generic HTTP-layer alert, not by the Redis-specific instrumentation built for this exact purpose.

## Resolution

Redis was restarted manually (`docker start ous-redis`). No application code was changed to recover — the fix was purely restarting the failed dependency. Recovery was confirmed by a successful `GET /{short_code}` request returning a clean 302, followed by `HighErrorRate` returning to Inactive on its next evaluation cycle.

**No code fix has been implemented yet.** This postmortem documents the incident as observed against the current, unmodified code.

## Action Items

1. **Add fault handling to the rate limiter for Redis connection failures.** Decide and implement a fail-open vs. fail-closed policy: fail-open (allow the request through, log a warning, skip rate limiting) prioritizes availability; fail-closed (return a controlled 503) prioritizes protecting against abuse but leaves the API unavailable during a Redis outage, which is not meaningfully better than the current behavior. Leaning toward fail-open with a logged warning, since losing rate-limiting protection for the (hopefully rare, hopefully short) duration of a Redis outage is a more acceptable tradeoff than a full API outage — final decision pending.
2. **Close the alerting blind spot for Redis reachability.** `RedisOperationSlow` cannot detect a hard connection failure because it only measures completed operations. Consider either: (a) a dedicated Redis reachability check (e.g. `up{job="redis"}` via a redis_exporter, or the app's own `/ready` endpoint result exposed as a metric), or (b) recording a Redis-operation *attempt* counter (incremented before the call, regardless of outcome) alongside the existing duration histogram, so failed attempts are visible even when they never complete.
3. **Verify the fix with a repeat of this exact failure injection** once item 1 is implemented — stop Redis again under load, and confirm requests degrade gracefully (fail open or return a controlled 503, per whichever policy is chosen) instead of 500ing.
4. **Confirm whether `GET /api/urls/{short_code}/stats` shares this failure mode** — it was not exercised during this incident and its dependency on the rate limiter/Redis was not directly tested.
