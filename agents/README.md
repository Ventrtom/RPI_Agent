# agents/

Složka pro specializované subagenty Prime.

## Účel a kontext

`agents/` se liší od `core/` a `tools/`:

- **`core/`** — hlavní smyčka Prime (Agent, session management, reasoning engine)
- **`tools/`** — atomické nástroje volané přímo Claude API (HA, scheduler, vault, …)
- **`agents/`** — subagenti s vlastním izolovaným kontextem; každý dostane podmnožinu nástrojů a vrátí `SubagentResult`

## Pattern delegace

```
Uživatel → Prime (core/agent.py)
               │
               ├─ přímá odpověď (tools, memory, vault)
               │
               └─ delegace → BaseSubagent.run(task, context)
                                  │
                                  ├─ izolovaná konverzace s Claude
                                  ├─ scoped_tools (white-list)
                                  └─ SubagentResult → Prime → uživatel
```

Prime zůstává jediným kontaktním bodem pro uživatele. Subagenti nikdy nekomunikují přímo s uživatelem.

## Subagenty

| Subagent | Role | Scoped tools | Status | Fáze |
|----------|------|--------------|--------|------|
| **Glaedr** | Paměť a kurátorství | vault_read, vault_search, memory (read-only); curator: + vault_write (restricted) | ✅ Implemented (retrieve + curator, Phases 1+4) | 1+4 |
| **Veritas** | Výzkum a syntéza | web_search, vault_read, vault_search, memory (read-only seed) | ✅ Implemented (research) | 2 |
| **Aeterna** | Plánování a čas | scheduler CRUD, calendar CRUD, contacts (read-only) | ✅ Implemented (schedule + review) | 3 |

Hranice autonomie každého subagenta jsou zdokumentovány v
[vault/instrukce/autonomy_boundaries.md](../vault/instrukce/autonomy_boundaries.md).

## Delegation in practice

Příklad průchodu při volání `memory_dive`:

```
Uživatel: "Jak jsme se rozhodli řešit ten problém s cache invalidation v projektu Apollo?"
  ↓
Prime vidí dotaz vyžadující deep recall → volá memory_dive("cache invalidation projekt Apollo")
  ↓
Glaedr v izolovaném kontextu:
  1. memory.search("cache invalidation projekt Apollo", limit=15)  ← initial context
  2. vault_search("Apollo cache")
  3. vault_search("cache invalidation")
  4. vault_read("projekty/apollo.md")
  5. Syntéza briefu (200-400 slov)  →  ## Brief / ## Findings / ## Confidence
  ↓
Prime dostává SubagentResult.summary → formuluje odpověď uživateli
```

Kdy delegovat: komplexní recall vyžadující syntézu přes více zápisů nebo vault souborů.
Kdy NEDelegovat: přímá odpověď z paměťového kontextu v system promptu, nebo jednoduchý `vault_read` se známou cestou.

### Glaedr curator

Glaedr běží proaktivně na týdenním plánu (konfigurovatelné přes `CURATOR_CRON`) pro údržbu paměti. Produkuje markdown digesty v `vault/memory-digests/` obsahující:

- Přehled aktuálního stavu paměti
- Klíčová témata seskupená podle tematiky
- Potenciální duplikáty (návrhy ke konsolidaci)
- Detekované konflikty (návrhy k řešení)
- Navrhované tagy (návrhy ke kategorizaci)
- Pravděpodobně zastaralé vzpomínky (časově ohraničené fakty, které pravděpodobně vypršely)
- Poznámky k housekeepingu

**Curator je propose-only.** Nikdy nemodifikuje paměť autonomně. Všechny návrhy jsou manuálně kontrolovány uživatelem.

Manuální spuštění: tool `memory_housekeeping(scope, dry_run)` — volatelný Prime, když uživatel požádá.

Příklady frází, které spustí delegaci na curator:
- "Udělej mi housekeeping paměti"
- "Jak to vypadá v paměti?"
- "Zkontroluj si tam tu paměť"
- "Dej mi přehled toho, co si pamatuješ"
- "Máš tam bordel?"

Příklad průchodu při volání `deep_research`:

```
Uživatel: "Jaké jsou aktuální best practices pro memory management v osobních AI agentech?"
  ↓
Prime vidí dotaz vyžadující web + interní kontext → volá deep_research("memory management personal AI agents 2026")
  ↓
Veritas v izolovaném kontextu:
  1. memory.search("memory management AI agents", limit=15)  ← initial context seed
  2. web_search("personal AI agent memory management best practices 2026")
  3. web_search("RAG memory curation techniques agents")
  4. vault_search("memory")
  5. vault_read("projekty/prime-agent.md")  ← pokud relevantní
  6. Syntéza briefu s citacemi  →  ## Brief / ## Findings / ## Sources / ## Conflicts & Gaps / ## Confidence
  ↓
Prime dostává SubagentResult.summary → formuluje odpověď uživateli
```

Příklad průchodu při volání `plan_task`:

```
Uživatel: "Naplánuj mi týdenní review projektu Apollo každý pátek 10:00 do konce května."
  ↓
Prime rozpozná: scheduling intent s parsováním času → volá plan_task(intent="...")
  ↓
Aeterna v izolovaném kontextu:
  1. list_tasks() → check duplikátu "Apollo review" — žádný nenalezen
  2. Parse: "každý pátek 10:00" → cron "0 10 * * 5", "do konce května" → end_at = 2026-05-31
  3. schedule_task(prompt="Týdenní review projektu Apollo", cron="0 10 * * 5", end_at=...)
  4. get_task_details(task_id=X) → verifikace, že task existuje
  5. Strukturovaný output s ID a statusem  →  ## Action taken / ## Details (ID: X) / ## Status: success
  ↓
Prime dostává SubagentResult.summary + data["object_id"] → potvrzuje uživateli včetně task ID
```

### Kdy použít kterého subagenta

| Use case | Subagent | Tool |
|----------|----------|------|
| Jednoduchý recall ze system promptu | — | Přímá odpověď |
| Čtení konkrétního vault souboru (známá cesta) | — | `vault_read` přímo |
| Rychlý aktuální fakt (počasí, kurz, zpráva) | — | `web_search` přímo |
| Jednoduchý schedule dotaz ("co mám zítra?") | — | `get_calendar_events` přímo |
| Hlubší recall + syntéza přes více vzpomínek nebo vault souborů | Glaedr | `memory_dive` |
| Kontrola/housekeeping paměti | Glaedr curator | `memory_housekeeping` |
| Výzkum kombinující web + interní znalosti | Veritas | `deep_research` |
| Komplexní scheduling intent (čas, recurrence, konflikty) | Aeterna | `plan_task` |
| Přehled a health check naplánovaných věcí | Aeterna | `review_my_schedule` |

## Jak přidat nového subagenta

1. Vytvoř soubor `agents/<jmeno>.py`.
2. Definuj třídu dědící z `BaseSubagent`.
3. Přepiš `_build_task_prompt()` pokud potřebuješ specifické formátování zadání.
4. V `main.py` instanciuj subagenta a předej ho Prime (až bude Prime orchestraci podporovat).
5. Registruj odpovídající tool wrapper v `tools/subagent_tools.py`.
6. Zdokumentuj hranice autonomie v `vault/instrukce/autonomy_boundaries.md`.

## Observability

Každá delegace na subagenta je automaticky logovaná do `data/observability/telemetry.jsonl` jako `delegation` event s metadaty: subagent, method (retrieve/curate/research/schedule/review), latency_ms, success, task_preview.

Prime může tato data číst přes `get_observability_data` tool a odpovídat na otázky jako "Kolikrát jsi delegovala na Glaedra?" nebo "Kde se opakovaně dělají chyby?".

## Soubory

| Soubor | Obsah |
|--------|-------|
| `base.py` | `SubagentResult` dataclass + `BaseSubagent` třída |
| `glaedr.py` | Glaedr — paměťový specialista (retrieve + curator mode) |
| `veritas.py` | Veritas — výzkumný specialista |
| `aeterna.py` | Aeterna — specialistka na plánování a čas |
| `README.md` | Tento dokument |
