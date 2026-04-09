# RPI Agent

Osobní AI asistent běžící na Raspberry Pi 5. Přístupný přes Telegram a CLI.

## Předpoklady

- Python 3.11+
- API klíče: Anthropic, ElevenLabs, Telegram Bot Token

## Instalace

```bash
pip install -r requirements.txt
```

## Konfigurace

```bash
cp .env.example .env
```

Otevři `.env` a vyplň všechny klíče.

## Spuštění

### CLI (vývoj a debug)

```bash
python main.py cli
```

Dostupné příkazy v CLI:
- `/memory` — zobrazí všechny uložené vzpomínky
- `/clear` — začne novou session
- `/quit` — ukončí

### Telegram bot

```bash
python main.py telegram
```

Bot podporuje textové i hlasové zprávy. Příkazy:
- `/start` — uvítání
- `/memory` — zobrazí uložené vzpomínky
- `/newsession` — začne novou session

## Přidání nového nástroje

Viz `tools/example_tool.py` jako šablona. Každý nástroj musí:

1. Být `async` funkce
2. Vracet `dict`
3. Být zaregistrován přes `tool_registry.register()`
