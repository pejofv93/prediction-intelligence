"""
API endpoints: registro de bonos de matched betting (entrada manual, el usuario apuesta a mano).

  GET    /bonus-claims[?status=]       lista + resumen (P&L total, recuento por estado)
  POST   /bonus-claims                 alta
  GET    /bonus-claims/{id}
  PATCH  /bonus-claims/{id}            cambio de campos, patas o estado (force=true para corregir hacia atrás)
  DELETE /bonus-claims/{id}
  GET    /matched-bookmakers           lista ES + estado de cuenta de cada casa
  PUT    /matched-bookmakers/{slug}    estado de cuenta (sin_cuenta/activa/limitada/cerrada) y notas

Persistencia SOLO Firestore vía el cliente compartido: colecciones matched_bonus_claims y
matched_bookmakers (doc id = slug de shared/es_bookmakers.py). Solo casas con licencia en
España; la cobertura nunca en la casa del bono.

Estados de un bono (flujo principal, se puede saltar hacia delante):
  pendiente → calificante_colocada → esperando_freebet → freebet_disponible →
  freebet_colocada → (rollover_en_curso) → completado
Salidas desde cualquier estado no final: caducado, cancelado, limitado.
Volver atrás o salir de un estado final solo con force=true (corrección manual).
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from shared.es_bookmakers import (
    BETFAIR_ES_DEFAULT_COMMISSION,
    BY_SLUG,
    ES_BOOKMAKERS,
    EXCHANGE_SLUG,
    is_es_exchange,
    is_es_sportsbook,
    lookup,
)

logger = logging.getLogger(__name__)
router = APIRouter()

CLAIMS = "matched_bonus_claims"
BOOKMAKERS = "matched_bookmakers"

FLOW = ("pendiente", "calificante_colocada", "esperando_freebet", "freebet_disponible",
        "freebet_colocada", "rollover_en_curso", "completado")
EXITS = ("caducado", "cancelado", "limitado")
FINAL = ("completado",) + EXITS
STATUSES = FLOW + EXITS

ACCOUNT_STATUSES = ("sin_cuenta", "activa", "limitada", "cerrada")
BONUS_KINDS = ("freebet_snr", "freebet_sr", "refund", "deposit_bonus", "otro")
LEG_KINDS = ("back", "freebet_snr", "freebet_sr", "exchange_back", "lay")
LEG_ROLES = ("calificante", "freebet", "cobertura", "rollover")
LEG_RESULTS = ("pendiente", "ganada", "perdida", "nula")


def _col(name: str):
    from shared.firestore_client import col
    return col(name)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Lógica pura ───────────────────────────────────────────────────────────────────

def check_transition(current: str, new: str, force: bool = False) -> None:
    """ValueError si el paso current → new no está permitido."""
    if new not in STATUSES:
        raise ValueError(f"estado no válido: {new}")
    if current == new or force:
        return
    if current in FINAL:
        raise ValueError(f"'{current}' es un estado final (usa force=true para corregirlo)")
    if new in EXITS:
        return
    if FLOW.index(new) < FLOW.index(current):
        raise ValueError(f"no se puede volver de '{current}' a '{new}' (usa force=true)")


def leg_pnl(leg: dict) -> float | None:
    """P&L de una pata liquidada (None si pendiente). Para un lay, 'ganada' = ganó el lay."""
    result = leg.get("result", "pendiente")
    if result == "pendiente":
        return None
    if result == "nula":
        return 0.0
    kind = leg.get("kind", "back")
    stake = float(leg.get("stake") or 0)
    odds = float(leg.get("odds") or 0)
    c = leg.get("commission")
    c = BETFAIR_ES_DEFAULT_COMMISSION if c is None else float(c)
    won = result == "ganada"
    if kind == "back":
        pnl = stake * (odds - 1) if won else -stake
    elif kind == "freebet_snr":
        pnl = stake * (odds - 1) if won else 0.0
    elif kind == "freebet_sr":
        pnl = stake * odds if won else 0.0
    elif kind == "exchange_back":
        pnl = stake * (odds - 1) * (1 - c) if won else -stake
    elif kind == "lay":
        pnl = stake * (1 - c) if won else -stake * (odds - 1)
    else:
        raise ValueError(f"tipo de pata no válido: {kind}")
    return round(pnl, 2)


def normalize_leg(leg: dict, claim_bookmaker: str) -> dict:
    """Valida y normaliza una pata. ValueError si no cumple las reglas."""
    kind = leg.get("kind", "back")
    role = leg.get("role", "cobertura")
    result = leg.get("result", "pendiente")
    if kind not in LEG_KINDS:
        raise ValueError(f"tipo de pata no válido: {kind}")
    if role not in LEG_ROLES:
        raise ValueError(f"rol de pata no válido: {role}")
    if result not in LEG_RESULTS:
        raise ValueError(f"resultado de pata no válido: {result}")
    try:
        odds = float(leg.get("odds"))
        stake = float(leg.get("stake"))
    except (TypeError, ValueError):
        raise ValueError("cada pata necesita cuota y stake numéricos")
    if odds <= 1 or stake <= 0:
        raise ValueError("cuota > 1 y stake > 0")
    book = leg.get("bookmaker") or ""
    if kind in ("exchange_back", "lay"):
        book = book or EXCHANGE_SLUG
        if not is_es_exchange(book):
            raise ValueError(f"exchange '{book}' no permitido: solo betfair.es")
        book = EXCHANGE_SLUG
    else:
        if not is_es_sportsbook(book):
            raise ValueError(f"casa '{book}' sin licencia en España (o no está en la lista)")
        book = lookup(book).slug
        if role == "cobertura" and book == claim_bookmaker:
            raise ValueError("la cobertura no puede ir en la misma casa del bono")
    out = {
        "role": role, "kind": kind, "bookmaker": book,
        "event": str(leg.get("event") or ""), "selection": str(leg.get("selection") or ""),
        "odds": odds, "stake": round(stake, 2), "result": result,
    }
    if kind in ("exchange_back", "lay"):
        c = leg.get("commission")
        out["commission"] = BETFAIR_ES_DEFAULT_COMMISSION if c is None else float(c)
        if kind == "lay":
            out["liability"] = round(stake * (odds - 1), 2)
    out["pnl"] = leg_pnl(out)
    return out


def claim_pnl(legs: list[dict]) -> tuple[float, int]:
    """(P&L de las patas liquidadas, nº de patas pendientes)."""
    total, pending = 0.0, 0
    for leg in legs:
        p = leg_pnl(leg)
        if p is None:
            pending += 1
        else:
            total += p
    return round(total, 2), pending


def _claim_bookmaker(slug_or_name: str) -> str:
    if not is_es_sportsbook(slug_or_name):
        raise ValueError(f"casa '{slug_or_name}' sin licencia en España (o no está en la lista)")
    return lookup(slug_or_name).slug


# ── Modelos ───────────────────────────────────────────────────────────────────────

class ClaimIn(BaseModel):
    bookmaker: str
    title: str = ""
    bonus_kind: str = "freebet_snr"
    amount: float = 0.0
    min_odds: float | None = None
    rollover: float | None = None
    expires_at: str | None = None
    status: str = "pendiente"
    legs: list[dict] = []
    notes: str = ""


class ClaimPatch(BaseModel):
    title: str | None = None
    bonus_kind: str | None = None
    amount: float | None = None
    min_odds: float | None = None
    rollover: float | None = None
    expires_at: str | None = None
    status: str | None = None
    legs: list[dict] | None = None
    notes: str | None = None
    force: bool = False


class BookmakerStatusIn(BaseModel):
    account_status: str
    notes: str = ""


def _finish(doc: dict) -> dict:
    pnl, pending = claim_pnl(doc.get("legs") or [])
    doc["pnl"], doc["pending_legs"] = pnl, pending
    return doc


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(e))


# ── Endpoints: bonos ───────────────────────────────────────────────────────────────

@router.get("/bonus-claims")
async def list_claims(status: str = "") -> dict:
    try:
        claims = [(d.to_dict() or {}) | {"id": d.id} for d in _col(CLAIMS).stream()]
    except Exception:
        logger.error("bonus_claims: error leyendo Firestore", exc_info=True)
        raise HTTPException(status_code=500, detail="Error leyendo el registro de bonos")
    if status:
        claims = [c for c in claims if c.get("status") == status]
    claims.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    by_status: dict[str, int] = {}
    for c in claims:
        by_status[c.get("status", "?")] = by_status.get(c.get("status", "?"), 0) + 1
    return {
        "claims": claims,
        "count": len(claims),
        "pnl_total": round(sum(float(c.get("pnl") or 0) for c in claims), 2),
        "by_status": by_status,
        "statuses": list(STATUSES),
    }


@router.post("/bonus-claims")
async def create_claim(req: ClaimIn) -> dict:
    try:
        book = _claim_bookmaker(req.bookmaker)
        if req.bonus_kind not in BONUS_KINDS:
            raise ValueError(f"tipo de bono no válido: {req.bonus_kind}")
        if req.status not in STATUSES:
            raise ValueError(f"estado no válido: {req.status}")
        legs = [normalize_leg(l, book) for l in req.legs]
    except ValueError as e:
        raise _bad(e)
    now = _now()
    doc = _finish({
        "bookmaker": book, "title": req.title, "bonus_kind": req.bonus_kind,
        "amount": req.amount, "min_odds": req.min_odds, "rollover": req.rollover,
        "expires_at": req.expires_at, "status": req.status, "legs": legs, "notes": req.notes,
        "status_history": [{"status": req.status, "at": now}],
        "created_at": now, "updated_at": now,
    })
    try:
        ref = _col(CLAIMS).document()
        ref.set(doc)
    except Exception:
        logger.error("bonus_claims: error guardando", exc_info=True)
        raise HTTPException(status_code=500, detail="Error guardando el bono")
    return doc | {"id": ref.id}


def _get_or_404(claim_id: str):
    ref = _col(CLAIMS).document(claim_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Bono no encontrado")
    return ref, snap.to_dict() or {}


@router.get("/bonus-claims/{claim_id}")
async def get_claim(claim_id: str) -> dict:
    _ref, doc = _get_or_404(claim_id)
    return doc | {"id": claim_id}


@router.patch("/bonus-claims/{claim_id}")
async def update_claim(claim_id: str, req: ClaimPatch) -> dict:
    ref, doc = _get_or_404(claim_id)
    now = _now()
    try:
        if req.bonus_kind is not None and req.bonus_kind not in BONUS_KINDS:
            raise ValueError(f"tipo de bono no válido: {req.bonus_kind}")
        for field in ("title", "bonus_kind", "amount", "min_odds", "rollover", "expires_at", "notes"):
            v = getattr(req, field)
            if v is not None:
                doc[field] = v
        if req.legs is not None:
            doc["legs"] = [normalize_leg(l, doc.get("bookmaker", "")) for l in req.legs]
        if req.status is not None and req.status != doc.get("status"):
            check_transition(doc.get("status", "pendiente"), req.status, req.force)
            doc["status"] = req.status
            doc.setdefault("status_history", []).append(
                {"status": req.status, "at": now, **({"forced": True} if req.force else {})})
    except ValueError as e:
        raise _bad(e)
    doc["updated_at"] = now
    _finish(doc)
    ref.set(doc)
    return doc | {"id": claim_id}


@router.delete("/bonus-claims/{claim_id}")
async def delete_claim(claim_id: str) -> dict:
    ref, _doc = _get_or_404(claim_id)
    ref.delete()
    return {"id": claim_id, "deleted": True}


# ── Endpoints: estado de cuenta por casa ────────────────────────────────────────────

@router.get("/matched-bookmakers")
async def list_bookmakers() -> dict:
    try:
        stored = {d.id: (d.to_dict() or {}) for d in _col(BOOKMAKERS).stream()}
    except Exception:
        logger.error("matched_bookmakers: error leyendo Firestore", exc_info=True)
        stored = {}
    out = []
    for b in ES_BOOKMAKERS:
        s = stored.get(b.slug, {})
        out.append(b.to_dict() | {
            "account_status": s.get("account_status", "sin_cuenta"),
            "notes": s.get("notes", ""),
            "updated_at": s.get("updated_at"),
        })
    return {"bookmakers": out, "account_statuses": list(ACCOUNT_STATUSES)}


@router.put("/matched-bookmakers/{slug}")
async def set_bookmaker_status(slug: str, req: BookmakerStatusIn) -> dict:
    if slug not in BY_SLUG:
        raise HTTPException(status_code=404, detail="Casa fuera de la lista de casas con licencia en España")
    if req.account_status not in ACCOUNT_STATUSES:
        raise HTTPException(status_code=400, detail=f"estado no válido (usa {', '.join(ACCOUNT_STATUSES)})")
    doc = {"slug": slug, "account_status": req.account_status, "notes": req.notes, "updated_at": _now()}
    _col(BOOKMAKERS).document(slug).set(doc)
    return doc
