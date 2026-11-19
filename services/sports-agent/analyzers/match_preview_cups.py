"""
services/sports-agent/analyzers/match_preview_cups.py
Backtest de copas con el modelo de clubes (mp-v1). Puro: sin Firestore, sin red, no escribe nada.

Datos:
  - Partidos de copa: hl_team_stats grupo "cups" (Highlightly, solo estadísticas; cruces entre dos
    equipos de las 8 ligas, 2020-2026). Goles del marcador del listado; córners, tarjetas
    (amarillas + rojas), remates (a puerta + fuera + bloqueados), a puerta y faltas de /statistics.
  - Historial de liga de cada equipo: CSV de football-data.co.uk (el mismo History de mp-v1).
  - Id de Highlightly → nombre de football-data: por votación sobre los partidos de liga de
    Highlightly ya cargados (hl_matches) emparejados con la fila del CSV del mismo día y división.

Qué se mide (walk-forward: cada partido solo con lo anterior a su fecha):
  - p_model: mp-v1 tal cual (ataque/defensa de cada equipo en su liga, media de la liga del local).
  (Sin factor de copa: se probó observado/esperado en las copas anteriores y no mejoró ningún
  mercado; decisión del usuario el 19-nov-2026.)
  - Referencias: (1) frecuencia de la línea en la liga del local (la de mp-v1); (2) frecuencia de
    la línea en las copas anteriores del mismo país (todas las copas si hay menos de CUP_REF_MIN).
Solo cruces de la misma división (SP1-SP2 no es comparable: cada índice es relativo a su liga) y
sin supercopas (campo neutral, un partido al año). Los dos equipos con al menos MIN_CUR partidos de
liga de la temporada (como mp-v1).
"""
from collections import Counter, defaultdict
from typing import Iterable, Optional

from analyzers import match_preview as mp

# id de copa de Highlightly → país (división principal del país en MAIN_DIVS)
CUP_COUNTRY = {122477: "ES", 39079: "EN", 41632: "EN", 117371: "IT", 69715: "DE",
               56950: "FR", 82480: "PT", 83331: "PT", 77374: "NL"}
SUPERCUPS = {473940, 450112, 466281, 450963, 448410, 468834, 462877}
COUNTRY_DIVS = {"ES": ("SP1", "SP2"), "EN": ("E0",), "IT": ("I1",), "DE": ("D1",), "FR": ("F1",),
                "PT": ("P1",), "NL": ("N1",)}
CUP_REF_MIN = 30         # copas anteriores del país necesarias para su propia referencia


def season_of(date_iso: str) -> str:
    """'2025-01-08' → '2425' (de julio en adelante, la temporada siguiente)."""
    y, m = int(date_iso[:4]), int(date_iso[5:7])
    end = y + 1 if m >= 7 else y
    return f"{str(end - 1)[-2:]}{str(end)[-2:]}"


def cup_stats(doc: dict) -> Optional[dict]:
    """Doc de hl_team_stats (grupo cups) → {'h': {stat: v}, 'a': {...}} con las STATS de mp-v1.
    None si falta el marcador o las estadísticas."""
    st = doc.get("stats") or {}
    if doc.get("goals_home") is None or doc.get("goals_away") is None or not st.get("home") or not st.get("away"):
        return None
    out = {}
    for side, key, goals in (("h", "home", doc["goals_home"]), ("a", "away", doc["goals_away"])):
        s = dict(st[key])
        # Highlightly no manda "Red cards" cuando son 0: con el resto de estadísticas presentes,
        # una roja ausente es 0 (sin esto, tarjetas solo tenía 191 de ~530 partidos).
        if s.get("reds") is None and s.get("yellows") is not None:
            s["reds"] = 0

        def tot(*fs):
            vals = [s.get(f) for f in fs]
            return None if any(v is None for v in vals) else sum(vals)

        out[side] = {"goals": goals, "corners": s.get("corners"), "cards": tot("yellows", "reds"),
                     "shots": tot("shots_on", "shots_off", "shots_blocked"), "sot": s.get("shots_on"),
                     "fouls": s.get("fouls")}
    return out


def hl_id_names(hl_league_matches: Iterable[dict], csv_by_div_day: dict[tuple[str, str], list[dict]],
                similarity, min_score: float = 0.75) -> dict[int, str]:
    """{id de Highlightly: nombre de football-data} por mayoría sobre los partidos de liga de
    Highlightly emparejados con la fila del CSV del mismo día y división."""
    votes: dict[int, Counter] = defaultdict(Counter)
    for m in hl_league_matches:
        rows = csv_by_div_day.get((m.get("div"), m.get("date")), [])
        best, score = None, 0.0
        for r in rows:
            sc = min(similarity(m.get("home_team") or "", r["home"]), similarity(m.get("away_team") or "", r["away"]))
            if sc > score:
                best, score = r, sc
        if best is None or score < min_score:
            continue
        for tid, name in ((m.get("home_team_id"), best["home"]), (m.get("away_team_id"), best["away"])):
            if tid is not None:
                votes[tid][name] += 1
    return {tid: c.most_common(1)[0][0] for tid, c in votes.items()}


def team_divs(hist: mp.History) -> dict[tuple[str, str], str]:
    """{(equipo, temporada): división principal} desde el historial de liga."""
    out = {}
    for (div, season), ms in hist.by_ds.items():
        if div not in mp.MAIN_DIVS:
            continue
        for m in ms:
            out[(m["home"], season)] = div
            out[(m["away"], season)] = div
    return out


def cup_matches(docs: Iterable[dict], id_names: dict[int, str], tdiv: dict[tuple[str, str], str],
                counts: Counter) -> list[dict]:
    """Docs de copa → partidos utilizables, ordenados por fecha. Cuenta los descartes en counts."""
    out = []
    for d in docs:
        counts["copas_leidas"] += 1
        lid = d.get("league_id")
        if lid in SUPERCUPS:
            counts["descartado_supercopa"] += 1
            continue
        country = CUP_COUNTRY.get(lid)
        if not country:
            counts["descartado_copa_desconocida"] += 1
            continue
        if d.get("status") != "done" or not d.get("finished", True):
            counts["descartado_sin_detalle"] += 1
            continue
        stats = cup_stats(d)
        st = d.get("stats") or {}
        for k in ("home", "away"):
            sd = st.get(k) or {}
            if sd.get("reds") is None and sd.get("yellows") is not None:
                counts["rojas_ausentes_contadas_0"] += 1
            if sd.get("yellows") is None:
                counts["amarillas_ausentes"] += 1
        if stats is None:
            counts["descartado_sin_estadisticas"] += 1
            continue
        home, away = id_names.get(d.get("home_team_id")), id_names.get(d.get("away_team_id"))
        if not home or not away:
            counts["descartado_equipo_sin_emparejar"] += 1
            continue
        date = (d.get("date") or "")[:10]
        season = season_of(date)
        dh, da = tdiv.get((home, season)), tdiv.get((away, season))
        if not dh or not da or dh not in COUNTRY_DIVS[country] or da not in COUNTRY_DIVS[country]:
            counts["descartado_equipo_sin_liga_esa_temporada"] += 1
            continue
        if dh != da:
            counts["descartado_distinta_division"] += 1
            continue
        out.append({"country": country, "cup": lid, "div": dh, "season": season, "date": date,
                    "home": home, "away": away, "h": stats["h"], "a": stats["a"]})
    out.sort(key=lambda m: m["date"])
    counts["copas_utilizables"] = len(out)
    return out


def _cup_ref(prior: list[dict], stat: str, side: Optional[str], line: float) -> Optional[float]:
    ys = []
    for m in prior:
        if side:
            y = m[side][stat]
        else:
            y = None if m["h"][stat] is None or m["a"][stat] is None else m["h"][stat] + m["a"][stat]
        if y is not None:
            ys.append(y > line)
    return sum(ys) / len(ys) if ys else None


def backtest_cup_rows(hist: mp.History, cups: list[dict], counts: Counter,
                      k: float = mp.SHRINK_K) -> dict[str, list[tuple]]:
    """Filas para mp.market_metrics: modelo_vs_liga y modelo_vs_copa.
    Fila: (temporada, país, mercado, línea, y, p_referencia, p_modelo, clave del partido)."""
    rows: dict[str, list[tuple]] = {"modelo_vs_liga": [], "modelo_vs_copa": []}
    done: list[dict] = []
    for m in cups:
        d, div, season = m["date"], m["div"], m["season"]
        hh = hist.team_history(m["home"], div, season, d)
        ha = hist.team_history(m["away"], div, season, d)
        if sum(1 for w, *_ in hh if w == mp.W_CUR) < mp.MIN_CUR or sum(1 for w, *_ in ha if w == mp.W_CUR) < mp.MIN_CUR:
            counts["sin_muestra_de_liga"] += 1
            done.append(m)
            continue
        counts["evaluados"] += 1
        prior_c = [p for p in done if p["country"] == m["country"]]
        prior = prior_c if len(prior_c) >= CUP_REF_MIN else done
        key = f"{season}_{m['country']}_{d}_{m['home']}"
        means = hist.league_means(div, season, d)
        for s in mp.STATS:
            lh, la = means[s]
            if m["h"][s] is None or m["a"][s] is None or not lh or not la:
                continue
            ah, dh, _ = hist.indices(hh, s, d, season, k)
            aa, da, _ = hist.indices(ha, s, d, season, k)
            lam, mu = lh * ah * da, la * aa * dh
            team_lines, total_lines = mp.LINES[s]
            r = mp.DISPERSION.get((s, "team"))
            for side, mean in (("h", lam), ("a", mu)):
                for ln in team_lines:
                    pl, _ = hist.league_line_rate(div, season, d, s, side, ln)
                    pc = _cup_ref(prior, s, side, ln) if prior else None
                    _add(rows, season, m["country"], mp.market_key(s, "team"), ln, int(m[side][s] > ln),
                         pl, pc, mp.p_over(ln, mean, r), key)
            r = mp.DISPERSION.get((s, "total"))
            yt = m["h"][s] + m["a"][s]
            for ln in total_lines:
                pl, _ = hist.league_line_rate(div, season, d, s, None, ln)
                pc = _cup_ref(prior, s, None, ln) if prior else None
                _add(rows, season, m["country"], mp.market_key(s, "total"), ln, int(yt > ln),
                     pl, pc, mp.p_over(ln, lam + mu, r), key)
        done.append(m)
    return rows


def _add(rows, season, country, mk, ln, y, pl, pc, pm, key):
    if pl is not None:
        rows["modelo_vs_liga"].append((season, country, mk, ln, y, pl, pm, key))
    if pc is not None:
        rows["modelo_vs_copa"].append((season, country, mk, ln, y, pc, pm, key))


# ── Etiquetas (decisión del usuario, 19-nov-2026) ─────────────────────────────────
LABEL_BACKTEST = "medido en backtest"     # se muestra, sin estrella
STARS_AFTER_LIVE = 50                      # cruces graduados en directo antes de pensar en estrellas


def labels_from_backtest(metrics: dict[str, dict]) -> dict[str, str]:
    """{mercado: etiqueta} con el backtest frente a la liga, todos los países juntos: "medido en
    backtest" si el IC95 de la diferencia de Brier queda entero bajo 0; si no, "solo referencia".
    Nunca "ventaja medida" (estrella): eso espera a ~STARS_AFTER_LIVE cruces graduados en directo."""
    return {v["market"]: (LABEL_BACKTEST if v["ci_label"] == mp.LABEL_EDGE else mp.LABEL_REF)
            for v in metrics.values() if v["div"] == "ALL"}


# ── Directo: graduación y estrellas (19-nov-2026) ────────────────────────────────
def cup_result(doc: Optional[dict]) -> Optional[dict]:
    """Doc de hl_team_stats (grupo cups) con detalle → resultado para grade_preview, o None.
    Mismas cuentas que el backtest (cup_stats): roja ausente con el resto presente = 0; amarilla
    ausente = sin dato (las líneas de tarjetas de ese lado no se gradúan)."""
    if not doc or doc.get("status") != "done" or not doc.get("finished", True):
        return None
    st = cup_stats(doc)
    if st is None:
        return None
    return {"h": st["h"], "a": st["a"], "date": (doc.get("date") or "")[:10]}


def rows_from_cup_grades(grades: Iterable[dict]) -> list[tuple]:
    """Filas para mp.market_metrics con el país en el sitio de la liga (como el backtest de copas)."""
    rows = []
    for g in grades:
        if g.get("status") != "graduada":
            continue
        for ln in g.get("lines") or []:
            rows.append((g.get("season"), g.get("country"), ln["market"], ln["line"], ln["y"], ln["p_league"],
                         ln["p_model"], g["match_id"]))
    return rows


def final_labels(backtest_labels: Optional[dict], live_metrics: Optional[dict],
                 n_min: int = STARS_AFTER_LIVE) -> dict[str, str]:
    """{mercado: etiqueta} de las fichas de copa. La estrella ("ventaja medida") se habilita con la
    regla de liga (IC95 por partido de la diferencia de Brier modelo − liga entero bajo 0, todos
    los países juntos) aplicada a los cruces graduados en directo, y solo cuando el mercado ya
    estaba "medido en backtest" y lleva al menos n_min cruces graduados. Si no, se queda la
    etiqueta del backtest (nunca baja de "medido en backtest" por el directo)."""
    out = dict(backtest_labels or {})
    for v in (live_metrics or {}).values():
        if v.get("div") != "ALL":
            continue
        mk = v["market"]
        if (out.get(mk) == LABEL_BACKTEST and (v.get("n_matches") or 0) >= n_min
                and v.get("ci_label") == mp.LABEL_EDGE):
            out[mk] = mp.LABEL_EDGE
    return out


def live_counts(live_metrics: Optional[dict]) -> dict[str, int]:
    """{mercado: cruces graduados en directo} (todos los países)."""
    return {v["market"]: int(v.get("n_matches") or 0) for v in (live_metrics or {}).values()
            if v.get("div") == "ALL"}


# ── Diagnóstico de amarillas ausentes (solo recuentos) ───────────────────────────
def yellow_diag(docs: Iterable[dict]) -> dict:
    """Lados de partido de copa sin "Yellow cards": ¿traen faltas, rojas, córners y cuántos campos?
    Y en los lados que sí traen amarillas, cuántos tienen 0."""
    out = Counter()
    for d in docs:
        st = d.get("stats") or {}
        for k in ("home", "away"):
            sd = st.get(k)
            if not sd:
                continue
            if sd.get("yellows") is None:
                out["sin_amarillas"] += 1
                out["sin_amarillas_con_faltas"] += sd.get("fouls") is not None
                out["sin_amarillas_con_rojas"] += sd.get("reds") is not None
                out["sin_amarillas_con_corners"] += sd.get("corners") is not None
                n = sd.get("n_fields") or 0
                out["sin_amarillas_campos_" + ("0" if n == 0 else "1-5" if n <= 5 else "6-10" if n <= 10 else "11+")] += 1
            else:
                out["con_amarillas"] += 1
                out["con_amarillas_igual_0"] += sd["yellows"] == 0
    return dict(out)
