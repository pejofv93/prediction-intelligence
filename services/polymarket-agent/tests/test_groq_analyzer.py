"""
Tests para groq_analyzer.py — verifica bugs 1, 2 y 3 del audit (2026-05-15).

Diseño TDD: cada test FALLA con el código actual y PASA después del fix.
Los tests de pure-regex (BUG-1) fallan inmediatamente sin ningún mock.
Los tests de integrate (BUG-2, BUG-3) mockean Firestore y Groq.

Ejecutar:
    cd prediction-intelligence-ok
    python -m pytest services/polymarket-agent/tests/ -v
"""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# ══════════════════════════════════════════════════════════════════════════════
# Helpers de factory
# ══════════════════════════════════════════════════════════════════════════════

_MARKET_ID = "test-market-audit-001"


def _enriched(
    price_yes: float = 0.50,
    news_trend: str = "NO_DATA",
    news_headlines: list | None = None,
    correlations: list | None = None,
    volume_spike: bool = False,
    smart_money: bool = False,
    **extra,
) -> dict:
    """Crea un enriched_market mínimo para tests."""
    return {
        "market_id": _MARKET_ID,
        "price_yes": price_yes,
        "volume_spike": volume_spike,
        "price_momentum": "STABLE",
        "orderbook": {
            "buy_pressure": 0.50,
            "spread": 0.02,
            "imbalance_signal": "NEUTRAL",
        },
        "news_sentiment": {
            "score": 0.0,
            "trend": news_trend,
            "headlines": news_headlines or [],
        },
        "smart_money": {"is_smart_money": smart_money},
        "arbitrage": {"detected": False, "inefficiency": 0.0},
        "correlations": correlations or [],
        "ctc_price": None,
        **extra,
    }


def _market_doc(
    question: str,
    price_yes: float = 0.50,
    volume_24h: float = 20_000,
    days: int = 30,
    slug: str = "test-slug",
) -> dict:
    """Crea un market document como vendría de Firestore poly_markets."""
    return {
        "question": question,
        "price_yes": price_yes,
        "volume_24h": volume_24h,
        "end_date": datetime.now(timezone.utc) + timedelta(days=days),
        "slug": slug,
    }


def _groq_json(
    real_prob: float,
    price_yes: float,
    recommendation: str | None = None,
    confidence: float = 0.70,
    trend: str = "STABLE",
    reasoning: str = "Test reasoning from mock LLM",
) -> str:
    """Genera el string JSON que devolvería el LLM de Groq."""
    if recommendation is None:
        if real_prob > price_yes + 0.01:
            recommendation = "BUY_YES"
        elif real_prob < price_yes - 0.01:
            recommendation = "BUY_NO"
        else:
            recommendation = "PASS"
    return json.dumps({
        "real_prob": real_prob,
        "edge": round(real_prob - price_yes, 4),
        "confidence": confidence,
        "trend": trend,
        "recommendation": recommendation,
        "key_factors": ["mock_factor"],
        "reasoning": reasoning,
    })


def _col_mock(market_data: dict, saved_docs: list | None = None):
    """
    Factory que retorna una función side_effect para patch("shared.firestore_client.col").
    Intercepta saves en poly_predictions → saved_docs list.
    """
    def col_fn(name: str):
        coll = MagicMock()
        if name == "poly_markets":
            doc = MagicMock()
            doc.exists = True
            doc.to_dict.return_value = market_data
            coll.document.return_value.get.return_value = doc
        elif name == "poly_predictions":
            prev = MagicMock()
            prev.exists = False
            prev.to_dict.return_value = {}
            coll.document.return_value.get.return_value = prev
            if saved_docs is not None:
                coll.document.return_value.set.side_effect = \
                    lambda d: saved_docs.append(d)
        else:  # agent_state, poly_model_weights, etc.
            dummy = MagicMock()
            dummy.exists = False
            coll.document.return_value.get.return_value = dummy
        return coll
    return col_fn


def _groq_client_mock(response_json: str) -> MagicMock:
    """Crea un mock del cliente OpenAI/Groq que devuelve un JSON fijo."""
    client = MagicMock()
    msg = MagicMock()
    msg.content = response_json
    client.chat.completions.create.return_value.choices = [MagicMock(message=msg)]
    return client


# ══════════════════════════════════════════════════════════════════════════════
# BUG 1 — _TENNIS_RE contiene palabras genéricas (toronto, cincinnati, rome)
# ══════════════════════════════════════════════════════════════════════════════

class TestTennisRE:
    """
    _TENNIS_RE debe detectar torneos de tenis reales sin falsos positivos
    en mercados NBA, NFL, política o fútbol.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        from groq_analyzer import _TENNIS_RE
        self.re = _TENNIS_RE

    # ── Falsos positivos que deben DESAPARECER después del fix ────────────────

    def test_toronto_nba_not_tennis(self):
        """
        'toronto' en _TENNIS_RE causa que mercados de Toronto Raptors (NBA)
        sean tratados como tenis → sin odds de tenis → mercado descartado.
        FALLA antes del fix. PASA después.
        """
        result = self.re.search("Will Toronto Raptors win NBA Finals?")
        assert result is None, (
            f"'toronto' activó _TENNIS_RE en mercado NBA. "
            f"Match: '{result.group()}'. "
            f"Eliminar 'toronto' del regex."
        )

    def test_cincinnati_nfl_not_tennis(self):
        """
        'cincinnati' en _TENNIS_RE → mercados de Cincinnati Bengals (NFL)
        tratados como tenis.
        """
        result = self.re.search("Will Cincinnati Bengals win the Super Bowl?")
        assert result is None, (
            f"'cincinnati' activó _TENNIS_RE en mercado NFL. "
            f"Match: '{result.group()}'. "
            f"Eliminar 'cincinnati' del regex."
        )

    def test_rome_politics_not_tennis(self):
        """
        'rome' en _TENNIS_RE → preguntas políticas sobre Roma o Roma FC
        categorizadas como 'sports' y luego tratadas como tenis.
        """
        result = self.re.search("Will Italy resolve its Rome government crisis by 2026?")
        assert result is None, (
            f"'rome' activó _TENNIS_RE en contexto político. "
            f"Match: '{result.group()}'. "
            f"Eliminar 'rome' del regex."
        )

    # ── Keywords reales que deben seguir funcionando ──────────────────────────

    def test_wta_always_detected(self):
        """WTA siempre debe activar la detección de tenis."""
        assert self.re.search("Parma: Solana Sierra vs Kaja Juvan WTA"), \
            "WTA no detectado — error en el regex base"

    def test_atp_always_detected(self):
        assert self.re.search("ATP Masters 1000 Madrid")

    def test_roland_garros_always_detected(self):
        assert self.re.search("Will Iga Swiatek win Roland Garros?")

    def test_wimbledon_always_detected(self):
        assert self.re.search("Will Djokovic win Wimbledon 2026?")

    def test_tennis_keyword_always_detected(self):
        assert self.re.search("Tennis Grand Slam prediction 2026")

    # ── Nuevas keywords que deben añadirse ───────────────────────────────────

    def test_french_open_now_detected(self):
        """
        'French Open' no está en _TENNIS_RE actual → mercados de Roland Garros
        en inglés categorizados como 'other' en lugar de 'sports'.
        FALLA antes del fix. PASA después.
        """
        result = self.re.search("Will Carlos Alcaraz win the French Open?")
        assert result is not None, (
            "'french open' no detectado por _TENNIS_RE. "
            "Mercados de Roland Garros en inglés no reciben contexto deportivo."
        )

    def test_italian_open_now_detected(self):
        """
        'Italian Open' no está en _TENNIS_RE → bug original de Solana Sierra
        (el torneo 'Italian Open' no se detecta aunque sea tenis real).
        FALLA antes del fix. PASA después.
        """
        result = self.re.search("Will Novak Djokovic win the Italian Open?")
        assert result is not None, (
            "'italian open' no detectado por _TENNIS_RE. "
            "Causa el bug de Solana Sierra cuando el slug no empieza por 'wta-'."
        )


class TestCategorizeTennis:
    """
    categorize_market() debe retornar 'sports' para mercados de tenis
    y NOT activar la categoría 'crypto' para nombres con 'solana'.
    """

    def test_solana_sierra_roland_garros_is_sports(self):
        """
        'Solana' como apellido + Roland Garros → categoría 'sports', no 'crypto'.
        El override de tenis en categorize_market corre antes del scan de crypto.
        """
        from groq_analyzer import categorize_market
        cat = categorize_market("Will Solana Sierra win Roland Garros?")
        assert cat == "sports", (
            f"'Solana Sierra' con 'Roland Garros' categorizó como '{cat}'. "
            f"El override de _TENNIS_RE debe correr antes de los keywords de crypto."
        )

    def test_french_open_categorized_as_sports(self):
        """
        'French Open' sin 'tennis' explícito → debe ser 'sports' tras el fix.
        FALLA antes del fix (categoría = 'other'). PASA después.
        """
        from groq_analyzer import categorize_market
        cat = categorize_market("Will X win the French Open?")
        assert cat == "sports", (
            f"'French Open' categorizó como '{cat}' en lugar de 'sports'. "
            f"Añadir 'french open' a _TENNIS_RE y a CATEGORY_KEYWORDS['sports']."
        )

    def test_toronto_raptors_is_sports(self):
        """Toronto Raptors → siempre 'sports' (correcto con y sin el fix)."""
        from groq_analyzer import categorize_market
        assert categorize_market("Will Toronto Raptors win the NBA Finals?") == "sports"

    def test_btc_price_is_crypto(self):
        """Mercado crypto puro no se ve afectado."""
        from groq_analyzer import categorize_market
        assert categorize_market("Will BTC reach $100,000?") == "crypto"


class TestIsTennisFlagDerivation:
    """
    La variable _is_tennis se deriva de slug + _TENNIS_RE.
    Verificar que la lógica produce el resultado correcto tras el fix.
    """

    def test_toronto_nba_slug_not_tennis(self):
        """
        Slug NBA + pregunta NBA → _is_tennis = False.
        Antes del fix: _is_tennis = True → TENNIS_NO_DATA → mercado descartado.
        """
        from groq_analyzer import _TENNIS_RE
        question = "Will Toronto Raptors win the NBA Finals?"
        slug = "nba-toronto-raptors-2026-finals"
        is_tennis = slug.startswith(("atp-", "wta-")) or bool(_TENNIS_RE.search(question))
        assert not is_tennis, (
            f"_is_tennis=True para '{slug}'. "
            f"El mercado será descartado por TENNIS_NO_DATA (busca odds de tenis, no los encuentra)."
        )

    def test_wta_parma_slug_is_tennis(self):
        """Slug WTA → _is_tennis siempre True (no cambia con el fix)."""
        from groq_analyzer import _TENNIS_RE
        question = "Parma: Solana Sierra vs Kaja Juvan"
        slug = "wta-parma-solana-sierra-vs-kaja-juvan"
        is_tennis = slug.startswith(("atp-", "wta-")) or bool(_TENNIS_RE.search(question))
        assert is_tennis

    def test_french_open_question_is_tennis_after_fix(self):
        """
        Pregunta 'French Open' sin slug ATP/WTA → _is_tennis debe ser True tras el fix.
        FALLA antes del fix.
        """
        from groq_analyzer import _TENNIS_RE
        question = "Will Carlos Alcaraz win the French Open?"
        slug = "french-open-2026-mens-winner"  # slug genérico sin prefijo atp-
        is_tennis = slug.startswith(("atp-", "wta-")) or bool(_TENNIS_RE.search(question))
        assert is_tennis, (
            f"_is_tennis=False para '{question}'. "
            f"Mercado de French Open no recibirá contexto de odds de tenis."
        )


# ══════════════════════════════════════════════════════════════════════════════
# BUG 3 — HIGH_PRICE_VOL_FLOOR no actualiza real_prob en Firestore
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_high_price_vol_floor_updates_real_prob():
    """
    BUG-3A: Cuando HIGH_PRICE_VOL_FLOOR activa (price>85%, vol>$50k, real_prob<floor),
    debe actualizar real_prob AL VALOR DEL FLOOR antes de guardar en Firestore.

    Escenario:
      price_yes = 0.94, volume = $60k
      LLM estima real_prob = 0.50 (muy pesimista)
      floor = 0.94 - 0.10 = 0.84

    ANTES del fix: Firestore.set() recibe real_prob=0.50 → ancla corrupta
    DESPUÉS del fix: Firestore.set() recibe real_prob=0.84 → correcto
    """
    price_yes = 0.94
    volume_24h = 60_000
    llm_real_prob = 0.50
    expected_floor = round(price_yes - 0.10, 4)  # 0.84

    market = _market_doc(
        question="Will the Fed keep rates unchanged through 2026?",
        price_yes=price_yes,
        volume_24h=volume_24h,
        slug="fed-rates-unchanged-2026",
    )
    saved_docs: list[dict] = []
    em = _enriched(
        price_yes=price_yes,
        news_trend="POSITIVE",
        news_headlines=["Fed signals prolonged pause on rate hikes"],
    )

    with (
        patch("shared.firestore_client.col",
              side_effect=_col_mock(market, saved_docs)),
        patch("shared.groq_client._get_groq",
              return_value=_groq_client_mock(
                  _groq_json(llm_real_prob, price_yes, recommendation="BUY_NO")
              )),
        patch("groq_analyzer._is_groq_quota_exhausted", return_value=False),
        patch("groq_analyzer._get_poly_weights", return_value={}),
    ):
        from groq_analyzer import analyze_market
        result = await analyze_market(em)

    # ── Verificaciones básicas
    assert result is not None, \
        "analyze_market devolvió None — el mercado fue descartado prematuramente"

    assert result["recommendation"] == "PASS", (
        f"Expected recommendation=PASS (HIGH_PRICE_VOL_FLOOR convierte BUY_NO→PASS), "
        f"got '{result['recommendation']}'"
    )

    # ── La clave: real_prob en Firestore debe ser el floor, no el valor del LLM
    assert saved_docs, "analyze_market no llamó a Firestore.set() — no se guardó nada"

    saved_rp = saved_docs[0]["real_prob"]
    assert saved_rp == expected_floor, (
        f"Firestore.set() recibió real_prob={saved_rp:.4f} "
        f"pero debería ser {expected_floor:.4f} (floor = price_yes - 0.10). "
        f"BUG-3A: HIGH_PRICE_VOL_FLOOR cambia 'recommendation' pero no actualiza "
        f"'real_prob' antes de guardar. El ancla de consistencia quedará corrupta "
        f"con {saved_rp:.2f} en lugar del valor ajustado {expected_floor:.2f}."
    )

    # edge en Firestore también debe reflejar el floor
    saved_edge = saved_docs[0]["edge"]
    expected_edge = round(expected_floor - price_yes, 4)  # 0.84 - 0.94 = -0.10
    assert saved_edge == expected_edge, (
        f"edge en Firestore = {saved_edge:.4f}, "
        f"expected {expected_edge:.4f} (= floor - price_yes)"
    )


# ══════════════════════════════════════════════════════════════════════════════
# BUG 2 — La correlación rompe el cap ±15% de mercados 'improvised'
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_improvised_cap_survives_correlation():
    """
    BUG-6A: En mercados 'improvised' (sin noticias/precios externos), el cap ±15%
    aplicado en el paso 26 puede ser roto por la corrección de correlación en el paso 30.

    Secuencia del bug:
      1. LLM real_prob = 0.72, capped a 0.55  (price_yes=0.40, cap_max=0.55)
      2. Correlación dispara (|0.55 - 0.75| = 0.20 > 0.15):
         real_prob = 0.55*0.70 + 0.75*0.30 = 0.61  ← FUERA DEL CAP
      3. Firestore.set() recibe real_prob=0.61

    DESPUÉS del fix: re-aplicar el cap DESPUÉS de la correlación → real_prob ≤ 0.55
    """
    price_yes = 0.40
    cap_max = round(price_yes + 0.15, 4)  # 0.55
    cap_min = round(price_yes - 0.15, 4)  # 0.25

    # 3 shared_keywords → activa la corrección por correlación (umbral ≥ 3)
    correlations = [
        {
            "price_yes": 0.75,       # muy diferente → inconsistencia > 0.15
            "shared_keywords": ["peace", "treaty", "ceasefire"],
            "question": "Will the ceasefire agreement hold through 2026?",
        }
    ]

    market = _market_doc(
        question="Will the peace treaty be signed by end of 2026?",
        price_yes=price_yes,
        volume_24h=8_000,
        slug="peace-treaty-signed-2026",
    )
    saved_docs: list[dict] = []
    em = _enriched(
        price_yes=price_yes,
        news_trend="NO_DATA",   # sin noticias → data_quality = "improvised"
        news_headlines=[],
        correlations=correlations,
    )

    with (
        patch("shared.firestore_client.col",
              side_effect=_col_mock(market, saved_docs)),
        patch("shared.groq_client._get_groq",
              return_value=_groq_client_mock(
                  _groq_json(0.72, price_yes, recommendation="BUY_YES")
              )),
        patch("groq_analyzer._is_groq_quota_exhausted", return_value=False),
        patch("groq_analyzer._get_poly_weights", return_value={}),
    ):
        from groq_analyzer import analyze_market
        result = await analyze_market(em)

    assert result is not None, "analyze_market devolvió None"

    real_prob = result["real_prob"]

    assert real_prob <= cap_max, (
        f"real_prob={real_prob:.4f} excede el cap improvised de {cap_max:.4f}. "
        f"La corrección por correlación rompió el cap ±15%. "
        f"BUG-6A: mover la re-aplicación del cap DESPUÉS del bloque de correlación "
        f"(o añadir una segunda aplicación tras él)."
    )
    assert real_prob >= cap_min, (
        f"real_prob={real_prob:.4f} está por debajo del mínimo improvised de {cap_min:.4f}."
    )

    if saved_docs:
        saved_rp = saved_docs[0]["real_prob"]
        assert saved_rp <= cap_max, (
            f"Firestore.set() recibió real_prob={saved_rp:.4f} > cap {cap_max:.4f}. "
            f"El cap no se re-aplica antes de guardar."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Tests de funciones puras (no necesitan mocks)
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractTargetPrice:
    """_extract_target_price detecta correctamente precios objetivo."""

    @pytest.fixture(autouse=True)
    def _fn(self):
        from groq_analyzer import _extract_target_price
        self.fn = _extract_target_price

    def test_dollar_comma(self):
        assert self.fn("Will BTC reach $100,000?") == 100_000.0

    def test_k_suffix(self):
        assert self.fn("Will ETH hit $10k?") == 10_000.0

    def test_no_price_tennis(self):
        assert self.fn("Will Solana Sierra win the Italian Open?") is None

    def test_no_price_election(self):
        assert self.fn("Will Trump win the 2026 election?") is None

    def test_plain_number(self):
        assert self.fn("Will GOLD reach $3,500?") == 3_500.0


class TestPriceTargetDirection:
    """
    _price_target_direction: dirección canónica derivada del marcador (LOW)/(HIGH)
    y del signo de pct_needed — NO de los verbos ambiguos hit/touch.

    Bug histórico: 'WTI hit (LOW) $70' (current $84.88) se clasificaba como subida
    por el verbo 'hit' → disparaba el floor ALREADY_EXCEEDED del 90% → BUY_YES,
    cuando en realidad bajar a $70 es DIFÍCIL.
    """

    @pytest.fixture(autouse=True)
    def _fn(self):
        from groq_analyzer import _price_target_direction
        self.fn = _price_target_direction

    @staticmethod
    def _pct(target: float, current: float) -> float:
        return (target / current - 1) * 100

    # ── Marcador (LOW) manda sobre el verbo 'hit'/'touch' ──────────────────────
    def test_hit_low_marker_is_down(self):
        assert self.fn("WTI Crude Oil hit (LOW) $70 by end of June",
                       self._pct(70, 84.88)) == "DOWN"

    def test_hit_low_marker_second_case(self):
        assert self.fn("WTI hit (LOW) $65", self._pct(65, 80.35)) == "DOWN"

    def test_touch_low_marker_is_down(self):
        assert self.fn("Will BTC touch (LOW) $90,000?", self._pct(90_000, 100_000)) == "DOWN"

    def test_hit_high_marker_is_up(self):
        assert self.fn("Will BTC hit (HIGH) $120,000?", self._pct(120_000, 100_000)) == "UP"

    def test_low_marker_overrides_even_when_current_below_target(self):
        # current < target en un LOW (ya tocado): el marcador sigue mandando → DOWN
        assert self.fn("WTI hit (LOW) $70", self._pct(70, 65.0)) == "DOWN"

    # ── Sin marcador: verbo no ambiguo ─────────────────────────────────────────
    def test_dip_to_is_down(self):
        assert self.fn("Will Bitcoin dip to $90,000?", self._pct(90_000, 100_000)) == "DOWN"

    def test_reach_is_up(self):
        assert self.fn("Will WTI Crude Oil reach $100?", self._pct(100, 84.88)) == "UP"

    def test_reach_up_even_when_already_exceeded(self):
        # 'reach $50k' con current $60k (ya superado): verbo manda → UP
        # (no debe degradarse a DOWN por el signo negativo de pct_needed)
        assert self.fn("Will BTC reach $50,000?", self._pct(50_000, 60_000)) == "UP"

    # ── Sin marcador ni verbo claro: signo de pct_needed ───────────────────────
    def test_bare_hit_below_current_falls_back_to_sign_down(self):
        # 'hit $70' es ambiguo (sin marcador) → signo pct_needed negativo → DOWN
        assert self.fn("Will WTI hit $70?", self._pct(70, 84.88)) == "DOWN"

    def test_bare_touch_above_current_falls_back_to_sign_up(self):
        assert self.fn("Will WTI touch $100?", self._pct(100, 84.88)) == "UP"


class TestPriceTargetAlreadyAchieved:
    """
    _price_target_already_achieved: el target solo está cumplido si el precio actual
    superó la dirección correcta. Verifica los 4 casos del diagnóstico + el simétrico.
    """

    @pytest.fixture(autouse=True)
    def _fn(self):
        from groq_analyzer import _price_target_already_achieved
        self.fn = _price_target_already_achieved

    # ── BUG principal: LOW con current > target → NO cumplido (bajar es difícil) ─
    def test_wti_low_70_not_achieved(self):
        # WTI hit (LOW) $70, current 84.88 → 84.88 > 70 → bajar pendiente → False
        assert self.fn("DOWN", 84.88, 70) is False

    def test_wti_low_65_not_achieved(self):
        assert self.fn("DOWN", 80.35, 65) is False

    # ── Simétrico: LOW con current < target → SÍ cumplido (ya tocó el mínimo) ───
    def test_wti_low_70_achieved_when_below(self):
        # current 65 < target 70 → tocó el mínimo $70 en su caída → True
        assert self.fn("DOWN", 65.0, 70) is True

    # ── UP: comportamiento clásico preservado ──────────────────────────────────
    def test_up_target_achieved_when_above(self):
        assert self.fn("UP", 60_000, 50_000) is True

    def test_up_target_not_achieved_when_below(self):
        # WTI reach $100, current 84.88 → 84.88 < 100 → no cumplido → False
        assert self.fn("UP", 84.88, 100) is False

    # ── Dirección desconocida → nunca cumplido (fail-safe) ─────────────────────
    def test_unknown_direction_is_never_achieved(self):
        assert self.fn(None, 84.88, 70) is False


class TestAlreadyAchievedIntegration:
    """
    Verificación end-to-end de los 4 casos del diagnóstico vía _price_target_direction
    + _price_target_already_achieved combinados (la condición real de analyze_market).
    """

    @pytest.fixture(autouse=True)
    def _fns(self):
        from groq_analyzer import _price_target_direction, _price_target_already_achieved
        self.dir_fn = _price_target_direction
        self.ach_fn = _price_target_already_achieved

    def _fires_90_floor(self, question: str, current: float, target: float) -> bool:
        pct = (target / current - 1) * 100
        direction = self.dir_fn(question, pct)
        return self.ach_fn(direction, current, target)

    def test_wti_hit_low_70_does_not_fire(self):
        assert self._fires_90_floor("WTI Crude Oil hit (LOW) $70 by end of June", 84.88, 70) is False

    def test_wti_hit_low_65_does_not_fire(self):
        assert self._fires_90_floor("WTI hit (LOW) $65", 80.35, 65) is False

    def test_bitcoin_dip_to_90k_does_not_fire(self):
        # current 100k > target 90k, dirección DOWN → no cumplido → no floor (correcto)
        assert self._fires_90_floor("Will Bitcoin dip to $90,000?", 100_000, 90_000) is False

    def test_wti_reach_100_does_not_fire(self):
        # UP pero current 84.88 < 100 → no cumplido → no floor (correcto)
        assert self._fires_90_floor("Will WTI Crude Oil reach $100?", 84.88, 100) is False

    def test_btc_reach_50k_already_exceeded_still_fires(self):
        # Regresión: 'reach $50k' con current $60k debe SEGUIR disparando el floor UP
        assert self._fires_90_floor("Will BTC reach $50,000?", 60_000, 50_000) is True

    def test_wti_low_70_already_touched_fires(self):
        # Simétrico: LOW ya tocado (current 65 < 70) → SÍ dispara floor del 90%
        assert self._fires_90_floor("WTI hit (LOW) $70", 65.0, 70) is True


class TestCleanContradictoryReasoning:
    """_clean_contradictory_reasoning reemplaza reasoning inconsistente."""

    @pytest.fixture(autouse=True)
    def _fn(self):
        from groq_analyzer import _clean_contradictory_reasoning
        self.fn = _clean_contradictory_reasoning

    def test_buy_no_market_above_real_gets_canonical_text(self):
        """BUY_NO con market > real → texto canónico sobre sobrevaluación."""
        result = self.fn("BUY_NO", "El mercado infravalora la probabilidad.", 0.70, 0.50)
        assert "sobrevalora" in result.lower() or "BUY_NO" in result, \
            f"Reasoning contradictorio no reemplazado: '{result[:100]}'"

    def test_buy_yes_real_above_market_gets_canonical_text(self):
        """BUY_YES con real > market → texto canónico sobre infravaloración."""
        result = self.fn("BUY_YES", "El mercado sobrevalora la probabilidad.", 0.30, 0.55)
        assert "infravalora" in result.lower() or "BUY_YES" in result, \
            f"Reasoning contradictorio no reemplazado: '{result[:100]}'"

    def test_pass_preserves_reasoning(self):
        """PASS no toca el reasoning."""
        original = "Este mercado parece eficiente con los datos disponibles."
        result = self.fn("PASS", original, 0.50, 0.52)
        assert result == original

    def test_floor_notes_preserved_in_buy_yes(self):
        """
        Las notas de floor (⚠️) deben estar presentes en el resultado final.
        Tras el fix de BUG-4A, _clean no borra estas notas.
        """
        reasoning_with_note = (
            "⚠️ ALREADY_EXCEEDED: BTC cotiza $120k > target $100k → prob_min=90%\n"
            "El LLM subestimó la probabilidad."
        )
        result = self.fn("BUY_YES", reasoning_with_note, 0.50, 0.90)
        assert "⚠️" in result or "ALREADY_EXCEEDED" in result or "120k" in result, (
            f"Las notas de floor (⚠️) fueron borradas por _clean_contradictory_reasoning. "
            f"BUG-4A: el fix debe PRESERVAR las notas técnicas, no reemplazar todo el texto.\n"
            f"Resultado actual: '{result[:200]}'"
        )


class TestDetectPriceMarket:
    """_detect_price_market no activa para preguntas sin precio objetivo."""

    @pytest.fixture(autouse=True)
    def _fn(self):
        from groq_analyzer import _detect_price_market
        self.fn = _detect_price_market

    def test_solana_sierra_no_price_returns_none(self):
        """'Solana Sierra' sin precio objetivo → None (no activa price logic)."""
        assert self.fn("Will Solana Sierra win the Italian Open?") is None

    def test_btc_with_price_detected(self):
        """'BTC reach $100k' → detecta BTC con target 100000."""
        result = self.fn("Will BTC reach $100,000?")
        assert result is not None
        asset, cg_id, target = result
        assert asset == "BTC"
        assert target == 100_000.0

    def test_gold_with_price_detected(self):
        result = self.fn("Will GOLD reach $3,500?")
        assert result is not None
        assert result[0] == "GOLD"
