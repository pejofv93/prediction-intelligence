"""
dashboard — FastAPI service con Basic Auth
Sirve la API REST y el frontend React compilado como archivos estaticos.
Todas las rutas protegidas con Basic Auth excepto /health.
"""
import logging
import os
import secrets

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from api import backtest, calculator, odds_finder, poly_stats, polymarket, predictions, shadow, tracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="dashboard")
security = HTTPBasic()

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS", "changeme")


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Basic Auth — protege todas las rutas excepto /health."""
    correct_user = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    correct_pass = secrets.compare_digest(credentials.password, DASHBOARD_PASS)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenciales incorrectas",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# API routes (protegidas con Basic Auth)
app.include_router(
    predictions.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    polymarket.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    poly_stats.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    calculator.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    odds_finder.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    tracker.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    shadow.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)
app.include_router(
    backtest.router,
    prefix="/api",
    dependencies=[Depends(verify_credentials)],
)

# Servir frontend React compilado (build en /app/static por el Dockerfile)
try:
    app.mount("/", StaticFiles(directory="static", html=True), name="static")
except RuntimeError:
    logger.warning("frontend/static no encontrado — ejecutar npm run build primero")
