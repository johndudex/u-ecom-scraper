"""Wave-52 candidate (6): writer-connection retry stretch.

Z.AI TTFB on the writer's ~25K-token prefill spikes past ~60s during load
windows; a middlebox resets connections after ~60s of silence, so each writer
LLM call dies at ~61s (jobs 514/515/517/518/519 — five identical
"code_writer failed twice without producing a draft (Connection error.)"
deaths). The global transient ladder (2 attempts, 30s cap, ~2min exposure)
cannot ride out even a short burst. The code-writer alone gets a stretched
ladder — per-agent override, global defaults byte-identical (test-locked in
test_llm_provider.py).
"""

import unittest.mock

import django
from django.test import SimpleTestCase

django.setup()

from webapp.agents import llm as mod  # noqa: E402
from webapp.agents import subagents as sub  # noqa: E402

_LLM_SRC = open(mod.__file__, encoding="utf-8").read()


class TestCodewriterRetryOverrides(SimpleTestCase):
    def test_default_stretched_ladder(self):
        """Resolver returns the stretched writer ladder by default."""
        ov = mod._codewriter_retry_overrides()
        assert ov["transient_max"] == 4
        assert ov["backoff_base"] == 15.0
        assert ov["backoff_cap"] == 90.0

    def test_settings_override_respected(self):
        """Every knob is env/config-tunable without a code change."""
        with self.settings(LLM_RETRY_CODEWRITER_TRANSIENT_MAX=7):
            assert mod._codewriter_retry_overrides()["transient_max"] == 7
        with self.settings(LLM_RETRY_CODEWRITER_BACKOFF_CAP=45.0):
            assert mod._codewriter_retry_overrides()["backoff_cap"] == 45.0

    def test_global_defaults_untouched(self):
        """The global transient ladder stays at its test-locked values."""
        cfg = mod._retry_settings()
        assert cfg["transient_max"] == 2
        assert cfg["backoff_base"] == 1.5
        assert cfg["backoff_cap"] == 30.0


class TestBackoffMathWithWriterCfg(SimpleTestCase):
    def test_writer_cap_bounds_sleep(self):
        """The ceiling passed to uniform is min(cap, base·2^n) — with the
        writer ladder the 90s cap binds from n=3 onward."""
        cfg = {**mod._retry_settings(), **mod._codewriter_retry_overrides()}
        seen = []
        with unittest.mock.patch(
            "random.uniform", side_effect=lambda a, b: (seen.append(b), 0.0)[1]
        ):
            mod._backoff_delay(0, cfg, "transient")
            mod._backoff_delay(2, cfg, "transient")
            mod._backoff_delay(4, cfg, "transient")
        # n=0: min(90, 15)=15; n=2: min(90, 60)=60; n=4: min(90, 240)→90
        assert seen == [15.0, 60.0, 90.0]

    def test_writer_exposure_fits_wall(self):
        """Worst-case sleeps must leave the bulk of the 900s
        AGENT_INVOKE_TIMEOUT wall for the react loop's other calls."""
        cfg = {**mod._retry_settings(), **mod._codewriter_retry_overrides()}
        worst_sleeps = sum(
            min(cfg["backoff_cap"], cfg["backoff_base"] * (2**n))
            for n in range(cfg["transient_max"])
        )
        # transient_max=4 → 5 calls, 4 sleeps: 15+30+60+90 = 195s worst
        assert worst_sleeps <= 200, worst_sleeps


class TestClassifiedRetryOverrideWiring(SimpleTestCase):
    def _instance(self):
        return mod.ClassifiedRetryChatOpenAI(
            max_retries=0,
            openai_api_base="http://x.local/v1",
            openai_api_key="k",
            model="m",
        )

    def test_override_merges_over_global(self):
        """An instance with _retry_cfg_override gets the stretched budget; the
        global settings stay untouched for every other caller."""
        inst = self._instance()
        assert mod._effective_retry_cfg(inst)["transient_max"] == 2  # no override
        inst._retry_cfg_override = mod._codewriter_retry_overrides()
        merged = mod._effective_retry_cfg(inst)
        assert merged["transient_max"] == 4
        assert merged["backoff_cap"] == 90.0
        # non-overridden keys pass through
        assert merged["ratelimit_max"] == mod._retry_settings()["ratelimit_max"]

    def test_all_four_lanes_read_the_override(self):
        """_generate/_agenerate/_stream/_astream must all merge — a lane that
        still reads the flat _retry_settings() silently keeps the short
        ladder (the job-84 streaming-retry lesson)."""
        lanes = ["_generate", "_agenerate", "_stream", "_astream"]
        starts = sorted(_LLM_SRC.index(f"def {lane}(self") for lane in lanes)
        bounds = starts + [len(_LLM_SRC)]
        for lane, lo, hi in zip(lanes, bounds, bounds[1:]):
            body = _LLM_SRC[lo:hi]
            assert "_effective_retry_cfg(self)" in body, (
                f"{lane} does not merge the override"
            )

    def test_get_llm_passes_retry_cfg(self):
        """get_llm(retry_cfg=...) sets the private attr; default leaves None."""
        llm = mod.get_llm(model="glm-5.3-flash", retry_cfg={"transient_max": 4})
        assert getattr(llm, "_retry_cfg_override", None) == {"transient_max": 4}
        llm2 = mod.get_llm(model="glm-5.3-flash")
        assert getattr(llm2, "_retry_cfg_override", None) is None


class TestSubagentsWiring(SimpleTestCase):
    def test_writer_build_wires_override(self):
        """_build_agent passes the stretched ladder for the code-writer stem
        (the first cut; kept as the anchor case)."""
        captured = []
        real_get_llm = sub.get_llm

        def _spy_get_llm(**kwargs):
            captured.append(kwargs)
            return real_get_llm(**kwargs)  # real client: the build needs a Runnable

        with unittest.mock.patch.object(sub, "get_llm", side_effect=_spy_get_llm):
            sub.create_code_writer(
                site_slug="x-com", template_code="x = 1",
                embed_mode="full", embed_kind="file",
            )
        assert captured, "code-writer build made no get_llm call"
        assert all(
            c.get("retry_cfg") and c["retry_cfg"].get("transient_max") == 4
            for c in captured
        ), f"writer call lost the stretch: {captured}"

    def test_every_react_agent_gets_the_stretch(self):
        """[job-521 widening] The same ~61s window deaths hit product_analyzer
        and url_judge during a 25-min Z.AI window — EVERY stem in
        AGENT_PROMPT_MAP must resolve to the stretched ladder."""
        overrides = sub._agent_retry_overrides()
        stems = set(sub.AGENT_PROMPT_MAP.values())
        assert set(overrides) == stems, (set(overrides), stems)
        for stem, cfg in overrides.items():
            assert cfg["transient_max"] == 4, stem
            assert cfg["backoff_cap"] == 90.0, stem
        # cleanup is the regression case from the writer-only first cut
        assert overrides["cleanup"]["transient_max"] == 4

    def test_override_map_values_are_independent(self):
        """Each stem gets its own dict — mutating one must not bleed into
        another (shared-literal class of bug)."""
        overrides = sub._agent_retry_overrides()
        a, b = overrides["cleanup"], overrides["code-writer"]
        assert a is not b
        a["transient_max"] = 99
        assert overrides["code-writer"]["transient_max"] == 4
