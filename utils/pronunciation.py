# -*- coding: utf-8 -*-
"""
pronunciation.py — Diccionario de pronunciacion para edge-tts en espanol.
Sustituye anglicismos por su pronunciacion espanola antes de sintetizar.
"""

import re
from typing import Dict

# Diccionario: palabra_original (minusculas) -> pronunciacion espanola
PRONUNCIATION_MAP: Dict[str, str] = {
    # Crypto-especificos
    "halving":        "jálving",
    "hash rate":      "jash reit",
    "hashrate":       "jash reit",
    "hash":           "jash",
    "blockchain":     "blókchein",
    "bitcoin":        "bitcoin",
    "wallet":         "wálet",
    "wallets":        "wálets",
    "exchange":       "exchénch",
    "exchanges":      "exchénches",
    "staking":        "stéiking",
    "stakeado":       "stéikeado",
    "bullish":        "búlish",
    "bearish":        "béarish",
    "rally":          "ráli",
    "dump":           "damp",
    "pump":           "pamp",
    "pumping":        "pamping",
    "dumping":        "damping",
    "trading":        "tréiding",
    "trader":         "tréider",
    "traders":        "tréiders",
    "token":          "tóken",
    "tokens":         "tókens",
    "mining":         "máining",
    "miner":          "máiner",
    "miners":         "máiners",
    "defi":           "diefai",
    "nft":            "eneféte",
    "nfts":           "enefétes",
    "altcoin":        "áltcoin",
    "altcoins":       "áltcoins",
    "stablecoin":     "estéiblcoin",
    "stablecoins":    "estéiblcoins",
    "usdt":           "u-ese-de-te",
    "usdc":           "u-ese-de-ce",
    "etf":            "e-te-efe",
    "etfs":           "e-te-efes",
    "hodl":           "jodl",
    "holding":        "jólding",
    "yield":          "yild",
    "airdrop":        "éirdrop",
    "fomo":           "fómo",
    "fud":            "fad",
    "leverage":       "léverage",
    "short":          "short",
    "long":           "long",
    "futures":        "fíutures",
    "spot":           "spot",
    "liquidity":      "likuíditi",
    "liquidez":       "liquidez",
    "market cap":     "márket cap",
    "marketcap":      "márket cap",
    "dominance":      "dóminance",
    "hacker":         "jáker",
    "hackers":        "jákers",
    "hackeo":         "jákeo",
    "hackeado":       "jákeado",
    "layer":          "léier",
    "mainnet":        "méinnet",
    "testnet":        "tésnet",
    "whitepaper":     "wáitpeiper",
    "roadmap":        "ródmap",
    "smart contract": "smart contráct",
    "fee":            "fi",
    "fees":           "fis",
    "gas":            "gas",
    "mempool":        "mémpool",
    "fork":           "fork",
    "hard fork":      "jard fork",
    "soft fork":      "soft fork",
}

# Acronimos (case-sensitive, solo cuando estan en mayusculas) y como leerlos
ACRONYM_MAP: Dict[str, str] = {
    "BTC":  "bitcoin",
    "ETH":  "éter",
    "SOL":  "sólana",
    "BNB":  "be-ene-be",
    "XRP":  "ripple",
    "USDT": "u-ese-de-te",
    "USDC": "u-ese-de-ce",
    "NFT":  "eneféte",
    "DeFi": "diefai",
    "ETF":  "e-te-efe",
    "SEC":  "ese-e-ce",
    "HODL": "jodl",
    "ATH":  "a-te-jache",
    "ATL":  "a-te-ele",
    "TVL":  "te-ve-ele",
    "CEX":  "sex",
    "DEX":  "dex",
    "P2P":  "pe-dos-pe",
}


def apply_pronunciation(text: str) -> str:
    """
    Aplica el diccionario de pronunciacion al texto.
    Solo sustituye palabras completas (word boundaries).
    Los acronimos se procesan antes que las palabras para evitar colisiones.
    Las frases de varias palabras (ej. 'hash rate') se procesan antes
    que las palabras individuales para evitar reemplazos parciales.
    """
    result = text

    # 1. Acronimos (case-sensitive, solo cuando estan en mayusculas/mixto exacto)
    for acronym, pronunciation in ACRONYM_MAP.items():
        pattern = r'(?<![A-Za-z])' + re.escape(acronym) + r'(?![A-Za-z])'
        result = re.sub(pattern, pronunciation, result)

    # 2. Palabras clave (case-insensitive)
    # Ordenar por longitud descendente para que las frases multi-palabra
    # se reemplacen antes que sus componentes individuales
    sorted_map = sorted(PRONUNCIATION_MAP.items(), key=lambda x: len(x[0]), reverse=True)
    for word, pronunciation in sorted_map:
        pattern = (
            r'(?<![A-Za-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1'
            r'\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1])'
            + re.escape(word)
            + r'(?![A-Za-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1'
            r'\u00c1\u00c9\u00cd\u00d3\u00da\u00dc\u00d1])'
        )
        result = re.sub(pattern, pronunciation, result, flags=re.IGNORECASE)

    return result


def test_pronunciation():
    """Test basico del diccionario."""
    cases = [
        ("El halving de Bitcoin llegara pronto", ["jálving"]),
        ("Los traders estan en modo bullish", ["tréiders", "búlish"]),
        ("El exchange fue hackeado", ["exchénch", "jákeado"]),
        ("Compra en el wallet correcto", ["wálet"]),
        ("El BTC sube por el ETF aprobado", ["bitcoin", "e-te-efe"]),
        ("DeFi y NFTs en auge", ["diefai", "enefétes"]),
    ]
    all_ok = True
    for case in cases:
        text, expected_words = case[0], case[1]
        result = apply_pronunciation(text)
        for expected in expected_words:
            if expected.lower() not in result.lower():
                print(f"FAIL: '{text}' -> '{result}' (expected '{expected}')")
                all_ok = False
            else:
                print(f"OK: '{expected}' found in '{result}'")
    return all_ok


if __name__ == "__main__":
    ok = test_pronunciation()
    print("All tests passed" if ok else "Some tests FAILED")

    print("\n--- Test caracteres especiales espanoles ---")
    test_words = [
        "anno", "Espanna", "sennal", "mannana", "pequenno",
        "analisis", "tecnico", "grafico", "basico",
        "halving", "blockchain", "exchange", "wallet",
    ]
    for w in test_words:
        result = apply_pronunciation(w)
        print(f"  {w!r:20} -> {result!r}")

    print("\n--- Verificacion tildes en diccionario ---")
    checks = [
        ("halving",    "jálving"),
        ("blockchain", "blókchein"),
        ("wallet",     "wálet"),
        ("exchange",   "exchénch"),
        ("bullish",    "búlish"),
    ]
    for word, expected in checks:
        from utils.pronunciation import PRONUNCIATION_MAP
        actual = PRONUNCIATION_MAP.get(word, "NOT_FOUND")
        status = "OK" if actual == expected else "FAIL"
        print(f"  [{status}] {word!r}: expected {expected!r}, got {actual!r}")
