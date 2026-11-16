"""One statistics module for every learning loop on the desk.

Before this, each loop judged itself its own way: Structure used date-clustered
excess return, the Brief raw accuracy plus a Brier score, Flow a binomial z, 0DTE
plain reject/breach rates. None was wrong alone, but "68% wins", "z = 2.1" and
"48% accurate" cannot be read side by side, and three of the four overstated their
certainty. Everything here answers ONE question the same way for all of them:

    Is this cohort's edge different from zero, and how sure are we?

An *edge* is a per-observation number that is zero in expectation when the signal
carries no information (a return net of its benchmark or drift; a hit minus the
rate you would get without the signal). A cohort is judged on the mean of its
edges, with an error bar that respects the three ways trading rows lie about how
many independent observations they are:

  1. Same-day rows move together. Three index ETFs on one date, or two hundred
     setups screened on one close, are closer to one draw than to many. So errors
     are clustered by date.
  2. Overlapping horizons. A 10-day outcome logged today shares nine days with
     the one logged tomorrow. So the date-level sums get their autocovariances
     added out to `overlap` dates (Hansen-Hodrick: unweighted, because the
     dependence is known to end there), and the degrees of freedom shrink to the
     number of non-overlapping windows. Checked by simulation (see the tests):
     on 10-day outcomes with no true edge, plain date-clustering "finds" one at
     the 10% level about 58% of the time; this finds one 10-13% of the time.
  3. Many tests. Fifty feature splits will throw up a few "significant" ones by
     chance. So verdicts are taken on Benjamini-Hochberg q-values within a family
     of related tests, not on raw p-values.

Verdict vocabulary (the only four words any loop may use):

  paying        the edge is positive and survives all three corrections
  inverted      the edge is reliably NEGATIVE — the signal is wrong more than
                chance, which is information (fade it, or fix it)
  noise         judged on enough independent evidence; no edge detected (the
                confidence interval says how large one could still be hiding)
  accumulating  not enough independent evidence yet to say anything

Pure stdlib — no numpy/pandas — so the flow path and the desk can import it cheaply.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

# Evidence a cohort needs before it is judged at all (below this: accumulating).
MIN_N = 30                 # observations
MIN_CLUSTERS = 15          # distinct dates
MIN_EFF_CLUSTERS = 10.0    # dates / (overlap + 1): non-overlapping windows
FDR = 0.10                 # Benjamini-Hochberg false-discovery rate within a family
CI_LEVEL = 0.90            # two-sided confidence level reported with every effect

PAYING, INVERTED, NOISE, ACCUMULATING = "paying", "inverted", "noise", "accumulating"

VERDICTS = {
    PAYING: "Edge is positive and holds up after clustering by date, correcting for "
            "overlapping horizons and for the number of related tests.",
    INVERTED: "Edge is reliably negative: the signal is wrong more often than chance. "
              "That is information — fade it or fix it.",
    NOISE: "Judged on enough independent evidence; no edge detected. The confidence "
           "interval shows how large an edge could still be hiding.",
    ACCUMULATING: "Not enough independent evidence yet to judge.",
}


# ---------------------------------------------------------------------------
# Student-t tail (regularized incomplete beta, continued fraction)
# ---------------------------------------------------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-13:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
          + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln) * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float | None:
    """P(|T| >= |t|) for Student's t with `df` degrees of freedom (df may be
    fractional — the overlap correction produces non-integer df)."""
    if t is None or df is None or df <= 0 or math.isnan(t):
        return None
    if math.isinf(t):
        return 0.0
    return betainc(df / 2.0, 0.5, df / (df + t * t))


def t_crit(df: float, level: float = CI_LEVEL) -> float | None:
    """Two-sided critical value: the t with P(|T| >= t) = 1 - level."""
    if df is None or df <= 0:
        return None
    alpha, lo, hi = 1.0 - level, 0.0, 1e4
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if t_two_sided_p(mid, df) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ---------------------------------------------------------------------------
# Effects with honest error bars
# ---------------------------------------------------------------------------

@dataclass
class Effect:
    n: int = 0                       # observations
    n_clusters: int = 0              # distinct dates
    eff_clusters: float = 0.0        # dates / (overlap + 1)
    effect: float | None = None      # the estimate (mean edge, or hi-minus-lo gap)
    se: float | None = None
    t: float | None = None
    df: float | None = None
    p: float | None = None           # two-sided, before the family correction
    ci_low: float | None = None
    ci_high: float | None = None

    def as_dict(self, ndigits: int = 4) -> dict:
        """JSON-safe: rounded, and a non-finite value (a t of ±inf when every date
        agrees exactly) becomes null rather than the invalid token `Infinity`."""
        out = asdict(self)
        for k, v in out.items():
            if isinstance(v, float):
                out[k] = round(v, 6 if k == "p" else ndigits) if math.isfinite(v) else None
        return out


def _finalize(n: int, estimate: float, infl: list, clusters: list,
              overlap: int, order: list | None) -> Effect:
    """Shared tail of every estimator. `infl[i]` is observation i's influence on
    the estimate (so the estimate's sampling error is the sum of the influences);
    summing them within a date, then adding the autocovariances of those date
    sums out to `overlap` lags, gives the cluster- and overlap-robust variance."""
    sums: dict = {}
    for v, c in zip(infl, clusters):
        sums[c] = sums.get(c, 0.0) + v
    g = len(sums)
    overlap = max(0, int(overlap))
    eff = g / (overlap + 1.0)
    out = Effect(n=n, n_clusters=g, eff_clusters=eff, effect=estimate)
    if g < 2:
        return out
    # Dates in calendar order. Passing the loop's full date list as `order` keeps
    # the lag spacing right when this cohort has no rows on some dates.
    seq = [sums.get(k, 0.0) for k in (order if order is not None else sorted(sums))]
    naive = sum(s * s for s in seq)
    var = naive
    for k in range(1, overlap + 1):
        var += 2.0 * sum(seq[i] * seq[i - k] for i in range(k, len(seq)))
    # Never tighter than plain date-clustering (the unweighted kernel can dip
    # below it, or below zero, by chance), and scaled for the 2*overlap+1
    # autocovariances estimated from only g dates. At overlap 0 this is exactly
    # the usual g/(g-1) cluster correction.
    var = max(var, naive) * g / max(g - (2.0 * overlap + 1.0), 1.0)
    se = math.sqrt(var)
    out.se = se
    out.df = max(eff - 1.0, 0.0)
    if se > 0:
        out.t = estimate / se
        out.p = t_two_sided_p(out.t, out.df) if out.df > 0 else None
    else:                                  # no variation at all between dates
        out.t = 0.0 if estimate == 0 else math.copysign(math.inf, estimate)
        out.p = 1.0 if estimate == 0 else 0.0
    tc = t_crit(out.df) if out.df > 0 else None
    if tc is not None:
        out.ci_low, out.ci_high = estimate - tc * se, estimate + tc * se
    return out


def _clean(values, clusters, extra=None):
    keep = [i for i, v in enumerate(values)
            if v is not None and not (isinstance(v, float) and math.isnan(v))]
    vs = [float(values[i]) for i in keep]
    cs = [clusters[i] for i in keep]
    return (vs, cs) if extra is None else (vs, cs, [extra[i] for i in keep])


def mean_effect(values, clusters, overlap: int = 0, order: list | None = None) -> Effect:
    """Mean of `values` (per-observation edges) with a date-clustered,
    overlap-corrected error bar. `clusters[i]` is observation i's date."""
    vs, cs = _clean(values, clusters)
    n = len(vs)
    if n == 0:
        return Effect()
    m = sum(vs) / n
    return _finalize(n, m, [(v - m) / n for v in vs], cs, overlap, order)


def gap_effect(values, hi_mask, clusters, overlap: int = 0,
               order: list | None = None) -> Effect:
    """mean(values[hi]) - mean(values[~hi]) with the same robust error bar. The two
    halves share dates, so their means are correlated; the influence form handles
    that where sqrt(se_hi^2 + se_lo^2) would not."""
    vs, cs, hs = _clean(values, clusters, hi_mask)
    hi = [v for v, h in zip(vs, hs) if h]
    lo = [v for v, h in zip(vs, hs) if not h]
    if not hi or not lo:
        return Effect(n=len(vs), n_clusters=len(set(cs)))
    mh, ml = sum(hi) / len(hi), sum(lo) / len(lo)
    infl = [(v - mh) / len(hi) if h else -(v - ml) / len(lo) for v, h in zip(vs, hs)]
    return _finalize(len(vs), mh - ml, infl, cs, overlap, order)


# ---------------------------------------------------------------------------
# Descriptive helpers (shown beside an effect, never used for the verdict)
# ---------------------------------------------------------------------------

def wilson(k: int, n: int, z: float = 1.645) -> tuple | None:
    """Wilson score interval for a proportion (z=1.645 -> 90%). Descriptive only:
    it treats rows as independent, which the verdict's clustered error does not."""
    if not n:
        return None
    p = k / n
    den = 1.0 + z * z / n
    mid = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4.0 * n * n)) / den
    return (max(0.0, mid - half), min(1.0, mid + half))


def brier(probs, outcomes) -> float | None:
    """Mean squared error of stated probabilities (0.25 = always saying 50/50)."""
    pairs = [(float(p), float(o)) for p, o in zip(probs, outcomes)
             if p is not None and o is not None]
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def median_split(xs) -> tuple:
    """(threshold, mask) with mask[i] True for the upper half. Ties at the median
    go low, so a feature that is mostly one value does not produce a fake split."""
    vals = sorted(x for x in xs if x is not None)
    if not vals:
        return None, [False] * len(xs)
    med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2.0
    return med, [x is not None and x > med for x in xs]


# ---------------------------------------------------------------------------
# Family-wise verdicts
# ---------------------------------------------------------------------------

def bh_qvalues(ps: list) -> list:
    """Benjamini-Hochberg q-values. None entries (untested cohorts) stay None and
    do not count toward the number of tests."""
    idx = sorted((i for i, p in enumerate(ps) if p is not None), key=lambda i: ps[i])
    m = len(idx)
    out: list = [None] * len(ps)
    running = 1.0
    for rank in range(m, 0, -1):
        i = idx[rank - 1]
        running = min(running, ps[i] * m / rank)
        out[i] = running
    return out


def ready(e: Effect) -> bool:
    """Has this cohort enough independent evidence to be judged at all?"""
    return (e is not None and e.se is not None and e.p is not None
            and e.n >= MIN_N and e.n_clusters >= MIN_CLUSTERS
            and e.eff_clusters >= MIN_EFF_CLUSTERS)


def shortfall(e: Effect) -> dict | None:
    """What an accumulating cohort still needs before it can be judged: more
    observations and/or more session dates (overlapping horizons need
    proportionally more dates). None when it is already ready."""
    if ready(e):
        return None
    g = e.n_clusters if e is not None else 0
    n = e.n if e is not None else 0
    span = g / e.eff_clusters if g and e.eff_clusters else 1.0      # overlap + 1
    dates = max(MIN_CLUSTERS - g, math.ceil(MIN_EFF_CLUSTERS * span) - g, 0)
    return {"observations": max(MIN_N - n, 0), "dates": int(dates)}


def judge(effects: list) -> list:
    """Verdicts for one family of related tests: [(verdict, q), ...] in order.
    Only cohorts that are ready count as tests; the rest are `accumulating`."""
    qs = bh_qvalues([e.p if ready(e) else None for e in effects])
    out = []
    for e, q in zip(effects, qs):
        if q is None:
            out.append((ACCUMULATING, None))
        elif q <= FDR and e.effect:
            out.append((PAYING if e.effect > 0 else INVERTED, q))
        else:
            out.append((NOISE, q))
    return out
