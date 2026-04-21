import os
import re
import threading
from datetime import date
from pathlib import Path

import yaml

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


class VaultManager:
    """Read/write/search markdown files in the vault directory."""

    def __init__(self, base_path: Path | None = None) -> None:
        self._base = Path(base_path or os.getenv("VAULT_PATH", "./vault")).resolve()
        self._base.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._debounce_delay = float(os.getenv("VAULT_INDEX_DEBOUNCE_SECONDS", "5"))
        self._timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(self, path: str) -> str:
        return self._abs(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        abs_path = self._abs(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        updated = self._set_updated(content)
        with self._lock:
            abs_path.write_text(updated, encoding="utf-8")
        self._schedule_reindex()

    def stop(self) -> None:
        """Cancel any pending debounced reindex timer."""
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def append(self, path: str, text: str) -> None:
        abs_path = self._abs(path)
        existing = abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
        separator = "\n" if existing and not existing.endswith("\n") else ""
        self.write(path, existing + separator + text)

    def patch_section(self, path: str, heading: str, new_content: str) -> str:
        abs_path = self._abs(path)
        if not abs_path.exists():
            raise FileNotFoundError(path)
        content = abs_path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)

        heading_pattern = heading.strip().lower()
        heading_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("#") and stripped.lstrip("#").strip().lower() == heading_pattern:
                heading_idx = i
                break

        if heading_idx is not None:
            end_idx = len(lines)
            for i in range(heading_idx + 1, len(lines)):
                if lines[i].startswith("#"):
                    end_idx = i
                    break
            body = new_content if new_content.endswith("\n") else new_content + "\n"
            result = "".join(lines[:heading_idx + 1] + [body] + lines[end_idx:])
        else:
            suffix = "" if content.endswith("\n") else "\n"
            result = content + suffix + f"\n## {heading}\n\n{new_content}"

        self.write(path, result)
        return result

    def search(self, query: str) -> list[dict]:
        results = []
        q = query.lower()
        for md_path in self._md_files(include_index=False):
            rel = str(md_path.relative_to(self._base))
            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError:
                continue
            if q not in text.lower():
                continue
            summary = self._extract_summary(text)
            snippet = self._snippet(text, q)
            results.append({"path": rel, "summary": summary, "snippet": snippet})
        return results

    def list_files(self) -> list[str]:
        return [
            str(p.relative_to(self._base))
            for p in self._md_files(include_index=False)
        ]

    def rebuild_index(self) -> None:
        files = sorted(self._md_files(include_index=False))
        today = date.today().strftime("%Y-%m-%d %H:%M")

        groups: dict[str, list[tuple[str, str]]] = {}
        for p in files:
            rel = p.relative_to(self._base)
            folder = str(rel.parent) if str(rel.parent) != "." else "/"
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                text = ""
            summary = self._extract_summary(text)
            stem = str(rel.with_suffix(""))
            groups.setdefault(folder, []).append((stem, summary))

        lines = [f"# Vault Index", f"_Poslední aktualizace: {today}_", ""]
        for folder in sorted(groups.keys()):
            header = folder if folder != "/" else "/"
            lines.append(f"## {header}")
            for stem, summary in groups[folder]:
                lines.append(f"- [[{stem}]] – {summary}")
            lines.append("")

        index_path = self._base / "_index.md"
        with self._lock:
            index_path.write_text("\n".join(lines), encoding="utf-8")

    def get_index(self) -> str:
        index_path = self._base / "_index.md"
        if not index_path.exists():
            self.rebuild_index()
        return index_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _schedule_reindex(self) -> None:
        if self._debounce_delay == 0:
            self.rebuild_index()
            return
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._debounce_delay, self.rebuild_index)
            self._timer.daemon = True
            self._timer.start()

    def _abs(self, path: str) -> Path:
        abs_path = (self._base / path).resolve()
        if not str(abs_path).startswith(str(self._base)):
            raise ValueError(f"Path traversal denied: {path!r}")
        return abs_path

    def _md_files(self, include_index: bool = True):
        for p in self._base.rglob("*.md"):
            if "__pycache__" in p.parts:
                continue
            if not include_index and p.name == "_index.md":
                continue
            yield p

    @staticmethod
    def _set_updated(content: str) -> str:
        today = date.today().isoformat()
        m = _FRONTMATTER_RE.match(content)
        if not m:
            return content
        fm_str = m.group(1)
        # Replace existing 'updated' line in-place to preserve all other formatting
        if re.search(r"^updated:", fm_str, re.MULTILINE):
            new_fm = re.sub(
                r'^updated:.*$', f'updated: "{today}"', fm_str, flags=re.MULTILINE
            )
        else:
            new_fm = fm_str.rstrip() + f'\nupdated: "{today}"'
        return f"---\n{new_fm}\n---\n" + content[m.end():]

    @staticmethod
    def _extract_summary(text: str) -> str:
        m = _FRONTMATTER_RE.match(text)
        if m:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
                if fm.get("summary"):
                    return str(fm["summary"])
            except yaml.YAMLError:
                pass
            body = text[m.end():]
        else:
            body = text
        for line in body.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped and not stripped.startswith("<!--"):
                return stripped
        return ""

    @staticmethod
    def _snippet(text: str, query: str, context: int = 100) -> str:
        lower = text.lower()
        idx = lower.find(query)
        if idx == -1:
            return ""
        start = max(0, idx - context // 2)
        end = min(len(text), idx + len(query) + context // 2)
        snippet = text[start:end].replace("\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(text):
            snippet += "…"
        return snippet
