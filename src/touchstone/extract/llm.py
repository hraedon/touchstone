"""Model-assisted spec extraction, for titles the regex cannot parse confidently.

This is the *only* place a model touches touchstone, and it touches an enrichment,
never the truth path. Observed prices, timestamps, and presence/absence are recorded
from the API exactly as returned. What a model proposes here is a reading of a
free-text title, stored with its method and confidence and correctable by hand.

Three rules hold this boundary:

1. **Never inline in a scan.** This runs from its own entry point on its own
   schedule. A scan completes and stores observations whether or not the provider is
   reachable; a model outage costs spec coverage, nothing else.
2. **Never trusted without a range check.** ``plausible()`` is applied to model
   output exactly as it is to regex output. The characteristic model failure is
   reading a speed as a capacity ("PC4-3200" becoming 3200GB), which arithmetic
   catches and hope does not.
3. **Never a ``-lab`` model.** Those tiers are not reliable enough to depend on, and
   the rule is enforced at construction rather than written in a comment where a
   later environment change can quietly ignore it.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from touchstone.extract.specs import SpecCandidate, plausible

log = logging.getLogger("touchstone.extract.llm")

DEFAULT_BASE_URL = "https://api.code.umans.ai/v1"
DEFAULT_MODEL = "umans-flash"
FALLBACK_MODEL = "umans-deepseek-v4-flash-0731"

# Confidence assigned to an accepted model reading. Below a hand correction, above a
# weak regex parse, and deliberately at the deal floor rather than above it — a model
# reading is good enough to cohort on, and only just good enough to spend money on.
LLM_CONFIDENCE = 0.8

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

PROMPT = """\
Extract memory module specifications from this eBay listing title.

Return ONLY a JSON object with these keys (use null when the title does not say):
  capacity_per_module_gb  integer, capacity of ONE module in GB
  module_count            integer, how many modules are included
  total_gb                integer, capacity_per_module_gb * module_count
  ddr_gen                 "DDR3" | "DDR4" | "DDR5" | null
  speed_mt                integer, MT/s (PC4-19200 and PC4-2400 both mean 2400)
  form_factor             "RDIMM" | "LRDIMM" | "UDIMM" | "SODIMM" | null
  rank_org                e.g. "2Rx4", or null
  ecc                     true | false | null
  registered              true | false | null

Rules:
- "Lot of 4 x 32GB" means module_count 4, capacity_per_module_gb 32, total_gb 128.
- "2Rx4" is a rank code, NOT a multiplier.
- A speed (2133, 2400, 2666, 3200) is never a capacity.
- If the title contradicts itself, return null for the capacity fields.

Title: {title}
"""


class ModelRejected(ValueError):
    """The configured model is not permitted."""


def validate_model(model: str) -> str:
    """Reject unreliable tiers. Enforced, not documented.

    A ``-lab`` model can be selected by a single environment variable, so the check
    lives where the value enters rather than in a comment someone has to read.
    """
    if model.endswith("-lab"):
        raise ModelRejected(
            f"model {model!r} is a -lab tier and is not permitted for extraction; "
            f"use {DEFAULT_MODEL} or {FALLBACK_MODEL}"
        )
    return model


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text).strip()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _as_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def parse_model_output(raw: str) -> SpecCandidate | None:
    """Turn a model response into a candidate, or None if it is unusable.

    Returns None rather than a partially-trusted candidate: a reading that fails the
    range check is not evidence of anything, and storing it would put a confident
    wrong answer where a known gap belongs.
    """
    try:
        payload = json.loads(_strip_fences(raw))
    except (json.JSONDecodeError, ValueError):
        log.warning("model returned unparseable output")
        return None
    if not isinstance(payload, dict):
        return None

    candidate = SpecCandidate(
        capacity_per_module_gb=_as_int(payload.get("capacity_per_module_gb")),
        module_count=_as_int(payload.get("module_count")),
        total_gb=_as_int(payload.get("total_gb")),
        ddr_gen=_as_str(payload.get("ddr_gen")),
        speed_mt=_as_int(payload.get("speed_mt")),
        form_factor=_as_str(payload.get("form_factor")),
        rank_org=_as_str(payload.get("rank_org")),
        ecc=_as_bool(payload.get("ecc")),
        registered=_as_bool(payload.get("registered")),
        confidence=LLM_CONFIDENCE,
        notes="llm",
    )

    if candidate.total_gb is None:
        # The model declined to commit, which the prompt explicitly permits for a
        # self-contradicting title. That is a useful answer, but not a specced one.
        return None
    if not plausible(candidate):
        log.warning("model output failed the range check; discarding")
        return None
    return candidate


@dataclass
class UmansExtractor:
    """Spec extraction via an OpenAI-compatible chat completion."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    fallback_model: str | None = FALLBACK_MODEL
    timeout: float = 60.0
    max_attempts: int = 3
    http: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.model = validate_model(self.model)
        if self.fallback_model is not None:
            self.fallback_model = validate_model(self.fallback_model)
        self.http = httpx.Client(timeout=self.timeout)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> UmansExtractor:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _complete(self, model: str, title: str) -> str | None:
        resp = self.http.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": PROMPT.format(title=title)}],
                "temperature": 0,
            },
        )
        if resp.status_code != 200:
            log.warning("umans %s returned %s", model, resp.status_code)
            return None
        choices = resp.json().get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content")
        return str(content) if content else None

    def extract(self, title: str) -> SpecCandidate | None:
        """Best-effort model extraction. None on any failure.

        Retries across attempts and then the fallback model. umans fails
        transiently at any tier, so a failure is a reason to try again, never a
        reason to record a model as bad — the next call frequently succeeds.
        """
        models = [self.model] + ([self.fallback_model] if self.fallback_model else [])
        for model in models:
            for attempt in range(self.max_attempts):
                try:
                    raw = self._complete(model, title)
                except httpx.HTTPError as exc:
                    log.warning("umans %s transport error: %s", model, exc)
                    raw = None
                if raw is not None:
                    candidate = parse_model_output(raw)
                    if candidate is not None:
                        return candidate
                if attempt + 1 < self.max_attempts:
                    time.sleep(min(2**attempt, 8))
        log.warning("extraction failed for a title after all attempts")
        return None
