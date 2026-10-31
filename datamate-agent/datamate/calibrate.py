"""Confidence calibration and abstention decisions.

The learned logistic model estimates P(candidate is correct | features). Isotonic
calibration is deliberately not fitted inside ``Calibrator.fit`` because fitting
it on the same observations used by logistic regression is optimistic. The
training script may attach an isotonic model fitted on out-of-fold predictions.
"""
from __future__ import annotations

import math
import pickle
from pathlib import Path

import numpy as np

FEATS = ["agree", "margin", "cross_family", "n_families", "tpl_fired", "tpl_agree", "tpl_strength",
         "err_frac", "empty_frac", "type_agree", "winner_is_ua", "ua_strong", "ua_any",
         "link_score", "link_ambiguous", "time_in_range", "log_n_cand", "llm_used", "escalated",
         "has_intent", "time_explicit", "view_line_math", "view_order_total", "view_unknown_status"]
INTENTS = ["revenue", "n_orders", "n_cancelled", "cancel_rate", "aov", "pct_change", "share", "shift_diff_pct",
           "best_period", "top_items", "top_customers", "top_staff", "n_customers", "n_customers_min_orders",
           "pct_customers_min_orders", "n_days_threshold", "n_orders_threshold", "n_items_min_orders", "qty",
           "qty_size", "monotonic", "revenue_series", "price_effect", "unit_price_on_day", "n_unsold_items",
           "import_total", "import_avg_price", "top_supplier", "multi_filter"]


def vec(f: dict) -> np.ndarray:
    base = [float(f.get(k, 0.0) or 0.0) for k in FEATS]
    it = f.get("intent")
    return np.asarray(base + [1.0 if it == i else 0.0 for i in INTENTS] + [1.0 if it is None else 0.0], dtype=float)


def _sig(z: float) -> float:
    z = max(-60.0, min(60.0, float(z)))
    return 1.0 / (1.0 + math.exp(-z))


class Calibrator:
    def __init__(self, offline_template_prior: float = 0.75):
        self.lr = None
        self.iso = None
        self.offline_prior = float(offline_template_prior)

    def prior(self, f: dict) -> float:
        if not f:
            return 0.0
        if f.get("winner_is_ua"):
            base = 0.82 if f.get("ua_strong") else 0.35
            if f.get("llm_used"):
                base = _sig(-2.0 + 2.5 * f.get("agree", 0) + 1.5 * f.get("ua_strong", 0)
                            + 0.8 * f.get("ua_any", 0) + 0.8 * f.get("cross_family", 0)
                            - 1.5 * f.get("tpl_fired", 0) * f.get("tpl_strength", 0))
            return float(base)
        if not f.get("llm_used"):
            # offline_template_prior is the probability assigned to a template-
            # supported winning answer. tpl_strength is a template ranking
            # signal, not a calibrated probability; multiplying by it caused
            # otherwise-supported answers to fall below the Chow threshold.
            return float(self.offline_prior) if f.get("tpl_agree") else 0.0
        z = (-2.2 + 3.2 * f.get("agree", 0) + 1.2 * f.get("cross_family", 0) + 1.2 * f.get("tpl_agree", 0)
             + 0.6 * f.get("type_agree", 0) - 1.5 * f.get("err_frac", 0)
             - 2.0 * (1 - f.get("time_in_range", 1)) - 1.5 * f.get("ua_strong", 0)
             - 1.0 * f.get("link_ambiguous", 0)
             - 1.0 * f.get("tpl_fired", 0) * (1 - f.get("tpl_agree", 0)) * f.get("tpl_strength", 0))
        return _sig(z)

    def fit(self, feats: list[dict], labels: list[int], C: float = 1.0):
        """Fit logistic regression only; optional isotonic fit belongs on OOF scores."""
        y = np.asarray(labels, dtype=int)
        if len(feats) != len(y):
            raise ValueError("feats and labels must have the same length")
        if len(y) == 0 or len(np.unique(y)) < 2:
            self.lr = None
            self.iso = None
            return self
        if not np.isin(y, [0, 1]).all():
            raise ValueError("labels must contain only 0 and 1")
        from sklearn.linear_model import LogisticRegression
        X = np.stack([vec(f) for f in feats])
        self.lr = LogisticRegression(C=C, max_iter=2000, random_state=0).fit(X, y)
        self.iso = None
        return self

    def p(self, f: dict) -> float:
        if not f:
            return 0.0
        if self.lr is None:
            return self.prior(f)
        raw = float(self.lr.predict_proba(vec(f)[None])[0, 1])
        if self.iso is not None:
            return float(np.clip(self.iso.predict([raw])[0], 0.0, 1.0))
        return raw

    def save(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as fh:
            pickle.dump({"version": 2, "lr": self.lr, "iso": self.iso,
                         "offline_prior": self.offline_prior}, fh)

    @classmethod
    def load(cls, path, offline_template_prior=0.75):
        c = cls(offline_template_prior)
        try:
            with open(path, "rb") as fh:
                d = pickle.load(fh)
            if isinstance(d, dict):
                c.lr = d.get("lr")
                c.iso = d.get("iso")
                c.offline_prior = float(d.get("offline_prior", offline_template_prior))
        except (OSError, EOFError, pickle.PickleError, AttributeError, TypeError, ValueError):
            pass
        return c


def expected_pa(ps, correct, tau):
    ps, c = np.asarray(ps, dtype=float), np.asarray(correct, dtype=int)
    if len(ps) != len(c):
        raise ValueError("ps and correct must have the same length")
    return float(np.where(ps > tau, np.where(c == 1, 1, -1), 0).mean()) if len(ps) else 0.0


def best_threshold(ps, correct, grid=None, n_boot: int = 200, seed: int = 0):
    """Exploratory PA threshold; evaluate on held-out data before deployment."""
    grid = np.linspace(0.3, 0.9, 61) if grid is None else np.asarray(grid, dtype=float)
    ps, c = np.asarray(ps, dtype=float), np.asarray(correct, dtype=int)
    if len(ps) != len(c):
        raise ValueError("ps and correct must have the same length")
    if len(ps) == 0:
        return 0.5, 0.0
    if n_boot <= 0:
        scores = [expected_pa(ps, c, t) for t in grid]
    else:
        rng = np.random.default_rng(seed)
        acc = np.zeros(len(grid))
        for _ in range(n_boot):
            idx = rng.integers(0, len(ps), len(ps))
            acc += np.array([expected_pa(ps[idx], c[idx], t) for t in grid])
        scores = acc / n_boot
    tau = float(grid[int(np.argmax(scores))])
    return tau, expected_pa(ps, c, tau)
