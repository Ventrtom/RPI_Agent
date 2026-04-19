# RPI Agent

Osobní AI asistent navržený pro trvalý provoz na Raspberry Pi. Dostupný přes Telegram (textové i hlasové zprávy) a CLI. Pamatuje si kontext napříč konverzacemi a obnovuje session historii po restartu.

---

## Obsah

- [Architektura](#architektura)
- [Předpoklady](#předpoklady)
- [Potřebné API klíče](#potřebné-api-klíče)
- [Instalace](#instalace)
- [Konfigurace](#konfigurace)
- [Spuštění](#spuštění)
- [Trvalý provoz na Raspberry Pi (systemd)](#trvalý-provoz-na-raspberry-pi-systemd)
- [Telegram bot — příkazy](#telegram-bot--příkazy)
- [CLI — příkazy](#cli--příkazy)
- [Logování a monitoring](#logování-a-monitoring)
- [Přidání nového nástroje](#přidání-nového-nástroje)

---

## Architektura

```
main.py                      ← entry point, validace env, sestavení závislostí, registrace nástrojů
│
├── core/
│   ├── agent.py             ← orchestrátor: zpracování zprávy, volání Claude, uložení paměti
│   ├── session.py           ← správa session v paměti + automatická expirace
│   ├── session_store.py     ← SQLite persistence — session přežijí restart
│   └── prompts.py           ← systémový prompt, voice/scheduled kontext
│
├── llm/
│   └── claude.py            ← klient Anthropic API (tool use loop)
│
├── memory/
│   └── client.py            ← Mem0 + ChromaDB — dlouhodobá paměť (fakta o uživateli)
│
├── voice/
│   ├── stt.py               ← Groq Whisper — přepis hlasu na text
│   └── tts.py               ← ElevenLabs — syntéza textu na hlas
│
├── interfaces/
│   ├── telegram_bot.py      ← Telegram bot (text + hlas)
│   ├── notifier.py          ← TelegramNotifier — proaktivní odesílání zpráv agentem
│   └── cli.py               ← interaktivní CLI (vývoj, debug)
│
├── scheduler/
│   ├── daemon.py            ← background scheduler — spouští naplánované úlohy (cron)
│   ├── models.py            ← datové modely Task, ExecutionLog
│   └── store.py             ← SQLite persistence naplánovaných úloh
│
└── tools/                   ← rozšiřitelný registr nástrojů pro agenta
    ├── __init__.py          ← třída ToolRegistry
    ├── system_tools.py      ← stav systému, shutdown, logy (RPi)
    ├── google_tools.py      ← Google Calendar a Gmail
    ├── contact_tools.py     ← správa kontaktů (data/contacts.json)
    ├── telegram_tools.py    ← odeslání zprávy uživateli přes Telegram
    ├── scheduler_tools.py   ← CRUD naplánovaných úloh
    └── example_tool.py      ← šablona pro nový nástroj
```

**Tok zpracování zprávy:**
1. Uživatel pošle zprávu přes Telegram (text nebo hlas) nebo CLI
2. Agent načte session historii ze SQLite, vyhledá relevantní vzpomínky z ChromaDB
3. Zavolá Claude API se systémovým promptem, historií a vzpomínkami
4. Claude může volat nástroje (calendar, email, scheduler, system…) — smyčka se opakuje dokud nenastane finální odpověď
5. Uloží zprávy do session (SQLite) a asynchronně extrahuje nová fakta do Mem0
6. Odešle odpověď uživateli (u hlasových zpráv také přes ElevenLabs TTS)

**Paměť:**
- **Krátkodobá (session)** — history zpráv v aktuální konverzaci, uložena v SQLite, přežije restart
- **Dlouhodobá (Mem0 + ChromaDB)** — fakta extrahovaná z konverzací, uložena na disku v `./data/chroma/`

**Scheduler:**
- Běží jako background task (asyncio), každých 60 sekund kontroluje naplánované úlohy
- Každý run dostane izolovanou session — história se nehromadí napříč spuštěními
- Podporuje jednorázové i opakující se úlohy (cron výraz), retry s exponenciálním backoff, `end_at` expiraci
- Agent může naplánovat úlohu sám přes nástroj `schedule_task`

---

## Předpoklady

- Python 3.11+
- Raspberry Pi (nebo jiný Linux systém) s přístupem na internet
- **Disk:** ~2–3 GB na Raspberry Pi (CPU-only torch), ~5+ GB na x86 s CUDA torch
- Systémový balíček pro audio (u hlasových zpráv): žádný — vše přes API

---

## Potřebné API klíče

| Klíč | Kde získat | K čemu |
|------|-----------|--------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | LLM (Claude) + extrakce faktů (Mem0) |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) na Telegramu | Telegram bot |
| `ELEVENLABS_API_KEY` | [elevenlabs.io](https://elevenlabs.io) | Text-to-Speech |
| `ELEVENLABS_VOICE_ID` | ElevenLabs dashboard → Voices | ID hlasu pro TTS |
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) | Whisper STT (přepis hlasu) |

---

## Instalace

```bash
# 1. Klonuj repozitář
git clone <url-repozitare>
cd agent

# 2. Spusť setup skript — detekuje hardware a nainstaluje správnou verzi PyTorche
chmod +x setup.sh
./setup.sh
```

Skript automaticky:
- Detekuje architekturu (ARM64 = RPi, x86_64 = server), NVIDIA GPU a verzi CUDA
- Nainstaluje PyTorch ve správné variantě **před** `sentence-transformers`:
  - **Raspberry Pi (ARM64):** CPU-only (~200 MB místo ~900 MB CUDA verze)
  - **x86 + NVIDIA GPU:** CUDA build (verze detekována z `nvidia-smi`)
  - **x86 bez GPU:** CPU-only
- Nainstaluje všechny skupiny závislostí ze `requirements/`
- Ověří import klíčových balíčků a zobrazí shrnutí

> **Poznámka k disku:** Na Raspberry Pi zabere venv přibližně 2–3 GB. Na x86 serveru s CUDA torch počítej s 5+ GB.

### Manuální instalace (pokročilí uživatelé)

```bash
python3 -m venv .venv
source .venv/bin/activate

# ARM64 (Raspberry Pi) — CPU-only torch
pip install torch --index-url https://download.pytorch.org/whl/cpu

# x86 s CUDA 12.4
# pip install torch --index-url https://download.pytorch.org/whl/cu124

pip install -r requirements/base.txt
pip install -r requirements/memory.txt
pip install -r requirements/voice.txt
pip install -r requirements/telegram.txt
pip install -r requirements/google.txt
```

---

## Konfigurace

```bash
cp .env.example .env
```

Otevři `.env` a vyplň hodnoty:

```env
# Agent identity
AGENT_USER_NAME=Firstname Lastname      # jméno uživatele v systémovém promptu

# LLM
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL=claude-haiku-4-5-20251001   # nebo jiný Claude model
CLAUDE_MAX_TOKENS=1024

# Embeddings — model pro vektorové vyhledávání vzpomínek
EMBEDDING_MODEL=all-MiniLM-L6-v2

# Voice — TTS
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_VOICE_ID=abc123...
ELEVENLABS_MODEL=eleven_multilingual_v2

# Voice — STT
GROQ_API_KEY=gsk_...
WHISPER_MODEL=whisper-large-v3-turbo
WHISPER_LANGUAGE=cs                      # prázdné = auto-detect

# Telegram
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=                        # volitelné: pre-init notifikatoru bez /start

# Session
SESSION_TIMEOUT_MINUTES=60              # po jak dlouhé nečinnosti se session uzavře
SESSION_CLEANUP_INTERVAL=300            # jak často kontrolovat expirované sessions (sekundy)
SESSION_DB_PATH=./data/sessions.db      # SQLite soubor pro persistenci session

# Mem0 / ChromaDB
CHROMA_DB_PATH=./data/chroma            # adresář pro vektorovou DB
MEM0_USER_ID=user                      # identifikátor uživatele v paměti

# Scheduler
SCHEDULER_TIMEZONE=Europe/Prague        # timezone pro cron výrazy naplánovaných úloh
TASKS_DB_PATH=./data/tasks.db           # SQLite soubor pro naplánované úlohy

# Logging
LOG_LEVEL=INFO                          # DEBUG / INFO / WARNING / ERROR
LOG_FILE=agent.log                      # prázdné = logovat jen do konzole
```

---

## Spuštění

### CLI (vývoj a debug)

```bash
source .venv/bin/activate
python main.py cli
```

### Telegram bot (manuálně)

```bash
source .venv/bin/activate
python main.py telegram
```

Ověř, že bot odpovídá, než ho nastavíš jako systemd službu.

---

## Trvalý provoz na Raspberry Pi (systemd)

Systemd zajistí, že agent se **automaticky spustí po rebootu** a **restartuje se po pádu** — bez nutnosti SSH přístupu.

### 1. Zkopíruj service soubor

```bash
sudo cp agent.service /etc/systemd/system/
```

### 2. Zkontroluj cesty v service souboru

Otevři `/etc/systemd/system/agent.service` a ověř, že:
- `User=tomas` odpovídá tvému uživatelskému jménu
- `WorkingDirectory=/home/tomas/agent` odpovídá skutečné cestě
- `EnvironmentFile=/home/tomas/agent/.env` vede na tvůj `.env` soubor
- `ExecStart=/home/tomas/agent/.venv/bin/python main.py telegram` vede na správný Python

```bash
# Uprav pokud je potřeba:
sudo nano /etc/systemd/system/agent.service
```

### 3. Aktivuj a spusť

```bash
sudo systemctl daemon-reload
sudo systemctl enable agent    # spustit automaticky po bootu
sudo systemctl start agent     # spustit hned teď
```

### 4. Ověř stav

```bash
sudo systemctl status agent
```

Výstup by měl obsahovat `Active: active (running)`.

### Správa služby

```bash
sudo systemctl stop agent       # zastavit
sudo systemctl restart agent    # restartovat
sudo systemctl disable agent    # zrušit auto-start po bootu
```

### Chování při pádu

- Agent se restartuje automaticky po 10 sekundách
- Po 5 pádech během 5 minut systemd přestane restartovat (ochrana před crash smyčkou)
- Po vyřešení příčiny pádu: `sudo systemctl reset-failed agent && sudo systemctl start agent`

---

## Logování a monitoring

Logy se zapisují do `agent.log` (ve WorkingDirectory) a zároveň do `journald`:

```bash
# Sledovat logy živě
tail -f /home/tomas/agent/agent.log

# Nebo přes journald
journalctl -u agent -f

# Posledních 100 řádků
journalctl -u agent -n 100
```

Úroveň logování nastav přes `LOG_LEVEL=DEBUG` v `.env` pro více detailů.

---

## Telegram bot — příkazy

| Příkaz | Popis |
|--------|-------|
| `/start` | Uvítací zpráva, inicializuje session |
| `/memory` | Zobrazí všechny uložené vzpomínky |
| `/newsession` | Ukončí aktuální konverzaci a začne novou |

Bot přijímá textové i **hlasové zprávy**. Hlasová zpráva je přepsána Whisperem, zpracována, a odpověď je odeslána i jako audio.

---

## CLI — příkazy

| Příkaz | Popis |
|--------|-------|
| `/memory` | Zobrazí všechny uložené vzpomínky |
| `/clear` | Začne novou session |
| `/quit` | Ukončí CLI |

---

## Přidání nového nástroje

Viz [tools/example_tool.py](tools/example_tool.py) jako šablona. Každý nástroj musí:

1. Být `async` funkce s docstringem (popis funkce pro agenta)
2. Vracet `dict`
3. Mít definované input schema (dict kompatibilní s Claude tool use API)
4. Být zaregistrován v `main.py` přes `registry.register()`

```python
# tools/weather_tools.py

GET_WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "Název města"},
    },
    "required": ["city"],
}

async def get_weather(city: str) -> dict:
    """Vrátí aktuální počasí pro zadané město."""
    ...
    return {"city": city, "temp": 22}
```

Pak v `main.py` přidej import a registraci:

```python
from tools.weather_tools import GET_WEATHER_SCHEMA, get_weather

registry.register(get_weather, GET_WEATHER_SCHEMA)
```

Nástroje bez parametrů schema nepotřebují — `registry.register(get_weather)` stačí.
