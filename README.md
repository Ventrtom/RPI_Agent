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
main.py                   ← entry point, načítá konfigurace, sestavuje závislosti
│
├── core/
│   ├── agent.py          ← hlavní smyčka: zpracování zprávy, volání Claude, uložení paměti
│   ├── session.py        ← správa session v paměti + automatická expirace
│   ├── session_store.py  ← SQLite persistence — session přežijí restart
│   └── prompts.py        ← systémový prompt, voice context
│
├── llm/
│   └── claude.py         ← klient Anthropic API
│
├── memory/
│   └── client.py         ← Mem0 + ChromaDB — dlouhodobá paměť (fakta o uživateli)
│
├── voice/
│   ├── stt.py            ← Groq Whisper — přepis hlasu na text
│   └── tts.py            ← ElevenLabs — syntéza textu na hlas
│
├── interfaces/
│   ├── telegram_bot.py   ← Telegram bot (text + hlas)
│   └── cli.py            ← interaktivní CLI (vývoj, debug)
│
└── tools/                ← rozšiřitelný registr nástrojů
```

**Tok zpracování zprávy:**
1. Uživatel pošle zprávu přes Telegram (text nebo hlas)
2. Agent načte session historii ze SQLite, vyhledá relevantní vzpomínky z ChromaDB
3. Zavolá Claude API se systémovým promptem, historií a vzpomínkami
4. Uloží zprávy do session (SQLite) a asynchronně extrahuje nová fakta do Mem0
5. Odešle odpověď uživateli (u hlasových zpráv také přes ElevenLabs TTS)

**Paměť:**
- **Krátkodobá (session)** — history zpráv v aktuální konverzaci, uložena v SQLite, přežije restart
- **Dlouhodobá (Mem0 + ChromaDB)** — fakta extrahovaná z konverzací, uložena na disku v `./data/chroma/`

---

## Předpoklady

- Python 3.11+
- Raspberry Pi (nebo jiný Linux systém) s přístupem na internet
- ~500 MB místa na disku (ChromaDB, embedding model, venv)
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

# 2. Vytvoř a aktivuj virtuální prostředí
python3 -m venv .venv
source .venv/bin/activate

# 3. Nainstaluj závislosti
pip install -r requirements.txt
```

> **Poznámka:** Instalace `sentence-transformers` stahuje embedding model (~90 MB). Na Raspberry Pi to může trvat několik minut.

---

## Konfigurace

```bash
cp .env.example .env
```

Otevři `.env` a vyplň hodnoty:

```env
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

# Session
SESSION_TIMEOUT_MINUTES=60              # po jak dlouhé nečinnosti se session uzavře
SESSION_CLEANUP_INTERVAL=300            # jak často kontrolovat expirované sessions (sekundy)
SESSION_DB_PATH=./data/sessions.db      # SQLite soubor pro persistenci session

# Mem0 / ChromaDB
CHROMA_DB_PATH=./data/chroma            # adresář pro vektorovou DB
MEM0_USER_ID=tomas                      # identifikátor uživatele v paměti

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
3. Být zaregistrován přes `tool_registry.register()`

```python
from tools import tool_registry

async def get_weather(city: str) -> dict:
    """Vrátí aktuální počasí pro zadané město."""
    ...
    return {"city": city, "temp": 22}

tool_registry.register(get_weather)
```
