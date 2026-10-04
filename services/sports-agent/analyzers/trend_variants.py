"""
services/sports-agent/analyzers/trend_variants.py

Variantes de las reglas de racha del feed de tendencias que se prueban EN SOMBRA:
se generan para todos los partidos que mira el feed, se guardan en trend_signals con
shadow=True y su rule_id, el grader las gradúa igual que las demás y no se envían ni
cuentan en el acierto global, la calibración, el dashboard ni el resumen semanal (que
las muestra aparte, en "Probando").

Las variantes son las ganadoras del backtest (scripts/trend_variant_backtest.py, menú
3fdd95f9…), con las condiciones copiadas literalmente. Junto a ellas corre en sombra la
regla actual (v1) sobre los mismos partidos: lo que se envía es solo el top del día, así
que la comparación justa es variante frente a v1 en sombra.

Promoción (evaluate_promotions, al final del grader): una variante queda LISTA con
TREND_PROMOTION_MIN_SHADOW graduadas en sombra (y v1 en sombra también), acierto >=
objetivo del mercado y mejor que v1 en sombra. NUNCA se promueve sola: cada variante LISTA
se avisa en el resumen semanal y solo se aplica cuando el usuario la confirma
(confirmed=True en trend_rule_promotions/{mercado}), una por una.
Promover = trend_market_rules/{mercado}.active_rule; borrar ese doc vuelve a v1.
"""
import logging
from datetime import datetime, timezone

from shared.config import TREND_MARKET_TARGET, TREND_PROMOTION_MIN_SHADOW

logger = logging.getLogger(__name__)

# Condiciones como en el backtest: who = team | opp (mercados de equipo) ·
# any | both | home | away (Ambos marcan, de partido) · venue = solo partidos en la
# misma condición.
SHADOW_VARIANTS: dict[str, list[dict]] = {
    "btts": [
        {"id": "v1", "desc": "un equipo con Ambos marcan >=70% (10)",
         "conds": [{"who": "any", "stat": "btts", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "B6", "desc": "los dos marcan >=80% y los dos encajan >=80% (10)",
         "conds": [{"who": "both", "stat": "score", "rate": 0.80, "W": 10, "min_n": 8},
                   {"who": "both", "stat": "concede", "rate": 0.80, "W": 10, "min_n": 8}]},
    ],
    "team_goals_over": [
        {"id": "v1", "desc": "equipo marca 2+ >=70% (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "G5", "desc": "equipo 2+ >=70% y rival encaja 2+ >=50% (10)",
         "conds": [{"who": "team", "stat": "goals2", "rate": 0.70, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "concede2", "rate": 0.50, "W": 10, "min_n": 8}]},
    ],
    "team_goals_over_3": [
        {"id": "v1", "desc": "equipo marca 3+ >=70% (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "T5", "desc": "equipo 3+ >=50% y rival encaja 3+ >=30% (10)",
         "conds": [{"who": "team", "stat": "goals3", "rate": 0.50, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "concede3", "rate": 0.30, "W": 10, "min_n": 8}]},
    ],
    "handicap": [
        {"id": "v1", "desc": "gana por 2+ >=70% (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.70, "W": 10, "min_n": 8}]},
        {"id": "H5", "desc": "gana por 2+ >=50% y rival pierde por 2+ >=30% (10)",
         "conds": [{"who": "team", "stat": "win2", "rate": 0.50, "W": 10, "min_n": 8},
                   {"who": "opp", "stat": "lose2", "rate": 0.30, "W": 10, "min_n": 8}]},
    ],
}
MATCH_LEVEL = {"btts"}
_THRESHOLD = {"team_goals_over": 2, "team_goals_over_3": 3, "handicap": 2, "btts": None}

_STATS = {
    "btts": lambda gf, ga: gf > 0 and ga > 0,
    "score": lambda gf, ga: gf > 0,
    "concede": lambda gf, ga: ga > 0,
    "goals2": lambda gf, ga: gf >= 2,
    "goals3": lambda gf, ga: gf >= 3,
    "win2": lambda gf, ga: gf - ga >= 2,
    "concede2": lambda gf, ga: ga >= 2,
    "concede3": lambda gf, ga: ga >= 3,
    "lose2": lambda gf, ga: ga - gf >= 2,
}


def team_history(raw_matches: list[dict], team_id) -> list[tuple]:
    """raw_matches → [(fecha, gf, ga, en_casa)] en orden cronológico (más antiguo primero)."""
    tid = str(team_id)
    out = []
    for m in raw_matches or []:
        gh, ga = m.get("goals_home"), m.get("goals_away")
        if gh is None or ga is None:
            continue
        home = str(m.get("home_team_id")) == tid
        gf, gc = (gh, ga) if home else (ga, gh)
        out.append((str(m.get("date") or m.get("match_date") or "")[:10], int(gf), int(gc), home))
    out.sort(key=lambda h: h[0])
    return out


def _cond_rate(hist: list[tuple], cond: dict, at_home: bool) -> tuple[float, int] | None:
    if cond.get("venue"):
        hist = [h for h in hist if h[3] == at_home]
    window = hist[-cond["W"]:]
    if len(window) < cond["min_n"]:
        return None
    fn = _STATS[cond["stat"]]
    return sum(1 for _, gf, ga, _h in window if fn(gf, ga)) / len(window), len(window)


def _evaluate(variant: dict, t_hist, o_hist, t_home: bool, match_level: bool) -> tuple[float, int] | None:
    """(rate de la primera condición, partidos) si la variante dispara; si no, None."""
    first = None
    for c in variant["conds"]:
        who = c["who"]
        if match_level:  # t_hist = local, o_hist = visitante
            h = _cond_rate(t_hist, c, True)
            a = _cond_rate(o_hist, c, False)
            h_ok, a_ok = bool(h and h[0] >= c["rate"]), bool(a and a[0] >= c["rate"])
            ok = {"any": h_ok or a_ok, "both": h_ok and a_ok, "home": h_ok, "away": a_ok}[who]
            r = max((x for x in (h, a) if x), default=None, key=lambda x: x[0])
        else:
            r = _cond_rate(t_hist if who == "team" else o_hist, c,
                           t_home if who == "team" else not t_home)
            ok = bool(r and r[0] >= c["rate"])
        if not ok:
            return None
        first = first or r
    return first


def variant_candidates(enriched: dict, home_stats: dict, away_stats: dict,
                       skip_rules: set[str] = frozenset()) -> list[dict]:
    """Candidatos de todas las variantes del menú para un partido (rule_id en cada uno)."""
    home_team, away_team = enriched.get("home_team", ""), enriched.get("away_team", "")
    home_id, away_id = enriched.get("home_team_id"), enriched.get("away_team_id")
    if not home_id or not away_id:
        return []
    hh = team_history(home_stats.get("raw_matches"), home_id)
    ah = team_history(away_stats.get("raw_matches"), away_id)

    out: list[dict] = []
    for market, variants in SHADOW_VARIANTS.items():
        for v in variants:
            rule_id = f"{market}.{v['id']}"
            if rule_id in skip_rules:
                continue
            if market in MATCH_LEVEL:
                sides = [("match", f"{home_team}/{away_team}", None, hh, ah, True)]
            else:
                sides = [("home", home_team, home_id, hh, ah, True),
                         ("away", away_team, away_id, ah, hh, False)]
            for side, team, team_id, t_hist, o_hist, t_home in sides:
                r = _evaluate(v, t_hist, o_hist, t_home, market in MATCH_LEVEL)
                if r is None:
                    continue
                rate, n = r
                out.append({
                    "market": market, "rule_id": rule_id, "variant": v["id"],
                    "pattern_type": "series", "threshold": _THRESHOLD[market],
                    "team": team, "team_id": team_id, "side": side,
                    "opponent": "" if market in MATCH_LEVEL else (away_team if side == "home" else home_team),
                    "sample": n, "rate": round(rate, 4),
                    "label": v["desc"], "detail": f"{team}: {v['desc']}",
                    "line": f"{team} — {v['desc']}",
                })
    return out


# ── Reglas activas y promoción ─────────────────────────────────────────────────

_ACTIVE_CACHE: tuple[datetime, dict[str, str]] | None = None


def active_rules() -> dict[str, str]:
    """{mercado: id de variante} de los mercados con una variante promovida (cache 1h)."""
    global _ACTIVE_CACHE
    now = datetime.now(timezone.utc)
    if _ACTIVE_CACHE and (now - _ACTIVE_CACHE[0]).total_seconds() < 3600:
        return _ACTIVE_CACHE[1]
    rules: dict[str, str] = {}
    try:
        from shared.firestore_client import col
        for d in col("trend_market_rules").stream():
            rule = (d.to_dict() or {}).get("active_rule")
            if rule and rule != "v1" and any(v["id"] == rule for v in SHADOW_VARIANTS.get(d.id, [])):
                rules[d.id] = rule
    except Exception:
        logger.error("trend_variants: error leyendo trend_market_rules", exc_info=True)
    _ACTIVE_CACHE = (now, rules)
    return rules


def _shadow_tally(rows: list[dict], rule_id: str) -> tuple[int, int]:
    graded = [r for r in rows if r.get("rule_id") == rule_id and r.get("result") in ("hit", "miss")]
    return sum(1 for r in graded if r.get("result") == "hit"), len(graded)


def promotion_status(shadow_rows: list[dict], active: dict[str, str]) -> list[dict]:
    """Estado de cada variante candidata (función pura): testing | ready | promoted."""
    out = []
    for market, variants in SHADOW_VARIANTS.items():
        v1_hits, v1_n = _shadow_tally(shadow_rows, f"{market}.v1")
        target = TREND_MARKET_TARGET[market]
        for v in variants:
            if v["id"] == "v1":
                continue
            hits, n = _shadow_tally(shadow_rows, f"{market}.{v['id']}")
            rate = hits / n if n else None
            v1_rate = v1_hits / v1_n if v1_n else None
            if active.get(market) == v["id"]:
                status = "promoted"
            elif (n >= TREND_PROMOTION_MIN_SHADOW and v1_n >= TREND_PROMOTION_MIN_SHADOW
                  and rate >= target and rate > v1_rate):  # v1 con muestra propia: comparación real
                status = "ready"
            else:
                status = "testing"
            out.append({"market": market, "variant": v["id"], "desc": v["desc"], "target": target,
                        "hits": hits, "n": n, "rate": rate, "v1_hits": v1_hits, "v1_n": v1_n,
                        "v1_rate": v1_rate, "status": status})
    return out


async def evaluate_promotions() -> dict:
    """Recalcula el estado de las variantes y promueve solo las LISTAS que el usuario
    ha confirmado una a una. No hay promoción automática."""
    from shared.firestore_client import col

    try:
        shadow_rows = [r for r in (d.to_dict() or {} for d in col("trend_accuracy_log").stream())
                       if r.get("shadow")]
    except Exception:
        logger.error("trend_variants: error leyendo datos de promoción", exc_info=True)
        return {"error": "read_failed"}

    global _ACTIVE_CACHE
    _ACTIVE_CACHE = None
    active = active_rules()
    now = datetime.now(timezone.utc)
    promoted: list[str] = []
    for st in promotion_status(shadow_rows, active):
        ref = col("trend_rule_promotions").document(st["market"])
        try:
            prev = ref.get()
            prev_d = (prev.to_dict() or {}) if prev.exists else {}
            # Un doc por mercado: la mejor candidata manda (la lista por encima de la que prueba).
            if prev_d.get("variant") not in (None, st["variant"]) and prev_d.get("status") == "ready" \
                    and st["status"] == "testing":
                continue
            confirmed = bool(prev_d.get("confirmed")) and prev_d.get("variant") == st["variant"]
            doc = {**st, "confirmed": confirmed, "updated_at": now.isoformat()}
            if st["status"] == "ready" and confirmed:
                col("trend_market_rules").document(st["market"]).set({
                    "active_rule": st["variant"], "previous_rule": "v1", "promoted_at": now.isoformat(),
                    "evidence": {k: st[k] for k in ("hits", "n", "rate", "v1_hits", "v1_n", "v1_rate")},
                })
                doc["status"] = "promoted"
                doc["promoted_at"] = now.isoformat()
                promoted.append(f"{st['market']}.{st['variant']}")
                logger.info("trend_variants: PROMOVIDA %s.%s (%d/%d)",
                            st["market"], st["variant"], st["hits"], st["n"])
            ref.set(doc)
        except Exception:
            logger.error("trend_variants: error evaluando promoción %s", st["market"], exc_info=True)
    _ACTIVE_CACHE = None
    return {"promoted": promoted}
