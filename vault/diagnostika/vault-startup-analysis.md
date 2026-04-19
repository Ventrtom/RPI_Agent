---
title: Vault Index Auto-Startup Diagnostika
summary: Analýza startup flow, watchdog loop a systemd integraci
updated: "2026-04-18"
---

## Zjištění

### ❌ PROBLÉM #1: Watchdog Infinite Loop
- Handler se spustí každých ~3 sekundy
- Při rebuild_index() se změní _index.md
- Watchdog detekuje změnu souboru
- Spustí se rebuild znova → infinite loop

### ❌ PROBLÉM #2: Logika ignorování _index.md
```python
# vault/indexer.py řádka 24
if not src.endswith(".md") or "_index.md" in src:
    return
```
- Logika je správná (měla by ignorovat)
- ALE handler se stále spouští
- Příčina: Watchdog reaguje na vytváření/smazání, ne jen na modifikaci

### ✅ ŘEŠENÍ
1. Přidej flag `_rebuilding` do VaultManager
2. Handler nevolaný rebuild během indexingu
3. Přidej graceful startup bez watchdogu na prvních 10 sekund
