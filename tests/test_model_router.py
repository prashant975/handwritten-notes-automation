"""Router tests: health caching, task/mode routing, fallback, safety net.
Standalone: .venv\\Scripts\\python.exe tests\\test_model_router.py

Config under test (src/model_router.load_routing_config defaults):
  notes  = (3.5-flash, 3.6-flash)      # FALLBACK_2 empty; 2.5-flash removed
  vision = (3.5-flash, 3.6-flash)
  qc     = (3.5-flash, 3.6-flash)
  + safety net (3.6-flash flash backstop, 2.5-pro dormant) appended to each chain.
  All modes (incl. High Quality) make notes/vision on gemini-3.5-flash.
  gemini-2.5-flash is used NOWHERE; 2.5-pro is never health-probed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import model_router as mr  # noqa: E402

# Availability probing is OFF by default (it cost a billed call per model per
# run). The tests below exercise probe/fallback semantics, so they opt in;
# test_health_probing_is_off_by_default_and_costs_nothing covers the default.
os.environ["MODEL_HEALTH_CHECK"] = "true"

FAILS: list[str] = []


def ok(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  <- {detail}"))
    if not cond:
        FAILS.append(name)


def cfg():
    return mr.load_routing_config()


def make_probe(available: set[str], calls: dict | None = None):
    def probe(model: str) -> float:
        if calls is not None:
            calls[model] = calls.get(model, 0) + 1
        if model in available:
            return 0.01
        raise RuntimeError(f"vertex gemini error 404: model {model} not found")
    return probe


# The flash models that appear in the configured chains (no Pro, no 2.5-flash).
CONFIGURED = {"gemini-3.5-flash", "gemini-3.6-flash"}
# 3.6-flash is the flash backstop (already configured); 2.5-pro is the only extra
# safety-net id, and it is never health-probed (not in all_models()).
SAFETY = {"gemini-2.5-pro"}
ALLUP = CONFIGURED | SAFETY


def test_health_cache():
    mr.clear_health_cache()
    c = cfg()
    calls: dict = {}
    probe = make_probe(ALLUP, calls)
    h1 = mr.check_models(c.all_models(), probe, c)
    ok("health: all configured models probed", all(m in h1 for m in c.all_models()))
    ok("health: 3.6-flash-high available", h1["gemini-3.6-flash"].available)
    ok("health: latency recorded", h1["gemini-3.6-flash"].latency_ms is not None)
    n_first = dict(calls)
    mr.check_models(c.all_models(), probe, c)  # cached -> no new probes
    ok("health: cached (no re-probe)", calls == n_first, f"{calls} vs {n_first}")
    mr.check_models(c.all_models(), probe, c, force=True)
    ok("health: force re-probes", calls != n_first)


def test_balanced_routing():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    d = mr.resolve(mr.BALANCED, c, h)
    ok("balanced notes = 3.5-flash", d.notes.model == "gemini-3.5-flash", d.notes.model)
    ok("balanced vision = 3.5-flash", d.vision.model == "gemini-3.5-flash", d.vision.model)
    ok("balanced qc = 3.5-flash", d.qc and d.qc.model == "gemini-3.5-flash")
    ok("balanced pro NOT used", d.pro_used is False)
    ok("balanced no fallback", d.fallback_used is False)
    ok("balanced qc level basic", d.profile.qc_level == "basic")


def test_fast_routing():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    d = mr.resolve(mr.FAST, c, h)
    ok("fast notes = 3.5-flash", d.notes.model == "gemini-3.5-flash", d.notes.model)
    ok("fast vision = 3.5-flash", d.vision.model == "gemini-3.5-flash", d.vision.model)
    ok("fast qc = OFF", d.qc is None)
    ok("fast vision default OFF", d.profile.vision_default_on is False)
    ok("fast no redraw", d.profile.redraw_diagrams is False)
    ok("fast pro NOT used", d.pro_used is False)


def test_high_quality_routing():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    d = mr.resolve(mr.HIGH_QUALITY, c, h)
    ok("hq notes = 3.5-flash", d.notes.model == "gemini-3.5-flash", d.notes.model)
    ok("hq vision = 3.5-flash", d.vision.model == "gemini-3.5-flash", d.vision.model)
    ok("hq pro NOT used", d.pro_used is False)
    ok("hq redraw ON", d.profile.redraw_diagrams is True)
    ok("hq qc strict", d.profile.qc_level == "strict")
    ok("hq has warning", bool(d.profile.warn))


def test_fallback_when_primary_down():
    mr.clear_health_cache()
    c = cfg()
    # 3.5-flash (primary) DOWN; balanced notes must fall to the next available.
    avail = (ALLUP - {"gemini-3.5-flash"})
    h = mr.check_models(c.all_models(), make_probe(avail), c)
    d = mr.resolve(mr.BALANCED, c, h)
    ok("fallback: notes not the dead primary", d.notes.model != "gemini-3.5-flash")
    ok("fallback: notes = 3.6-flash", d.notes.model == "gemini-3.6-flash", d.notes.model)
    ok("fallback: flagged", d.notes.fallback_used is True)
    ok("fallback: reason logged", "unavailable" in d.notes.reason.lower())
    ok("fallback: decision fallback_used", d.fallback_used is True)


def test_both_flash_down_raises():
    mr.clear_health_cache()
    c = cfg()
    # Both flash models down. 2.5-flash was removed and 2.5-pro is dormant
    # (never probed), so there is no rescue — generation must report an outage
    # rather than silently fall back to a removed/empty-prone model.
    h = mr.check_models(c.all_models(), make_probe(set()), c)
    try:
        mr.resolve(mr.BALANCED, c, h)
        ok("both-flash-down raises RouterError", False, "no exception")
    except mr.RouterError:
        ok("both-flash-down raises RouterError", True)
    ok("no 2.5-flash anywhere in notes chain",
       "gemini-2.5-flash" not in mr._effective_chain("notes", mr.BALANCED, c),
       mr._effective_chain("notes", mr.BALANCED, c))


def test_all_down_raises():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(set()), c)  # nothing works
    try:
        mr.resolve(mr.BALANCED, c, h)
        ok("all-down raises RouterError", False, "no exception")
    except mr.RouterError:
        ok("all-down raises RouterError", True)


def test_classify_never_pro():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    d = mr.resolve(mr.BALANCED, c, h)
    ok("classify never uses pro", not mr.is_pro(d.classify.model), d.classify.model)


def test_manual_override():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    # Override notes to a configured, health-up model that isn't the primary.
    d = mr.resolve(mr.BALANCED, c, h, manual={"notes": "gemini-3.6-flash"})
    ok("manual: notes overridden", d.notes.model == "gemini-3.6-flash", d.notes.model)
    ok("manual: override marked primary", d.notes.is_primary is True)


def _health(cfg_, avail, latency_ms=50):
    """Build a health dict directly (no probe): models in `avail` are up.
    `avail` may be a set (uniform latency) or a dict model->latency_ms."""
    h = {}
    for m in cfg_.all_models():
        up = m in avail
        lat = (avail[m] if isinstance(avail, dict) else latency_ms) if up else None
        h[m] = mr.ModelHealth(m, up, lat, None if up else "down",
                              mr.cost_tier(m), [], mr.model_label(m), 0.0)
    return h


def test_recommend_all_healthy_balanced():
    c = cfg()
    h = _health(c, ALLUP, latency_ms=80)
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: all healthy -> Balanced", mode == mr.BALANCED, mode)
    ok("recommend: never auto High Quality", mode != mr.HIGH_QUALITY)
    ok("recommend: reason mentions Balanced", "Balanced" in reason)


def test_recommend_slow_gives_fast():
    c = cfg()
    # Everything up, but the resolved balanced notes+vision model is very slow.
    lat = {m: 60 for m in ALLUP}
    lat["gemini-3.5-flash"] = 20000   # balanced notes AND vision primary
    h = _health(c, lat)
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: slow responses -> Fast", mode == mr.FAST, f"{mode}: {reason}")
    ok("recommend: fast reason mentions slow", "slow" in reason.lower())


def test_recommend_only_36_balanced():
    c = cfg()
    h = _health(c, {"gemini-3.6-flash"}, latency_ms=90)   # only 3.6-flash up
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: only 3.6 up -> Balanced", mode == mr.BALANCED, mode)
    ok("recommend: notes resolves to 3.6-flash",
       mr._select_task("notes", mr.BALANCED, c, h).model == "gemini-3.6-flash")


def test_recommend_nothing_up_reports_outage():
    c = cfg()
    h = _health(c, set())
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: nothing up -> Balanced (outage)", mode == mr.BALANCED, mode)
    ok("recommend: outage reason", "outage" in reason.lower() or "no notes" in reason.lower())


def test_failed_health_is_not_cached():
    """An outage verdict must never suppress the next probe.

    A cold start probes every model concurrently while the 7-day session pass
    is still being minted, so a slow/failed mint fails them all at once. When
    that verdict was cached for the full 15 min TTL the app was stuck on "No
    Gemini model is available" on every click — a restart just hit the same
    race. Failures now expire immediately so the next attempt self-heals.
    """
    c = cfg()
    mr.clear_health_cache()
    calls = {}
    mr.check_models(c.all_models(), make_probe(set(), calls), c)   # everything down
    first = dict(calls)
    ok("failed probe recorded", all(v == 1 for v in first.values()), first)

    # Same call again, NO force: must genuinely re-probe, not replay the failure.
    mr.check_models(c.all_models(), make_probe(set(c.all_models()), calls), c)
    reprobed = {m: calls[m] - first.get(m, 0) for m in first}
    ok("failure did not suppress re-probe", all(v == 1 for v in reprobed.values()), reprobed)

    h = mr.peek_health(c.all_models())
    ok("recovered after re-probe", all(v.available for v in h.values() if v))


def test_successful_health_is_still_cached():
    """The negative-TTL fix must not disable caching for healthy models."""
    c = cfg()
    mr.clear_health_cache()
    calls = {}
    up = set(c.all_models())
    mr.check_models(c.all_models(), make_probe(up, calls), c)
    mr.check_models(c.all_models(), make_probe(up, calls), c)   # should hit cache
    ok("success cached (probed once, not twice)", all(v == 1 for v in calls.values()), calls)


def test_safety_nets_are_probed_not_just_appended():
    """Every model _effective_chain() may pick must appear in all_models().

    _select_task() accepts a model only if health has an entry for it. The
    safety nets were appended to each chain but never probed, so health.get()
    returned None and they could never rescue a run: with both flash primaries
    down the app raised "No available model for notes (tried: gemini-3.5-flash,
    gemini-3.6-flash, gemini-2.5-pro)" — naming a model it had never tested.
    """
    c = cfg()
    probed = set(c.all_models())
    for task in ("notes", "vision", "qc", "classify"):
        for mode in mr.MODES:
            for m in mr._effective_chain(task, mode, c):
                ok(f"safety net probed: {m} ({task}/{mode})", m in probed,
                   f"{m} is selectable but never health-probed")


def test_pro_safety_net_rescues_when_all_flash_is_down():
    """With every flash model down, the Pro safety net must carry the run."""
    c = cfg()
    h = _health(c, {mr.SAFETY_NET_PRO})
    d = mr.resolve(mr.BALANCED, c, h)
    ok("pro safety net selected for notes", d.notes.model == mr.SAFETY_NET_PRO, d.notes.model)
    ok("marked as a fallback", d.notes.fallback_used)
    ok("notes reported available", d.notes.available)


def test_health_probing_is_off_by_default_and_costs_nothing():
    """Availability probing must make ZERO billed API calls by default.

    Each probe was a real generateContent call per model — visible in the
    Usage Cost sheet as 1-token-input rows (~0.45 INR per cycle for three
    models), repeated on every app start and every 15-min cache expiry.
    """
    import os
    c = cfg()
    mr.clear_health_cache()
    os.environ.pop("MODEL_HEALTH_CHECK", None)

    def exploding(model):
        raise AssertionError(f"probe called for {model} — that is a billed request")

    h = mr.check_models(c.all_models(), exploding, c)
    ok("no probe calls by default", True)
    ok("all models reported available", all(v.available for v in h.values()))
    d = mr.resolve(mr.BALANCED, c, h)
    ok("routing still resolves a notes model", bool(d.notes.model), d.notes.model)
    ok("peek_health serves the cached assumption", len(mr.peek_health(c.all_models())) == len(h))

    # Opting back in must restore real probing.
    os.environ["MODEL_HEALTH_CHECK"] = "true"
    mr.clear_health_cache()
    calls = {}
    mr.check_models(c.all_models(), make_probe(set(c.all_models()), calls), c)
    ok("MODEL_HEALTH_CHECK=true restores probing", sum(calls.values()) > 0, calls)
    os.environ["MODEL_HEALTH_CHECK"] = "true"
    mr.clear_health_cache()


def main():
    for fn in [test_health_cache, test_balanced_routing, test_fast_routing,
               test_high_quality_routing, test_fallback_when_primary_down,
               test_both_flash_down_raises, test_all_down_raises,
               test_classify_never_pro, test_manual_override,
               test_recommend_all_healthy_balanced, test_recommend_slow_gives_fast,
               test_recommend_only_36_balanced, test_recommend_nothing_up_reports_outage,
               test_safety_nets_are_probed_not_just_appended,
               test_pro_safety_net_rescues_when_all_flash_is_down,
               test_failed_health_is_not_cached,
               test_successful_health_is_still_cached,
               test_health_probing_is_off_by_default_and_costs_nothing]:
        fn()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("ALL ROUTER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
