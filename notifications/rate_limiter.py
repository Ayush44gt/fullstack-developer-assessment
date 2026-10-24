"""Sliding-window-log rate limiter on a Redis sorted set (DESIGN.md, "Rate limiter").

Each admitted send is one member of a sorted set, scored by its admission time
in ms. Admitting a send means: drop members older than the window, count what
is left, add one if under the limit. Those steps run inside one Lua script.
Redis runs a script to completion without interleaving any other client's
commands, so two workers can never both see 199 and both add the 200th.
"""

import uuid
from dataclasses import dataclass

import redis

# KEYS[1]  sorted set of admitted sends (score = admission time, ms)
# ARGV[1]  window length, ms
# ARGV[2]  limit that applies to this caller (bulk callers get a lower one)
# ARGV[3]  unique member id for this send
# ARGV[4]  "now" in ms; empty string = use the Redis server clock (tests pin it)
SLIDING_WINDOW_LUA = """
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now
if ARGV[4] == '' then
  local t = redis.call('TIME')
  now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
else
  now = tonumber(ARGV[4])
end

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, 0, count + 1}
end

-- Denied. A slot opens for this caller when the (count - limit + 1)-th oldest
-- entry leaves the window, i.e. at its score + window.
local entry = redis.call('ZRANGE', KEYS[1], count - limit, count - limit, 'WITHSCORES')
return {0, tonumber(entry[2]) + window - now, count}
"""


class RateLimiterUnavailable(Exception):
    """Redis could not be reached; callers must fail closed (not send)."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_ms: int  # 0 when allowed
    in_window: int  # admitted sends currently in the window


class SlidingWindowRateLimiter:
    def __init__(self, client, *, key, limit, window_ms):
        self.key = key
        self.limit = limit
        self.window_ms = window_ms
        # EVALSHA; on NOSCRIPT redis-py runs SCRIPT LOAD and retries the EVALSHA
        self._script = client.register_script(SLIDING_WINDOW_LUA)

    def try_acquire(self, *, limit=None, now_ms=None):
        """Take one slot if available. Never blocks and never sleeps."""
        args = [self.window_ms, limit or self.limit, uuid.uuid4().hex, "" if now_ms is None else now_ms]
        try:
            allowed, retry_after_ms, in_window = self._script(keys=[self.key], args=args)
        except redis.RedisError as exc:
            raise RateLimiterUnavailable(str(exc)) from exc
        return Decision(bool(allowed), int(retry_after_ms), int(in_window))
