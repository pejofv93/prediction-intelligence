"""SOLO LECTURA. ¿Qué les falta a los partidos de la ACB que salieron incompletos?

hlb_games con complete=False (la ficha de /statistics llegó, pero algún equipo no trae todo lo
que usa el modelo: _NEEDED de highlightly_basket). Por partido: temporada, estado, campos por
equipo y, de cada campo imprescindible que falta, el motivo:
  - «no viene»: ningún nombre de la lista de alias está en la ficha;
  - «no numérico»: viene, pero vacío o sin número;
  - «anotados>intentados»: venía, y la comprobación de coherencia vació el par;
  - «sin puntos»: falta el marcador del partido.
Además, los nombres de campo de la ficha que el colector no reconoce (son etiquetas del
esquema de la API). Ni equipos, ni ids, ni cifras: solo agregados. No escribe nada.
"""
import os
import sys
from collections import Counter

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "services", "sports-agent"))

from collectors.highlightly_basket import (  # noqa: E402
    GAMES, _NEEDED, _PAIR_ALIASES, _STAT_ALIASES, _num, _pair, map_stats)
from shared.firestore_client import iter_all  # noqa: E402

_KNOWN = {n for names in _STAT_ALIASES.values() for n in names} | \
         {n for names in _PAIR_ALIASES.values() for n in names}


def reasons(raw: dict) -> dict[str, str]:
    low = {str(k).strip().lower(): v for k, v in (raw or {}).items()}
    mapped = map_stats(raw)
    out = {}
    for f in _NEEDED:
        if mapped.get(f) is not None:
            continue
        names = [n for n in _STAT_ALIASES.get(f, ()) if n in low]
        pair = next(((m, a) for (m, a) in _PAIR_ALIASES if f in (m, a)), None)
        pair_names = [n for n in (_PAIR_ALIASES.get(pair, ()) if pair else ()) if n in low]
        had_value = (any(_num(low[n]) is not None for n in names)
                     or any(_pair(low.get(n))[0] is not None for n in pair_names))
        if not names and not pair_names:
            out[f] = "no viene"
        elif had_value:      # venía con número y map_stats lo vació: solo lo hace la coherencia
            out[f] = "anotados>intentados"
        else:
            out[f] = "no numérico"
    return out


def main() -> int:
    total: Counter = Counter()
    unknown: Counter = Counter()
    n = 0
    for snap in iter_all(GAMES):
        d = snap.to_dict() or {}
        if d.get("complete") is not False:
            continue
        n += 1
        raw = d.get("raw_stats") or {}
        line = [f"temporada {d.get('hl_season')}", f"estado {d.get('status')}",
                f"campos {d.get('n_fields')}"]
        if d.get("pts_home") is None or d.get("pts_away") is None:
            line.append("sin puntos")
            total["sin puntos"] += 1
        for side in ("home", "away"):
            r = reasons(raw.get(side) or {})
            line.append(f"{'local' if side == 'home' else 'visitante'}: "
                        + (", ".join(f"{k} {v}" for k, v in r.items()) or "completo")
                        + f" ({len(raw.get(side) or {})} campos)")
            for k, v in r.items():
                total[f"{k} {v}"] += 1
            for name in raw.get(side) or {}:
                if str(name).strip().lower() not in _KNOWN:
                    unknown[str(name)] += 1
        print(f"  {n}. " + " · ".join(line))
    print(f"\nincompletos: {n}")
    print(f"motivos (equipo-campo): {dict(total.most_common())}")
    print(f"campos que el colector no reconoce: {dict(unknown.most_common())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
