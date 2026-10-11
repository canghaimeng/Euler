"""Read counters from a launcher-selected native JSON protocol.

This is provider-reported accounting, not a price estimate or cryptographic
attestation. Plain text, nested model/tool text, absent counters and incomplete
streams never become measured usage. Raw protocol artifacts remain available.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import hashlib
import json
from pathlib import Path
from typing import Any


FORMATS = {"codex": "codex-json", "grok": "grok-streaming-json"}
COUNTERS = ("input_tokens", "output_tokens", "reasoning_tokens", "cached_tokens")
GROK_TURN_COMPLETE = frozenset({"EndTurn", "end_turn", "stop"})
# 1 USD = 10^10 ticks; 1 microusd = 10^6 USD^-1, so 10^4 ticks per microusd.
_TICKS_PER_MICROUSD = Decimal("10000")
CONVENTIONS = {
    "codex": (
        "codex-json: input_tokens as exported; cached_input_tokens separate; "
        "do not add cache into input"
    ),
    "grok": (
        "grok-streaming-json: uncached input_tokens; cache_read_input_tokens "
        "separate; do not add cache into input"
    ),
}


@dataclass(frozen=True)
class NativeUsage:
    provider: str
    output_format: str
    source_sha256: str
    complete: bool
    counters: dict[str, int | None]
    cost_microusd: int | None
    record_count: int
    input_convention: str

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "provider": self.provider,
                "output_format": self.output_format, "source_sha256": self.source_sha256,
                "complete": self.complete, "counters": dict(self.counters),
                "cost_microusd": self.cost_microusd, "record_count": self.record_count,
                "input_convention": self.input_convention}


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _cost(record: dict[str, Any]) -> int | None:
    if "cost_microusd" in record:
        return _integer(record["cost_microusd"])
    value = record.get("cost_usd")
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            return None
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _sum_known(values: list[int | None]) -> int | None:
    return sum(values) if values and all(x is not None for x in values) else None


def microusd_from_cost_usd(value: Any) -> int | None:
    """Convert a provider cost_usd figure to integer micro-USD. Never infers 0."""
    return _cost({"cost_usd": value})


def microusd_from_ticks(ticks: Any) -> int | None:
    """Convert Grok total_cost_usd_ticks (1 USD = 10^10 ticks) to micro-USD.

    Fractional micro-USD rounds toward +inf so a reported spend is never
    understated. This is still provider-reported, not a billed invoice.
    """
    if isinstance(ticks, bool) or not isinstance(ticks, int) or ticks < 0:
        return None
    try:
        return int(
            (Decimal(ticks) / _TICKS_PER_MICROUSD).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _end_cost(event: dict[str, Any]) -> int | None:
    if event.get("cost_is_partial") is True or event.get("usage_is_incomplete") is True:
        return None
    ticks = microusd_from_ticks(event.get("total_cost_usd_ticks"))
    if ticks is not None:
        return ticks
    return _cost({"cost_usd": event.get("total_cost_usd")})


def _counters_from_records(provider: str, records: list[dict[str, Any]]) -> dict[str, int | None]:
    aliases = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "reasoning_tokens": (
            "reasoning_output_tokens" if provider == "codex" else "reasoning_tokens"
        ),
        "cached_tokens": (
            "cached_input_tokens" if provider == "codex" else "cache_read_input_tokens"
        ),
    }
    return {
        name: _sum_known([_integer(record.get(field)) for record in records])
        for name, field in aliases.items()
    }


def parse_native_usage(data: bytes, *, provider: str, output_format: str) -> NativeUsage:
    """Parse only the explicitly selected provider protocol, never infer it.

    Input counters retain the native exporter's convention. This avoids silently
    adding cache counters that can already be included in input tokens. Matched
    evaluations must use the same exporter/convention, or an explicit converter.
    """
    unknown = {key: None for key in COUNTERS}
    digest = hashlib.sha256(data).hexdigest()
    convention = CONVENTIONS.get(
        provider, "provider-reported; cached tokens retained separately"
    )
    if FORMATS.get(provider) != output_format:
        return NativeUsage(provider, output_format, digest, False, unknown, None, 0, convention)
    records = []
    complete = False
    malformed = False
    seen_signed = set()
    end_usage: dict[str, Any] | None = None
    end_cost: int | None = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            # Diagnostic stderr is not accounting. Native usage frames still
            # need a complete final protocol event before they can be settled.
            continue
        if not isinstance(event, dict):
            continue
        if provider == "codex":
            if event.get("type") == "turn.failed":
                malformed = True
            if event.get("type") != "turn.completed":
                continue
            complete = True
            record = event.get("usage")
        else:
            if event.get("type") == "end":
                complete = event.get("stopReason") in GROK_TURN_COMPLETE
                if event.get("usage_is_incomplete") is True:
                    malformed = True
                if isinstance(event.get("usage"), dict):
                    end_usage = event["usage"]
                end_cost = _end_cost(event)
                continue
            if event.get("type") == "error":
                malformed = True
            if event.get("type") != "usage":
                continue
            record = event.get("usage")
            # Retransmission of the exact same signed frame must not double
            # count. No authenticity claim is made without signature checking.
            signature = event.get("signature")
            if isinstance(signature, str) and signature:
                identity = (signature, json.dumps(record, sort_keys=True))
                if identity in seen_signed:
                    continue
                seen_signed.add(identity)
        if not isinstance(record, dict):
            malformed = True
            continue
        records.append(record)
    token_records = [end_usage] if end_usage is not None else records
    complete = bool(complete and token_records and not malformed)
    if not complete:
        return NativeUsage(
            provider, output_format, digest, False, unknown, None, len(records), convention
        )
    counters = _counters_from_records(provider, token_records)
    cost = end_cost if end_cost is not None else _sum_known([_cost(item) for item in records])
    return NativeUsage(
        provider,
        output_format,
        digest,
        True,
        counters,
        cost,
        len(token_records),
        convention,
    )


def read_native_usage(path: Path, *, provider: str, output_format: str) -> NativeUsage:
    return parse_native_usage(path.read_bytes(), provider=provider, output_format=output_format)
