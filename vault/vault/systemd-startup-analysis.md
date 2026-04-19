---
updated: "2026-04-18"
summary: "Analýza: Vault Index Infinite Rebuild Loop — Root Cause & Solution"
tags: ["system", "vault", "bug-fix"]
---

## 🔴 **PROBLÉM: Infinite Rebuild Loop**

### Příčina
- `VaultManager.rebuild_index()` (řádka 62-88) zapíše do `_index.md`
- Watchdog (`VaultIndexer`) sleduje VŠECHNY .md zápisy
- Zápis do `_index.md` spustí handler → timer → rebuild_index() znova
- **Result: INFINITE LOOP každých 3 sekundy**

### Aktuální logika (CHYBNÁ)
```python
# indexer.py řádka 20-25
def on_any_event(self, event) -> None:
    if event.is_directory:
        return
    src = getattr(event, "src_path", "")
    if not src.endswith(".md") or "_index.md" in src:  # ← Mělo by ignorovat _index.md!
        return
```

Condition JE správná (ignoruje `_index.md`), ale **watchdog stále slyší zápis** během `rebuild_index()`.

---

## ✅ **ŘEŠENÍ: Dvoustupňový Fix**

### Step 1: Přidej rekurzi-lock do VaultManager
Při `rebuild_index()` se zápis do `_index.md` musí zablokovat watchdogu.

### Step 2: Přidej file modification tracking do Handler
Když zpracovávám zápis `_index.md`, poznám, že to udělal SAMI SEBou.

---

## 🔧 **Implementace**
Upravit:
1. `vault/vault_manager.py` — přidej `_rebuilding` flag
2. `vault/indexer.py` — přidej check na `_rebuilding`
