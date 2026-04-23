import asyncio
import uuid

from core.agent import Agent

_HELP_TEXT = """Dostupné příkazy:

/start — nová konverzace, inicializace session
/clear — ukončí aktuální session a začne novou (alias pro /newsession)
/memory — zobrazí všechny uložené vzpomínky
/feedback <text> — uloží tvoji poznámku k mému chování s kontextem posledních zpráv
/self-reflect — vytvořím sebereflexi aktuální session
/snapshot [tag] — uloží aktuální session (volitelně s tagem)
/help — tento přehled
/quit — ukončí CLI"""


async def run_cli(agent: Agent, user_id: str, observability=None, session_manager=None) -> None:
    """Spustí interaktivní CLI smyčku."""
    session_id = str(uuid.uuid4())
    print("Agent CLI — zadejte zprávu nebo příkaz (/memory, /clear, /quit, /help)")
    print(f"Session: {session_id}\n")

    while True:
        try:
            user_input = await asyncio.get_event_loop().run_in_executor(None, input, "Vy: ")
        except (EOFError, KeyboardInterrupt):
            print("\nUkončuji.")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        if user_input == "/quit":
            print("Ukončuji.")
            break

        if user_input == "/clear":
            await agent.close_session(session_id)
            session_id = str(uuid.uuid4())
            print(f"Nová session: {session_id}\n")
            continue

        if user_input == "/memory":
            memories = await agent.get_all_memories()
            if memories:
                print("Vzpomínky:")
                for m in memories:
                    print(f"  - {m}")
            else:
                print("Žádné vzpomínky.")
            print()
            continue

        if user_input == "/help":
            print(_HELP_TEXT)
            print()
            continue

        if user_input.startswith("/feedback"):
            parts = user_input.split(" ", 1)
            text = parts[1].strip() if len(parts) > 1 else ""
            if not text:
                print("Použití: /feedback <tvá poznámka>\n")
                continue
            if observability is None or session_manager is None:
                print("Observability není nakonfigurováno.\n")
                continue
            history = await session_manager.get_history(session_id)
            try:
                path = await observability.feedback.record_feedback(text, session_id, history)
                print(f"Feedback uložen: {path}\n")
            except Exception as e:
                print(f"Chyba: {e}\n")
            continue

        if user_input == "/self-reflect":
            if observability is None:
                print("Observability není nakonfigurováno.\n")
                continue
            print("Generuji reflexi…")
            reflection = await agent.generate_self_reflection(session_id)
            try:
                path = await observability.feedback.save_reflection(reflection, session_id, {})
                print(f"\nReflexe:\n{reflection}\n\nUloženo: {path}\n")
            except Exception as e:
                print(f"Reflexe:\n{reflection}\n\nChyba při ukládání: {e}\n")
            continue

        if user_input.startswith("/snapshot"):
            parts = user_input.split(" ", 1)
            tag = parts[1].strip() if len(parts) > 1 else None
            if observability is None or session_manager is None:
                print("Observability není nakonfigurováno.\n")
                continue
            session = session_manager.get_session(session_id)
            if session is None:
                print("Žádná aktivní session k uložení.\n")
                continue
            try:
                path = await observability.snapshots.save_snapshot(
                    session, snapshot_type="manual", tag=tag
                )
                print(f"Snapshot uložen: {path}\n")
            except ValueError as e:
                print(f"Neplatný tag: {e}\n")
            except Exception as e:
                print(f"Chyba: {e}\n")
            continue

        response = await agent.process(user_input, session_id, user_id)
        print(f"Agent: {response}\n")
