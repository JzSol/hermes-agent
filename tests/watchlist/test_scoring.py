"""Golden vectors for the two independent watchlist scoring axes."""

from copy import deepcopy

from watchlist.config import DEFAULT_CONFIG, DEFAULT_SCORE_ANCHORS, load_config
from watchlist.render import render_shortlist
from watchlist.scoring import (
    DEEP,
    SCAN,
    UNKNOWN,
    coverage_eligible,
    is_rug_shape,
    score_asymmetry,
    score_risk,
)


def test_fully_known_low_risk_low_asymmetry_vector():
    risk = score_risk({
        "backer_overlap": 2,
        "audit": "audited_clean",
        "team_disclosure": "doxxed_history",
        "vesting_disclosure": "full",
        "raise_disclosed": "exact",
        "round_type_disclosed": "named",
    })
    asymmetry = score_asymmetry({
        "fdv": 500_000_000,
        "raise_size": 25_000_000,
        "float_at_tge": 30,
        "fdv_raise_ratio": 150,
    })
    assert risk.value == 100
    assert risk.coverage == 1
    assert asymmetry.value == 0
    assert asymmetry.coverage == 1


def test_fully_known_high_asymmetry_high_risk_vector_is_rug_shape():
    risk = score_risk({
        "backer_overlap": 0,
        "audit": "no_audit",
        "team_disclosure": "anonymous",
        "vesting_disclosure": "undisclosed",
        "raise_disclosed": "undisclosed",
        "round_type_disclosed": "undisclosed",
    })
    asymmetry = score_asymmetry({
        "fdv": 25_000_000,
        "raise_size": 1_000_000,
        "float_at_tge": 5,
        "fdv_raise_ratio": 20,
    })
    assert risk.value == 0
    assert asymmetry.value == 100
    assert is_rug_shape(risk.value, asymmetry.value)


def test_unknown_heavy_vector_reduces_coverage_and_is_not_a_zero_score():
    risk = score_risk({"backer_overlap": 2})
    asymmetry = score_asymmetry({"fdv": UNKNOWN, "raise_size": UNKNOWN})

    assert risk.value == 100
    assert risk.coverage == 0.3
    assert asymmetry.coverage == 0
    assert set(risk.facts) == {
        "backer_overlap",
        "audit",
        "team_disclosure",
        "vesting_disclosure",
        "raise_disclosed",
        "round_type_disclosed",
    }


def test_malformed_backer_count_is_unknown_instead_of_zero():
    score = score_risk({"backer_overlap": "several-ish"}, stage=SCAN)

    assert score.value is None
    assert score.coverage == 0
    assert score.facts["backer_overlap"] == UNKNOWN


def test_backer_name_lists_use_token_boundaries():
    score = score_risk(
        {
            "backer_overlap": [
                "NotParadigm",
                "Jumpstart Capital",
                "Paradigm\u0301",
                "\u0301Paradigm",
            ]
        },
        stage=SCAN,
    )
    assert score.value == 0
    assert score.coverage == 0.55


def test_no_evidence_scores_none_and_stays_distinct_from_a_genuine_zero():
    """A weighted mean over zero inputs has no value.

    Returning 0 would read as "maximally risky" on this higher-is-safer scale —
    a verdict invented from no evidence — and would be indistinguishable from a
    fully-covered project that genuinely fails every check.  Those are opposite
    facts and must not collapse.
    """
    no_evidence = score_risk({})
    genuinely_bad = score_risk({
        "backer_overlap": 0,
        "audit": "no_audit",
        "team_disclosure": "anonymous",
        "vesting_disclosure": "undisclosed",
        "raise_disclosed": "undisclosed",
        "round_type_disclosed": "undisclosed",
    })

    assert no_evidence.value is None
    assert no_evidence.coverage == 0
    assert genuinely_bad.value == 0
    assert genuinely_bad.coverage == 1

    # The whole point: the two are not the same fact.
    assert no_evidence.value != genuinely_bad.value
    assert not coverage_eligible(no_evidence)
    assert coverage_eligible(genuinely_bad)


def test_no_evidence_cannot_produce_a_rug_verdict():
    assert not is_rug_shape(score_risk({}).value, 90)


def test_categories_match_exactly_and_unrecognized_values_are_unknown():
    """Substring matching invented favourable facts.

    ``"audit_pending"`` contains ``"audit"``, so an audit that had not happened
    scored as a clean one; ``"audited_no_criticals"`` contains ``"critical"``,
    so a genuinely clean audit scored *below* a pending one.  Unrecognized
    values must degrade to UNKNOWN — dragging coverage — rather than being
    guessed in either direction.
    """
    pending = score_risk({"audit": "audit_pending"})
    assert pending.facts["audit"] == UNKNOWN
    assert pending.coverage == 0  # not scored at all, in either direction

    clean = score_risk({"audit": "audited_clean"})
    assert clean.value == 100
    assert clean.coverage == 0.25

    # The inversion: a clean audit must never rank below an unparsed one.
    assert not coverage_eligible(pending)

    for invalid in (True, False, 42, 0.5):
        result = score_risk({"audit": invalid})
        assert result.value is None
        assert result.coverage == 0
        assert result.facts["audit"] == UNKNOWN

    for invalid in (-1, float("nan"), float("inf"), [], (1, 2)):
        result = score_risk({"raise_disclosed": invalid}, stage=SCAN)
        assert result.value is None
        assert result.coverage == 0
        assert result.facts["raise_disclosed"] == UNKNOWN


def test_negative_and_non_finite_numbers_are_unknown():
    for invalid in (-1, float("nan"), float("inf"), "-5", 10**400, "1,2"):
        result = score_asymmetry({"fdv": invalid})
        assert result.value is None
        assert result.coverage == 0
        assert result.facts["fdv"] == UNKNOWN

    backers = score_risk({"backer_overlap": -1}, stage=SCAN)
    assert backers.value is None
    assert backers.facts["backer_overlap"] == UNKNOWN

    malformed_list = score_risk({"backer_overlap": [1, 2]}, stage=SCAN)
    assert malformed_list.value is None
    assert malformed_list.facts["backer_overlap"] == UNKNOWN


def test_huge_config_numbers_and_non_string_categories_fall_back():
    configured = load_config({
        "watchlist": {
            "shortlist_limit": 10**400,
            "score_anchors": {
                "risk": {"audit": {"low": 42}},
                "asym": {"fdv": {"worst": 10**400}},
            },
        }
    })

    assert configured["shortlist_limit"] == DEFAULT_CONFIG["shortlist_limit"]
    assert (
        configured["score_anchors"]["risk"]["audit"]
        == DEFAULT_SCORE_ANCHORS["risk"]["audit"]
    )
    assert (
        configured["score_anchors"]["asym"]["fdv"]
        == DEFAULT_SCORE_ANCHORS["asym"]["fdv"]
    )


def test_score_anchors_from_config_actually_drive_risk_scoring():
    """The anchors are the source of truth for what a category *is*.

    They were previously hardcoded for every risk field except backers, so
    editing `watchlist.score_anchors` silently did nothing.
    """
    custom = deepcopy(DEFAULT_SCORE_ANCHORS)
    custom["risk"]["audit"]["high"] = "reviewed_by_us"

    assert score_risk({"audit": "reviewed_by_us"}, anchors=custom).value == 100
    # And the old label is no longer recognized under the custom anchors.
    assert (
        score_risk({"audit": "audited_clean"}, anchors=custom).facts["audit"] == UNKNOWN
    )


def test_scan_stage_scores_listing_only_facts_without_penalising_coverage():
    """A listing has no audit/team/vesting fields — that is not the project's fault.

    Scoring a scanned project against the deep rubric pins coverage at ~0.35
    forever, so every row renders INSUFFICIENT DATA and the shortlist is empty
    every day: accurate and useless. Coverage must be measured against the
    inputs obtainable at this stage.
    """
    listing_facts = {
        "backer_overlap": 2,
        "raise_disclosed": "exact",
        "round_type_disclosed": "named",
    }
    scan_score = score_risk(listing_facts, stage=SCAN)
    deep_score = score_risk(listing_facts, stage=DEEP)

    assert scan_score.coverage == 1  # fully covered *for a listing*
    assert coverage_eligible(scan_score)
    assert deep_score.coverage < 0.5  # the old behavior
    assert not coverage_eligible(deep_score)


def test_stage_defaults_to_deep_so_a_forgotten_stage_underclaims():
    """Forgetting the argument must under-claim confidence, never over-claim."""
    facts = {
        "backer_overlap": 2,
        "raise_disclosed": "exact",
        "round_type_disclosed": "named",
    }
    assert score_risk(facts).coverage == score_risk(facts, stage=DEEP).coverage


def test_scan_and_deep_scores_are_labelled_differently():
    """A scan 100 and a verified 100 are not the same claim."""
    row = {
        "name": "P",
        "risk_score": 100,
        "risk_coverage": 1.0,
        "asym_score": 80,
        "asym_coverage": 1.0,
        "score_stage": "scan",
    }
    assert "scan" in render_shortlist([row])
    assert "verified" in render_shortlist([{**row, "score_stage": "deep"}])


def test_interpolation_boundaries_are_pinned():
    score = score_asymmetry({
        "fdv": 150_000_000,
        "raise_size": 5_000_000,
        "float_at_tge": 15,
        "fdv_raise_ratio": 60,
    })
    assert score.value == 50
    assert score.coverage == 1


def test_coverage_floor_keeps_unknown_axis_out_of_the_ranked_block():
    output = render_shortlist([
        {
            "name": "Unknown",
            "risk_score": 100,
            "risk_coverage": 0.3,
            "risk_facts": {"backer_overlap": 2},
            "asym_score": 100,
            "asym_coverage": 1,
        },
        {
            "name": "Known",
            "risk_score": 80,
            "risk_coverage": 1,
            "asym_score": 60,
            "asym_coverage": 1,
        },
    ])
    assert output.index("Known") < output.index("INSUFFICIENT DATA")
    assert "Unknown" in output


def test_insufficient_coverage_never_carries_a_rug_verdict():
    """A row cannot print INSUFFICIENT DATA and a confident RUG SHAPE at once.

    The verdict was drawn from the raw scores — the very numbers the same line
    tells the reader not to trust.
    """
    output = render_shortlist([
        {
            "name": "Sparse",
            "risk_score": 10,  # rug-shaped on the raw numbers...
            "risk_coverage": 0.1,  # ...but built from almost nothing
            "asym_score": 95,
            "asym_coverage": 0.1,
        },
    ])
    assert "INSUFFICIENT DATA" in output
    assert "RUG SHAPE" not in output


def test_shortlist_limit_is_one_combined_budget():
    rows = [
        {
            "name": f"Ranked{i}",
            "risk_score": 80,
            "risk_coverage": 1,
            "asym_score": 50,
            "asym_coverage": 1,
        }
        for i in range(4)
    ] + [
        {
            "name": f"Sparse{i}",
            "risk_score": 80,
            "risk_coverage": 0.1,
            "asym_score": 50,
            "asym_coverage": 0.1,
        }
        for i in range(4)
    ]
    output = render_shortlist(rows, {"watchlist": {"shortlist_limit": 3}})
    # Three rows total, not three per block.
    assert sum(output.count(n) for n in ("Ranked", "Sparse")) == 3
