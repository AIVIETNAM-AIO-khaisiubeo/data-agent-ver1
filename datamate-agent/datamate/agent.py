"""The DataMate agent: one question in, one scored-answer decision out.

Per question:
  0. Shop context (built once per business, cached): files -> DuckDB, README
     dictionary, column roles, canonical rulebook-aware views (v_sales, v_orders),
     value index, shop card text for the LLM.
  1. Deterministic understanding: intent/type/slots, time ranges (as_of_date +
     rulebook + lunar calendar), value linking, answerability evidence.
  2. Candidates, adaptive compute:
       round 1: templates (free) + n_sql direct-SQL samples (one batched call)
       early exit when >= early_exit_agree candidates agree (and the template, if
       it fired, agrees) -> most questions stop here (inference time is the
       leaderboard tie-break);
       round 2: plan->SQL, pandas (CodeAct), optional second SQL model, ReAct
       explorer; each with one execution-feedback repair.
  3. Selection: cluster by canonical executed answer, weighted vote (+ judge when
     the top two clusters are close).
  4. Calibrated P(correct) -> Chow's rule -> answer / unanswerable / abstain.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

from . import salesview
from .answerability import evidence as ua_evidence
from .executor import extract_blocks, preview, run_python, run_sql
from .formatter import ANSWER_TYPES, answer_key, format_answer
from .attributes import ProductAttributes
from .linker import ValueIndex, item_mention, mention_linked, resolve_entity, unit_value
from .loader import Business
from .prompts import FIX, JUDGE, PYTHON, REACT, SQL_DIRECT, SQL_PLAN, business_card, question_block
from .question import analyze
from .readme import parse_readme
from .templates import run_templates
from .textnorm import qnorm
from .timeresolver import TimeResolver

TYPE_RE = re.compile(r"TYPE\s*[:=]\s*`?([a-zA-Z_]+)", re.I)
REASON_RE = re.compile(r"REASON\s*[:=]\s*(.+)", re.I)


@dataclass
class Candidate:
    family: str
    fa: tuple | None = None
    ua: bool = False
    ok: bool = True
    empty: bool = False
    err: str | None = None
    code: str | None = None
    weight: float = 1.0
    declared: str | None = None
    output: str | None = None

    @property
    def key(self):
        if self.ua:
            return ("unanswerable",)
        return answer_key(*self.fa) if self.fa else None


@dataclass
class BizCtx:
    biz: Business
    readme: object
    sm: object
    vindex: ValueIndex
    card: str
    data_lo: object
    data_hi: object
    snapshot: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Agent:
    def __init__(self, cfg: dict, rulebook, lunar, llm=None, sql_llm=None, calibrator=None):
        self.cfg, self.rb, self.lunar = cfg, rulebook, lunar
        self.llm, self.sql_llm = llm, sql_llm
        self.cal = calibrator
        self.resolver = TimeResolver(rulebook, lunar)
        self._ctx: dict[str, BizCtx] = {}
        self._ctx_lock = threading.Lock()
        self._biz_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._dirs = None
        self.A = cfg["agent"]
        self.weekdays = cfg.get("format", {}).get("weekday_names")

    # ------------------------------------------------------------ context --
    def _find_dir(self, bid: str) -> str | None:
        root = self.cfg["data"]["root"]
        for cand in (os.path.join(root, "data", bid), os.path.join(root, bid)):
            if os.path.isdir(cand):
                return cand
        if self._dirs is None:
            self._dirs = {}
            for dp, dns, _ in os.walk(root):
                for d in dns:
                    self._dirs.setdefault(d, os.path.join(dp, d))
        return self._dirs.get(bid)

    def context(self, bid: str) -> BizCtx | None:
        with self._ctx_lock:
            if bid in self._ctx:
                return self._ctx[bid]
            lock = self._biz_locks[bid]
        with lock:
            if bid in self._ctx:
                return self._ctx[bid]
            d = self._find_dir(bid)
            if d is None:
                return None
            biz = Business(d, bid, cache_dir=self.cfg["data"].get("cache_dir"))
            readme = parse_readme(biz.readme)
            sm = salesview.build(biz, readme, self.rb)
            try:
                sm.attrs = ProductAttributes(biz, sm, biz.readme)
            except Exception as e:  # attribute filters are optional
                sm.attrs = None
                sm.warnings.append(f"attributes failed: {type(e).__name__}: {e}"[:200])
            vindex = ValueIndex(biz, sm.roles)
            if sm.orders is not None and len(sm.orders):
                lo, hi = sm.orders["order_date"].min().date(), sm.orders["order_date"].max().date()
            else:
                lo, hi = biz.date_range()
            if readme.data_end is not None:
                hi = readme.data_end if hi is None else max(hi, readme.data_end)
            if readme.data_start is not None:
                lo = readme.data_start if lo is None else min(lo, readme.data_start)
            card = business_card(biz, sm, readme, biz.profile(roles=sm.roles), lo, hi)
            ctx = BizCtx(biz, readme, sm, vindex, card, lo, hi)
            with self._ctx_lock:
                self._ctx[bid] = ctx
            return ctx

    def release(self, bid: str):
        """Free a shop's memory once all its questions are answered."""
        with self._ctx_lock:
            ctx = self._ctx.pop(bid, None)
        if ctx is not None:
            try:
                ctx.biz.con.close()
            except Exception:
                pass

    def _snapshot(self, ctx: BizCtx) -> str:
        with ctx.lock:
            if ctx.snapshot is None:
                ctx.snapshot = ctx.biz.snapshot(os.path.join(self.cfg["data"].get("cache_dir", ".cache/"), "snapshots"))
        return ctx.snapshot

    # ------------------------------------------------------- llm helpers --
    def _system(self, style: str) -> str:
        return style + "\n# RULEBOOK (authoritative for this split)\n" + (self.rb.text or "(none provided)")

    @staticmethod
    def _seed(item, family):
        """Deterministic per-question seed (reproducible re-runs, Rule 8); derived from
        the question TEXT and as-of date, never from ids."""
        h = hashlib.sha256(f"{item['question']}|{item['as_of_date']}|{family}".encode()).hexdigest()
        return int(h[:8], 16)

    @staticmethod
    def _parse(text: str):
        m = TYPE_RE.search(text or "")
        typ = m.group(1).lower() if m else None
        if typ in ("unanswerable", "khong_tra_loi_duoc") or (typ is None and re.search(r"\bUNANSWERABLE\b", text or "")):
            return "unanswerable", None, None
        for lang in ("sql", "python", "py"):
            b = extract_blocks(text, lang)
            if b:
                return typ, ("python" if lang != "sql" else "sql"), b[-1]
        m = list(re.finditer(r"(?ims)^\s*(WITH\s+\w+\s+AS\s*\(|SELECT\s).*?(?:;|\Z|\n\s*\n)", text or ""))
        if m:
            return typ, "sql", m[-1].group(0).strip().rstrip(";")
        return typ, None, None

    def _execute(self, ctx, lang, code, as_of):
        if lang == "sql":
            return run_sql(ctx.biz.con, code, self.A.get("exec_timeout_s", 8), self.A.get("max_result_rows", 500))
        return run_python(self._snapshot(ctx), code, as_of, self.A.get("py_timeout_s", 15))

    @staticmethod
    def _fmt(res, atype, month):
        if not res["ok"] or (res["empty"] and atype not in ("list", "table")):
            return None
        return format_answer(res["df"], atype, month)

    def _materialise(self, ctx, family, text, user, spec, tctx, item, llm, sys_prompt) -> Candidate:
        typ, lang, code = self._parse(text)
        if typ == "unanswerable":
            r = REASON_RE.search(text or "")
            return Candidate(family, ua=True, declared="unanswerable", output=r.group(1)[:200] if r else None)
        if not code:
            return Candidate(family, ok=False, err="no code block", declared=typ)
        atype = typ if typ in ANSWER_TYPES else spec.answer_type
        month = tctx.month_answer or spec.entity == "month"
        res = self._execute(ctx, lang, code, item["as_of_date"])
        fa = self._fmt(res, atype, month)
        if fa is None and self.A.get("max_fix_rounds", 1) > 0 and llm is not None:
            if not res["ok"]:
                err = res["err"]
            elif res["empty"]:
                err = "the program returned no rows (or only NULLs)"
            else:
                err = f"the result could not be read as TYPE {atype}: {preview(res['df'], 5)}"
            try:
                fix_txt = llm.chat([{"role": "system", "content": sys_prompt}, {"role": "user", "content": user},
                                    {"role": "assistant", "content": text},
                                    {"role": "user", "content": FIX.format(err=err, lang=lang, code=code)}],
                                   n=1, temperature=0.2, seed=self._seed(item, family + "fix"))[0]
            except Exception as e:
                return Candidate(family, None, ok=False, empty=res["empty"], err=f"{err} | fix failed: {e}"[:300],
                                 code=code, declared=typ)
            t2, l2, c2 = self._parse(fix_txt)
            if t2 == "unanswerable":
                # giving up after an execution error is not evidence that the data lacks the answer
                return Candidate(family + "+fix", None, ok=False, empty=res["empty"], err=f"gave up after: {err}"[:300],
                                 code=code, declared=typ)
            if c2:
                atype2 = t2 if t2 in ANSWER_TYPES else atype
                res2 = self._execute(ctx, l2 or lang, c2, item["as_of_date"])
                fa2 = self._fmt(res2, atype2, month)
                return Candidate(family + "+fix", fa2, ok=res2["ok"], empty=res2["empty"], err=res2["err"], code=c2,
                                 weight=0.8, declared=t2, output=preview(res2["df"], 3) if res2["ok"] else None)
        return Candidate(family, fa, ok=res["ok"], empty=res["empty"], err=res["err"], code=code, declared=typ,
                         output=preview(res["df"], 3) if res["ok"] else None)

    def _gen(self, ctx, family, style, user, n, spec, tctx, item, llm=None, thinking=None, temperature=None):
        llm = llm or self.llm
        if llm is None or n <= 0:
            return []
        sys_prompt = self._system(style)
        try:
            texts = llm.chat([{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}],
                             n=n, thinking=thinking, temperature=temperature, seed=self._seed(item, family))
        except Exception as e:
            return [Candidate(family, ok=False, err=f"llm error: {e}"[:200])]
        return [self._materialise(ctx, family, t, user, spec, tctx, item, llm, sys_prompt) for t in texts]

    def _react(self, ctx, user, spec, tctx, item) -> list[Candidate]:
        steps = int(self.A.get("react_steps", 0))
        if self.llm is None or steps <= 0:
            return []
        sys_prompt = self._system(REACT.replace("{steps}", str(steps)))
        msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
        for i in range(steps + 1):
            try:
                txt = self.llm.chat(msgs, n=1, temperature=0.3, thinking=bool(self.A.get("escalate_thinking")),
                                    seed=self._seed(item, f"react{i}"))[0]
            except Exception as e:
                return [Candidate("react", ok=False, err=f"llm error: {e}"[:200])]
            if "FINAL" in txt or i == steps or not re.search(r"ACTION\s*:\s*probe", txt, re.I):
                final = txt.split("FINAL", 1)[-1]
                return [self._materialise(ctx, "react", final, user, spec, tctx, item, self.llm, sys_prompt)]
            probe = extract_blocks(txt, "sql")
            obs = preview(run_sql(ctx.biz.con, probe[-1], 5)["df"]) if probe else "(no probe found)"
            msgs += [{"role": "assistant", "content": txt}, {"role": "user", "content": f"OBSERVATION:\n{obs}"}]
        return []

    # --------------------------------------------------------------- solve --
    def solve(self, item: dict, fast: bool = False):
        t0 = time.time()
        trace = {"id": item["id"], "question": item["question"], "as_of_date": item["as_of_date"]}
        ctx = self.context(item["business_id"])
        if ctx is None:
            return {"type": "abstain"}, {}, {**trace, "error": "business not found"}
        q = item["question"]
        spec = analyze(q)
        tctx = self.resolver.resolve(q, item["as_of_date"])
        links = ctx.vindex.link(q)
        mention = item_mention(q)
        item_linked = mention_linked(mention, ctx.vindex)
        uv = unit_value(mention, ctx.vindex)
        if uv and spec.intent in (None, "qty", "revenue") and "multi" not in spec.flags:
            spec.intent, spec.answer_type, spec.metric, spec.unit_word = "qty", "number", "quantity", uv
        if item_linked is False and any((l.role or "").startswith("product_") and l.score >= 95 and len(l.span.split()) >= 2
                                        for l in links):
            item_linked = True          # the item is named exactly elsewhere in the question
        ev = ua_evidence(ctx.biz, ctx.sm, spec, tctx, item_linked, ctx.data_lo, ctx.data_hi, links)
        trace.update(spec={"intent": spec.intent, "type": spec.answer_type, "k": spec.k, "metric": spec.metric,
                           "threshold": spec.threshold, "group": spec.group_name},
                     time=[r.as_dict() for r in tctx.ranges], snapshot=str(tctx.snapshot) if tctx.snapshot else None,
                     links=[(l.span, l.value, round(l.score)) for l in links[:5]], mention=mention,
                     item_linked=item_linked, ua_evidence=ev, data_range=[str(ctx.data_lo), str(ctx.data_hi)])

        cands: list[Candidate] = []
        tpl = []
        strong = [e for e in ev if e[0] == "strong"]
        if self.A.get("use_templates", True):
            try:
                tpl = run_templates(spec, tctx, ctx.sm, links, self.rb, ctx.data_lo, ctx.data_hi, self.weekdays)
            except Exception as e:
                trace["template_error"] = repr(e)[:200]
        for t in tpl:
            if t.answer[0] == "unanswerable":
                cands.append(Candidate(f"tpl:{t.name}", ua=True, weight=1.0 + t.strength, code=t.detail,
                                       output=t.answer[1]))
            elif not strong:
                cands.append(Candidate(f"tpl:{t.name}", t.answer, weight=1.0 + t.strength, code=t.detail))
        if strong:
            cands.append(Candidate("rule", ua=True, weight=1.5, code="; ".join(e[1] for e in strong)))

        escalated = False
        if self.llm is not None and not fast:
            user = ctx.card + "\n\n" + question_block(item, qnorm(q), tctx, links, spec, self.rb, ctx.sm)
            cands += self._gen(ctx, "sql", SQL_DIRECT, user, int(self.A.get("n_sql", 3)), spec, tctx, item)
            if not self._agreed(cands):
                escalated = True
                cands += self._gen(ctx, "sql_plan", SQL_PLAN, user, int(self.A.get("n_plan", 2)), spec, tctx, item,
                                   temperature=0.5)
                # Escalate in stages: independent plan->SQL confirmation can
                # avoid more expensive Python execution and ReAct probes.
                if not self._agreed(cands):
                    cands += self._gen(ctx, "py", PYTHON, user, int(self.A.get("n_py", 2)), spec, tctx, item)
                if not self._agreed(cands) and self.sql_llm is not None:
                    cands += self._gen(ctx, "sql2", SQL_DIRECT, user, int(self.A.get("n_sql_model", 2)), spec, tctx,
                                       item, llm=self.sql_llm)
                if not self._agreed(cands):
                    cands += self._react(ctx, user, spec, tctx, item)

        pred, feats = self._select(cands, spec, tpl, ev, links, tctx, ctx, escalated)
        if self.llm is not None and not fast and feats.get("_needs_judge") and self.A.get("use_judge", True):
            pred, feats = self._judge(ctx, item, spec, tctx, links, cands, pred, feats)
        feats.pop("_needs_judge", None)
        feats.pop("_ranked", None)
        feats["intent"] = spec.intent
        if pred.get("type") == "unanswerable":
            win = [c for c in cands if c.ua]
            pred["reason"] = next((c.output or c.code for c in win if (c.output or c.code)), "Dữ liệu không đủ để trả lời")
        trace["candidates"] = [{"family": c.family, "key": str(c.key)[:200], "err": c.err, "w": round(c.weight, 2),
                                "code": (c.code or "")[:1500], "output": (c.output or "")[:600]} for c in cands]
        trace["seconds"] = round(time.time() - t0, 3)
        return pred, feats, trace

    def _agreed(self, cands) -> bool:
        """Early-exit needs independent corroboration, not repeated SQL samples.

        Three completions from one prompt/model are correlated and should not
        suppress the independent planning/Python families on unseen schemas.
        """
        need = int(self.A.get("early_exit_agree", 3))
        llm_c = [c for c in cands if not c.family.startswith(("tpl:", "rule")) and c.key is not None]
        if len(llm_c) < need:
            return False
        keys = [c.key for c in llm_c]
        top = max(set(keys), key=lambda k: (keys.count(k), str(k)))
        if keys.count(top) < need:
            return False
        tpl_keys = [c.key for c in cands if c.family.startswith(("tpl:", "rule")) and c.key is not None]
        if tpl_keys:
            return top in tpl_keys
        if not self.A.get("early_exit_cross_family", True):
            return True
        agreeing_families = {c.family.split(":")[0].split("+")[0] for c in llm_c if c.key == top}
        return len(agreeing_families) >= 2

    # ---------------------------------------------------------- selection --
    def _select(self, cands, spec, tpl, ev, links, tctx, ctx, escalated):
        W, fams, members = defaultdict(float), defaultdict(set), defaultdict(list)
        for c in cands:
            if c.key is None:
                continue
            W[c.key] += c.weight
            fams[c.key].add(c.family.split(":")[0].split("+")[0])
            members[c.key].append(c)
        total_w = sum(c.weight for c in cands) or 1.0
        n = len(cands)
        p = tctx.primary
        in_range = 1.0
        if p is not None and ctx.data_lo is not None:
            in_range = float(p.end > ctx.data_lo and p.start <= ctx.data_hi)
        top_link, amb = resolve_entity(links, ("product", "customer"))
        llm_c = [c for c in cands if not c.family.startswith(("tpl:", "rule"))]
        all_fams = {c.family.split(":")[0].split("+")[0] for c in cands if c.key is not None}
        f = {
            "err_frac": sum(1 for c in llm_c if not c.ok) / max(1, len(llm_c)),
            "empty_frac": sum(1 for c in llm_c if c.ok and c.empty) / max(1, len(llm_c)),
            "tpl_fired": float(bool(tpl)), "tpl_strength": max([t.strength for t in tpl], default=0.0),
            "ua_strong": float(any(e[0] == "strong" for e in ev)), "ua_any": float(bool(ev)),
            "link_score": (top_link.score / 100) if top_link else 0.0, "link_ambiguous": float(amb),
            "time_in_range": in_range, "log_n_cand": math.log1p(n),
            "llm_used": float(any(c.key is not None for c in llm_c)), "escalated": float(escalated),
            "n_families": float(len(all_fams)), "has_intent": float(spec.intent is not None),
            "time_explicit": float(tctx.explicit),
            "view_line_math": float(ctx.sm.flags.get("line_math_agree", 1.0)),
            "view_order_total": float(ctx.sm.flags.get("order_total_agree", 1.0)),
            "view_unknown_status": float(ctx.sm.flags.get("unknown_status_share", 0.0)),
        }
        if not W:
            f.update(agree=0.0, margin=0.0, cross_family=0.0, tpl_agree=0.0, type_agree=0.0, winner_is_ua=0.0)
            return {"type": "abstain"}, f
        ranked = sorted(W.items(), key=lambda kv: (-kv[1], -len(fams[kv[0]])))
        win_key, win_w = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        win = members[win_key][0]
        f.update(agree=win_w / total_w, margin=(win_w - second) / total_w,
                 cross_family=(len(fams[win_key]) - 1) / max(1, len(all_fams) - 1) if len(all_fams) > 1 else 0.0,
                 tpl_agree=float(any(c.family.startswith("tpl:") for c in members[win_key])),
                 winner_is_ua=float(win.ua),
                 type_agree=float(win.ua or (win.fa is not None and win.fa[0] == spec.answer_type)))
        f["_needs_judge"] = len(ranked) > 1 and abs(win_w - second) < 0.5 and not win.ua and ranked[1][0] != ("unanswerable",)
        f["_ranked"] = [(k, members[k]) for k, _ in ranked[:2]]
        if win.ua:
            # A group of identical LLM unanswerable guesses is not proof of
            # absent data. Require deterministic evidence or another family.
            if (self.A.get("ua_requires_cross_family", True) and not ev
                    and not any(c.family.startswith("tpl:") and c.ua for c in members[win_key])
                    and len(fams[win_key]) < 2):
                f["ua_unverified"] = 1.0
                return {"type": "abstain"}, f
            return {"type": "unanswerable"}, f
        return {"type": win.fa[0], "answer": win.fa[1]}, f

    def _judge(self, ctx, item, spec, tctx, links, cands, pred, feats):
        (ka, ma), (kb, mb) = feats.pop("_ranked")
        a = next((c for c in ma if c.code and not c.family.startswith("tpl")), ma[0])
        b = next((c for c in mb if c.code and not c.family.startswith("tpl")), mb[0])
        user = ctx.card + "\n\n" + question_block(item, qnorm(item["question"]), tctx, links, spec, self.rb, ctx.sm) + \
            f"\n\n## CANDIDATE A ({a.family})\n{a.code}\nANSWER A: {a.fa}\n\n## CANDIDATE B ({b.family})\n{b.code}\nANSWER B: {b.fa}"
        try:
            out = self.llm.chat([{"role": "system", "content": self._system(JUDGE)}, {"role": "user", "content": user}],
                                n=1, temperature=0.0, seed=self._seed(item, "judge"))[0]
        except Exception:
            return pred, feats
        m = re.search(r"WINNER\s*:\s*([AB])", out)
        if m and m.group(1) == "B" and b.fa:
            pred = {"type": b.fa[0], "answer": b.fa[1]}
            feats["tpl_agree"] = float(any(c.family.startswith("tpl:") for c in mb))
            feats["agree"] = sum(c.weight for c in mb) / max(1e-9, sum(c.weight for c in cands))
        feats["judged"] = 1.0
        return pred, feats

    # ---------------------------------------------------------- decision --
    def decide(self, pred: dict, feats: dict) -> tuple[dict, float]:
        """Chow's rule: answer (value or unanswerable) iff calibrated P(correct) > tau."""
        D = self.cfg["decision"]
        feats.pop("_ranked", None)
        p = self.cal.p(feats) if self.cal else 0.0
        # Low-strength templates commonly contain unresolved modifiers or
        # unreliable view lineage. A constant offline prior must not make
        # these guesses automatically worth the -1 wrong-answer risk.
        if (pred.get("type") in ANSWER_TYPES and not feats.get("llm_used")
                and feats.get("tpl_agree")
                and float(feats.get("tpl_strength", 0.0)) < float(D.get("min_template_strength", 0.45))):
            return {"type": "abstain"}, min(p, 0.49)
        if pred.get("type") == "unanswerable":
            if p > D.get("tau_unanswerable", 0.5):
                out = {"type": "unanswerable"}
                if pred.get("reason"):
                    out["reason"] = str(pred["reason"])[:300]
                return out, p
            return {"type": "abstain"}, p
        if pred.get("type") in ANSWER_TYPES and p > D.get("tau_answer", 0.5):
            return {"type": pred["type"], "answer": _tidy(pred["answer"])}, p
        return {"type": "abstain"}, p


def _tidy(v):
    """41.0 -> 41 (same JSON number, cleaner file); lists / tables element-wise."""
    if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 53:
        return int(v)
    if isinstance(v, list):
        return [_tidy(x) for x in v]
    if isinstance(v, dict) and "rows" in v:
        return {**v, "rows": [[_tidy(x) for x in r] for r in v["rows"]]}
    return v
