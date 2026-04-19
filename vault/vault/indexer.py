import logging
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from vault.vault_manager import VaultManager

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 3.0


class _Handler(FileSystemEventHandler):
    def __init__(self, vault_manager: VaultManager) -> None:
        self._vm = vault_manager
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._rebuilding = False  # ← Prevence rekurze

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        src = getattr(event, "src_path", "")
        # Ignoruj všechny non-.md soubory
        if not src.endswith(".md"):
            return
        # Ignoruj _index.md změny (aby se netvořil infinite loop)
        if "_index.md" in src:
            return
        
        with self._lock:
            # Pokud už probíhá rebuild, nespouštěj dalšího
            if self._rebuilding:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(_DEBOUNCE_SECONDS, self._rebuild)
            self._timer.daemon = True
            self._timer.start()

    def _rebuild(self) -> None:
        try:
            with self._lock:
                self._rebuilding = True
            self._vm.rebuild_index()
            logger.info("Vault index rebuilt")
        except Exception:
            logger.exception("Vault index rebuild failed")
        finally:
            with self._lock:
                self._rebuilding = False


class VaultIndexer:
    """Watches the vault directory and rebuilds _index.md on changes."""

    def __init__(self, vault_manager: VaultManager) -> None:
        self._vm = vault_manager
        self._observer: Observer | None = None

    def start(self) -> None:
        handler = _Handler(self._vm)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._vm._base), recursive=True)
        self._observer.daemon = True
        self._observer.start()
        logger.info("VaultIndexer started watching %s", self._vm._base)

    def stop(self) -> None:
        if self._observer:
            self._observer.stop()
            self._observer.join()
