"""Model availability + task-based routing for Gemini calls.

Goal (per product spec): NEVER fan a task out across several models. Instead —
  1. health-check the candidate models ONCE (tiny prompt, cached 10-15 min),
  2. for each task pick the single best AVAILABLE model given the speed mode
     and cost rules,
  3. use only that model; a runtime failure falls to the next available model
     and is logged, but generation never probes several models up front.

The three models the product asks for are configured via env (see
load_routing_config). They may not exist on every proxy, so a real known-good
safety net (gemini-2.5-flash / gemini-2.5-pro) is always appended to each task
chain — a mis-typed or unreleased model ID can never hard-fail generation, and
the health panel shows exactly which IDs actually responded.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

# --------------------------------------------------------------------------
# Speed modes
# --------------------------------------------------------------------------
FAST = "fast"
BALANCED = "balanced"
HIGH_QUALITY = "high_quality"
MODES = (FAST, BALANCED, HIGH_QUALITY)
MODE_LABELS = {FAST: "Fast Mode", BALANCED: "Balanced Mode", HIGH_QUALITY: "High Quality Mode"}
DEFAULT_MODE = BALANCED

# "auto" is a meta-mode: it is NOT a real profile — the app health-checks the
# models and recommend_mode() maps it to one of the real modes above at run time.
AUTO = "auto"
AUTO_LABEL = "Auto (Recommended)"

# Known-good models that ALWAYS work on this proxy — appended to every task
# chain so a bad configured ID never leaves a task with nothing to run.
SAFETY_NET_FLASH = "gemini-2.5-flash"
SAFETY_NET_PRO = "gemini-2.5-pro"

# Cost tiers + display labels for models we know about. Unknown models get a
# heuristic tier from their name (pro=high, flash=low).
_KNOWN = {
    "gemini-3.5-flash": ("low", "Gemini 3.5 Flash"),
    "gemini-3.6-flash": ("low", "Gemini 3.6 Flash"),
    "gemini-3.1-pro-preview": ("high", "Gemini 3.1 Pro Preview"),
    "gemini-2.5-flash": ("low", "Gemini 2.5 Flash"),
    "gemini-2.5-pro": ("high", "Gemini 2.5 Pro"),
    "gemini-2.5-flash-image": ("medium", "Gemini 2.5 Flash Image"),
}


def cost_tier(model: str) -> str:
    if model in _KNOWN:
        return _KNOWN[model][0]
    m = model.lower()
    if "pro" in m:
        return "high"
    if "flash" in m:
        return "low"
    return "medium"


def model_label(model: str) -> str:
    return _KNOWN.get(model, (None, model))[1]


def is_pro(model: str) -> bool:
    return "pro" in (model or "").lower()


# --------------------------------------------------------------------------
# Routing config (env-driven — matches the .env names in the spec)
# --------------------------------------------------------------------------
def _env(name: str, default: str) -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class RoutingConfig:
    notes: tuple[str, ...]
    vision: tuple[str, ...]
    qc: tuple[str, ...]
    classify: tuple[str, ...]
    health_cache_minutes: int
    fallback_enabled: bool
    use_pro_only_when_needed: bool

    def all_models(self) -> list[str]:
        seen: list[str] = []
        for chain in (self.notes, self.vision, self.qc, self.classify):
            for m in chain:
                if m and m not in seen:
                    seen.append(m)
        return seen


def load_routing_config() -> RoutingConfig:
    # The three A/B models are the primaries — gemini-3.6-flash and gemini-3.5-flash
    # (fast/cheap) and gemini-3.1-pro-preview (Pro, used only for High Quality vision).
    # NOTE: use the BARE ids — the earlier gemini-3.x-flash-HIGH names 404 on the
    # proxy; the real IDs have no "-high" suffix (reasoning is a separate param,
    # thinkingConfig.thinkingLevel). They are health-gated, and gemini-2.5-flash is
    # wired as the fast fallback for the high-volume tasks (notes, qc); gemini-2.5-flash
    # / gemini-2.5-pro are always appended as the final safety net.
    notes = (
        _env("NOTES_MODEL_PRIMARY", "gemini-3.5-flash"),
        _env("NOTES_MODEL_FALLBACK_1", "gemini-3.6-flash"),
        _env("NOTES_MODEL_FALLBACK_2", "gemini-2.5-flash"),
    )
    vision = (
        _env("VISION_MODEL_PRIMARY", "gemini-3.5-flash"),
        _env("VISION_MODEL_FALLBACK_1", "gemini-3.6-flash"),
        _env("VISION_MODEL_FALLBACK_2", "gemini-2.5-flash"),
    )
    qc = (
        _env("QC_MODEL_PRIMARY", "gemini-3.5-flash"),
        _env("QC_MODEL_FALLBACK_1", "gemini-3.6-flash"),
        _env("QC_MODEL_FALLBACK_2", "gemini-2.5-flash"),
    )
    classify = (notes[0], SAFETY_NET_FLASH)  # cheapest flash; never the Pro model
    try:
        cache_min = int(os.getenv("MODEL_HEALTH_CACHE_MINUTES", "15"))
    except ValueError:
        cache_min = 15
    cache_min = max(1, min(120, cache_min))
    return RoutingConfig(
        notes=notes, vision=vision, qc=qc, classify=classify,
        health_cache_minutes=cache_min,
        fallback_enabled=os.getenv("MODEL_FALLBACK_ENABLED", "true").lower() != "false",
        use_pro_only_when_needed=os.getenv("USE_PRO_ONLY_WHEN_NEEDED", "true").lower() != "false",
    )


# Per-mode PRIMARY choice for each task (spec section 3 tables). The task's own
# fallback chain + the safety net are appended after this primary.
def _mode_primaries(cfg: RoutingConfig) -> dict:
    notes_p = cfg.notes[0]
    vision_p = cfg.vision[0]
    qc_p = cfg.qc[0]
    return {
        FAST:         {"notes": notes_p, "vision": notes_p, "qc": None},
        BALANCED:     {"notes": notes_p, "vision": vision_p, "qc": qc_p},
        # High Quality uses the SAME fast notes/vision model (gemini-3.5-flash),
        # which reads dense handwritten equations reliably WITHOUT the Pro model's
        # empty-response behaviour, and layers on strict equation checks, on-demand
        # equation OCR, and diagram redraw for quality (see MODE_PROFILES).
        HIGH_QUALITY: {"notes": notes_p, "vision": vision_p, "qc": qc_p},
    }


@dataclass(frozen=True)
class ModeProfile:
    """Non-model behaviour per speed mode."""
    vision_default_on: bool
    redraw_diagrams: bool
    qc_level: str          # "off" | "basic" | "strict"
    warn: str = ""


MODE_PROFILES = {
    FAST: ModeProfile(vision_default_on=False, redraw_diagrams=False, qc_level="off"),
    BALANCED: ModeProfile(vision_default_on=True, redraw_diagrams=False, qc_level="basic"),
    HIGH_QUALITY: ModeProfile(
        vision_default_on=True, redraw_diagrams=True, qc_level="strict",
        warn="High Quality Mode: notes/vision on Gemini 3.5 Flash + strict equation "
             "checks + on-demand equation OCR + diagram redraw. Full quality without "
             "the Pro model's empty-response risk.",
    ),
}


# --------------------------------------------------------------------------
# Health check (cached)
# --------------------------------------------------------------------------
@dataclass
class ModelHealth:
    model: str
    available: bool
    latency_ms: int | None = None
    error: str | None = None
    cost_tier: str = "medium"
    recommended_for: list[str] = field(default_factory=list)
    label: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model": self.model, "available": self.available,
            "latency_ms": self.latency_ms, "error": self.error,
            "cost_tier": self.cost_tier, "recommended_for": self.recommended_for,
        }


_recommended_for = {
    "notes": {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"},
    "vision": {"gemini-3.5-flash", "gemini-3.1-pro-preview", "gemini-3.6-flash"},
    "qc": {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash"},
}


def _roles(model: str) -> list[str]:
    return [role for role, members in _recommended_for.items() if model in members]


# Probe = a callable(model) -> latency_seconds, raising on failure. Injected so
# tests can stub it and the app supplies one backed by GeminiClient.
Probe = Callable[[str], float]

_cache: dict[str, tuple[ModelHealth, float]] = {}
_cache_lock = threading.Lock()


def clear_health_cache() -> None:
    with _cache_lock:
        _cache.clear()


def peek_health(models: list[str]) -> dict[str, ModelHealth]:
    """Return only the CACHED, non-expired health for `models` — never probes.
    Used by the frontend panel so a page rerun makes zero API calls."""
    now = time.time()
    out: dict[str, ModelHealth] = {}
    for m in models:
        hit = _cached(m, now)
        if hit is not None:
            out[m] = hit
    return out


def cache_age_seconds() -> float | None:
    """Seconds since the most recent health probe, or None if nothing cached."""
    with _cache_lock:
        if not _cache:
            return None
        newest = max(h.checked_at for h, _ in _cache.values())
    return max(0.0, time.time() - newest)


def _cached(model: str, now: float) -> ModelHealth | None:
    with _cache_lock:
        hit = _cache.get(model)
    if hit and now < hit[1]:
        return hit[0]
    return None


def _store(model: str, health: ModelHealth, ttl: float) -> None:
    with _cache_lock:
        _cache[model] = (health, time.time() + ttl)


def _probe_one(model: str, probe: Probe) -> ModelHealth:
    tier = cost_tier(model)
    label = model_label(model)
    roles = _roles(model)
    start = time.monotonic()
    try:
        probe(model)
        latency = int((time.monotonic() - start) * 1000)
        return ModelHealth(model, True, latency, None, tier, roles, label, time.time())
    except Exception as e:
        return ModelHealth(model, False, None, str(e)[:200], tier, roles, label, time.time())


def check_models(models: list[str], probe: Probe, cfg: RoutingConfig, *,
                 force: bool = False, max_workers: int = 4) -> dict[str, ModelHealth]:
    """Health-check `models`, reusing cached results within the TTL. Only the
    stale/uncached models are probed, concurrently."""
    ttl = cfg.health_cache_minutes * 60
    now = time.time()
    result: dict[str, ModelHealth] = {}
    to_probe: list[str] = []
    for m in models:
        hit = None if force else _cached(m, now)
        if hit is not None:
            result[m] = hit
        else:
            to_probe.append(m)
    if to_probe:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(to_probe))) as ex:
            for m, health in zip(to_probe, ex.map(lambda mm: _probe_one(mm, probe), to_probe)):
                _store(m, health, ttl)
                result[m] = health
    return result


def make_probe(google_token, *, timeout: float = 20.0) -> Probe:
    """A real probe backed by the proxy: a tiny generateContent per model.

    A health probe only answers "does this model exist and respond?", so ANY
    non-error HTTP response counts as available — INCLUDING a
    finishReason=MAX_TOKENS / empty-text response. This matters because Gemini
    2.5+ are *thinking* models: given a small token budget they can spend it all
    on internal reasoning and return no visible text. The app's normal
    empty-response retry (correct for real note generation) would wrongly mark
    such a healthy model 'unavailable' and knock out the safety net, so the
    probe deliberately bypasses that path and calls the proxy directly. Only a
    real transport/HTTP error (404 model-not-found, 401/403 auth, 5xx, timeout)
    means unavailable.
    """
    body = {
        "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
        "generationConfig": {"maxOutputTokens": 256, "temperature": 0.0},
    }

    def probe(model: str) -> float:
        import pw_access  # lazy: bundled top-level module

        start = time.monotonic()
        # Any successful return (even empty/MAX_TOKENS) => the model is serving.
        pw_access.gemini_generate(google_token, model=model, request=body, timeout=timeout)
        return time.monotonic() - start
    return probe


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------
class RouterError(RuntimeError):
    """No model is available for a required task."""


@dataclass
class TaskSelection:
    task: str
    model: str | None
    available: bool
    is_primary: bool
    fallback_used: bool
    reason: str


@dataclass
class RoutingDecision:
    mode: str
    profile: ModeProfile
    notes: TaskSelection
    vision: TaskSelection
    qc: TaskSelection | None
    classify: TaskSelection
    pro_used: bool
    fallback_used: bool
    health: dict[str, ModelHealth]

    def reasons(self) -> list[str]:
        out = []
        for sel in (self.notes, self.vision, self.qc, self.classify):
            if sel and sel.fallback_used and sel.model:
                out.append(sel.reason)
        return out

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "notes_model": self.notes.model,
            "vision_model": self.vision.model,
            "qc_model": self.qc.model if self.qc else None,
            "pro_used": self.pro_used,
            "fallback_used": self.fallback_used,
            "reasons": self.reasons(),
        }


def _effective_chain(task: str, mode: str, cfg: RoutingConfig) -> list[str]:
    """[mode primary] + [task fallback chain] + [safety net], de-duplicated."""
    primaries = _mode_primaries(cfg)[mode]
    task_chain = {"notes": cfg.notes, "vision": cfg.vision, "qc": cfg.qc, "classify": cfg.classify}[task]
    safety = [SAFETY_NET_FLASH, SAFETY_NET_PRO]
    chain: list[str] = []
    for m in [primaries.get(task)] + list(task_chain) + safety:
        if m and m not in chain:
            chain.append(m)
    return chain


def _select_task(task: str, mode: str, cfg: RoutingConfig,
                 health: dict[str, ModelHealth], override: str | None = None) -> TaskSelection:
    if override:
        h = health.get(override)
        ok = bool(h and h.available)
        return TaskSelection(task, override if ok else None, ok, True, False,
                             f"Manual selection: {override}" + ("" if ok else " (UNAVAILABLE)"))
    chain = _effective_chain(task, mode, cfg)
    primary = chain[0] if chain else None
    for m in chain:
        h = health.get(m)
        if h and h.available:
            fb = m != primary
            reason = (f"Used {model_label(m)} because {model_label(primary)} was unavailable."
                      if fb else f"Selected {model_label(m)} ({cost_tier(m)} cost) for {task}.")
            return TaskSelection(task, m, True, not fb, fb, reason)
    return TaskSelection(task, None, False, False, False,
                         f"No available model for {task} (tried: {', '.join(chain)}).")


def resolve(mode: str, cfg: RoutingConfig, health: dict[str, ModelHealth], *,
            manual: dict[str, str] | None = None) -> RoutingDecision:
    """Pick one model per task, health-gated. `manual` maps task->model to
    override auto routing (still health-checked)."""
    mode = mode if mode in MODES else DEFAULT_MODE
    manual = manual or {}
    profile = MODE_PROFILES[mode]

    notes = _select_task("notes", mode, cfg, health, manual.get("notes"))
    vision = _select_task("vision", mode, cfg, health, manual.get("vision"))
    classify = _select_task("classify", mode, cfg, health, manual.get("classify"))
    qc = None
    if profile.qc_level != "off":
        qc = _select_task("qc", mode, cfg, health, manual.get("qc"))

    if not notes.available:
        raise RouterError(
            "No Gemini model is available for note generation right now. "
            "Click 'Refresh Model Availability'; if it persists, the proxy or "
            "Vertex may be down. " + (notes.reason or "")
        )

    used = [s for s in (notes, vision, qc, classify) if s and s.model]
    pro_used = any(is_pro(s.model) for s in used)
    fallback_used = any(s.fallback_used for s in used)
    return RoutingDecision(mode, profile, notes, vision, qc, classify,
                           pro_used, fallback_used, health)


def available_chain(task: str, mode: str, cfg: RoutingConfig,
                    health: dict[str, ModelHealth]) -> list[str]:
    """Health-available models for a task, in fallback order (primary first).

    resolve() picks the FIRST of these; the rest are the RUNTIME fallback list.
    If the chosen model returns an empty/failed response mid-generation (e.g. a
    Pro model exhausting its output budget on internal thinking over an
    equation-dense slide), the pipeline retries that chunk on the next model here
    — typically a flash model that won't over-think — so a single stubborn deck
    can't fail the whole run."""
    mode = mode if mode in MODES else DEFAULT_MODE
    out: list[str] = []
    for m in _effective_chain(task, mode, cfg):
        h = health.get(m)
        if h and h.available and m not in out:
            out.append(m)
    return out


# --------------------------------------------------------------------------
# Auto mode selection — pick the best speed mode for current availability
# --------------------------------------------------------------------------
# Latency (ms) above which a health probe counts as "slow" — a sign the proxy
# or the Vertex region is congested. When responses are slow, Auto drops to Fast
# Mode so a run makes fewer AI calls and finishes reliably. Kept well above a
# healthy thinking-model probe (Gemini 2.5+ can take ~10s just to answer a tiny
# ping), so normal latency doesn't get mistaken for a degraded proxy.
_SLOW_PROBE_MS = 16000


def _is_slow(h: ModelHealth | None) -> bool:
    return bool(h and h.available and h.latency_ms is not None and h.latency_ms > _SLOW_PROBE_MS)


def recommend_mode(cfg: RoutingConfig, health: dict[str, ModelHealth]) -> tuple[str, str]:
    """Pick the best PROCESSING MODE for the current model availability.

    Returns ``(mode, human_reason)``. Auto chooses between Balanced (premium /
    flash models are healthy and responsive) and Fast (degraded: no vision model
    or slow responses) — it NEVER auto-selects High Quality, because that forces
    the expensive Pro model and must stay an explicit opt-in (spec: use Pro only
    when needed). If nothing responds it returns Balanced so ``resolve`` raises a
    single, clear outage error.
    """
    notes = _select_task("notes", BALANCED, cfg, health)
    if not notes.available:
        return BALANCED, "No notes model responded; generation will report the outage."

    vision = _select_task("vision", BALANCED, cfg, health)
    notes_h = health.get(notes.model)
    vision_h = health.get(vision.model) if vision.model else None

    degraded: list[str] = []
    if not vision.available:
        degraded.append("no vision model is responding")
    if _is_slow(notes_h) or _is_slow(vision_h):
        degraded.append("model responses are slow right now")
    if degraded:
        return FAST, ("Fast Mode — " + " and ".join(degraded)
                      + " (fewer AI calls = quicker and more stable).")

    return BALANCED, (f"Balanced Mode — {model_label(notes.model)} is available and responsive.")


def status_rows(cfg: RoutingConfig, health: dict[str, ModelHealth]) -> list[dict]:
    """Rows for the frontend Model Availability table (configured models only)."""
    rows = []
    for m in cfg.all_models():
        h = health.get(m)
        rows.append({
            "model": model_label(m),
            "id": m,
            "status": "Available" if (h and h.available) else ("Unavailable" if h else "Unknown"),
            "latency": f"{h.latency_ms} ms" if (h and h.latency_ms is not None) else "—",
            "cost": cost_tier(m).capitalize(),
            "used_for": ", ".join(_roles(m)) or "—",
            "error": (h.error if h else None),
        })
    return rows
