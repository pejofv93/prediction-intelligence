# Estado del sistema — 19 junio 2026

## Resumen ejecutivo

Sesión corta: diagnóstico y cierre definitivo de la integración Betfair Exchange.
El sistema de cuotas sigue operativo sin cambios — The Odds API + odds-api.io cubren
todos los mercados activos (1X2, AH, OU 2.5, BTTS, corners, bookings).

---

## Betfair Exchange — APARCADO (bloqueo WAF por IP)

### Estado de la integración

| Elemento                        | Estado                                       |
|---------------------------------|----------------------------------------------|
| Cuenta Betfair                  | Activa — pejofeve@hotmail.com                |
| App Key (Delayed 1.0-DELAY)     | Obtenida y configurada                       |
| `BETFAIR_USERNAME`              | Configurada en Cloud Run                     |
| `BETFAIR_PASSWORD`              | Configurada en Cloud Run                     |
| `BETFAIR_APP_KEY`               | Configurada en Cloud Run                     |
| Login desde IP residencial      | ✓ Funciona (verificado con get_betfair_appkey.py) |
| Login desde Cloud Run (GCP)     | ✗ 403 Forbidden — WAF antifraude             |

### Causa raíz confirmada

Betfair bloquea con HTTP 403 las IPs de datacenter de GCP en
`identitysso.betfair.es/api/login`. El bloqueo ocurre **antes** de validar
credenciales — es un filtro por ASN/IP, no por headers ni User-Agent.

Variantes probadas sin éxito desde Cloud Run:
1. `X-Application: <real_app_key>` → 403
2. `X-Application: "1"` (igual que el script local) → 403
3. `User-Agent: Mozilla/5.0 Chrome/125` → 403

El mismo request con exactamente los mismos headers funciona desde IP residencial.

### Decisión

**Betfair aparcado** hasta disponer de proxy residencial de pago.
Sin proxy (Bright Data, Oxylabs, Smartproxy), no es viable desde ningún proveedor
cloud (GCP, AWS, Railway, Fly.io — todos tienen ASNs bloqueados por Betfair).

### Código

- `services/sports-agent/clients/betfair_client.py` — conservado íntegro, con
  comentario de estado al inicio del módulo.
- `services/sports-agent/main.py` endpoint `/test-betfair` — devuelve
  `{"ok": false, "disabled": true, "reason": "..."}` inmediatamente. El código
  funcional queda debajo del return, inaccesible pero preservado.

### Impacto en cobertura de cuotas

Ninguno. Betfair era candidata a fuente de respaldo sin cuota mensual, pero
todos los mercados están ya cubiertos:

| Mercado       | Fuente activa                            |
|---------------|------------------------------------------|
| h2h (1X2)     | odds-api.io (primaria) + The Odds API    |
| Asian Handicap| odds-api.io + The Odds API spreads       |
| Totals OU 2.5 | odds-api.io → The Odds API → TOA sintético |
| BTTS          | odds-api.io + The Odds API btts          |
| Corners O/U   | OddsPapi fixtures                        |
| Bookings O/U  | OddsPapi fixtures                        |

---

## Infraestructura — sin cambios

| Servicio          | Estado   | Revisión                                                          |
|-------------------|----------|-------------------------------------------------------------------|
| sports-agent      | LIVE     | `sports-agent-327240737877.europe-west1.run.app` rev post-`b17338c` |
| polymarket-agent  | LIVE     | Cloud Run europe-west1                                            |
| telegram-bot      | LIVE     | Cloud Run europe-west1                                            |
| GitHub Actions    | LIVE     | 4 workflows analyze (01:00 / 07:00 / 13:00 / 19:00 UTC)          |

---

## Commits de esta sesión

| Commit    | Descripción                                                         |
|-----------|---------------------------------------------------------------------|
| `133d3ab` | fix(betfair): usar X-Application: 1 en SSO login                   |
| `b17338c` | fix(betfair): añadir User-Agent Chrome — ambos sin efecto (WAF IP) |
| siguiente | docs: Betfair desactivado, endpoint disabled, comentario módulo     |

---

## Estado WC26 (sin cambios respecto al 15 junio)

Mundial 2026 en curso (fase de grupos hasta 2 julio). Señales operativas con
ELO FIFA real, FORM_ELO_CONFLICT y RISING_ODDS_BLOCK activos.
Ver `ESTADO_15_JUNIO_2026.md` para detalle completo.

---

## Próximos pasos (sin cambio de prioridad)

1. **[URGENTE] Investigar ROI -14.6%** — separar por mercado/liga antes de agosto.
2. **[30 junio] Verificar Wimbledon en odds-api.io** — `/api/oddsapiio-coverage`.
3. **[1 octubre] Verificar NBA/Euroleague en odds-api.io** — mismo endpoint.
4. **[Agosto] Auditar mercados alt en ligas EU** cuando arranque la temporada.
5. **[Fase KO WC — 3 julio]** — verificar AWAY_GATE_CONF sin mercados de empate.
6. **[Futuro con proxy] Betfair** — reactivar `clients/betfair_client.py` añadiendo
   proxy residencial al `httpx.AsyncClient`. Todo lo demás ya está listo.
