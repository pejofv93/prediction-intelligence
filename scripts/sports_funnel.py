"""
Embudo de señales de fútbol — diagnóstico READ-ONLY.

Responde de un vistazo: de los partidos de liga recogidos, cuántos se enriquecen, cuántos
tienen cuotas, cuántos generan señal y cuántos se cortan y por qué filtro.

Contexto de por qué existe: durante la sequía de agosto-2026 hubo que reconstruir este
recuento a mano varias veces. Los cuellos son dos y conviene distinguirlos siempre:
  1. VENTANA DE CUOTAS — odds-api.io pre-carga [ahora, ahora+7d] y The Odds API omite
     ligas sin partidos en 48h. Un partido fuera de ventana no es un corte del modelo:
     es que todavía no se le han pedido cuotas.
  2. FILTROS — divergencia, gates AWAY, underdog extremo, umbral de EV. Esos sí son
     decisiones del motor y quedan registrados en la colección filter_blocks.

Uso:
    python scripts/sports_funnel.py
    python scripts/sports_funnel.py --days 7

Requiere credenciales de Firestore (ADC o GOOGLE_APPLICATION_CREDENTIALS) y
GOOGLE_CLOUD_PROJECT / FIRESTORE_COLLECTION_PREFIX como en Cloud Run.
"""
import argparse
import collections
import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, _ROOT)

from shared.firestore_client import col  # noqa: E402

# Ventanas reales del código de producción (mantener sincronizadas):
#   collectors/odds_apiio_client.py  _PREFETCH_WINDOW = 7 días
#   analyzers/value_bet_engine.py    _fetch_the_odds_api guard = 48h
_ODDSAPIIO_WINDOW_H = 168
_THE_ODDS_API_WINDOW_H = 48

FOOTBALL_LEAGUES = {"PD", "PL", "SA", "BL1", "FL1"}


def as_dt(v):
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except Exception:
            return None
    if hasattr(v, "tzinfo") and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v


def sec(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="ventana de partidos (default 7)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    today = now.date()
    win_end = today + timedelta(days=args.days)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)

    # ── Partidos de liga en ventana ──────────────────────────────────────────
    matches = []
    for d in col("upcoming_matches").stream():
        u = d.to_dict() or {}
        if u.get("league") not in FOOTBALL_LEAGUES:
            continue
        if u.get("status") not in ("SCHEDULED", "TIMED"):
            continue
        md = as_dt(u.get("match_date") or u.get("date"))
        if md and today <= md.date() <= win_end:
            matches.append((md, u))
    matches.sort(key=lambda x: x[0])

    sec(f"EMBUDO — {len(matches)} partidos de liga en los próximos {args.days} días")
    print(f"hora de referencia: {now.isoformat(timespec='seconds')}")
    print("por liga:", dict(collections.Counter(u.get("league") for _, u in matches)))

    # ── Ventana de cuotas ────────────────────────────────────────────────────
    oaio_end = now + timedelta(hours=_ODDSAPIIO_WINDOW_H)
    toa_end = now + timedelta(hours=_THE_ODDS_API_WINDOW_H)
    in_oaio = [m for m in matches if m[0] <= oaio_end]
    # The Odds API cachea la jornada ENTERA de una liga en cuanto uno de sus partidos
    # entra en 48h — por eso se cuenta por liga, no por partido.
    leagues_toa = {u.get("league") for md, u in matches if md <= toa_end}

    # "Pedible" = el motor puede conseguirle cuotas. Son dos vías, y la segunda es la que
    # más cubre: si The Odds API tiene la liga activa, cachea su JORNADA ENTERA, no solo los
    # partidos dentro de la ventana. Contar solo la ventana de 72h infraestima mucho el
    # denominador (18-ago: 2 por ventana vs 11 realmente evaluados en PD).
    pedibles = [
        (md, u) for md, u in matches
        if md <= oaio_end or u.get("league") in leagues_toa
    ]

    sec("1. VENTANA DE CUOTAS (esto NO es un corte del modelo)")
    print(f"odds-api.io  — pre-fetch {_ODDSAPIIO_WINDOW_H}h (hasta {oaio_end:%Y-%m-%d %H:%M}): "
          f"{len(in_oaio)}/{len(matches)} partidos")
    print("   por liga:", dict(collections.Counter(u.get("league") for _, u in in_oaio)))
    print(f"The Odds API — guard {_THE_ODDS_API_WINDOW_H}h (hasta {toa_end:%Y-%m-%d %H:%M}): "
          f"ligas activas {sorted(leagues_toa) or '—'}")
    print("   (una liga activa cachea su jornada entera, no solo los partidos en ventana)")
    print(f"\nCON CUOTAS PEDIBLES (union de ambas vias): {len(pedibles)}/{len(matches)}")
    print("   por liga:", dict(collections.Counter(u.get("league") for _, u in pedibles)))

    # ── Estado de enriquecimiento ────────────────────────────────────────────
    sec("2. ENRIQUECIMIENTO — de dónde sale la probabilidad del modelo")
    poisson_real = elo_only = sin_enriquecer = 0
    filas = []
    for md, u in matches:
        mid = str(u.get("match_id", ""))
        snap = col("enriched_matches").document(mid).get()
        e = (snap.to_dict() or {}) if snap.exists else None
        if e is None:
            sin_enriquecer += 1
            origen = "SIN ENRIQUECER"
        elif e.get("poisson_home_win") is not None:
            poisson_real += 1
            origen = "poisson"
        else:
            elo_only += 1
            origen = "ELO"
        filas.append((md, u, e or {}, origen))
    print(f"enriquecidos con Poisson real : {poisson_real}")
    print(f"solo ELO (arranque temporada) : {elo_only}")
    print(f"sin enriquecer                : {sin_enriquecer}")

    # ── Señales emitidas hoy ─────────────────────────────────────────────────
    sec("3. SEÑALES EMITIDAS HOY")
    preds = []
    for d in col("predictions").stream():
        p = d.to_dict() or {}
        t = as_dt(p.get("created_at"))
        if t and t >= day_start:
            preds.append((t, p))
    preds.sort(key=lambda x: x[0])
    print(f"total: {len(preds)}")
    for t, p in preds:
        print(f"  {t:%H:%M:%S} {p.get('league'):>4} {p.get('home_team')} vs {p.get('away_team')}")
        print(f"       back={p.get('team_to_back')} mkt={p.get('market_type')} "
              f"odds={p.get('odds')} edge={p.get('edge')} conf={p.get('confidence')}")
    if preds:
        print("\npor liga:", dict(collections.Counter(p.get("league") for _, p in preds)))

    # ── Cortes de filtro hoy ─────────────────────────────────────────────────
    sec("4. CORTES DE FILTRO HOY (esto SÍ son decisiones del motor)")
    blocks = []
    for d in col("filter_blocks").stream():
        b = d.to_dict() or {}
        t = as_dt(b.get("blocked_at"))
        if t and t >= day_start:
            blocks.append((t, b))
    blocks.sort(key=lambda x: x[0])
    print(f"total: {len(blocks)}")
    print("por filtro:", dict(collections.Counter(b.get("filter_name") for _, b in blocks)))
    print("por liga  :", dict(collections.Counter(b.get("league") for _, b in blocks)))
    for t, b in blocks:
        print(f"  {t:%H:%M:%S} {b.get('filter_name'):<20} {b.get('league'):>4} "
              f"{b.get('home_team')} vs {b.get('away_team')} | back={b.get('team_to_back')} "
              f"odds={b.get('odds')} conf={b.get('confidence')}")

    # ── Detalle por partido ──────────────────────────────────────────────────
    sec("5. DETALLE POR PARTIDO")
    emitidos = {str(p.get("match_id", "")) for _, p in preds}
    bloqueados = collections.defaultdict(list)
    for _, b in blocks:
        bloqueados[str(b.get("match_id", ""))].append(b.get("filter_name"))
    for md, u, e, origen in filas:
        mid = str(u.get("match_id", ""))
        if mid in emitidos:
            estado = "SEÑAL"
        elif mid in bloqueados:
            estado = "cortado: " + ",".join(bloqueados[mid])
        elif (md, u) not in pedibles:
            estado = "fuera de ventana de cuotas"
        else:
            estado = "evaluado, sin señal (edge/umbral)"
        print(f"  {u.get('league'):>4} {md:%d-%b %H:%M} "
              f"{str(u.get('home_team'))[:22]:22s} vs {str(u.get('away_team'))[:22]:22s} "
              f"| {origen:<14} | {estado}")

    # ── Resumen ──────────────────────────────────────────────────────────────
    sec("RESUMEN")
    print(f"partidos de liga en ventana {args.days}d : {len(matches)}")
    print(f"  con cuotas pedibles                  : {len(pedibles)}")
    print(f"  señales emitidas hoy                 : {len(preds)}")
    print(f"  cortes de filtro hoy                 : {len(blocks)}")
    if pedibles:
        print(f"  tasa de emisión sobre los pedibles   : {len(preds)}/{len(pedibles)} "
              f"= {100.0 * len(preds) / len(pedibles):.1f}%")
    faltan = len(matches) - len(pedibles)
    if faltan:
        print(f"\n  OJO: {faltan} partidos siguen fuera de ventana de cuotas — la muestra")
        print("       aún no es completa. Repetir cuando ese número baje a 0 o casi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
