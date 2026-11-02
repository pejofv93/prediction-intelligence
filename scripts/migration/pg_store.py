"""
Capa Postgres con la misma forma que el subconjunto de Firestore que usa el proyecto.

ESTADO: preparación de la migración. Vive en scripts/migration/ (no en shared/) para que
deploy.yml no lo copie a las imágenes. NINGÚN servicio lo importa y
shared/firestore_client.py no cambia. Solo lo usan tests/pg_store/ y
scripts/migration/load_jsonl_pg.py.

Modelo de datos: una tabla `fs_docs(collection, id, data jsonb, create_time, update_time)`
con PK (collection, id). `collection` es el nombre completo con prefijo (igual que en
Firestore: f"{FIRESTORE_COLLECTION_PREFIX}{name}").

Codificación en JSONB (sin pérdida de tipos de Firestore):
  None/bool/int/float/str/list/dict  -> JSON nativo (jsonb conserva 1 frente a 1.0)
  datetime -> {"__ts__": "YYYY-MM-DDTHH:MM:SS.ffffffZ"} (UTC, ancho fijo: orden lexicográfico = cronológico)
  bytes    -> {"__bytes__": hex}  (hex conserva el orden por bytes)
  referencia -> {"__ref__": path}, GeoPoint -> {"__geo__": [lat, lng]}
  float no finito o >=1e16 entero -> {"__float__": "nan" | "inf" | repr}
  un dict del usuario con una sola clave reservada se envuelve en {"__map__": {...}}

Semántica Firestore que se reproduce (cubierta por tests/pg_store/):
  - campo ausente != null: `== None` solo casa null; order_by y desigualdades excluyen
    los documentos sin el campo; `!=`/`not-in` también (confirmado en la documentación).
    RIESGO NO CONFIRMADO: aquí `!=`/`not-in` INCLUYEN los docs con null salvo que se
    filtre null (modelo "<v OR >v"). No he encontrado documentación que lo confirme;
    verificarlo contra Firestore real antes de cablear un servicio que dependa de ello.
  - comparaciones estrictas por tipo (5 no casa con "5"; int y float sí se comparan);
    orden entre tipos: null < bool < número < timestamp < string < bytes < ref < geo < array < map.
  - orden implícito por __name__ con la dirección del último order_by; los campos con
    desigualdad sin order_by se ordenan implícitamente.
  - set / set(merge=True) / set(merge=[campos]) / update con rutas "a.b" / create / delete.
  - Increment, ArrayUnion, ArrayRemove, DELETE_FIELD, SERVER_TIMESTAMP (también los
    objetos equivalentes de google.cloud.firestore, por duck typing).
  - WriteBatch atómico; Transaction + @transactional con reintento. Cada documento leído o
    escrito se bloquea con pg_advisory_xact_lock(collection/id), que también bloquea
    documentos que todavía no existen (lo que necesita el _claim de alert_engine).

Aproximaciones conocidas: el orden entre dos maps o dos arrays usa el orden de jsonb, no
el de Firestore; NaN ordena como -inf. Ninguna consulta del proyecto depende de eso.

Variables de entorno: PG_DSN (obligatoria al conectar), PG_POOL_MAX (por defecto 10),
FIRESTORE_COLLECTION_PREFIX (mismo prefijo que firestore_client).
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import functools
import json
import math
import os
import random
import string
import threading
import time
from collections import namedtuple
from decimal import Decimal
from typing import Any, Iterable, Iterator

UTC = dt.timezone.utc
COLLECTION_PREFIX = os.environ.get("FIRESTORE_COLLECTION_PREFIX", "")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fs_docs (
    collection  text COLLATE "C" NOT NULL,
    id          text COLLATE "C" NOT NULL,
    data        jsonb NOT NULL,
    create_time timestamptz NOT NULL,
    update_time timestamptz NOT NULL,
    PRIMARY KEY (collection, id)
)
"""

# --------------------------------------------------------------------------- errores
try:  # mismas clases que lanza google-cloud-firestore, para que los except existentes sirvan
    from google.api_core.exceptions import AlreadyExists, Conflict, NotFound  # type: ignore
except Exception:  # noqa: BLE001
    class NotFound(Exception):
        pass

    class Conflict(Exception):
        pass

    class AlreadyExists(Conflict):
        pass


# --------------------------------------------------------------------------- centinelas y transformaciones
class _Sentinel:
    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"pg_store.{self.name}"

    def __copy__(self):  # los centinelas se comparan por identidad: no se copian
        return self

    def __deepcopy__(self, memo):
        return self


DELETE_FIELD = _Sentinel("DELETE_FIELD")
SERVER_TIMESTAMP = _Sentinel("SERVER_TIMESTAMP")


class Increment:
    def __init__(self, value):
        self.value = value


class ArrayUnion:
    def __init__(self, values):
        self.values = list(values)


class ArrayRemove:
    def __init__(self, values):
        self.values = list(values)


GeoPoint = namedtuple("GeoPoint", "latitude longitude")


class Reference:
    """Valor de tipo referencia leído de la BD (solo la ruta)."""

    def __init__(self, path: str):
        self.path = path

    def __eq__(self, other):
        return isinstance(other, Reference) and other.path == self.path

    def __hash__(self):
        return hash(self.path)

    def __repr__(self):
        return f"Reference({self.path!r})"


def _kind(v) -> str | None:
    """Identifica centinelas y transformaciones propias o de google.cloud.firestore."""
    if v is DELETE_FIELD:
        return "delete"
    if v is SERVER_TIMESTAMP:
        return "server_ts"
    cls = type(v).__name__
    if cls == "Sentinel":  # google.cloud.firestore_v1.transforms.Sentinel
        d = (getattr(v, "description", "") or "").lower()
        if "delete" in d:
            return "delete"
        if "server" in d or "timestamp" in d:
            return "server_ts"
    if isinstance(v, Increment) or (cls == "Increment" and hasattr(v, "value")):
        return "increment"
    if isinstance(v, ArrayUnion) or (cls == "ArrayUnion" and hasattr(v, "values")):
        return "union"
    if isinstance(v, ArrayRemove) or (cls == "ArrayRemove" and hasattr(v, "values")):
        return "remove"
    return None


# --------------------------------------------------------------------------- codificación
_TAGS = ("__ts__", "__bytes__", "__ref__", "__geo__", "__float__", "__map__")


def _ts_str(t: dt.datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def encode(v):
    """Valor Python -> valor JSON con etiquetas (ver docstring del módulo)."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if not math.isfinite(v):
            return {"__float__": "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")}
        if v.is_integer() and abs(v) >= 1e16:
            return {"__float__": repr(v)}
        return v
    if isinstance(v, dt.datetime):
        return {"__ts__": _ts_str(v)}
    if isinstance(v, (bytes, bytearray)):
        return {"__bytes__": bytes(v).hex()}
    if isinstance(v, GeoPoint) or type(v).__name__ == "GeoPoint":
        return {"__geo__": [float(v.latitude), float(v.longitude)]}
    if isinstance(v, Reference):
        return {"__ref__": v.path}
    if hasattr(v, "path") and hasattr(v, "id") and hasattr(v, "parent"):  # DocumentReference
        return {"__ref__": v.path}
    if isinstance(v, (list, tuple)):
        return [encode(x) for x in v]
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise TypeError(f"clave de map no es str: {k!r}")
            if _kind(x) is not None:
                raise TypeError(f"transformación/centinela en posición no permitida: {k}")
            out[k] = encode(x)
        if len(out) == 1 and next(iter(out)) in _TAGS:
            return {"__map__": out}
        return out
    raise TypeError(f"tipo no soportado por Firestore: {type(v).__name__}")


def decode(v):
    """Valor JSON con etiquetas -> valor Python."""
    if isinstance(v, list):
        return [decode(x) for x in v]
    if isinstance(v, dict):
        if len(v) == 1:
            (k, x), = v.items()
            if k == "__ts__":
                return dt.datetime.fromisoformat(x.replace("Z", "+00:00"))
            if k == "__bytes__":
                return bytes.fromhex(x)
            if k == "__ref__":
                return Reference(x)
            if k == "__geo__":
                return GeoPoint(x[0], x[1])
            if k == "__float__":
                return float(x)
            if k == "__map__":
                return {kk: decode(xx) for kk, xx in x.items()}
        return {k: decode(x) for k, x in v.items()}
    return v


def _dumps(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fs_equal(a, b) -> bool:
    """Igualdad de Firestore: 1 == 1.0, pero True != 1; maps y arrays en profundidad."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(fs_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(fs_equal(a[k], b[k]) for k in a)
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return _ts_str(a) == _ts_str(b)
    return type(a) is type(b) and a == b


# --------------------------------------------------------------------------- rutas de campo
def split_path(path: str) -> tuple[str, ...]:
    """'a.b' -> ('a','b'); admite segmentos entre comillas invertidas: 'a.`b.c`'."""
    out, cur, quoted = [], "", False
    for ch in path:
        if ch == "`":
            quoted = not quoted
        elif ch == "." and not quoted:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    if any(p == "" for p in out):
        raise ValueError(f"ruta de campo inválida: {path!r}")
    return tuple(out)


def _path_of(field) -> tuple[str, ...]:
    if isinstance(field, (tuple, list)):
        return tuple(field)
    if hasattr(field, "parts"):  # FieldPath de google
        return tuple(field.parts)
    return split_path(str(field))


_MISSING = object()


def _get_path(doc: dict, path: tuple[str, ...]):
    cur = doc
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


def _set_path(doc: dict, path: tuple[str, ...], value) -> None:
    cur = doc
    for p in path[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[path[-1]] = value


def _del_path(doc: dict, path: tuple[str, ...]) -> None:
    cur = doc
    for p in path[:-1]:
        cur = cur.get(p)
        if not isinstance(cur, dict):
            return
    cur.pop(path[-1], None)


# --------------------------------------------------------------------------- aplicación de escrituras
def _split_transforms(value, base_path: tuple[str, ...], ops: list, allow_delete: bool):
    """Devuelve `value` sin transformaciones y añade a `ops` las transformaciones encontradas."""
    k = _kind(value)
    if k == "delete":
        if not allow_delete:
            raise ValueError("DELETE_FIELD solo se permite en update() o set(merge=...)")
        ops.append(("delete", base_path, None))
        return _MISSING
    if k is not None:
        ops.append((k, base_path, value))
        return _MISSING
    if isinstance(value, dict):
        out = {}
        for key, x in value.items():
            y = _split_transforms(x, base_path + (key,), ops, allow_delete)
            if y is not _MISSING:
                out[key] = y
        return out
    return value


def _leaves(value: dict, base: tuple[str, ...]) -> Iterator[tuple[tuple[str, ...], Any]]:
    for key, x in value.items():
        p = base + (key,)
        if isinstance(x, dict) and x and _kind(x) is None:
            yield from _leaves(x, p)
        else:
            yield p, x


def _apply_transform(kind: str, current, value, now: dt.datetime):
    if kind == "server_ts":
        return now
    if kind == "increment":
        n = value.value
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            return current + n
        return n
    if kind == "union":
        out = list(current) if isinstance(current, list) else []
        for x in value.values:
            if not any(fs_equal(x, y) for y in out):
                out.append(x)
        return out
    if kind == "remove":
        if not isinstance(current, list):
            return []
        return [y for y in current if not any(fs_equal(x, y) for x in value.values)]
    raise ValueError(kind)


def compute_write(old: dict | None, op: str, data: dict | None, merge, now: dt.datetime) -> dict | None:
    """Documento resultante (sin codificar) de aplicar una escritura sobre `old`.
    op: set | update | create | delete. Devuelve None si el documento queda borrado."""
    if op == "delete":
        return None
    if op == "create" and old is not None:
        raise AlreadyExists("el documento ya existe")
    if op == "update":
        if old is None:
            raise NotFound("no existe el documento a actualizar")
        if not data:
            raise ValueError("update() necesita al menos un campo")
    if not isinstance(data, dict):
        raise TypeError("los datos deben ser un dict")

    ops: list = []
    if op in ("set", "create") and not merge:
        new = copy.deepcopy(_split_transforms(data, (), ops, allow_delete=False))
        for kind, path, value in ops:
            _set_path(new, path, _apply_transform(kind, _MISSING, value, now))
        return new

    new = copy.deepcopy(old) if old else {}
    if op == "update":
        writes = [(split_path(k) if isinstance(k, str) else _path_of(k), v) for k, v in data.items()]
    else:  # set con merge
        writes = list(_leaves(data, ()))
        if merge is not True:
            wanted = [_path_of(f) for f in merge]
            keep = [(p, v) for p, v in writes if any(p[: len(w)] == w for w in wanted)]
            for w in wanted:  # campo pedido en merge pero ausente en data -> se borra
                if _get_path(data, w) is _MISSING:
                    keep.append((w, DELETE_FIELD))
            writes = keep
    for path, value in writes:
        plain = _split_transforms(value, path, ops, allow_delete=True)
        if plain is not _MISSING:
            _set_path(new, path, copy.deepcopy(plain))
    for kind, path, value in ops:
        if kind == "delete":
            _del_path(new, path)
        else:
            cur = _get_path(new, path)
            _set_path(new, path, _apply_transform(kind, None if cur is _MISSING else cur, value, now))
    return new


# --------------------------------------------------------------------------- conexión
_pool = None
_pool_lock = threading.Lock()


def get_pool(dsn: str | None = None):
    global _pool
    with _pool_lock:
        if _pool is None:
            from psycopg_pool import ConnectionPool  # import diferido: nadie lo carga sin usarlo
            dsn = dsn or os.environ["PG_DSN"]
            _pool = ConnectionPool(dsn, min_size=1, max_size=int(os.environ.get("PG_POOL_MAX", "10")),
                                   open=True)
            with _pool.connection() as conn:
                conn.execute(SCHEMA_SQL)
        return _pool


def close_pool() -> None:
    global _pool, _client, _async_client
    with _pool_lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _client = None
        _async_client = None


def _lock(conn, collection: str, doc_id: str) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"{collection}/{doc_id}",))


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


def _read_row(conn, collection: str, doc_id: str):
    return conn.execute(
        "SELECT data, create_time, update_time FROM fs_docs WHERE collection=%s AND id=%s",
        (collection, doc_id)).fetchone()


def _write(conn, collection: str, doc_id: str, op: str, data, merge, now: dt.datetime) -> None:
    """Lee-modifica-escribe con el documento bloqueado. Debe ir dentro de una transacción."""
    _lock(conn, collection, doc_id)
    row = _read_row(conn, collection, doc_id)
    old = decode(row[0]) if row else None
    new = compute_write(old, op, data, merge, now)
    if new is None:
        conn.execute("DELETE FROM fs_docs WHERE collection=%s AND id=%s", (collection, doc_id))
        return
    conn.execute(
        "INSERT INTO fs_docs (collection, id, data, create_time, update_time) "
        "VALUES (%s, %s, %s::jsonb, %s, %s) "
        "ON CONFLICT (collection, id) DO UPDATE SET data=EXCLUDED.data, update_time=EXCLUDED.update_time",
        (collection, doc_id, _dumps(encode(new)), now, now))


# --------------------------------------------------------------------------- snapshots y referencias
_AUTO_ID_CHARS = string.ascii_letters + string.digits


class DocumentSnapshot:
    def __init__(self, reference, data: dict | None, create_time=None, update_time=None):
        self.reference = reference
        self._data = data
        self.create_time = create_time
        self.update_time = update_time
        self.read_time = _now()

    @property
    def id(self) -> str:
        return self.reference.id

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return copy.deepcopy(self._data) if self._data is not None else None

    def get(self, field_path):
        if self._data is None:
            return None
        v = _get_path(self._data, _path_of(field_path))
        if v is _MISSING:
            raise KeyError(field_path)
        return copy.deepcopy(v)


class DocumentReference:
    def __init__(self, client: "PgClient", collection: str, doc_id: str):
        self._client = client
        self._collection = collection
        self.id = doc_id

    @property
    def path(self) -> str:
        return f"{self._collection}/{self.id}"

    @property
    def parent(self):
        return CollectionReference(self._client, self._collection)

    def __eq__(self, other):
        return isinstance(other, DocumentReference) and other.path == self.path

    def __hash__(self):
        return hash(self.path)

    def collection(self, name):
        raise NotImplementedError("pg_store no implementa subcolecciones (el proyecto no las usa)")

    def get(self, field_paths=None, transaction: "Transaction | None" = None) -> DocumentSnapshot:
        if transaction is not None:
            snap = transaction._get(self)
        else:
            with self._client.pool.connection() as conn:
                row = _read_row(conn, self._collection, self.id)
            snap = DocumentSnapshot(self, decode(row[0]), row[1], row[2]) if row else DocumentSnapshot(self, None)
        return _project(snap, field_paths)

    def _single(self, op, data=None, merge=False):
        now = _now()
        with self._client.pool.connection() as conn:
            with conn.transaction():
                _write(conn, self._collection, self.id, op, data, merge, now)
        return WriteResult(now)

    def set(self, document_data: dict, merge=False):
        return self._single("set", document_data, merge)

    def create(self, document_data: dict):
        return self._single("create", document_data)

    def update(self, field_updates: dict):
        return self._single("update", field_updates)

    def delete(self):
        return self._single("delete")


WriteResult = namedtuple("WriteResult", "update_time")
AggregationResult = namedtuple("AggregationResult", "alias value read_time")


def _project(snap: DocumentSnapshot, field_paths):
    if not field_paths or snap._data is None:
        return snap
    out: dict = {}
    for f in field_paths:
        p = _path_of(f)
        v = _get_path(snap._data, p)
        if v is not _MISSING:
            _set_path(out, p, v)
    return DocumentSnapshot(snap.reference, out, snap.create_time, snap.update_time)


# --------------------------------------------------------------------------- consultas
_RANK_SQL = """CASE jsonb_typeof({x})
  WHEN 'null' THEN 0 WHEN 'boolean' THEN 1 WHEN 'number' THEN 2 WHEN 'string' THEN 4 WHEN 'array' THEN 8
  WHEN 'object' THEN CASE WHEN {x} ? '__float__' THEN 2 WHEN {x} ? '__ts__' THEN 3 WHEN {x} ? '__bytes__' THEN 5
                          WHEN {x} ? '__ref__' THEN 6 WHEN {x} ? '__geo__' THEN 7 ELSE 9 END END"""


def _key_parts(path: tuple[str, ...]) -> list[tuple[str, list]]:
    """Componentes (rango, bool, número, texto, jsonb) que ordenan como Firestore."""
    x = "(data #> %s::text[])"
    parts = [
        _RANK_SQL.format(x=x),
        f"COALESCE(CASE WHEN jsonb_typeof({x})='boolean' THEN {x}::boolean END, false)",
        f"COALESCE(CASE WHEN jsonb_typeof({x})='number' THEN {x}::numeric "
        f"WHEN jsonb_typeof({x})='object' AND {x} ? '__float__' THEN "
        f"CASE WHEN {x}->>'__float__'='nan' THEN '-Infinity'::numeric ELSE ({x}->>'__float__')::numeric END END, 0)",
        f"(COALESCE(CASE WHEN jsonb_typeof({x})='string' THEN {x} #>> '{{}}' "
        f"WHEN jsonb_typeof({x})='object' THEN COALESCE({x}->>'__ts__', {x}->>'__bytes__', {x}->>'__ref__') END, '') "
        f"COLLATE \"C\")",
        f"CASE WHEN jsonb_typeof({x})='array' THEN {x} "
        f"WHEN jsonb_typeof({x})='object' AND NOT ({x} ?| array['__float__','__ts__','__bytes__','__ref__']) "
        f"THEN {x} ELSE 'null'::jsonb END",
    ]
    return [(sql, [list(path)] * sql.count("%s")) for sql in parts]


def _key_sql(path: tuple[str, ...]) -> tuple[str, list]:
    """ROW(...) de los componentes; solo para comparar con otro ROW (respeta COLLATE)."""
    parts = _key_parts(path)
    return "ROW(" + ", ".join(sql for sql, _ in parts) + ")", [p for _, ps in parts for p in ps]


def _lit_key(v) -> tuple[str, list]:
    """Mismo ROW(...) para un valor literal Python."""
    e = encode(v)
    rank = 9
    b, n, t, j = False, "0", "", None
    if e is None:
        rank = 0
    elif isinstance(e, bool):
        rank, b = 1, e
    elif isinstance(e, (int, float)):
        rank, n = 2, repr(e) if isinstance(e, float) else str(e)
    elif isinstance(e, str):
        rank, t = 4, e
    elif isinstance(e, list):
        rank, j = 8, e
    elif isinstance(e, dict) and len(e) == 1 and next(iter(e)) in _TAGS and "__map__" not in e:
        tag, val = next(iter(e.items()))
        if tag == "__float__":
            rank, n = 2, ("-Infinity" if val == "nan" else {"inf": "Infinity", "-inf": "-Infinity"}.get(val, val))
        elif tag == "__ts__":
            rank, t = 3, val
        elif tag == "__bytes__":
            rank, t = 5, val
        elif tag == "__ref__":
            rank, t = 6, val
        elif tag == "__geo__":
            rank, j = 7, e
    else:
        rank, j = 9, e
    sql = "ROW(%s::int, %s::boolean, %s::numeric, %s::text, %s::jsonb)"
    return sql, [rank, b, n, t, _dumps(j)]


_INEQ = {"<", "<=", ">", ">=", "!=", "not-in"}
_OPS = {"<", "<=", ">", ">=", "==", "!=", "in", "not-in", "array_contains", "array_contains_any"}


class FieldFilter:
    def __init__(self, field_path, op_string, value=None):
        self.field_path = field_path
        self.op_string = op_string
        self.value = value


class Or:
    def __init__(self, filters):
        self.filters = list(filters)


class And:
    def __init__(self, filters):
        self.filters = list(filters)


def _norm_op(op) -> str:
    op = str(getattr(op, "name", op)).lower()
    return {"array-contains": "array_contains", "array-contains-any": "array_contains_any",
            "not_in": "not-in", "not_equal": "!=", "equal": "=="}.get(op, op)


def _id_of(v) -> str:
    if isinstance(v, (DocumentReference,)) or hasattr(v, "id") and hasattr(v, "path"):
        return v.id
    if isinstance(v, str):
        return v.rsplit("/", 1)[-1]
    raise TypeError(f"valor de __name__ no válido: {v!r}")


def _filter_sql(f) -> tuple[str, list, set]:
    """(sql, params, campos con desigualdad)."""
    if hasattr(f, "filters"):
        is_or = type(f).__name__ == "Or" or "OR" in str(getattr(f, "operator", "")).upper()
        parts, params, ineq = [], [], set()
        for sub in f.filters:
            s, p, i = _filter_sql(sub)
            parts.append(f"({s})")
            params += p
            ineq |= i
        return (" OR " if is_or else " AND ").join(parts) or "TRUE", params, ineq
    field, op, value = f.field_path, _norm_op(f.op_string), f.value
    if op not in _OPS:
        raise ValueError(f"operador no soportado: {op}")
    path = _path_of(field)
    ineq = {path} if op in _INEQ else set()

    if path == ("__name__",):
        sqlop = {"==": "=", "!=": "<>"}.get(op, op)
        if op in ("in", "not-in"):
            ids = [_id_of(v) for v in value]
            return ("id = ANY(%s::text[])" if op == "in" else "NOT (id = ANY(%s::text[]))"), [ids], ineq
        return f"id {sqlop} %s", [_id_of(value)], ineq

    x = "(data #> %s::text[])"
    exists = f"{x} IS NOT NULL"
    key, kp = _key_sql(path)

    def eq(v):
        ls, lp = _lit_key(v)
        return f"{key} = {ls}", kp + lp

    if op == "array_contains" or op == "array_contains_any":
        vals = [value] if op == "array_contains" else list(value)
        conds = ["EXISTS (SELECT 1 FROM jsonb_array_elements(" + x + ") e WHERE e = %s::jsonb)"] * len(vals)
        params = []
        for v in vals:
            params += [list(path), _dumps(encode(v))]
        return (f"jsonb_typeof({x})='array' AND (" + " OR ".join(conds or ["FALSE"]) + ")",
                [list(path)] + params, ineq)
    if op == "==":
        s, p = eq(value)
        return f"{exists} AND {s}", [list(path)] + p, ineq
    if op == "in" or op == "not-in":
        parts, params = [], []
        for v in value:
            s, p = eq(v)
            parts.append(f"({s})")
            params += p
        body = " OR ".join(parts) or "FALSE"
        if op == "in":
            return f"{exists} AND ({body})", [list(path)] + params, ineq
        return f"{exists} AND NOT ({body})", [list(path)] + params, ineq
    if op == "!=":
        s, p = eq(value)
        return f"{exists} AND NOT ({s})", [list(path)] + p, ineq
    # < <= > >=: solo valores del mismo tipo
    ls, lp = _lit_key(value)
    rank = lp[0]
    return (f"{exists} AND ({_RANK_SQL.format(x=x)}) = %s AND {key} {op} {ls}",
            [list(path)] + [list(path)] * _RANK_SQL.count("{x}") + [rank] + kp + lp, ineq)


class Query:
    ASCENDING = "ASCENDING"
    DESCENDING = "DESCENDING"

    def __init__(self, client: "PgClient", collection: str):
        self._client = client
        self._collection = collection
        self._filters: list = []
        self._orders: list[tuple[tuple[str, ...], str]] = []
        self._limit: int | None = None
        self._offset: int = 0
        self._select = None
        self._start = None  # (values|snapshot, inclusive)
        self._end = None

    def _copy(self, **kw):
        q = copy.copy(self)
        q._filters = list(self._filters)
        q._orders = list(self._orders)
        for k, v in kw.items():
            setattr(q, k, v)
        return q

    # -- construcción
    def where(self, field_path=None, op_string=None, value=None, *, filter=None):
        f = filter if filter is not None else FieldFilter(field_path, op_string, value)
        return self._copy(_filters=self._filters + [f])

    def order_by(self, field_path, direction=ASCENDING):
        d = str(getattr(direction, "name", direction)).upper()
        d = "DESCENDING" if d.startswith("DESC") else "ASCENDING"
        return self._copy(_orders=self._orders + [(_path_of(field_path), d)])

    def limit(self, count: int):
        return self._copy(_limit=count)

    def offset(self, num: int):
        return self._copy(_offset=num)

    def select(self, field_paths):
        return self._copy(_select=list(field_paths))

    def start_after(self, cursor):
        return self._copy(_start=(cursor, False))

    def start_at(self, cursor):
        return self._copy(_start=(cursor, True))

    def end_before(self, cursor):
        return self._copy(_end=(cursor, False))

    def end_at(self, cursor):
        return self._copy(_end=(cursor, True))

    # -- compilación
    def _effective_orders(self, ineq: set) -> list:
        orders = [o for o in self._orders]
        have = {p for p, _ in orders}
        for p in sorted(ineq - have):
            if p != ("__name__",):
                orders.append((p, "ASCENDING"))
        if ("__name__",) not in {p for p, _ in orders}:
            orders.append((("__name__",), orders[-1][1] if orders else "ASCENDING"))
        return orders

    def _cursor_values(self, cursor, orders) -> list:
        if isinstance(cursor, DocumentSnapshot) or hasattr(cursor, "to_dict") and hasattr(cursor, "reference"):
            data = cursor.to_dict() or {}
            out = []
            for p, _ in orders:
                if p == ("__name__",):
                    out.append(cursor.id)
                else:
                    v = _get_path(data, p)
                    if v is _MISSING:
                        raise ValueError(f"el cursor no tiene el campo {'.'.join(p)}")
                    out.append(v)
            return out
        if isinstance(cursor, dict):
            out = []
            for p, _ in orders:
                v = _get_path(cursor, p)
                if v is _MISSING:
                    break
                out.append(v)
            return out
        return list(cursor)

    def _cursor_sql(self, cursor, inclusive: bool, is_start: bool, orders) -> tuple[str, list]:
        values = self._cursor_values(cursor, orders)
        terms, params = [], []
        eq_prefix, eq_params = [], []
        for i, v in enumerate(values):
            p, d = orders[i]
            forward = (d == "ASCENDING") == is_start
            cmp = ">" if forward else "<"
            if p == ("__name__",):
                k, kp, lit, lp = "id", [], "%s", [_id_of(v)]
            else:
                k, kp = _key_sql(p)
                lit, lp = _lit_key(v)
            terms.append("(" + " AND ".join(eq_prefix + [f"{k} {cmp} {lit}"]) + ")")
            params += eq_params + kp + lp
            eq_prefix = eq_prefix + [f"{k} = {lit}"]
            eq_params = eq_params + kp + lp
        if inclusive and values:
            terms.append("(" + " AND ".join(eq_prefix) + ")")
            params += eq_params
        return ("(" + " OR ".join(terms) + ")" if terms else "TRUE"), params

    def _compile(self, count_only=False) -> tuple[str, list]:
        where, params, ineq = ["collection = %s"], [self._collection], set()
        for f in self._filters:
            s, p, i = _filter_sql(f)
            where.append(f"({s})")
            params += p
            ineq |= i
        orders = self._effective_orders(ineq)
        for p, _ in orders:
            if p != ("__name__",):
                where.append("(data #> %s::text[]) IS NOT NULL")
                params.append(list(p))
        if self._start:
            s, p = self._cursor_sql(self._start[0], self._start[1], True, orders)
            where.append(s)
            params += p
        if self._end:
            s, p = self._cursor_sql(self._end[0], self._end[1], False, orders)
            where.append(s)
            params += p
        order_sql = []
        for p, d in orders:
            dd = "DESC" if d == "DESCENDING" else "ASC"
            if p == ("__name__",):
                order_sql.append(f"id {dd}")
            else:
                for k, kp in _key_parts(p):  # componente a componente: ORDER BY ROW() ignoraría COLLATE "C"
                    order_sql.append(f"{k} {dd}")
                    params += kp
        sql = "SELECT id, data, create_time, update_time FROM fs_docs WHERE " + " AND ".join(where)
        sql += " ORDER BY " + ", ".join(order_sql)
        if self._limit is not None:
            sql += " LIMIT %s"
            params.append(self._limit)
        if self._offset:
            sql += " OFFSET %s"
            params.append(self._offset)
        if count_only:
            sql = f"SELECT count(*) FROM ({sql}) q"
        return sql, params

    # -- ejecución
    def stream(self, transaction: "Transaction | None" = None) -> Iterator[DocumentSnapshot]:
        sql, params = self._compile()
        if transaction is not None:
            rows = transaction._query(sql, params)
        else:
            with self._client.pool.connection() as conn:
                rows = conn.execute(sql, params).fetchall()
        for doc_id, data, ct, ut in rows:
            ref = DocumentReference(self._client, self._collection, doc_id)
            yield _project(DocumentSnapshot(ref, decode(data), ct, ut), self._select)

    def get(self, transaction=None) -> list[DocumentSnapshot]:
        return list(self.stream(transaction=transaction))

    def count(self, alias: str | None = None):
        q = self

        class _Agg:
            def get(self_inner, transaction=None):
                sql, params = q._compile(count_only=True)
                with q._client.pool.connection() as conn:
                    n = conn.execute(sql, params).fetchone()[0]
                return [[AggregationResult(alias or "field_1", n, _now())]]

        return _Agg()


class CollectionReference(Query):
    @property
    def id(self) -> str:
        return self._collection

    def document(self, document_id: str | None = None) -> DocumentReference:
        if document_id is None:
            document_id = "".join(random.choice(_AUTO_ID_CHARS) for _ in range(20))
        return DocumentReference(self._client, self._collection, document_id)

    def add(self, document_data: dict, document_id: str | None = None):
        ref = self.document(document_id)
        res = ref.create(document_data)
        return res.update_time, ref

    def list_documents(self) -> Iterator[DocumentReference]:
        with self._client.pool.connection() as conn:
            rows = conn.execute("SELECT id FROM fs_docs WHERE collection=%s ORDER BY id", (self._collection,)).fetchall()
        for (doc_id,) in rows:
            yield DocumentReference(self._client, self._collection, doc_id)


# --------------------------------------------------------------------------- batch y transacciones
class WriteBatch:
    def __init__(self, client: "PgClient"):
        self._client = client
        self._writes: list = []

    def set(self, reference, document_data, merge=False):
        self._writes.append((reference, "set", document_data, merge))
        return self

    def create(self, reference, document_data):
        self._writes.append((reference, "create", document_data, False))
        return self

    def update(self, reference, field_updates):
        self._writes.append((reference, "update", field_updates, False))
        return self

    def delete(self, reference):
        self._writes.append((reference, "delete", None, False))
        return self

    def __len__(self):
        return len(self._writes)

    def commit(self):
        now = _now()
        with self._client.pool.connection() as conn:
            with conn.transaction():
                # orden fijo de bloqueo para evitar interbloqueos entre lotes
                for ref in sorted({r.path: r for r, *_ in self._writes}.values(), key=lambda r: r.path):
                    _lock(conn, ref._collection, ref.id)
                for ref, op, data, merge in self._writes:
                    _write(conn, ref._collection, ref.id, op, data, merge, now)
        n = len(self._writes)
        self._writes = []
        return [WriteResult(now)] * n


class Transaction(WriteBatch):
    """Transacción de lectura-escritura: las lecturas bloquean el documento hasta el commit."""

    def __init__(self, client: "PgClient", max_attempts: int = 5):
        super().__init__(client)
        self._max_attempts = max_attempts
        self._conn = None
        self._ctx = None

    @property
    def in_progress(self) -> bool:
        return self._conn is not None

    def _begin(self):
        self._ctx = self._client.pool.connection()
        self._conn = self._ctx.__enter__()  # psycopg 3 abre la transacción en la 1.ª sentencia
        self._writes = []

    def _finish(self, commit: bool):
        try:
            if commit:
                now = _now()
                for ref, op, data, merge in self._writes:
                    _write(self._conn, ref._collection, ref.id, op, data, merge, now)
                self._conn.commit()
            else:
                self._conn.rollback()
        except Exception:
            try:
                self._conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            ctx, self._ctx, self._conn = self._ctx, None, None
            self._writes = []
            ctx.__exit__(None, None, None)

    def _get(self, ref: DocumentReference) -> DocumentSnapshot:
        if self._conn is None:
            raise ValueError("la transacción no ha empezado (usa @transactional)")
        if self._writes:
            raise ValueError("en una transacción todas las lecturas van antes de las escrituras")
        _lock(self._conn, ref._collection, ref.id)
        row = _read_row(self._conn, ref._collection, ref.id)
        return DocumentSnapshot(ref, decode(row[0]), row[1], row[2]) if row else DocumentSnapshot(ref, None)

    def _query(self, sql, params):
        if self._conn is None:
            raise ValueError("la transacción no ha empezado (usa @transactional)")
        rows = self._conn.execute(sql, params).fetchall()
        for doc_id, *_ in rows:  # mismo aislamiento que un get(): bloquear lo leído
            _lock(self._conn, params[0], doc_id)  # params[0] = colección (ver Query._compile)
        return rows

    def get(self, ref_or_query):
        if isinstance(ref_or_query, DocumentReference):
            return iter([ref_or_query.get(transaction=self)])
        return ref_or_query.stream(transaction=self)

    def commit(self):  # en Transaction el commit lo hace @transactional
        raise RuntimeError("usa @transactional")


def _retryable(exc: Exception) -> bool:
    code = getattr(exc, "sqlstate", None)
    return code in ("40P01", "40001")  # deadlock_detected, serialization_failure


def transactional(fn):
    """Equivalente a @firestore.transactional: reintenta ante interbloqueo."""
    @functools.wraps(fn)
    def wrapper(transaction: Transaction, *args, **kwargs):
        for attempt in range(transaction._max_attempts):
            transaction._begin()
            try:
                result = fn(transaction, *args, **kwargs)
            except Exception as exc:
                transaction._finish(commit=False)
                if _retryable(exc) and attempt < transaction._max_attempts - 1:
                    time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
                    continue
                raise
            try:
                transaction._finish(commit=True)
            except Exception as exc:
                if _retryable(exc) and attempt < transaction._max_attempts - 1:
                    time.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
                    continue
                raise
            return result
        raise RuntimeError("transacción agotó los reintentos")
    return wrapper


# --------------------------------------------------------------------------- cliente
class PgClient:
    def __init__(self, dsn: str | None = None, pool=None):
        self._dsn = dsn
        self._pool = pool

    @property
    def pool(self):
        if self._pool is None:
            self._pool = get_pool(self._dsn)
        return self._pool

    def collection(self, name: str) -> CollectionReference:
        return CollectionReference(self, name)

    def document(self, path: str) -> DocumentReference:
        coll, doc_id = path.rsplit("/", 1)
        return DocumentReference(self, coll, doc_id)

    def batch(self) -> WriteBatch:
        return WriteBatch(self)

    def transaction(self, max_attempts: int = 5) -> Transaction:
        return Transaction(self, max_attempts=max_attempts)

    def get_all(self, references: Iterable[DocumentReference], field_paths=None, transaction=None):
        refs = list(references)
        if not refs:
            return
        if transaction is not None:
            for r in refs:
                yield _project(transaction._get(r), field_paths)
            return
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT collection, id, data, create_time, update_time FROM fs_docs "
                "WHERE (collection, id) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
                ([r._collection for r in refs], [r.id for r in refs])).fetchall()
        found = {(c, i): (d, ct, ut) for c, i, d, ct, ut in rows}
        for r in refs:
            hit = found.get((r._collection, r.id))
            snap = DocumentSnapshot(r, decode(hit[0]), hit[1], hit[2]) if hit else DocumentSnapshot(r, None)
            yield _project(snap, field_paths)


# --------------------------------------------------------------------------- API async (hilo aparte)
class AsyncDocumentReference:
    def __init__(self, ref: DocumentReference):
        self._ref = ref
        self.id = ref.id
        self.path = ref.path

    async def get(self, field_paths=None):
        return await asyncio.to_thread(self._ref.get, field_paths)

    async def set(self, document_data, merge=False):
        return await asyncio.to_thread(self._ref.set, document_data, merge)

    async def create(self, document_data):
        return await asyncio.to_thread(self._ref.create, document_data)

    async def update(self, field_updates):
        return await asyncio.to_thread(self._ref.update, field_updates)

    async def delete(self):
        return await asyncio.to_thread(self._ref.delete)


class AsyncQuery:
    def __init__(self, q: Query):
        self._q = q

    def __getattr__(self, name):
        attr = getattr(self._q, name)
        if name in ("where", "order_by", "limit", "offset", "select",
                    "start_after", "start_at", "end_before", "end_at"):
            return lambda *a, **k: type(self)(attr(*a, **k))
        raise AttributeError(name)

    async def get(self):
        return await asyncio.to_thread(self._q.get)

    async def stream(self):
        for snap in await asyncio.to_thread(self._q.get):
            yield snap


class AsyncCollectionReference(AsyncQuery):
    def document(self, document_id: str | None = None) -> AsyncDocumentReference:
        return AsyncDocumentReference(self._q.document(document_id))

    async def add(self, document_data, document_id=None):
        return await asyncio.to_thread(self._q.add, document_data, document_id)


# --------------------------------------------------------------------------- API de módulo (como firestore_client)
_client: PgClient | None = None
_async_client = None
_ITER_ALL_PAGE_SIZE = 1000


def get_client() -> PgClient:
    global _client
    if _client is None:
        _client = PgClient()
    return _client


def col(name: str) -> CollectionReference:
    return get_client().collection(f"{COLLECTION_PREFIX}{name}")


def async_col(name: str) -> AsyncCollectionReference:
    return AsyncCollectionReference(col(name))


def iter_all(name: str, page_size: int = _ITER_ALL_PAGE_SIZE, where: tuple | None = None):
    """Misma firma y comportamiento que firestore_client.iter_all."""
    coll = col(name)
    cursor = None
    while True:
        q = coll
        if where is not None:
            field, op, value = where
            q = q.where(filter=FieldFilter(field, op, value))
        q = q.order_by("__name__").limit(page_size)
        if cursor is not None:
            q = q.start_after(cursor)
        page = list(q.stream())
        if not page:
            return
        yield from page
        if len(page) < page_size:
            return
        cursor = page[-1]
