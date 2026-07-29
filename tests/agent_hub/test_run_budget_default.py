"""The run budget is sized for a run, not for one call.

`max_output_tokens` and `max_total_tokens` both defaulted to 131072. They are
different things -- one bounds a single provider reply, the other bounds
everything a plan spends -- and giving them the same number made the run budget
far too small to be useful.

Measured on this machine before the change, from the step ledger:

    search   max 247,203   avg 143,252
    chat     max 161,099   avg  60,592
    write    max 123,031   avg  41,953
    review   max  57,489   avg  22,750

A single web search step therefore did not fit inside a whole run's budget.
Every plan containing one paused on run_token_budget_exhausted after the first
step and needed a human to grant more, which is not a budget doing its job --
it is a budget set to the wrong quantity.
"""

from __future__ import annotations

from agent_hub.orchestrator import MAX_PLAN_STEPS
from agent_hub.v2.contracts import output_token_limit, total_token_limit
from agent_hub.v2.policy import DEFAULT_POLICY

BUDGETS = DEFAULT_POLICY["budgets"]

# The largest single step measured, and what the run that exposed this spent.
LARGEST_OBSERVED_STEP = 247_203
LARGEST_OBSERVED_RUN = 423_027


def test_the_run_budget_is_not_the_per_call_cap():
    """Equal values are what made this wrong; they must not drift back."""

    assert BUDGETS["max_total_tokens"] != BUDGETS["max_output_tokens"]
    assert BUDGETS["max_total_tokens"] > BUDGETS["max_output_tokens"]


def test_a_single_search_step_fits_inside_a_run():
    assert BUDGETS["max_total_tokens"] > LARGEST_OBSERVED_STEP


def test_the_run_that_exposed_this_would_now_finish_its_first_wave():
    assert BUDGETS["max_total_tokens"] > LARGEST_OBSERVED_RUN


def test_a_full_plan_of_the_largest_observed_steps_fits():
    """MAX_PLAN_STEPS is the ceiling on how many steps a plan may hold, so the
    budget is sized for a maximal plan rather than a typical one."""

    assert BUDGETS["max_total_tokens"] >= MAX_PLAN_STEPS * LARGEST_OBSERVED_STEP


def test_it_still_stops_something_runaway():
    """A budget that cannot be exhausted is not a budget. This is a ceiling, not
    an allowance -- runs spend what they spend."""

    assert BUDGETS["max_total_tokens"] < 100_000_000


def test_a_task_without_constraints_gets_the_same_number_either_way():
    """The contracts fallback and the policy default must agree, or a task with
    no policy is budgeted differently from one with the default policy."""

    assert total_token_limit({}) == BUDGETS["max_total_tokens"]
    assert output_token_limit({}) == BUDGETS["max_output_tokens"]


def test_an_explicit_task_limit_still_wins():
    assert total_token_limit({"max_total_tokens": 5_000}) == 5_000
