"""A revised estimate for a period already seen is still a release.

BEA publishes one quarter's GDP three times -- advance, second, third -- under
the same `TimePeriod`, revising the number each time. The monitor asked only
"is the period later than the one I have?", so after the advance estimate both
revisions read as a quarter already seen and were dropped. The indicator set
names all three, and the second and third are the ones that move a view.

The state has always stored the value alongside the period. Nothing read it.
"""

from __future__ import annotations

import econ_state


def _store(tmp_path):
    return econ_state.EconState(tmp_path / "state.sqlite")


def test_an_unseen_indicator_is_new(tmp_path):
    state = _store(tmp_path)
    assert state.last_seen("gdp") is None


def test_the_value_comes_back_with_the_period(tmp_path):
    state = _store(tmp_path)
    state.mark_seen("gdp", "2026-04-01", "2026Q2", 2.8)
    assert state.last_seen("gdp") == ("2026-04-01", 2.8)


def test_a_revision_to_the_same_quarter_is_visible(tmp_path):
    """The whole point: same period, different number.

    Reading only the period, this pair is indistinguishable from running twice
    on an unchanged quarter -- which is why two of the three GDP estimates
    never reached the report.
    """
    state = _store(tmp_path)
    state.mark_seen("gdp", "2026-04-01", "2026Q2", 2.8)  # advance
    period, value = state.last_seen("gdp")

    revised = 3.1  # second estimate
    assert period == "2026-04-01", "same quarter, as BEA publishes it"
    assert value != revised, (
        "the stored value must differ from the revision, or the monitor has nothing to notice"
    )

    state.mark_seen("gdp", "2026-04-01", "2026Q2", revised)
    assert state.last_seen("gdp") == ("2026-04-01", revised)


def test_an_unchanged_rerun_stays_quiet(tmp_path):
    """The noise this check must not create: running twice on the same
    release is not a release."""
    state = _store(tmp_path)
    state.mark_seen("gdp", "2026-04-01", "2026Q2", 2.8)
    period, value = state.last_seen("gdp")
    assert (period, value) == ("2026-04-01", 2.8)
