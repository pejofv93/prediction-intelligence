"""
scripts/get_betfair_appkey.py

Solo hace login contra identitysso.betfair.es e imprime
el ssoid completo para pegarlo en el demo tool:
  apps.betfair.com/visualisers/api-ng-account-operations/

Uso:
    python scripts/get_betfair_appkey.py

Requiere:
    pip install httpx
"""
import httpx

BETFAIR_USERNAME = "pejofeve@hotmail.com"
BETFAIR_PASSWORD = "Pjofutbolistas21!"

SSO_URL = "https://identitysso.betfair.es/api/login"


if __name__ == "__main__":
    resp = httpx.post(
        SSO_URL,
        data={"username": BETFAIR_USERNAME, "password": BETFAIR_PASSWORD},
        headers={
            "Accept":        "application/json",
            "Content-Type":  "application/x-www-form-urlencoded",
            "X-Application": "1",
        },
        verify=False,  # Norton MITM workaround
    )
    resp.raise_for_status()
    body   = resp.json()
    status = body.get("status", "")
    token  = body.get("token", "")

    if status != "SUCCESS" or not token:
        print(f"[ERROR] Login fallido: status={status!r}  error={body.get('error')!r}")
        raise SystemExit(1)

    print(f"\nssoid:\n{token}\n")
    print("Pega ese valor en el campo 'ssoid' del demo tool y pulsa Execute.")
