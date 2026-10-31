"""Regression tests for PA-focused selection and answer-shape safeguards."""
from types import SimpleNamespace

import pandas as pd

from datamate.agent import Agent, Candidate
from datamate.calibrate import Calibrator
from datamate.formatter import format_answer


def _agent():
    agent = Agent.__new__(Agent)
    agent.A = {"early_exit_agree": 3, "early_exit_cross_family": True,
               "ua_requires_cross_family": True}
    agent.cfg = {"decision": {"tau_answer": 0.5, "tau_unanswerable": 0.5,
                              "min_template_strength": 0.45}}
    agent.cal = Calibrator(offline_template_prior=0.75)
    return agent


def test_multirow_scalar_answers_are_not_silently_truncated():
    d = pd.DataFrame({"name": ["A", "B"], "quantity": [10, 9]})
    assert format_answer(d, "string") is None
    assert format_answer(d, "number") is None
    assert format_answer(pd.DataFrame({"a": [10], "b": [9]}), "number") is None
    assert format_answer(pd.DataFrame({"name": ["A"], "amount": [100]}), "number") == ("number", 100)
    assert format_answer(d, "list") == ("list", ["A", "B"])


def test_llm_samples_from_one_family_do_not_shortcircuit_independent_checks():
    a = _agent()
    sql = [Candidate("sql", ("number", 42)) for _ in range(3)]
    assert not a._agreed(sql)
    assert a._agreed(sql + [Candidate("sql_plan", ("number", 42))])
    assert not a._agreed(sql + [Candidate("sql_plan", ("number", 43))])
    assert a._agreed(sql + [Candidate("tpl:revenue", ("number", 42))])


def test_template_strength_controls_offline_abstention():
    a = _agent()
    pred = {"type": "number", "answer": 100}
    base = {"llm_used": 0.0, "tpl_agree": 1.0, "tpl_strength": 0.20}
    assert a.decide(pred, dict(base))[0]["type"] == "abstain"
    assert a.decide(pred, {**base, "tpl_strength": 0.9})[0] == pred
    assert a.decide(pred, {**base, "tpl_strength": 0.9, "llm_used": 1})[0]["type"] in ("number", "abstain")


def test_llm_unanswerable_needs_independent_evidence():
    a = _agent()
    spec = SimpleNamespace(intent=None, answer_type="number")
    tctx = SimpleNamespace(primary=None, explicit=False)
    ctx = SimpleNamespace(data_lo=None, data_hi=None, sm=SimpleNamespace(flags={}))
    sql_ua = [Candidate("sql", ua=True) for _ in range(3)]
    pred, _ = a._select(sql_ua, spec, [], [], [], tctx, ctx, False)
    assert pred["type"] == "abstain"
    pred, _ = a._select(sql_ua + [Candidate("sql_plan", ua=True)], spec, [], [], [], tctx, ctx, True)
    assert pred["type"] == "unanswerable"
    pred, _ = a._select(sql_ua + [Candidate("rule", ua=True)], spec, [], [("strong", "missing column")],
                        [], tctx, ctx, False)
    assert pred["type"] == "unanswerable"


def test_pa_default_does_not_repeat_unvalidated_strength_gate():
    from datamate.config import load_config
    cfg = load_config()
    assert cfg["decision"]["min_template_strength"] == 0.0


def test_replay_recovers_low_strength_valid_template():
    from scripts.sweep_policy import replay
    cal = Calibrator(offline_template_prior=0.75)
    row = {"pred": {"type": "number", "answer": 29},
           "feats": {"tpl_agree": 1.0, "tpl_strength": 0.20, "llm_used": 0.0}}
    assert replay(row, 0.45, cal)["type"] == "abstain"
    assert replay(row, 0.0, cal) == {"type": "number", "answer": 29}
    # The stored 0.49 probability from the old decision must not override
    # a fresh recalibration of the candidate under the new policy.
    row["p"] = 0.49
    assert replay(row, 0.0, cal)["type"] == "number"
