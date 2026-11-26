"""SOLO LECTURA. ¿Por qué la cuenta de GCP_SA_KEY no puede escribir en Cloud Logging?

Con el token de esa cuenta (TOKEN) pregunta a GCP, en el proyecto PROJECT:
  1. si la clave es de github-deployer y si la cuenta es de este proyecto (sí/no);
  2. la política IAM del proyecto (getIamPolicy): qué roles tiene la cuenta, si alguno lleva
     condición, si hay una «github-deployer» de otro proyecto o una borrada (deleted:) con roles;
  3. testIamPermissions de logging.logEntries.create/list;
  4. si las API de Logging y de Resource Manager están activadas.
No imprime correos ni la política: solo sí/no, nombres de roles y motivos de error.
"""
import json
import os
import sys
import urllib.error
import urllib.request

WRITE_ROLES = {"roles/logging.logWriter", "roles/logging.admin", "roles/editor", "roles/owner"}


def call(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def why(resp: dict) -> str:
    """Estado y motivos (ErrorInfo.reason) de un error de Google, sin el mensaje (puede llevar correos)."""
    err = resp.get("error") or {}
    reasons = sorted({d.get("reason") for d in err.get("details") or [] if d.get("reason")})
    return f"{err.get('status') or err.get('code')}" + (f" (motivo: {', '.join(reasons)})" if reasons else "")


def policy_summary(policy: dict, email: str) -> list[str]:
    local, _, domain = email.partition("@")
    me = f"serviceAccount:{email}"
    mine, cond, others, deleted = [], [], [], []
    for b in policy.get("bindings") or []:
        members = b.get("members") or []
        if me in members:
            (cond if b.get("condition") else mine).append(b["role"])
        for m in members:
            parts = m.split(":")      # serviceAccount:x@d · deleted:serviceAccount:x@d?uid=…
            if parts[-1].split("@")[0] == local and m != me:
                if parts[0] == "deleted":
                    deleted.append(b["role"])
                elif parts[0] == "serviceAccount":
                    others.append(b["role"])
    out = [f"roles de la cuenta (sin condición): {sorted(mine) or 'ninguno'}",
           f"roles de la cuenta con condición IAM: {sorted(cond) or 'ninguno'}",
           f"¿algún rol suyo permite escribir logs?: {'sí' if WRITE_ROLES & set(mine) else 'no'}"
           + (" (hay uno con condición: puede no aplicarse)" if WRITE_ROLES & set(cond) else ""),
           f"«github-deployer» de OTRO proyecto con roles aquí: {sorted(others) or 'no'}",
           f"«github-deployer» BORRADA (deleted:) con roles aquí: {sorted(deleted) or 'no'}"]
    return out


def main() -> int:
    token, project = os.environ["TOKEN"], os.environ["PROJECT"]
    with open(os.environ["GOOGLE_APPLICATION_CREDENTIALS"], encoding="utf-8") as f:
        email = json.load(f).get("client_email", "")
    local, _, domain = email.partition("@")
    print(f"¿la clave de GCP_SA_KEY es de github-deployer?: {'sí' if local == 'github-deployer' else 'no'}")
    print(f"¿la cuenta es de este mismo proyecto?: {'sí' if domain.startswith(project + '.') else 'no'}")

    st, pol = call("POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:getIamPolicy",
                   token, {"options": {"requestedPolicyVersion": 3}})
    if st == 200:
        print("política IAM del proyecto: leída")
        for line in policy_summary(pol, email):
            print("  " + line)
    else:
        print(f"política IAM del proyecto: no se puede leer con esta cuenta — {why(pol)}")

    st, r = call("POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
                 token, {"permissions": ["logging.logEntries.create", "logging.logEntries.list"]})
    print("testIamPermissions: " + (str(r.get("permissions") or "ninguno") if st == 200 else why(r)))

    for api in ("logging.googleapis.com", "cloudresourcemanager.googleapis.com"):
        st, r = call("GET", f"https://serviceusage.googleapis.com/v1/projects/{project}/services/{api}", token)
        print(f"API {api}: " + (r.get("state", "?") if st == 200 else f"no se puede consultar — {why(r)}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
