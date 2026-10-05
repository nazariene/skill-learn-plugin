def token_count(value):
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0 and value.is_integer():
        return int(value)
    return None


def normalize_usage(usage):
    """Keep provider fields; add common counters without inventing missing values."""
    if not isinstance(usage, dict):
        return None
    normalized = dict(usage)
    for target, source in (("input_tokens", "prompt_tokens"), ("output_tokens", "completion_tokens")):
        if target not in normalized and source in usage:
            normalized[target] = usage[source]
    details = usage.get("input_tokens_details")
    if not isinstance(details, dict):
        details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        for target, source in (("cache_read_tokens", "cached_tokens"), ("cache_write_tokens", "cache_write_tokens")):
            if target not in normalized and source in details:
                normalized[target] = details[source]
    return normalized


def call_usage(call):
    response = call.get("response") or {}
    usage = normalize_usage(response.get("usage")) or {}
    counters = {
        "input": token_count(usage.get("input_tokens")),
        "output": token_count(usage.get("output_tokens")),
        "reasoning": token_count(usage.get("reasoning_tokens")),
        "hit": token_count(usage.get("cache_read_tokens")),
        "write": token_count(usage.get("cache_write_tokens")),
        "miss": None,
    }
    if counters["input"] is not None and counters["hit"] is not None:
        counters["miss"] = max(0, counters["input"] - counters["hit"])
    return counters


def sum_usage(calls):
    keys = ("input", "output", "reasoning", "hit", "miss", "write")
    total = {key: 0 for key in keys}
    reported = {key: 0 for key in keys}
    for call in calls:
        for key, value in call_usage(call).items():
            if value is not None:
                total[key] += value
                reported[key] += 1
    total["known"] = {key: reported[key] > 0 for key in keys}
    total["partial"] = {key: 0 < reported[key] < len(calls) for key in keys}
    return total


def host_usage(tokens):
    """Normalize available host counters; original provider availability is lost."""
    tokens = tokens if isinstance(tokens, dict) else {}
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
    raw = {key: token_count(tokens.get(key)) for key in ("input", "output", "reasoning")}
    raw.update(read=token_count(cache.get("read")), write=token_count(cache.get("write")))
    total = sum(raw[key] for key in ("input", "read", "write")) if all(raw[key] is not None for key in ("input", "read", "write")) else None
    return {"input_tokens": total, "output_tokens": raw["output"], "reasoning_tokens": raw["reasoning"],
            "cache_read_tokens": raw["read"], "cache_write_tokens": raw["write"],
            "fidelity": "host_normalized", "normalization": "uncached-input-plus-cache-read-write",
            "host_counters": tokens, "raw_provider_usage": None, "provider_counter_availability": "unavailable"}
