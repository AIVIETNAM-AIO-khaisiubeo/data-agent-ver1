import pytest

from datamate.calibrate import Calibrator


def test_offline_template_prior_is_not_scaled_by_template_strength():
    cal = Calibrator(offline_template_prior=0.75)
    feats = {"llm_used": 0.0, "tpl_agree": 1.0, "tpl_strength": 0.19}
    assert cal.p(feats) == pytest.approx(0.75)


def test_offline_candidate_without_template_agreement_is_not_accepted():
    cal = Calibrator(offline_template_prior=0.75)
    feats = {"llm_used": 0.0, "tpl_agree": 0.0, "tpl_strength": 0.9}
    assert cal.p(feats) == 0.0


def test_single_class_fit_falls_back_to_prior():
    cal = Calibrator(offline_template_prior=0.8)
    feats = [{"llm_used": 0.0, "tpl_agree": 1.0, "tpl_strength": 0.3}]
    cal.fit(feats, [1])
    assert cal.lr is None
    assert cal.iso is None
    assert cal.p(feats[0]) == pytest.approx(0.8)


def test_offline_prior_round_trips_through_pickle(tmp_path):
    path = tmp_path / "offline.pkl"
    Calibrator(offline_template_prior=0.83).save(path)
    loaded = Calibrator.load(path, offline_template_prior=0.5)
    assert loaded.offline_prior == pytest.approx(0.83)
