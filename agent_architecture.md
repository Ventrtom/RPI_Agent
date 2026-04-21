# Architektura agenta

Osobní AI asistent běžící na Raspberry Pi jako systemd služba. Dostupný přes Telegram (text + hlas) a CLI. Uchovává paměť napříč sezeními, plánuje úkoly autonomně a ovládá externí služby.

---

## Architekturní diagram

```
┌──────────────────────────────────────────────────────┐
│                     main.py                          │
│  Načte .env → sestaví závislosti → spustí démon      │
│  → zaregistruje nástroje → spustí CLI nebo Telegram  │
└─────────────────────┬────────────────────────────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
  interfaces/    reasoning/    scheduler/
  CLI / Telegram  ReasoningEng  TaskScheduler
  Notifier        (multi-step)  (SQLite, cron)
        │             │             │
        └─────────────┼─────────────┘
                      ▼
             ┌─────────────────┐
             │  core/Agent     │  ← hlavní orchestrátor
             │  process()      │
             └────────┬────────┘
          ┌───────────┼───────────┐
          ▼           ▼           ▼
      core/        llm/        memory/
  SessionManager  ClaudeClient  MemoryClient
  SessionStore    tool use loop Mem0 + ChromaDB
                      │
                      ▼
              tools/ToolRegistry
              ├─ system_tools
              ├─ google_tools
              ├─ ha_tools
              ├─ telegram_tools
              ├─ web_tools
              ├─ vault tools
              ├─ contact_tools
              ├─ scheduler_tools
              ├─ self_tools
              └─ source_tools
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
       vault/                  voice/
  Markdown KB              STT (Groq Whisper)
  VaultManager             TTS (ElevenLabs)
  VaultIndexer
```

---

## Startovací sekvence (`main.py`)

1. Načte `.env` přes `python-dotenv`
2. Validuje `ANTHROPIC_API_KEY` (exit pokud chybí)
3. Instantiuje v pořadí: `ClaudeClient` → `MemoryClient` → `SessionStore` + `SessionManager` → `VaultManager` → `ReasoningEngine` → nástroje + `ToolRegistry`
4. Spustí background služby: `VaultIndexer.start()`, `TaskScheduler.start()`
5. Přesměruje na `run_cli()` nebo `run_telegram()`

---

## Moduly

### `core/`

**`agent.py`** — hlavní orchestrátor, jedna async metoda `process(user_message, session_id, user_id, is_voice, is_scheduled)`:

1. Načte nebo vytvoří sezení
2. Semantic search paměti (ChromaDB) → relevantní fakta
3. Sestaví system prompt (čas, paměti, kontext)
4. Zavolá `ClaudeClient.complete()` (tool use loop uvnitř)
5. Uloží zprávy do sezení (RAM + SQLite)
6. Async na pozadí: extrahuje nová fakta přes Mem0 do ChromaDB

Speciální chování:
- Nebezpečné nástroje (`restart`, `shutdown`, `ha_call_service`) → `ConfirmationGate` (Telegram inline keyboard, 60s timeout)
- `is_scheduled=True` → v system promptu: "žádný uživatel není přítomen, nikdy se neptej"
- `is_voice=True` → v system promptu: "odpovídej v plynulých větách bez markdownu"

**`session.py`** — in-memory cache sezení s automatickým vypršením (default 30 min), background cleanup (každých 5 min)

**`session_store.py`** — SQLite persistence: tabulky `sessions` a `messages`; při startu obnoví aktivní sezení

**`prompts.py`** — dynamický system prompt: osobnost agenta "Prime" (ženská, přátelská, efektivní), aktuální čas, paměti, doplněk pro scheduled context

---

### `llm/`

**`claude.py`** — obaluje `AsyncAnthropic`:
- Interní tool use loop: volá API → pokud tool call → spustí nástroj → přidá výsledek → opakuje → vrátí finální text
- Sleduje tokeny (input/output) na sezení
- Loguje každé volání API

---

### `memory/`

**`client.py`** — dlouhodobá paměť přes Mem0AI + ChromaDB:
- `search(query)` → semantic search (default 10 výsledků) před každým requestem
- Po každém requestu: async extrakce faktů z konverzace → uložení do ChromaDB
- Embedder: HuggingFace `all-MiniLM-L6-v2` (CPU-friendly, ~200 MB)
- LLM pro extrakci faktů: stejný Claude model
- Fyzické uložení: `./data/chroma/`

---

### `voice/`

**`stt.py`** — Groq Whisper API, `transcribe(audio_bytes)` → text; default jazyk: `cs`

**`tts.py`** — ElevenLabs API, `synthesize(text)` → MP3 bytes; stripuje markdown před odesláním

---

### `interfaces/`

**`cli.py`** — jednoduchý REPL: `/quit`, `/clear`, `/memory`; async input přes executor

**`telegram_bot.py`** — python-telegram-bot:
- Příkazy: `/start`, `/memory`, `/newsession`
- Text → `agent.process()`
- Hlas → STT → `agent.process()` → TTS → audio odpověď
- "🔄 Thinking…" status message aktualizovaný během reasoning
- Inline keyboard pro potvrzení nebezpečných operací

**`notifier.py`** — proaktivní zprávy agentovi:
- `send(text)`, `send_with_keyboard(text, token)` → Approve/Deny tlačítka
- Chat ID persistováno v `data/telegram_chat_id`
- Při CLI módu gracefully selže (není inicializován)

---

### `reasoning/`

**`engine.py`** — multi-krokové uvažování pro složité requesty:

Trigger (automaticky):
- zpráva > 150 znaků, nebo
- ≥2 otazníky, nebo
- klíčová slova: `naplánuj, zjisti, porovnej, rozhodni, analyzuj, co si myslíš o, jak bych měl`
- Vypnutí: `SKIP_REASONING=true`

Iterace (max 4):
1. **diagnose** — porozumění requestu, identifikace informačních mezer
2. **gather** — doplnění chybějícího kontextu (vault, web search, …)
3. **act** — provedení akcí (calendar, email, vault writes, …)
4. **reflect** — sebehodnocení připravenosti (score 1–5); ≥4 = stop iterací
5. **finalize** — finální odpověď

Trace každé iterace ukládán async do `vault/reasoning-traces/` jako markdown.

**`context.py`** — datové modely: `ReasoningStep` (typ, thought, tool_calls, tool_results), `ReasoningContext` (agregát kroků)

---

### `scheduler/`

**`daemon.py`** — `TaskScheduler`, asyncio loop každých 60 s:
- Načte splatné úkoly (`status IN (pending) AND next_run_at <= now`)
- Každý úkol: izolované sezení (žádná sdílená historie), `agent.process(is_scheduled=True)`
- Po dokončení: ONCE → COMPLETED; RECURRING → počítá next_run přes croniter, kontroluje `end_at`
- Selhání: exponenciální backoff (60s, 120s, 240s), max 3 pokusy → FAILED

**`models.py`** — `TaskType`: ONCE | RECURRING; `TaskStatus`: PENDING | RUNNING | COMPLETED | FAILED | DISABLED; `Task` dataclass; `ExecutionLog`

**`store.py`** — SQLite `data/scheduler.db`; tabulky `tasks`, `execution_log`; auto-migrace chybějících sloupců při startu; reset "running" tasků po pádu

---

### `tools/`

**`__init__.py` (ToolRegistry)** — `register(fn, schema)`, `get_schemas()` pro Claude API, `execute(name, kwargs)`

| Soubor | Nástroje | Popis |
|--------|----------|-------|
| `system_tools.py` | `get_system_status`, `get_agent_logs`, `restart_agent_service`*, `shutdown_raspberry_pi`* | CPU/RAM/teplota/log/systemd |
| `google_tools.py` | `get_calendar_events`, `create_calendar_event`, `delete_calendar_event`, `find_free_slots`, `send_email` | Google Calendar API v3, Gmail API v1; OAuth2 |
| `ha_tools.py` | `ha_list_entities`, `ha_get_state`, `ha_call_service`*, `ha_get_history` | Home Assistant REST API; optional |
| `telegram_tools.py` | `send_telegram_message` | Proaktivní zprávy uživateli |
| `web_tools.py` | `web_search` | Tavily API, max 10 výsledků |
| `vault/tools.py` | `vault_read`, `vault_write`, `vault_search` | Čtení/zápis/hledání v markdown KB |
| `contact_tools.py` | `get_contacts`, `get_contact_by_name`, `add_contact`, `remove_contact` | JSON soubor `data/contacts.json` |
| `scheduler_tools.py` | `schedule_task`, `list_tasks`, `get_task_details`, `cancel_task`, `enable_task`, `update_task` | CRUD pro scheduler |
| `self_tools.py` | `get_self_info` | Model, tokeny, paměť, seznam nástrojů |
| `source_tools.py` | `list_own_source`, `read_own_source` | Agent čte vlastní kód |

*Vyžaduje potvrzení přes Telegram (`confirmation.py`)

**`confirmation.py`** — `ConfirmationGate`: zachytí nebezpečný nástroj → pošle inline keyboard → čeká na Approve/Deny (60s timeout → Deny)

---

### `vault/`

Markdown knowledge base s YAML frontmatter. Fyzická cesta: `./vault/`

**`vault_manager.py`** — `read(path)`, `write(path, content)`, `append(path, text)`, `search(query)` (case-insensitive substring), `rebuild_index()` → `_index.md`

**`indexer.py`** — filesystem watcher (watchdog), debounce 3s, auto-rebuild `_index.md` při změnách

---

## Datový tok — typický request

```
Uživatel pošle text přes Telegram
    → telegram_bot._handle_text()
        → agent.process(message, session_id, user_id)
            → session: načti historii z RAM/SQLite
            → memory.search(message) → relevantní fakta z ChromaDB
            → build_system_prompt(fakta, čas)
            → reasoning_engine.needs_reasoning(message)?
                Ano → reasoning_engine.process() [max 4 kroků]
                Ne  → claude_client.complete(history, tools)
                          → tool call? → execute → append results → repeat
                          → finální text
            → session: ulož [user, assistant] do RAM + SQLite
            → asyncio.create_task(_save_memory()) [na pozadí]
            → vrátí text
    → telegram_bot.reply_text(text)
```

## Datový tok — scheduled task

```
scheduler.daemon._loop() [každých 60s]
    → TaskStore.get_due_tasks()
    → pro každý task:
        → nové izolované session_id
        → agent.process(task.prompt, is_scheduled=True)
            → system prompt: "žádný uživatel, neodpovídej otázkami"
            → ConfirmationGate: deaktivován
        → úspěch: ONCE→COMPLETED, RECURRING→počítej next_run
        → selhání: retry s backoff, po max_retries→FAILED
```

---

## Schopnosti agenta

- **Konverzace** — multi-turn, paměť faktů napříč sezeními (Mem0 + ChromaDB)
- **Google Calendar** — čtení, vytváření, mazání událostí, hledání volných slotů
- **Gmail** — odesílání emailů (ověření přes contacts)
- **Home Assistant** — ovládání chytrých zařízení, čtení stavů a historie
- **Web search** — aktuální informace přes Tavily
- **Plánování úkolů** — cron i one-time, retries, expiry, execution history
- **Hlas** — STT (Groq Whisper) + TTS (ElevenLabs) přes Telegram
- **Vault** — čtení/zápis/hledání v markdown KB
- **Systémové informace** — CPU, RAM, teplota, logy, restart/shutdown RPi
- **Proaktivní zprávy** — agent může sám kontaktovat uživatele přes Telegram
- **Multi-krokové uvažování** — pro složité requesty automaticky spustí reasoning engine
- **Introspekce** — čte vlastní kód a konfiguraci

---

## Limity a omezení

| Oblast | Limit |
|--------|-------|
| Hardware | CPU-only inference na ARM64 (RPi); embeddings ~200 MB RAM |
| Model | Default: `claude-haiku-4-5`, max 1024 tokenů na odpověď |
| Streaming | Žádný — odpovědi se vrátí celé až po dokončení |
| Počet uživatelů | Jeden (architekturu by musel rozšířit pro multi-user) |
| Vault search | Pouze substring matching, žádný fuzzy/semantic search |
| Potvrzení | 60s window pro nebezpečné operace; hardcoded |
| Scheduled context | Žádná sdílená konverzační historie mezi scheduled runy |
| Chat ID | Jeden soubor `data/telegram_chat_id`; bez zálohy |
| Python | Vyžaduje 3.11+ |
| Reasoning | Max 4 iterace; každá je samostatné volání Claude = cena tokenů |

---

## Proměnné prostředí

**Povinné:**
| Proměnná | Účel |
|----------|------|
| `ANTHROPIC_API_KEY` | Claude API |
| `TELEGRAM_BOT_TOKEN` | Telegram bot (pokud Telegram mód) |

**Volitelné:**
| Proměnná | Default | Účel |
|----------|---------|------|
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | LLM model |
| `CLAUDE_MAX_TOKENS` | `1024` | Max tokeny na odpověď |
| `SESSION_TIMEOUT_MINUTES` | `30` | Expiry sezení |
| `CHROMA_DB_PATH` | `./data/chroma` | Vektorová DB |
| `SESSION_DB_PATH` | `./data/sessions.db` | SQLite sezení |
| `TASKS_DB_PATH` | `./data/scheduler.db` | SQLite scheduler |
| `SCHEDULER_TIMEZONE` | `Europe/Prague` | Timezone pro cron |
| `MEM0_USER_ID` | `tomas` | Identifikátor v Mem0 |
| `LOG_FILE` | `agent.log` | Log soubor |
| `SKIP_REASONING` | `false` | Vypnutí reasoning enginu |
| `TAVILY_API_KEY` | — | Web search |
| `GROQ_API_KEY` | — | STT |
| `ELEVENLABS_API_KEY` | — | TTS |
| `ELEVENLABS_VOICE_ID` | — | Hlas pro TTS |
| `GOOGLE_OAUTH_CREDENTIALS` | — | Cesta k `google_oauth.json` |
| `HA_URL` | — | Home Assistant URL |
| `HA_TOKEN` | — | HA long-lived access token |
| `WHISPER_LANGUAGE` | `cs` | Jazyk STT |

---

## Extension points

**Přidat nový nástroj:**
1. Implementuj async funkci + input schema v `tools/`
2. Zaregistruj v `main.py` přes `tool_registry.register(fn, schema)`
3. Claude ho automaticky uvidí a může použít

**Přidat nové rozhraní:**
1. Vytvoř modul v `interfaces/`
2. Implementuj async smyčku volající `agent.process()`
3. Přidej routing v `main.py`

**Přidat externí službu:**
1. Vytvoř klienta (např. `tools/weather_client.py`)
2. Obal do nástrojů, zaregistruj
3. Přidej env proměnnou pro API klíč

**Upravit chování agenta:**
- Osobnost a pravidla: `core/prompts.py`
- Trigger pro reasoning: `reasoning/engine.py:_needs_reasoning()`
- Nebezpečné nástroje: `tools/confirmation.py:DANGEROUS_TOOLS`
