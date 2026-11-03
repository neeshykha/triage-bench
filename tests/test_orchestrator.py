import itertools

import pytest

from triage.agents import AgentError
from triage.orchestrator import Pipeline, run_case
from triage.schemas import Category, Channel, Priority, Queue, Route

from .conftest import AGREE, WINDOW, ScriptedAgents, disagree, make_case, make_triage, usage

CASE = make_case("The thermostat won't respond and it's 40 degrees in here.")

A = Pipeline("A", rules=None, challenger=False)
B = Pipeline("B", rules=WINDOW, challenger=False)
C = Pipeline("C", rules=None, challenger=True)
D = Pipeline("D", rules=WINDOW, challenger=True)


def test_triage_alone_never_calls_the_challenger():
    agents = ScriptedAgents(make_triage())
    result = run_case(CASE, agents, A)
    assert result.route == Route.NO_CHALLENGER
    assert agents.count("challenge") == 0 and result.revisions == 0
    assert [u.stage for u in result.usage] == ["triage"]


def test_agreement_on_first_pass_ships_triage_unchanged():
    proposal = make_triage()
    agents = ScriptedAgents(proposal, challenges=[AGREE])
    result = run_case(CASE, agents, C)
    assert result.route == Route.AGREED
    assert result.final == proposal and result.revisions == 0
    assert agents.count("revise") == 0


def test_disagreement_resolved_by_one_revision():
    revised = make_triage(priority=Priority.P2)
    agents = ScriptedAgents(make_triage(), challenges=[disagree(), AGREE], revisions=[revised])
    result = run_case(CASE, agents, C)
    assert result.route == Route.AGREED_AFTER_REVISION
    assert result.final.priority == Priority.P2 and result.revisions == 1
    assert [s.stage for s in result.trace] == ["triage", "challenge", "revise", "recheck"]


def test_loop_stops_at_one_revision_and_routes_to_human_review():
    # An endless supply of disagreement: the bound, not the script, has to stop the loop.
    agents = ScriptedAgents(make_triage(), challenges=itertools.repeat(disagree()))
    result = run_case(CASE, agents, C)
    assert result.route == Route.UNRESOLVED
    assert result.queue == Queue.HUMAN_REVIEW
    assert agents.count("revise") == 1 and agents.count("challenge") == 2
    assert result.revisions == 1


@pytest.mark.parametrize("max_revisions", [0, 1, 2, 3])
def test_revise_calls_never_exceed_the_bound(max_revisions):
    agents = ScriptedAgents(make_triage(), challenges=itertools.repeat(disagree()))
    result = run_case(CASE, agents, Pipeline("C", rules=None, challenger=True, max_revisions=max_revisions))
    assert agents.count("revise") == max_revisions == result.revisions
    assert agents.count("challenge") == max_revisions + 1
    assert result.route == Route.UNRESOLVED


def test_human_review_keeps_the_more_urgent_priority():
    agents = ScriptedAgents(make_triage(priority=Priority.P3), challenges=itertools.repeat(disagree(suggested="P1")))
    result = run_case(CASE, agents, C)
    assert result.final.priority == Priority.P1


def test_human_review_ignores_a_less_urgent_suggestion():
    agents = ScriptedAgents(make_triage(priority=Priority.P2), challenges=itertools.repeat(disagree(suggested="P4")))
    result = run_case(CASE, agents, C)
    assert result.final.priority == Priority.P2


def test_rules_apply_before_the_challenger_sees_the_proposal():
    case = make_case("door wont open", channel=Channel.SMS)
    proposal = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P3, queue=Queue.TIER1_ACCESS)
    agents = ScriptedAgents(proposal, challenges=[AGREE])
    result = run_case(case, agents, D)
    _, seen, _ = next(call for call in agents.calls if call[0] == "challenge")
    assert seen.priority == Priority.P1
    assert result.final.priority == Priority.P1
    assert [(o.rule_id, o.stage) for o in result.rule_overrides] == [("R1_lockout_now", "triage")]


def test_rules_reapply_after_a_revision_lowers_priority():
    case = make_case("door wont open", channel=Channel.SMS)
    lowered = make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P3, queue=Queue.TIER1_ACCESS)
    agents = ScriptedAgents(
        make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P1, queue=Queue.TIER1_ACCESS),
        challenges=[disagree(suggested="P3"), AGREE],
        revisions=[lowered],
    )
    result = run_case(case, agents, D)
    assert result.final.priority == Priority.P1
    assert [(o.rule_id, o.stage) for o in result.rule_overrides] == [("R1_lockout_now", "revise")]


def test_rules_off_means_no_precheck_and_no_overrides():
    case = make_case("door wont open", channel=Channel.SMS)
    agents = ScriptedAgents(make_triage(category=Category.ACCESS_LOCKS, priority=Priority.P3))
    result = run_case(case, agents, A)
    assert result.final.priority == Priority.P3 and result.rule_overrides == []
    assert "precheck" not in [s.stage for s in result.trace]


class FailingChallenger(ScriptedAgents):
    def challenge(self, case, proposal, prior=None):
        raise AgentError("challenge: stop_reason=refusal", usage=usage("challenge"))


def test_agent_failure_routes_to_human_review_and_keeps_its_cost():
    agents = FailingChallenger(make_triage(priority=Priority.P2))
    result = run_case(CASE, agents, C)
    assert result.route == Route.AGENT_ERROR and result.queue == Queue.HUMAN_REVIEW
    assert result.final.priority == Priority.P2  # triage's work survives for the human
    assert [u.stage for u in result.usage] == ["triage", "challenge"]
    assert "refusal" in result.error


class FailingTriage(ScriptedAgents):
    def triage(self, case):
        raise AgentError("triage: output failed schema validation")


def test_triage_failure_leaves_no_final_but_still_routes_to_a_person():
    result = run_case(CASE, FailingTriage(make_triage()), D)
    assert result.final is None and result.queue == Queue.HUMAN_REVIEW
    assert result.route == Route.AGENT_ERROR
