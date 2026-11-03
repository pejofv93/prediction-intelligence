"""Semántica Firestore sobre Postgres real (requiere PG_DSN)."""
import asyncio
import datetime as dt
import threading
from datetime import timedelta, timezone

import pytest

import pg_store as ps
from pg_store import FieldFilter

UTC = timezone.utc


def ids(q):
    return [s.id for s in q.stream()]


# ------------------------------------------------------------------ lectura/escritura básica
def test_set_get_types(coll):
    t = dt.datetime(2026, 5, 1, 10, 30, 0, 250000, tzinfo=UTC)
    coll.document("a").set({"i": 1, "f": 1.0, "t": t, "n": None, "m": {"x": [1, "y"]}, "b": b"\x01"})
    s = coll.document("a").get()
    assert s.exists and s.id == "a"
    d = s.to_dict()
    assert type(d["i"]) is int and type(d["f"]) is float
    assert d["t"] == t and d["n"] is None and d["m"] == {"x": [1, "y"]} and d["b"] == b"\x01"
    assert s.get("m.x") == [1, "y"]
    with pytest.raises(KeyError):
        s.get("missing")
    assert not coll.document("nope").get().exists
    assert coll.document("nope").get().to_dict() is None


def test_create_update_delete(coll):
    ref = coll.document("d")
    ref.create({"a": 1})
    with pytest.raises(ps.AlreadyExists):
        ref.create({"a": 2})
    ref.update({"a": 2, "m.x": 1})
    assert ref.get().to_dict() == {"a": 2, "m": {"x": 1}}
    with pytest.raises(ps.NotFound):
        coll.document("missing").update({"a": 1})
    ref.delete()
    ref.delete()  # borrar lo inexistente no falla
    assert not ref.get().exists
    _, added = coll.add({"z": 1})
    assert len(added.id) == 20 and added.get().to_dict() == {"z": 1}


def test_create_time_kept_on_overwrite(coll):
    ref = coll.document("ct")
    ref.set({"a": 1})
    first = ref.get()
    ref.set({"a": 2})
    second = ref.get()
    assert second.create_time == first.create_time
    assert second.update_time >= first.update_time


# ------------------------------------------------------------------ campo ausente frente a null
@pytest.fixture
def nullish(coll):
    coll.document("absent").set({"other": 1})
    coll.document("null").set({"v": None})
    coll.document("one").set({"v": 1})
    coll.document("two").set({"v": 2.5})
    coll.document("str").set({"v": "x"})
    return coll


def test_missing_vs_null_equality(nullish):
    assert ids(nullish.where(filter=FieldFilter("v", "==", None))) == ["null"]
    snap = nullish.document("null").get().to_dict()
    assert "v" in snap and snap["v"] is None
    assert "v" not in nullish.document("absent").get().to_dict()


def test_missing_vs_null_not_equal(nullish):
    # Como Firestore real (verificado 2026-11-02): != y not-in excluyen el doc sin el
    # campo y también el que vale null.
    assert sorted(ids(nullish.where(filter=FieldFilter("v", "!=", None)))) == ["one", "str", "two"]
    assert sorted(ids(nullish.where(filter=FieldFilter("v", "!=", 1)))) == ["str", "two"]
    assert sorted(ids(nullish.where(filter=FieldFilter("v", "not-in", [1, "x"])))) == ["two"]


def test_missing_excluded_from_order_by(nullish):
    # orden entre tipos: null < número < string; el doc sin campo no aparece
    assert ids(nullish.order_by("v")) == ["null", "one", "two", "str"]
    assert ids(nullish.order_by("v", direction="DESCENDING")) == ["str", "two", "one", "null"]


def test_inequality_is_type_strict(nullish):
    nullish.document("str5").set({"v": "5"})
    assert ids(nullish.where(filter=FieldFilter("v", ">", 0))) == ["one", "two"]
    assert ids(nullish.where(filter=FieldFilter("v", ">=", 1.0))) == ["one", "two"]  # int y float se comparan
    assert ids(nullish.where(filter=FieldFilter("v", ">", ""))) == ["str5", "str"]


# ------------------------------------------------------------------ consultas
def test_where_order_limit_cursor(coll):
    base = dt.datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(10):
        coll.document(f"d{i:02d}").set({"n": i % 3, "t": base + timedelta(hours=i), "tag": "a" if i < 5 else "b"})
    q = coll.where(filter=FieldFilter("t", ">=", base + timedelta(hours=4)))
    assert ids(q) == [f"d{i:02d}" for i in range(4, 10)]  # orden implícito por el campo de la desigualdad
    q2 = coll.where(filter=FieldFilter("tag", "==", "a")).order_by("n", direction="DESCENDING").limit(3)
    assert ids(q2) == ["d02", "d04", "d01"]  # n desc y desempate por __name__ desc
    page1 = coll.order_by("n").limit(4).get()
    page2 = coll.order_by("n").start_after(page1[-1]).limit(4).get()
    page3 = coll.order_by("n").start_after(page2[-1]).get()
    all_ids = [s.id for s in page1 + page2 + page3]
    assert sorted(all_ids) == [f"d{i:02d}" for i in range(10)] and len(set(all_ids)) == 10
    assert ids(coll.order_by("n").start_at({"n": 2})) == ["d02", "d05", "d08"]
    assert ids(coll.order_by("n").end_before({"n": 1})) == ["d00", "d03", "d06", "d09"]
    assert coll.where(filter=FieldFilter("tag", "==", "b")).count().get()[0][0].value == 5
    sel = coll.select(["n"]).limit(1).get()[0].to_dict()
    assert set(sel) == {"n"}


def test_in_and_array_ops(coll):
    coll.document("a").set({"k": 1, "arr": [1, "x", {"m": 1}]})
    coll.document("b").set({"k": 2.0, "arr": ["y"]})
    coll.document("c").set({"k": "1", "arr": [{"m": 1, "extra": 2}]})
    assert ids(coll.where(filter=FieldFilter("k", "in", [1.0, 2]))) == ["a", "b"]
    assert ids(coll.where(filter=FieldFilter("arr", "array_contains", "x"))) == ["a"]
    assert ids(coll.where(filter=FieldFilter("arr", "array_contains", {"m": 1}))) == ["a"]  # map exacto, no subconjunto
    assert ids(coll.where(filter=FieldFilter("arr", "array_contains_any", ["y", 1]))) == ["a", "b"]
    assert ids(coll.where(filter=ps.Or([FieldFilter("k", "==", "1"), FieldFilter("k", "==", 2)]))) == ["b", "c"]


def test_iter_all_pages(client, monkeypatch):
    import uuid
    name = f"t_{uuid.uuid4().hex[:12]}"
    monkeypatch.setattr(ps, "COLLECTION_PREFIX", "")
    monkeypatch.setattr(ps, "_client", client)
    b = client.batch()
    for i in range(2500):
        b.set(client.collection(name).document(f"{i:05d}"), {"i": i, "even": i % 2 == 0})
    b.commit()
    try:
        got = [s.id for s in ps.iter_all(name)]
        assert len(got) == 2500 and got == sorted(got)
        evens = list(ps.iter_all(name, page_size=300, where=("even", "==", True)))
        assert len(evens) == 1250
    finally:
        with client.pool.connection() as conn:
            conn.execute("DELETE FROM fs_docs WHERE collection=%s", (name,))


def test_get_all(client, coll):
    coll.document("x").set({"a": 1})
    snaps = list(client.get_all([coll.document("x"), coll.document("y")]))
    assert {s.id: s.exists for s in snaps} == {"x": True, "y": False}


# ------------------------------------------------------------------ transformaciones en BD
def test_increment_db(coll):
    ref = coll.document("c")
    ref.set({"runs": ps.Increment(1), "sent": ps.Increment(0)}, merge=True)   # doc inexistente
    ref.set({"runs": ps.Increment(1), "sent": ps.Increment(2.5)}, merge=True)
    d = ref.get().to_dict()
    assert d == {"runs": 2, "sent": 2.5} and type(d["runs"]) is int


def test_increment_concurrent(coll):
    ref = coll.document("ctr")
    ref.set({"n": 0})

    def bump():
        for _ in range(5):
            ref.update({"n": ps.Increment(1)})

    th = [threading.Thread(target=bump) for _ in range(20)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert ref.get().to_dict()["n"] == 100


def test_array_union_db(coll):
    # mismo patrón que trend_finder: contador + ArrayUnion de lista y de historial (maps)
    ref = coll.document("sig")
    for markets in (["1X2", "BTTS"], ["BTTS", "O2.5"]):
        ref.set({"sends": ps.Increment(1), "markets_sent": ps.ArrayUnion(markets),
                 "history": ps.ArrayUnion([{"markets": markets}])}, merge=True)
    ref.set({"history": ps.ArrayUnion([{"markets": ["1X2", "BTTS"]}])}, merge=True)  # repetido: no se añade
    d = ref.get().to_dict()
    assert d["sends"] == 2
    assert d["markets_sent"] == ["1X2", "BTTS", "O2.5"]
    assert d["history"] == [{"markets": ["1X2", "BTTS"]}, {"markets": ["BTTS", "O2.5"]}]
    ref.update({"markets_sent": ps.ArrayRemove(["BTTS"])})
    assert ref.get().to_dict()["markets_sent"] == ["1X2", "O2.5"]


def test_delete_field_db(coll):
    # mismo patrón que value_bet_engine: borrar campos con set(merge=True)
    ref = coll.document("m")
    ref.set({"all_markets": {"x": 1}, "all_markets_fetched_at": dt.datetime.now(UTC), "keep": 1})
    ref.set({"all_markets": ps.DELETE_FIELD, "all_markets_fetched_at": ps.DELETE_FIELD}, merge=True)
    assert ref.get().to_dict() == {"keep": 1}
    ref.update({"keep": ps.DELETE_FIELD})
    assert ref.get().to_dict() == {}
    assert ref.get().exists  # el doc sigue existiendo aunque quede vacío
    assert ids(coll.where(filter=FieldFilter("keep", "==", 1))) == []


# ------------------------------------------------------------------ batch y transacciones
def test_batch_atomic(coll):
    b = coll._client.batch()
    b.set(coll.document("ok"), {"a": 1})
    b.update(coll.document("missing"), {"a": 1})
    with pytest.raises(ps.NotFound):
        b.commit()
    assert not coll.document("ok").get().exists


def _make_claim(client, ref, now):
    """Réplica de _claim de services/polymarket-agent/alert_engine.py con pg_store."""
    @ps.transactional
    def _claim(transaction):
        snap = ref.get(transaction=transaction)
        cur = (snap.to_dict() or {}) if snap.exists else {}
        stale = (cur.get("status") == "pending" and cur.get("sent_at") is not None
                 and now - cur["sent_at"] > timedelta(minutes=10))
        if snap.exists and not stale:
            return False
        transaction.set(ref, {"status": "pending", "sent_at": now, "market_id": "m1"})
        return True
    return _claim


def test_claim_concurrency_single_winner(client, coll):
    ref = coll.document("m1_BUY_YES")
    now = dt.datetime.now(UTC)
    claim = _make_claim(client, ref, now)
    results, errors = [], []
    barrier = threading.Barrier(20)

    def worker():
        try:
            barrier.wait()
            results.append(claim(client.transaction()))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    th = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert not errors
    assert results.count(True) == 1 and results.count(False) == 19
    assert ref.get().to_dict()["status"] == "pending"


def test_claim_stale_pending(client, coll):
    now = dt.datetime.now(UTC)
    stale = coll.document("stale")
    stale.set({"status": "pending", "sent_at": now - timedelta(minutes=11)})
    fresh = coll.document("fresh")
    fresh.set({"status": "pending", "sent_at": now - timedelta(minutes=5)})
    sent = coll.document("sent")
    sent.set({"status": "sent", "sent_at": now - timedelta(days=3)})
    assert _make_claim(client, stale, now)(client.transaction()) is True
    assert stale.get().to_dict()["sent_at"] == now.replace(microsecond=now.microsecond)
    assert _make_claim(client, fresh, now)(client.transaction()) is False
    assert _make_claim(client, sent, now)(client.transaction()) is False


def test_transaction_rollback(client, coll):
    ref = coll.document("r")

    @ps.transactional
    def boom(tx):
        ref.get(transaction=tx)
        tx.set(ref, {"a": 1})
        raise RuntimeError("fallo")

    with pytest.raises(RuntimeError):
        boom(client.transaction())
    assert not ref.get().exists


# ------------------------------------------------------------------ API async
def test_async_api(coll):
    acoll = ps.AsyncCollectionReference(coll)

    async def go():
        await acoll.document("a").set({"v": 1})
        await acoll.document("b").set({"v": 2})
        snap = await acoll.document("a").get()
        got = [s.id async for s in acoll.where(filter=FieldFilter("v", ">", 1)).stream()]
        lst = await acoll.order_by("v").get()
        return snap.to_dict(), got, [s.id for s in lst]

    d, got, lst = asyncio.run(go())
    assert d == {"v": 1} and got == ["b"] and lst == ["a", "b"]


# ------------------------------------------------------------------ objetos de google.cloud.firestore
def test_accepts_google_objects(coll):
    firestore = pytest.importorskip("google.cloud.firestore")
    from google.cloud.firestore_v1.base_query import FieldFilter as GFieldFilter
    ref = coll.document("g")
    ref.set({"n": firestore.Increment(2), "a": firestore.ArrayUnion(["x"]), "gone": 1}, merge=True)
    ref.set({"gone": firestore.DELETE_FIELD, "ts": firestore.SERVER_TIMESTAMP}, merge=True)
    d = ref.get().to_dict()
    assert d["n"] == 2 and d["a"] == ["x"] and "gone" not in d and isinstance(d["ts"], dt.datetime)
    assert ids(coll.where(filter=GFieldFilter("n", "==", 2))) == ["g"]
