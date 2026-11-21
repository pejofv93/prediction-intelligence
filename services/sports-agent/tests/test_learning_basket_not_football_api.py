"""Los ids NBA (numéricos de ESPN) no se consultan a football-data: check_result es solo fútbol.
21-nov-2026: 11-15 HTTP 400 de football-data /matches/{id} cada noche a las 02:00. Sin red."""
import asyncio
from datetime import datetime, timedelta, timezone

from learner import learning_engine as le


class _Doc:
    def __init__(self, i, d):
        self.id, self._d = i, d

    def to_dict(self):
        return dict(self._d)


class _Q:
    def __init__(self, docs):
        self.docs = docs

    def where(self, *a, **k):
        return self

    order_by = limit = where

    def stream(self):
        return iter(self.docs)

    def document(self, *_):
        return self


def test_bloqueos_de_basket_no_van_a_football_data(monkeypatch):
    old = datetime.now(timezone.utc) - timedelta(days=2)
    docs = [_Doc("nba", {"sport": "basketball", "league": "NBA", "match_id": "401810123", "match_date": old}),
            _Doc("pl", {"sport": "football", "league": "PL", "match_id": "537001", "match_date": old})]
    asked = []

    async def fake_check(mid):
        asked.append(mid)
        return None

    monkeypatch.setattr(le, "col", lambda name: _Q(docs))
    monkeypatch.setattr(le, "check_result", fake_check)
    summary = asyncio.run(le.grade_filter_blocks())
    assert asked == ["537001"] and summary["pendientes"] == 1
