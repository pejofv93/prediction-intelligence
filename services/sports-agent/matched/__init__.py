"""
matched/ — Detector matched-betting / surebets (motor back/lay unificado).

Reemplaza collectors/arbitrage_detector.py (roto: comparaba solo back-back y
etiquetaba "Lay" a un back). Aquí el LAY es real: The Odds API markets=h2h_lay,
bookmaker betfair_ex_eu (única fuente de lay; Betfair directo bloqueado por IP en GCP).

Un solo motor, dos umbrales sobre el mismo cálculo back(casa) vs lay(Betfair):
  - rating qualifying positivo  → SUREBET (beneficio garantizado sin bono)
  - rating pequeño-negativo      → COVERAGE (buen qualifying para cubrir un bono)

Fase 1: escáner + detector + persistencia Firestore. Sin alertas Telegram (Fase 2).
"""
