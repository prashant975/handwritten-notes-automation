"""Router tests: health caching, task/mode routing, fallback, safety net.
Standalone: .venv\\Scripts\\python.exe tests\\test_model_router.py

Config under test (src/model_router.load_routing_config defaults):
  notes  = (3.6-flash-high, 3.5-flash-high, 2.5-flash)
  vision = (3.5-flash-high, 3.1-pro-preview, 3.6-flash-high)
  qc     = (3.6-flash-high, 3.5-flash-high, 2.5-flash)
  + safety net (2.5-flash, 2.5-pro) auto-appended to every task chain.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import model_router as mr  # noqa: E402

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


# The A/B primaries + the fast fallback that appear in the configured chains.
CONFIGURED = {"gemini-3.6-flash", "gemini-3.5-flash",
              "gemini-3.1-pro-preview", "gemini-2.5-flash"}
SAFETY = {"gemini-2.5-flash", "gemini-2.5-pro"}
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
    ok("balanced notes = 3.6-flash-high", d.notes.model == "gemini-3.6-flash", d.notes.model)
    ok("balanced vision = 3.5-flash-high", d.vision.model == "gemini-3.5-flash", d.vision.model)
    ok("balanced qc = 3.6-flash-high", d.qc and d.qc.model == "gemini-3.6-flash")
    ok("balanced pro NOT used", d.pro_used is False)
    ok("balanced no fallback", d.fallback_used is False)
    ok("balanced qc level basic", d.profile.qc_level == "basic")


def test_fast_routing():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    d = mr.resolve(mr.FAST, c, h)
    ok("fast notes = 3.6-flash-high", d.notes.model == "gemini-3.6-flash", d.notes.model)
    ok("fast vision = 3.6-flash-high", d.vision.model == "gemini-3.6-flash", d.vision.model)
    ok("fast qc = OFF", d.qc is None)
    ok("fast vision default OFF", d.profile.vision_default_on is False)
    ok("fast no redraw", d.profile.redraw_diagrams is False)
    ok("fast pro NOT used", d.pro_used is False)


def test_high_quality_routing():
    mr.clear_health_cache()
    c = cfg()
    h = mr.check_models(c.all_models(), make_probe(ALLUP), c)
    d = mr.resolve(mr.HIGH_QUALITY, c, h)
    ok("hq notes = 3.1-pro-preview", d.notes.model == "gemini-3.1-pro-preview", d.notes.model)
    ok("hq vision = 3.1-pro-preview", d.vision.model == "gemini-3.1-pro-preview", d.vision.model)
    ok("hq pro IS used", d.pro_used is True)
    ok("hq redraw ON", d.profile.redraw_diagrams is True)
    ok("hq qc strict", d.profile.qc_level == "strict")
    ok("hq has warning", bool(d.profile.warn))


def test_fallback_when_primary_down():
    mr.clear_health_cache()
    c = cfg()
    # 3.6-flash-high DOWN; balanced notes must fall to the next available.
    avail = (ALLUP - {"gemini-3.6-flash"})
    h = mr.check_models(c.all_models(), make_probe(avail), c)
    d = mr.resolve(mr.BALANCED, c, h)
    ok("fallback: notes not the dead primary", d.notes.model != "gemini-3.6-flash")
    ok("fallback: notes = 3.5-flash-high", d.notes.model == "gemini-3.5-flash", d.notes.model)
    ok("fallback: flagged", d.notes.fallback_used is True)
    ok("fallback: reason logged", "unavailable" in d.notes.reason.lower())
    ok("fallback: decision fallback_used", d.fallback_used is True)


def test_falls_to_25flash_when_3x_down():
    mr.clear_health_cache()
    c = cfg()
    # All 3.x models down (the current proxy reality) — only 2.5 works.
    h = mr.check_models(c.all_models(), make_probe({"gemini-2.5-flash"}), c)
    d = mr.resolve(mr.BALANCED, c, h)
    ok("safety: notes = 2.5-flash", d.notes.model == "gemini-2.5-flash", d.notes.model)
    ok("safety: generation still resolvable", d.notes.available is True)
    ok("safety: pro NOT used (cheap path)", d.pro_used is False)


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
    d = mr.resolve(mr.BALANCED, c, h, manual={"notes": "gemini-3.1-pro-preview"})
    ok("manual: notes overridden to pro", d.notes.model == "gemini-3.1-pro-preview")
    ok("manual: pro_used reflects override", d.pro_used is True)


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
    # Everything up, but the resolved balanced notes+vision models are very slow.
    lat = {m: 60 for m in ALLUP}
    lat["gemini-3.6-flash"] = 20000   # balanced notes
    lat["gemini-3.5-flash"] = 20000   # balanced vision
    h = _health(c, lat)
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: slow responses -> Fast", mode == mr.FAST, f"{mode}: {reason}")
    ok("recommend: fast reason mentions slow", "slow" in reason.lower())


def test_recommend_only_25_balanced():
    c = cfg()
    h = _health(c, {"gemini-2.5-flash"}, latency_ms=90)   # only 2.5-flash up
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: only 2.5 up -> Balanced", mode == mr.BALANCED, mode)
    ok("recommend: notes resolves to 2.5-flash",
       mr._select_task("notes", mr.BALANCED, c, h).model == "gemini-2.5-flash")


def test_recommend_nothing_up_reports_outage():
    c = cfg()
    h = _health(c, set())
    mode, reason = mr.recommend_mode(c, h)
    ok("recommend: nothing up -> Balanced (outage)", mode == mr.BALANCED, mode)
    ok("recommend: outage reason", "outage" in reason.lower() or "no notes" in reason.lower())


def main():
    for fn in [test_health_cache, test_balanced_routing, test_fast_routing,
               test_high_quality_routing, test_fallback_when_primary_down,
               test_falls_to_25flash_when_3x_down, test_all_down_raises,
               test_classify_never_pro, test_manual_override,
               test_recommend_all_healthy_balanced, test_recommend_slow_gives_fast,
               test_recommend_only_25_balanced, test_recommend_nothing_up_reports_outage]:
        fn()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {FAILS}")
        return 1
    print("ALL ROUTER TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
