"""CLV real al cierre (learner/closing_clv) y su resumen para el informe (shared/clv_summary)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from learner import closing_clv as cc
from shared import clv_summary as cs

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
ROW = {"Date": "08/11/2026", "HomeTeam": "Arsenal", "AwayTeam": "Chelsea",
       "AvgCH": "2.00", "AvgCD": "3.50", "AvgCA": "4.00",
       "MaxCH": "2.10", "B365CH": "1.95"}


def _alert(**kw):
    a = {"type": "sports_sent", "sport": "football", "league": "PL", "market_type": "h2h",
         "home_team": "Arsenal FC", "away_team": "Chelsea FC", "team_to_back": "Arsenal FC",
         "odds": 2.20, "match_date": "2026-11-08T15:00:00+00:00",
         "sent_at": NOW - timedelta(days=13)}
    a.update(kw)
    return a


def test_cierre_sin_margen_y_clv():
    out = cc.closing_from_row(ROW, "H", 2.20)
    inv = [1 / 2.0, 1 / 3.5, 1 / 4.0]
    p = inv[0] / sum(inv)
    assert out["close_p_fair"] == pytest.approx(p, abs=1e-5)
    assert out["clv_close"] == pytest.approx(2.20 * p - 1, abs=1e-5)
    assert (out["close_odds_avg"], out["close_odds_max"], out["close_odds_b365"]) == (2.0, 2.1, 1.95)
    assert out["clv_close_max"] == pytest.approx(2.20 / 2.10 - 1, abs=1e-5)
    assert cc.closing_from_row({**ROW, "MaxCH": ""}, "H", 2.2)["clv_close_max"] is None
    assert out["close_odds_pinnacle"] is None and out["close_source"] == "fdco-media-cierre"


def test_usa_pinnacle_si_vuelve():
    row = {**ROW, "PSCH": "2.05", "PSCD": "3.6", "PSCA": "4.1"}
    assert cc.closing_from_row(row, "H", 2.2)["close_source"] == "fdco-pinnacle-cierre"


def test_sin_cuotas_de_cierre_no_hay_clv():
    assert cc.closing_from_row({**ROW, "AvgCD": ""}, "H", 2.2) is None


def test_lado_temporada_y_fechas():
    assert cc.side_of(_alert()) == "H"
    assert cc.side_of(_alert(team_to_back="Chelsea FC")) == "A"
    assert cc.side_of(_alert(team_to_back="Draw")) == "D"
    assert cc.side_of(_alert(team_to_back="Otro")) is None
    assert cc.season_code(datetime(2026, 11, 8)) == "2627"
    assert cc.season_code(datetime(2027, 3, 1)) == "2627"
    assert cc.parse_dt("2026-11-08 15:00").day == 8
    assert cc.parse_dt("2026-11-08").tzinfo is not None


def _map(name, teams):
    short = name.replace(" FC", "")
    return short, short in teams


def test_busca_la_fila_con_fecha_mas_menos_un_dia():
    teams = {"Arsenal", "Chelsea"}
    assert cc.find_row(_alert(match_date="2026-11-09T00:30:00+00:00"), [ROW], teams, _map) is ROW
    assert cc.find_row(_alert(match_date="2026-11-11"), [ROW], teams, _map) is None


class _Doc:
    def __init__(self, i, d):
        self.id, self._d = i, d

    def to_dict(self):
        return self._d


def _run(monkeypatch, alerts, rows, dry_run=False):
    import collectors.match_fixtures as mf
    import shared.firestore_client as fc
    writes = {}

    class _Ref:
        def __init__(self, i):
            self.i = i

        def update(self, data):
            writes[self.i] = data

    class _Col:
        def document(self, i):
            return _Ref(i)

    monkeypatch.setattr(fc, "iter_all",
                        lambda name, where=None: [_Doc(f"d{i}", a) for i, a in enumerate(alerts)])
    monkeypatch.setattr(fc, "col", lambda name: _Col())
    monkeypatch.setattr(mf, "map_team", _map)

    async def fake_dl(client, season, div):
        return rows if (season, div) == ("2627", "E0") else []

    monkeypatch.setattr(cc, "_download", fake_dl)
    summary = asyncio.run(cc.run_closing_clv(dry_run=dry_run, now=NOW))
    return summary, writes


def test_pasada_escribe_cierre_y_marca_sin_cierre(monkeypatch):
    alerts = [
        _alert(),                                                              # con cierre
        _alert(home_team="Leeds FC", team_to_back="Leeds FC", match_date="2026-10-20T15:00:00+00:00"),  # >21 días sin fila
        _alert(home_team="Leeds FC", team_to_back="Leeds FC", match_date="2026-11-15T15:00:00+00:00"),  # aún no publicado
        _alert(market_type="totals"),                                          # no es 1X2
        _alert(clv_close=0.01),                                                # ya hecho
        _alert(league="WC26"),                                                 # sin CSV
        _alert(match_date="2026-11-20T10:00:00+00:00"),                        # aún no jugado
    ]
    summary, writes = _run(monkeypatch, alerts, [ROW])
    assert summary["con_cierre"] == 1 and summary["sin_cierre"] == 1 and summary["sin_fila_aun"] == 1
    assert summary["liga_sin_csv"] == 1 and summary["pendientes"] == 3
    assert writes["d0"]["clv_close"] == pytest.approx(cc.closing_from_row(ROW, "H", 2.2)["clv_close"])
    assert writes["d0"]["close_checked_at"] == NOW
    assert writes["d1"] == {"close_status": "sin_cierre", "close_checked_at": NOW}
    assert set(writes) == {"d0", "d1"}


def test_dry_run_no_escribe(monkeypatch):
    summary, writes = _run(monkeypatch, [_alert()], [ROW], dry_run=True)
    assert summary["con_cierre"] == 1 and writes == {}


def test_resumen_por_version_con_intervalo():
    src = {"close_source": "fdco-media-cierre"}
    docs = ([_alert(clv_close=x, clv_close_max=x - 0.03, **src) for x in (0.02, -0.04, 0.01)]
            + [_alert(clv_close=x, poisson_fit="lbfgs-l2-v1", **src) for x in (0.05, 0.03)]
            + [_alert(), _alert(close_status="sin_cierre"),
               _alert(clv_close=0.9, sent_at=NOW - timedelta(days=40)),     # fuera de la ventana
               _alert(clv_close=0.9, market_type="totals")])
    s = cs.summarize(docs, NOW)
    old, new = s["groups"]["nelder-mead-2000"], s["groups"]["lbfgs-l2-v1"]
    assert old["n"] == 3 and old["mean"] == pytest.approx(-0.01 / 3)
    assert old["lo"] < old["mean"] < old["hi"] and old["pos"] == pytest.approx(2 / 3)
    assert new["n"] == 2 and new["mean"] == pytest.approx(0.04)
    assert s["pending"] == 1
    mx = s["groups_max"]
    assert set(mx) == {"nelder-mead-2000"} and mx["nelder-mead-2000"]["n"] == 3
    assert mx["nelder-mead-2000"]["mean"] == pytest.approx(-0.01 / 3 - 0.03)
    lines = cs.report_lines(s)
    assert lines[0] == "CLV real al cierre (enviadas 1X2, 4 semanas):"
    assert lines[1] == " Frente a media sin margen:"
    assert "Poisson anterior" in lines[2] and "Poisson nuevo" in lines[3]
    assert lines[4] == " Frente a cuota máxima:" and "Poisson anterior" in lines[5]
    assert lines[-1] == "  Pendientes de cierre: 1"
    assert cs.report_lines(cs.summarize([], NOW)) == []
