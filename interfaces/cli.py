import asyncio
import uuid

from core.agent import Agent


async def run_cli(agent: Agent, user_id: str) -> None:
    """Spustí interaktivní CLI smyčku."""
    session_id = str(uuid.uuid4())
    print("Agent CLI — zadejte zprávu nebo příkaz (/memory, /clear, /quit)")
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
            await agent._sessions.close_session(session_id)
            session_id = str(uuid.uuid4())
            print(f"Nová session: {session_id}\n")
            continue

        if user_input == "/memory":
            memories = await agent._memory.get_all()
            if memories:
                print("Vzpomínky:")
                for m in memories:
                    print(f"  - {m}")
            else:
                print("Žádné vzpomínky.")
            print()
            continue

        response = await agent.process(user_input, session_id, user_id)
        print(f"Agent: {response}\n")
