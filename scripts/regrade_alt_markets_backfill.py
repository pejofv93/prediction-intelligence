"""
scripts/regrade_alt_markets_backfill.py

Backfill para re-graduar predicciones de mercados alternativos de fútbol
(totals / asian_handicap / btts / correct_score) que quedaron mal graduadas
por el bug de coincidencia "" == "" en evaluate_prediction (sesión 2026-08-25,
ver services/sports-agent/learner/learning_engine.py — commit del guard).

Causa raíz: esos mercados nunca guardan `team_to_back` ni `home_team_id`/
`away_team_id`. Antes del guard, cuando no había marcador disponible
(check_score() no existía o falló) evaluate_prediction caía en la rama
clásica, comparaba "" (backed) contra "" (home_id) — "" == "" es True por
coincidencia — y `correct` terminaba siendo literalmente "¿ganó el local?",
ignorando la selección real (Over/Under, lado del hándicap, marcador exacto).

Alcance: predicciones sport=football, market_type en
{totals, asian_handicap, btts, correct_score}, ya resueltas (correct es bool)
y SIN campo `model_epoch` — es decir, graduadas antes de que existiera el
sistema de épocas (MODEL_EPOCH_PRE_ELO_REBUILD), por lo que nunca estuvieron
protegidas de contaminar pesos/accuracy/shadow_trades.

Para cada candidato:
  1. Intenta re-graduar con el marcador real (match_results.goals_home/away),
     igual que evaluate_prediction._grade_with_score en producción.
  2. Si no hay marcador (típico en IDs WC26_* sin doc en match_results) pero es
     asian_handicap con línea ±0.5 o 0.0, se resuelve SIN marcador: un ±0.5
     nunca empata (empate = pierde el lado que respalda), un 0.0 empata en
     push. Es exacto, no una aproximación.
  3. Si no se puede verificar de ninguna forma (ej. líneas de cuarto ±0.25/
     ±0.75/±1.25 en WC26 sin match_results), se deja `correct` como está y se
     reporta como NO VERIFICABLE — no se adivina.

En todos los casos (verificados o no) el doc se marca con
model_epoch=1 (MODEL_EPOCH_PRE_ELO_REBUILD) + excluded_from_learning=True,
igual que se hizo con la reconstrucción del ELO: no se intenta recalcular los
pesos del ensemble (poisson/elo/form/h2h) porque no hay ledger para reproducir
el orden exacto de todas las actualizaciones de esa época — se marca la época
como contaminada y se excluye hacia adelante, en vez de fingir precisión que
no existe.

Se corrige además:
  - El shadow_trade asociado (signal_id=match_id, source=sports): result
    (win/loss/void) y pnl_virtual — solo si el valor de `correct` cambió.
    calculate_metrics() lee shadow_trades en vivo, así que ROI/bankroll/win
    rate se autocorrigen sin tocar nada más.
  - model_weights/current.total_predictions/.correct_predictions NO se tocan:
    ese contador arrancó de nuevo en la reconstrucción del ELO (2026-08-19,
    model_weights/elo_rebuild.rebuilt_at) y solo suma predicciones de la época
    ACTUAL — los docs de este backfill son de época legacy (mayo/junio) y
    nunca lo incrementaron, así que restarles su contribución ahí resta de
    algo que nunca los tuvo (lo comprobí en producción: estaba en 10/7, no en
    un acumulado histórico grande — un primer intento de "corregirlo" lo dejó
    en 0/0 y hubo que revertirlo a mano, sesión 2026-08-25).
  - accuracy_log/{semana}.predictions_total/.predictions_correct/.accuracy —
    este SÍ es un ledger histórico real por semana (createTime de mayo/junio,
    no tocado por el rebuild), ahí sí corresponde el delta.
    agrupando cada doc por la semana ISO de su updateTime original (la semana
    en la que se graduó, que es la que ese contador incrementó en su momento).
  - accuracy_by_market / accuracy_by_league (en model_weights/current y en
    accuracy_log) NO se tocan: son una foto del último batch diario que las
    escribió, no un acumulado — no hay forma de invertir con precisión una
    tasa ya promediada sin saber su n original. Se diluyen solas en cada
    corrida diaria posterior (ya llevan ~2 meses de corridas encima).

Uso:
    python3 scripts/regrade_alt_markets_backfill.py            # dry-run
    python3 scripts/regrade_alt_markets_backfill.py --apply    # escribe

Requiere:
    - gcloud auth print-access-token --account pejocanal@gmail.com (activo)
"""

import json
import logging
import subprocess
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("regrade")

APPLY = "--apply" in sys.argv

PROJECT = "prediction-intelligence"
PREFIX = "prod"
BASE = f"https://firestore.googleapis.com/v1/projects/{PROJECT}/databases/(default)/documents"
ACCOUNT = "pejocanal@gmail.com"

MODEL_EPOCH_PRE_ELO_REBUILD = 1  # ver learning_engine.py — mismo valor, misma semántica
_TARGET_MARKETS = ["totals", "asian_handicap", "btts", "correct_score"]


# ── Auth / REST helpers (mismo patrón que backfill_predictions_result.py) ─────

def _get_token() -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", f"--account={ACCOUNT}"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(f"No se pudo obtener token para {ACCOUNT}. Comprueba: gcloud auth list")
    return token


_TOKEN_CACHE = [None]


def _tok() -> str:
    if not _TOKEN_CACHE[0]:
        _TOKEN_CACHE[0] = _get_token()
    return _TOKEN_CACHE[0]


def _request(url: str, method: str = "GET", body: dict | None = None) -> dict:
    """
    Via `curl` (no urllib): en este entorno Windows, urllib/ssl no confía en la CA que
    Norton inyecta en el tráfico HTTPS (SSLCertVerificationError), mientras que curl sí
    usa el almacén de certificados de Windows y funciona sin cambios — ver
    feedback_gcloud_ssl_norton.md. Evita depender de reconstruir el cert bundle de Python.
    """
    cmd = ["curl", "-s", "-X", method, "-H", f"Authorization: Bearer {_tok()}",
           "-H", "Content-Type: application/json"]
    if body is not None:
        cmd += ["-d", json.dumps(body)]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"curl failed ({result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


def _get_doc(collection: str, doc_id: str) -> dict | None:
    url = f"{BASE}/{PREFIX}{collection}/{doc_id}"
    resp = _request(url)
    if isinstance(resp, dict) and resp.get("error", {}).get("code") == 404:
        return None
    return resp


def _query_market(market_type: str) -> list[dict]:
    body = {
        "structuredQuery": {
            "from": [{"collectionId": f"{PREFIX}predictions"}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {"fieldFilter": {"field": {"fieldPath": "market_type"}, "op": "EQUAL",
                                         "value": {"stringValue": market_type}}},
                        {"fieldFilter": {"field": {"fieldPath": "sport"}, "op": "EQUAL",
                                         "value": {"stringValue": "football"}}},
                    ],
                }
            },
            "limit": 300,
        }
    }
    results = _request(f"{BASE}:runQuery", method="POST", body=body)
    return [r["document"] for r in results if "document" in r]


def _fv(fields: dict, key: str):
    v = fields.get(key, {})
    if "stringValue" in v:
        return v["stringValue"]
    if "booleanValue" in v:
        return v["booleanValue"]
    if "integerValue" in v:
        return int(v["integerValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "nullValue" in v:
        return None
    if "mapValue" in v:
        return v["mapValue"].get("fields", {})
    return None


def _to_fs_value(val) -> dict:
    if val is None:
        return {"nullValue": None}
    if isinstance(val, bool):
        return {"booleanValue": val}
    if isinstance(val, int):
        return {"integerValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    if isinstance(val, datetime):
        dt = val if val.tzinfo else val.replace(tzinfo=timezone.utc)
        return {"timestampValue": dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    return {"stringValue": str(val)}


def _patch_doc(collection: str, doc_id: str, fields: dict) -> None:
    update_mask = "&".join(f"updateMask.fieldPaths={k}" for k in fields)
    url = f"{BASE}/{PREFIX}{collection}/{doc_id}?{update_mask}"
    fs_fields = {k: _to_fs_value(v) for k, v in fields.items()}
    _request(url, method="PATCH", body={"fields": fs_fields})


def _query_shadow_trade(match_id: str) -> tuple[str, dict] | None:
    body = {
        "structuredQuery": {
            "from": [{"collectionId": f"{PREFIX}shadow_trades"}],
            "where": {
                "compositeFilter": {
                    "op": "AND",
                    "filters": [
                        {"fieldFilter": {"field": {"fieldPath": "signal_id"}, "op": "EQUAL",
                                         "value": {"stringValue": match_id}}},
                        {"fieldFilter": {"field": {"fieldPath": "source"}, "op": "EQUAL",
                                         "value": {"stringValue": "sports"}}},
                    ],
                }
            },
            "limit": 1,
        }
    }
    results = _request(f"{BASE}:runQuery", method="POST", body=body)
    docs = [r["document"] for r in results if "document" in r]
    if not docs:
        return None
    doc = docs[0]
    return doc["name"].split("/")[-1], doc.get("fields", {})


def _norm(s: str) -> str:
    return (
        unicodedata.normalize("NFD", str(s or "").strip().lower())
        .encode("ascii", "ignore")
        .decode()
    )


def _week_label(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _get_line(fields: dict):
    """
    El punto del hándicap NO vive en un campo `line` top-level para
    asian_handicap (ese campo solo existe en totals) — vive en
    `factors.ah_line` (mapValue anidado). totals sí usa `line` top-level.
    """
    line = _fv(fields, "line")
    if line is not None:
        return line
    factors = fields.get("factors", {}).get("mapValue", {}).get("fields", {})
    return _fv(factors, "ah_line")


def _base_match_id(match_id: str) -> str:
    parts = match_id.split("_", 1)
    if len(parts) > 1 and parts[0].isdigit():
        return parts[0]
    return match_id


# ── Graduación (misma lógica que _grade_with_score en producción) ────────────

def _grade_with_score(market: str, sel: str, line, home: str, gh: int, ga: int) -> dict | None:
    sel = (sel or "").strip()
    total = gh + ga

    if market == "btts":
        both = gh > 0 and ga > 0
        yes = sel.lower() in ("yes", "si", "sí", "btts yes", "btts si", "btts sí")
        return {"correct": both if yes else (not both)}

    if market == "totals":
        if line is None:
            return None
        line = float(line)
        if abs(total - line) < 1e-9:
            return {"correct": None}  # push
        over = sel.lower().startswith("over")
        return {"correct": (total > line) if over else (total < line)}

    if market == "asian_handicap":
        if line is None:
            return None
        point = float(line)
        backed_home = _norm(sel).startswith(_norm(home)) if home else False
        adj = (gh - ga) + point if backed_home else (ga - gh) - point
        if abs(adj) < 1e-9:
            return {"correct": None}  # push
        return {"correct": adj > 0}

    if market == "correct_score":
        return {"correct": sel.replace(" ", "") == f"{gh}-{ga}"}

    return None


def _grade_ah_scoreless(sel: str, line, home: str, actual_result: str) -> dict | None:
    """
    Hándicap asiático SIN marcador disponible (típico en WC26_* sin doc en
    match_results). Solo es exacto para líneas ±0.5 y 0.0 — el resto (cuartos
    de línea, o enteros >=1 sin marcador) requiere el margen de gol real.
    """
    if line is None:
        return None
    point = float(line)
    backed_home = _norm(sel).startswith(_norm(home)) if home else False

    if abs(abs(point) - 0.5) < 1e-9:
        # ±0.5 nunca empata: gana quien gana el partido, el empate pierde para ambos lados.
        if actual_result == "DRAW":
            return {"correct": False}
        won_home = actual_result == "HOME_WIN"
        return {"correct": won_home if backed_home else (not won_home)}

    if abs(point) < 1e-9:
        # 0.0 (pick'em): empate es push, gana quien gana el partido.
        if actual_result == "DRAW":
            return {"correct": None}
        won_home = actual_result == "HOME_WIN"
        return {"correct": won_home if backed_home else (not won_home)}

    return None  # línea de cuarto o entera >=1 sin marcador — no verificable


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logger.info("=== Regrade mercados alternativos — %s ===", "APPLY" if APPLY else "DRY-RUN")
    _get_token()

    candidates: list[tuple[str, dict]] = []  # (doc_id, fields)
    for mt in _TARGET_MARKETS:
        docs = _query_market(mt)
        for d in docs:
            doc_id = d["name"].split("/")[-1]
            fields = d.get("fields", {})
            if "booleanValue" not in fields.get("correct", {}):
                continue  # sin resolver — fuera de alcance
            if "model_epoch" in fields:
                continue  # ya pasó por el sistema de épocas — protegido, no tocar
            candidates.append((doc_id, fields, d.get("updateTime")))

    logger.info("Candidatos (market en %s, sport=football, resueltos, sin model_epoch): %d",
                _TARGET_MARKETS, len(candidates))

    flips: list[dict] = []       # correct cambió de valor (True<->False)
    to_push: list[dict] = []     # correct pasa a push (None)
    unverifiable: list[dict] = []
    unchanged: list[dict] = []

    # week_label -> {"total_delta": int, "correct_delta": int}  (se resta la
    # contribución ORIGINAL completa de cada doc tocado, no solo el flip)
    week_deltas: dict[str, dict] = defaultdict(lambda: {"total_delta": 0, "correct_delta": 0})

    for doc_id, fields, update_time in candidates:
        match_id = _fv(fields, "match_id") or doc_id
        market = str(_fv(fields, "market_type") or "").lower()
        selection = _fv(fields, "selection") or ""
        home = _fv(fields, "home_team") or ""
        away = _fv(fields, "away_team") or ""
        line = _get_line(fields)
        actual_result = _fv(fields, "result")
        old_correct = _fv(fields, "correct")  # bool (garantizado por el filtro de arriba)

        base_id = _base_match_id(match_id)
        mr_doc = _get_doc("match_results", base_id)
        graded = None

        if mr_doc:
            f = mr_doc.get("fields", {})
            gh, ga = _fv(f, "goals_home"), _fv(f, "goals_away")
            if gh is not None and ga is not None:
                graded = _grade_with_score(market, selection, line, home, int(gh), int(ga))

        if graded is None and market == "asian_handicap":
            graded = _grade_ah_scoreless(selection, line, home, actual_result)

        # Semana ISO de la corrida que graduó (y contó) este doc originalmente.
        week = _week_label(_parse_ts(update_time)) if update_time else None

        record = {
            "doc_id": doc_id, "match_id": match_id, "market": market,
            "selection": selection, "home": home, "away": away,
            "result": actual_result, "old_correct": old_correct, "week": week,
        }

        # La contribución original de este doc se resta del ledger semanal
        # (accuracy_log) — a partir de ahora es época legacy/excluded, cuente lo
        # que cuente su valor final de `correct`. NO se toca model_weights/current
        # (contador post-rebuild, ver nota más abajo).
        if week:
            week_deltas[week]["total_delta"] -= 1
            week_deltas[week]["correct_delta"] -= 1 if old_correct else 0

        if graded is None:
            record["new_correct"] = old_correct
            record["verifiable"] = False
            unverifiable.append(record)
            continue

        new_correct = graded["correct"]
        record["new_correct"] = new_correct
        record["verifiable"] = True

        if new_correct is None:
            to_push.append(record)
        elif new_correct != old_correct:
            flips.append(record)
        else:
            unchanged.append(record)

    # ── Reporte ───────────────────────────────────────────────────────────────
    def _fmt(r):
        return (f"{r['doc_id']:<45} {r['home']} vs {r['away']} | sel={r['selection']!r} "
                f"| result={r['result']} | old={r['old_correct']} -> new={r['new_correct']}")

    logger.info("--- FLIPS (correct cambia de valor): %d ---", len(flips))
    for r in flips:
        logger.info("  FLIP  %s", _fmt(r))
    logger.info("--- A PUSH (excluir de accuracy, no ganó ni perdió): %d ---", len(to_push))
    for r in to_push:
        logger.info("  PUSH  %s", _fmt(r))
    logger.info("--- Sin cambio (ya estaban bien graduadas): %d ---", len(unchanged))
    logger.info("--- NO VERIFICABLES (sin marcador disponible, se deja como está): %d ---",
                len(unverifiable))
    for r in unverifiable:
        logger.info("  ??    %s", _fmt(r))

    logger.info("=== Total candidatos: %d | flips=%d push=%d sin_cambio=%d no_verificable=%d ===",
                len(candidates), len(flips), len(to_push), len(unchanged), len(unverifiable))
    logger.info("(model_weights/current.total_predictions/.correct_predictions no se tocan — "
                "contador post-rebuild, ver nota en el paso de aplicar)")
    for wk, d in sorted(week_deltas.items()):
        logger.info("  accuracy_log[%s]: total=%d correct=%d", wk, d["total_delta"], d["correct_delta"])

    if not APPLY:
        logger.info("\n── DRY-RUN: nada escrito. Re-ejecutar con --apply para aplicar. ──")
        return

    # ── Aplicar: 1) predictions ──────────────────────────────────────────────
    logger.info("Actualizando prodpredictions...")
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()  # solo para campos que de verdad quieren string
    ok = err = 0
    for r in flips + to_push + unchanged + unverifiable:
        payload = {
            "model_epoch": MODEL_EPOCH_PRE_ELO_REBUILD,
            "excluded_from_learning": True,
            "regraded_at": now_dt,
            "regrade_note": "regrade_alt_markets_backfill.py — bug 'backed==home_id' vacio==vacio",
        }
        if r["verifiable"]:
            payload["correct"] = r["new_correct"]
            payload["error_type"] = None
            if r["new_correct"] is None:
                payload["push"] = True
        try:
            _patch_doc("predictions", r["doc_id"], payload)
            ok += 1
        except Exception as e:
            logger.error("  ERROR actualizando %s: %s", r["doc_id"], e)
            err += 1
    logger.info("prodpredictions: %d OK, %d errores", ok, err)

    # ── Aplicar: 2) shadow_trades (solo flips + push, donde el resultado cambió) ──
    logger.info("Actualizando prodshadow_trades para docs con correct cambiado...")
    st_ok = st_skip = st_err = 0
    for r in flips + to_push:
        try:
            found = _query_shadow_trade(r["match_id"])
            if not found:
                st_skip += 1
                continue
            trade_id, tf = found
            stake = float(_fv(tf, "virtual_stake") or 0.5)
            odds = float(_fv(tf, "odds") or 2.0)
            if r["new_correct"] is None:
                result_val, pnl = "void", 0.0
            elif r["new_correct"]:
                result_val, pnl = "win", round((odds - 1) * stake, 4)
            else:
                result_val, pnl = "loss", round(-stake, 4)
            _patch_doc("shadow_trades", trade_id, {
                "result": result_val,
                "pnl_virtual": pnl,
                "closed_at": now_dt,
                "regraded_at": now_dt,
            })
            st_ok += 1
            logger.info("  shadow_trade %s (%s) -> result=%s pnl=%.2f", trade_id, r["match_id"], result_val, pnl)
        except Exception as e:
            logger.error("  ERROR shadow_trade para %s: %s", r["match_id"], e)
            st_err += 1
    logger.info("shadow_trades: %d actualizados, %d sin encontrar, %d errores", st_ok, st_skip, st_err)

    # NO se toca model_weights/current.total_predictions/.correct_predictions:
    # ese contador arrancó de nuevo en la reconstrucción del ELO (2026-08-19,
    # model_weights/elo_rebuild.rebuilt_at) — solo suma predicciones de la época
    # ACTUAL (post-rebuild). Los ~21 docs de esta corrida son de época legacy
    # (mayo/junio) y nunca incrementaron ese contador, así que restarles su
    # contribución ahí sería restar de algo que nunca los tuvo — lo comprobé
    # en producción (sesión 2026-08-25: estaba en 10/7, un intento de ajuste
    # lo dejó en 0/0 y hubo que revertirlo a mano). accuracy_log SÍ es un
    # ledger histórico real por semana (createTime de mayo/junio, no tocado
    # por el rebuild) — ahí sí corresponde el delta.
    logger.info("model_weights/current.total_predictions/.correct_predictions: NO se tocan "
                "(contador post-rebuild, estos docs son legacy y nunca lo incrementaron)")

    # ── Aplicar: 3) accuracy_log por semana ──────────────────────────────────
    logger.info("Ajustando accuracy_log por semana...")
    for wk, d in sorted(week_deltas.items()):
        try:
            doc = _get_doc("accuracy_log", wk)
            if not doc:
                logger.warning("  accuracy_log[%s]: doc no existe, se omite", wk)
                continue
            f = doc.get("fields", {})
            total_prev = int(_fv(f, "predictions_total") or 0)
            correct_prev = int(_fv(f, "predictions_correct") or 0)
            new_total = max(0, total_prev + d["total_delta"])
            new_correct = max(0, correct_prev + d["correct_delta"])
            new_accuracy = round(new_correct / new_total, 4) if new_total > 0 else 0.0
            _patch_doc("accuracy_log", wk, {
                "predictions_total": new_total,
                "predictions_correct": new_correct,
                "accuracy": new_accuracy,
                "regrade_note": "regrade_alt_markets_backfill.py",
            })
            logger.info("  accuracy_log[%s]: total %d->%d, correct %d->%d, accuracy=%.1f%%",
                        wk, total_prev, new_total, correct_prev, new_correct, new_accuracy * 100)
        except Exception as e:
            logger.error("  ERROR ajustando accuracy_log[%s]: %s", wk, e)

    logger.info("=== Backfill completado ===")
    logger.info("NOTA: los pesos del ensemble (poisson/elo/form/h2h) en model_weights/current.weights")
    logger.info("NO se recalculan — no hay ledger para reproducir el orden exacto de todas las")
    logger.info("actualizaciones de esa epoca (estaban entrelazadas con cientos de otras senales")
    logger.info("correctamente graduadas en la misma ventana). Se marcan como epoca contaminada")
    logger.info("(model_epoch=1, excluded_from_learning=True) igual que la reconstruccion del ELO,")
    logger.info("en vez de fingir una precision que no existe.")


if __name__ == "__main__":
    main()
