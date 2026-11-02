"""Tests sin base de datos: codificación de tipos y cálculo de escrituras."""
import datetime as dt

import pytest

import pg_store as ps

NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=dt.timezone.utc)


def roundtrip(v):
    import json
    return ps.decode(json.loads(ps._dumps(ps.encode(v))))


def test_types_roundtrip():
    doc = {
        "i": 5, "f": 5.0, "neg": -0.25, "b": True, "n": None, "s": "hola", "e": "",
        "t": dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=dt.timezone.utc),
        "raw": b"\x00\xff", "arr": [1, 1.5, "x", None, {"k": [True]}],
        "m": {"nested": {"deep": 1}}, "big": 1e20, "nan": float("nan"),
        "geo": ps.GeoPoint(40.4, -3.7),
        "fake_tag": {"__ts__": "no es un timestamp"},  # dict del usuario con clave reservada
    }
    got = roundtrip(doc)
    assert type(got["i"]) is int and type(got["f"]) is float and type(got["big"]) is float
    assert got["t"] == doc["t"] and got["t"].tzinfo is not None
    assert got["raw"] == b"\x00\xff"
    assert got["fake_tag"] == {"__ts__": "no es un timestamp"}
    assert got["geo"] == ps.GeoPoint(40.4, -3.7)
    assert got["nan"] != got["nan"]
    for k in ("i", "neg", "b", "n", "s", "e", "arr", "m"):
        assert got[k] == doc[k]


def test_naive_datetime_is_utc():
    assert roundtrip(dt.datetime(2026, 1, 1, 10)) == dt.datetime(2026, 1, 1, 10, tzinfo=dt.timezone.utc)


def test_unsupported_type():
    with pytest.raises(TypeError):
        ps.encode(dt.date(2026, 1, 1))


def test_fs_equal():
    assert ps.fs_equal(1, 1.0)
    assert not ps.fs_equal(True, 1)
    assert ps.fs_equal({"a": [1, 2.0]}, {"a": [1.0, 2]})
    assert not ps.fs_equal("1", 1)


def test_increment():
    old = {"i": 2, "f": 1.5, "s": "x"}
    new = ps.compute_write(old, "update", {"i": ps.Increment(3), "f": ps.Increment(1), "s": ps.Increment(4),
                                           "missing": ps.Increment(7), "fi": ps.Increment(0.5)}, False, NOW)
    assert new["i"] == 5 and type(new["i"]) is int
    assert new["f"] == 2.5 and type(new["f"]) is float
    assert new["s"] == 4          # no numérico -> se reemplaza por el incremento
    assert new["missing"] == 7    # ausente -> el incremento
    assert new["fi"] == 0.5


def test_array_union_remove():
    old = {"a": [1, "x", {"k": 1}], "notlist": 3}
    new = ps.compute_write(old, "update", {
        "a": ps.ArrayUnion([1.0, "y", {"k": 1}, "y"]),   # 1.0 == 1 y el map repetido no se añaden
        "notlist": ps.ArrayUnion(["z"]),
        "fresh": ps.ArrayUnion([1, 1]),
    }, False, NOW)
    assert new["a"] == [1, "x", {"k": 1}, "y"]
    assert new["notlist"] == ["z"]
    assert new["fresh"] == [1]
    new2 = ps.compute_write(new, "update", {"a": ps.ArrayRemove([1.0, {"k": 1}]), "nope": ps.ArrayRemove([1])},
                            False, NOW)
    assert new2["a"] == ["x", "y"]
    assert new2["nope"] == []


def test_delete_field():
    old = {"a": 1, "m": {"x": 1, "y": 2}, "keep": True}
    new = ps.compute_write(old, "update", {"a": ps.DELETE_FIELD, "m.x": ps.DELETE_FIELD, "gone.z": ps.DELETE_FIELD},
                           False, NOW)
    assert new == {"m": {"y": 2}, "keep": True}
    new2 = ps.compute_write(old, "set", {"m": {"y": ps.DELETE_FIELD}, "b": 2}, True, NOW)
    assert new2 == {"a": 1, "m": {"x": 1}, "keep": True, "b": 2}
    with pytest.raises(ValueError):
        ps.compute_write(old, "set", {"a": ps.DELETE_FIELD}, False, NOW)


def test_set_merge_and_update_semantics():
    old = {"m": {"x": 1, "y": 2}, "z": 0}
    assert ps.compute_write(old, "set", {"m": {"x": 9}}, False, NOW) == {"m": {"x": 9}}
    assert ps.compute_write(old, "set", {"m": {"x": 9}}, True, NOW) == {"m": {"x": 9, "y": 2}, "z": 0}
    # update con dict reemplaza el map entero; con ruta con punto toca solo la hoja
    assert ps.compute_write(old, "update", {"m": {"x": 9}}, False, NOW) == {"m": {"x": 9}, "z": 0}
    assert ps.compute_write(old, "update", {"m.x": 9}, False, NOW) == {"m": {"x": 9, "y": 2}, "z": 0}
    # merge=[campos]: solo esos; el pedido y ausente en data se borra
    assert ps.compute_write(old, "set", {"m": {"x": 9}, "z": 5}, ["m.x", "q"], NOW) == {"m": {"x": 9, "y": 2}, "z": 0}
    assert ps.compute_write(old, "set", {"z": 5}, ["z", "m"], NOW) == {"z": 5}
    assert ps.compute_write(old, "set", {"t": ps.SERVER_TIMESTAMP}, True, NOW)["t"] == NOW
    with pytest.raises(ps.NotFound):
        ps.compute_write(None, "update", {"a": 1}, False, NOW)
    with pytest.raises(ps.AlreadyExists):
        ps.compute_write(old, "create", {"a": 1}, False, NOW)


def test_split_path():
    assert ps.split_path("a.b") == ("a", "b")
    assert ps.split_path("a.`b.c`") == ("a", "b.c")
