"""reset_wait_seconds: espera al reinicio de cuota allsports (07:58 UTC) en la siembra del cron."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rebuild_elo import reset_wait_seconds  # noqa: E402


def _t(h, m, s=0):
    return datetime(2026, 10, 24, h, m, s, tzinfo=timezone.utc)


def test_arranques_reales_antes_del_reinicio():
    # 24-oct arrancó a las 07:28 → 33 min hasta las 08:01
    assert reset_wait_seconds(_t(7, 28)) == 33 * 60
    assert reset_wait_seconds(_t(7, 56)) == 5 * 60


def test_a_75_min_espera_y_a_76_no():
    assert reset_wait_seconds(_t(6, 43)) == 78 * 60
    assert reset_wait_seconds(_t(6, 42)) == 0


def test_tras_el_reinicio_no_espera():
    assert reset_wait_seconds(_t(7, 58)) == 0
    assert reset_wait_seconds(_t(8, 24)) == 0
    assert reset_wait_seconds(_t(3, 0)) == 0


def test_tope_80_min():
    assert max(reset_wait_seconds(_t(6, 43, s)) for s in range(60)) <= 80 * 60
