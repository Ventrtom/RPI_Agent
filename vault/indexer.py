import logging
import threading
import time

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from vault.vault_manager import VaultManager

logger = logging.getLogger(__name__)

_DEBOUNCE_SECONDS = 3.0
_MIN_REBUILD_INTERVAL = 10.0  # prevents runaway rebuild loops


class _Handler(FileSystemEventHandler):
    def __init__(self, vault_manager: VaultManager) -> None:
        self._vm = vault_manager
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._rebuilding = False
        self._last_rebuilt: float = 0.0

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        src = getattr(event, "src_path", "")
        if not src.endswith(".md") or "_index.md" in src:
            return
        logger.info("Vault event: %s %s", event.event_type, src)
        with self._lock:
            if self._rebuilding:
                return
            if time.monotonic() - self._last_rebuilt < _MIN_REBUILD_INTERVAL:
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
            with self._lock:
                self._last_rebuilt = time.monotonic()
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
