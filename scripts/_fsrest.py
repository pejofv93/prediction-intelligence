"""
Cliente Firestore mínimo sobre la API REST, para scripts one-shot.

Por qué existe: en el puesto de trabajo el proxy TLS de Norton rompe gRPC
(google-cloud-firestore se queda colgado indefinidamente) pero HTTPS normal sí
funciona. Los scripts de diagnóstico y reconstrucción necesitan poder correr
tanto en CI (donde el cliente oficial funciona) como en local (donde no), así
que hablan contra la interfaz mínima de este módulo:

    read_collection(name)  -> list[dict]   (cada dict lleva "_id")
    write_docs(name, docs) -> int
    delete_docs(name, ids) -> int

`get_db(transport)` devuelve una implementación REST o una que envuelve al
cliente oficial, con la misma interfaz.

Auth REST: token de `gcloud auth print-access-token`. No se persiste en disco.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

_BASE = "https://firestore.googleapis.com/v1"
_TOKEN_TTL = 45 * 60  # los tokens de gcloud duran 1h; renovar antes


# ── Codificación de valores Firestore ────────────────────────────────────────

def encode(v):
    """python → Value de la API REST de Firestore."""
    if v is None:
        return {"nullValue": None}
    if isinstance(v, bool):
        return {"booleanValue": v}
    if isinstance(v, int):
        return {"integerValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    if isinstance(v, datetime):
        dt = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return {"timestampValue": dt.astimezone(timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ")}
    if isinstance(v, (list, tuple)):
        return {"arrayValue": {"values": [encode(x) for x in v]}}
    if isinstance(v, dict):
        return {"mapValue": {"fields": {str(k): encode(x) for k, x in v.items()}}}
    return {"stringValue": str(v)}


def decode(v):
    """Value de la API REST de Firestore → python."""
    kind, raw = next(iter(v.items()))
    if kind == "integerValue":
        return int(raw)
    if kind == "doubleValue":
        return float(raw)
    if kind == "booleanValue":
        return bool(raw)
    if kind == "nullValue":
        return None
    if kind == "timestampValue":
        return raw
    if kind == "arrayValue":
        return [decode(x) for x in raw.get("values", [])]
    if kind == "mapValue":
        return {k: decode(x) for k, x in raw.get("fields", {}).items()}
    return raw


# ── Implementación REST ──────────────────────────────────────────────────────

class RestDB:
    def __init__(self, project: str, prefix: str = "", account: str | None = None):
        self.project = project
        self.prefix = prefix
        self.account = account
        self._token = ""
        self._token_at = 0.0

    # -- auth --
    def _access_token(self) -> str:
        if self._token and (time.time() - self._token_at) < _TOKEN_TTL:
            return self._token
        # En Windows el ejecutable es gcloud.cmd; shutil.which lo resuelve en ambos SO.
        exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
        if not exe:
            raise RuntimeError("gcloud no está en el PATH — necesario para el token REST")
        cmd = [exe, "auth", "print-access-token"]
        if self.account:
            cmd += ["--account", self.account]
        out = subprocess.run(cmd, capture_output=True, text=True, shell=False)
        if out.returncode != 0:
            raise RuntimeError(f"gcloud auth print-access-token falló: {out.stderr[:200]}")
        self._token = out.stdout.strip()
        self._token_at = time.time()
        return self._token

    def _call(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        url = f"{_BASE}/projects/{self.project}/databases/(default)/documents{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._access_token()}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode() or "{}")

    def _doc_name(self, col: str, doc_id: str) -> str:
        return (f"projects/{self.project}/databases/(default)/documents/"
                f"{self.prefix}{col}/{urllib.parse.quote(str(doc_id), safe='')}")

    # -- interfaz --
    def read_collection(self, name: str) -> list[dict]:
        docs, token = [], ""
        while True:
            q = "?pageSize=300" + (f"&pageToken={token}" if token else "")
            page = self._call(f"/{self.prefix}{name}{q}")
            for d in page.get("documents", []):
                rec = {k: decode(v) for k, v in d.get("fields", {}).items()}
                rec["_id"] = d["name"].split("/")[-1]
                docs.append(rec)
            token = page.get("nextPageToken", "")
            if not token:
                return docs

    def write_docs(self, name: str, docs: dict[str, dict]) -> int:
        items = list(docs.items())
        written = 0
        for i in range(0, len(items), 200):          # 500 es el tope duro; 200 va sobrado
            writes = [
                {"update": {"name": self._doc_name(name, did),
                            "fields": {k: encode(v) for k, v in body.items()}}}
                for did, body in items[i:i + 200]
            ]
            self._call(":batchWrite", "POST", {"writes": writes})
            written += len(writes)
        return written

    def delete_docs(self, name: str, ids: list[str]) -> int:
        deleted = 0
        for i in range(0, len(ids), 200):
            writes = [{"delete": self._doc_name(name, did)} for did in ids[i:i + 200]]
            self._call(":batchWrite", "POST", {"writes": writes})
            deleted += len(writes)
        return deleted


# ── Implementación sobre el cliente oficial (CI / Cloud Run) ─────────────────

class GrpcDB:
    def __init__(self, project: str, prefix: str = ""):
        from google.cloud import firestore
        self.db = firestore.Client(project=project)
        self.prefix = prefix

    def read_collection(self, name: str) -> list[dict]:
        out = []
        for d in self.db.collection(f"{self.prefix}{name}").stream():
            rec = d.to_dict() or {}
            rec["_id"] = d.id
            out.append(rec)
        return out

    def write_docs(self, name: str, docs: dict[str, dict]) -> int:
        col = self.db.collection(f"{self.prefix}{name}")
        items = list(docs.items())
        for i in range(0, len(items), 400):
            batch = self.db.batch()
            for did, body in items[i:i + 400]:
                batch.set(col.document(str(did)), body)
            batch.commit()
        return len(items)

    def delete_docs(self, name: str, ids: list[str]) -> int:
        col = self.db.collection(f"{self.prefix}{name}")
        for i in range(0, len(ids), 400):
            batch = self.db.batch()
            for did in ids[i:i + 400]:
                batch.delete(col.document(str(did)))
            batch.commit()
        return len(ids)


def get_db(transport: str, project: str, prefix: str, account: str | None = None):
    """transport: 'rest' | 'grpc'. REST para local (gRPC bloqueado por el proxy TLS)."""
    if transport == "rest":
        return RestDB(project, prefix, account)
    return GrpcDB(project, prefix)
