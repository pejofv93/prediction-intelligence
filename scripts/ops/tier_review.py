"""Revisión mensual de niveles de Sports (shared/tier_review). Lo lanza sports-tier-review.

  ACCION=proponer  calcula y guarda la propuesta (model_weights/tier_review_proposal); no aplica.
  ACCION=aplicar   CONFIRMAR=APLICAR: la propuesta pasa a model_weights/tier_adjustments
                   (guarda la versión anterior).
  ACCION=revertir  CONFIRMAR=REVERTIR: vuelve a la versión anterior.
ENVIAR=true manda el resumen al canal General de Telegram en texto plano. Solo agregados:
nunca ids, equipos ni selecciones. El token no se imprime nunca.
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from shared import tier_review as tr  # noqa: E402
from shared.firestore_client import col  # noqa: E402

SPORT = {"football": "Fútbol", "basketball": "Baloncesto", "tennis": "Tenis"}


def label(key: str) -> str:
    sport, market, tier = key.split("|")
    return f"{SPORT.get(sport, sport)} · {market} · {tier}"


def send(text: str) -> None:
    import httpx
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("ENVIAR=true pero faltan TELEGRAM_TOKEN/TELEGRAM_CHAT_ID: no se envía")
        return
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text}, timeout=15)
        print(f"Telegram: HTTP {r.status_code}")
    except Exception as exc:  # sin el texto de la excepción: llevaría la URL con el token
        print(f"Telegram: error de envío ({type(exc).__name__})")


def do_propose() -> str:
    current = tr.current_factors()
    rows = tr.propose(tr.collect_records(), current)
    now = datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    col(tr.DOC_PROPOSAL[0]).document(tr.DOC_PROPOSAL[1]).set({
        "month": month, "created_at": now, "since": tr.SINCE.isoformat(),
        "params": {"min_n": tr.MIN_N, "k0": tr.K0, "fdr_q": tr.FDR_Q, "step": tr.STEP},
        "current": current, "rows": rows,
        "factors": {k: r["k_proposed"] for k, r in rows.items() if r["k_proposed"] < 1},
    })
    changes = {k: r for k, r in rows.items() if r["k_proposed"] != r["k_current"]}
    lines = [f"🔧 Revisión mensual de niveles Sports ({month}) — PROPUESTA, no aplicada",
             f"Enviadas y graduadas desde {tr.SINCE:%d/%m/%Y}; segmento = deporte · mercado · nivel del modelo.",
             f"Un segmento solo baja si tiene ≥{tr.MIN_N} graduadas, acierta menos de lo que decía el modelo "
             f"con significación corregida (FDR {tr.FDR_Q:.0%}) y en las dos mitades. Paso máximo {tr.STEP:.1f}.",
             "Solo cambia la etiqueta de nivel y el Kelly mostrados; las señales se siguen enviando igual.", ""]
    for k, r in rows.items():
        if "hit" in r:
            lines.append(f"{label(k)}: n={r['n']} acierto {r['hit']:.0%} · modelo {r['model']:.0%} · "
                         f"mercado {r['expected']:.0%} · k {r['k_current']:.2f} → {r['k_proposed']:.2f} ({r['reason']})")
        else:
            lines.append(f"{label(k)}: n={r['n']} · k {r['k_current']:.2f} → {r['k_proposed']:.2f} ({r['reason']})")
    if not rows:
        lines.append(f"Ningún segmento llega a {tr.MIN_N} graduadas: nada que proponer.")
    lines += ["", ("Hay cambios: para aplicarlos, lanza sports-tier-review con accion=aplicar y confirmar=APLICAR."
                   if changes else "Sin cambios propuestos.")]
    return "\n".join(lines)


def do_apply() -> str:
    snap = col(tr.DOC_PROPOSAL[0]).document(tr.DOC_PROPOSAL[1]).get()
    prop = snap.to_dict() if snap.exists else None
    if not prop:
        return "No hay propuesta guardada: nada que aplicar."
    ref = col(tr.DOC_ADJUST[0]).document(tr.DOC_ADJUST[1])
    old = ref.get()
    old = old.to_dict() if old.exists else {}
    ref.set({"version": int(old.get("version") or 0) + 1, "month": prop.get("month"),
             "applied_at": datetime.now(timezone.utc), "factors": prop.get("factors") or {},
             "previous": {"version": old.get("version"), "month": old.get("month"),
                          "factors": old.get("factors") or {}}})
    f = prop.get("factors") or {}
    return (f"🔧 Revisión de niveles {prop.get('month')} APLICADA: "
            + (", ".join(f"{label(k)} k={v:.2f}" for k, v in sorted(f.items())) or "sin ajustes (todo k=1)"))


def do_revert() -> str:
    ref = col(tr.DOC_ADJUST[0]).document(tr.DOC_ADJUST[1])
    snap = ref.get()
    cur = snap.to_dict() if snap.exists else None
    if not cur:
        return "No hay ajustes aplicados: nada que revertir."
    prev = cur.get("previous") or {}
    ref.set({"version": int(cur.get("version") or 0) + 1, "month": prev.get("month"),
             "applied_at": datetime.now(timezone.utc), "factors": prev.get("factors") or {},
             "previous": {"version": cur.get("version"), "month": cur.get("month"),
                          "factors": cur.get("factors") or {}}, "reverted": True})
    return f"🔧 Ajustes de nivel revertidos a la versión de {prev.get('month') or 'antes de la primera revisión'}."


def main() -> int:
    accion = os.environ.get("ACCION", "proponer")
    confirmar = os.environ.get("CONFIRMAR", "")
    if accion == "aplicar" and confirmar != "APLICAR":
        print("aplicar exige confirmar=APLICAR: no se hace nada")
        return 1
    if accion == "revertir" and confirmar != "REVERTIR":
        print("revertir exige confirmar=REVERTIR: no se hace nada")
        return 1
    text = {"proponer": do_propose, "aplicar": do_apply, "revertir": do_revert}[accion]()
    print(text)
    if os.environ.get("ENVIAR") == "true":
        send(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
