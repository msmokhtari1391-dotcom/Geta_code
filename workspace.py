"""مدیریت پوشه‌ی پروژه: مسیر امن، فهرست فایل‌ها، خواندن و نوشتن با بکاپ."""
import os
import shutil
import time
from pathlib import Path

IGNORE_DIRS = {".git", ".geta", "node_modules", "__pycache__", ".venv", "venv", "env",
               "dist", "build", ".idea", ".vscode", ".mypy_cache", ".pytest_cache"}
BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".exe", ".dll", ".pyd",
              ".so", ".zip", ".rar", ".7z", ".pdf", ".mp3", ".mp4", ".wav", ".ttf", ".otf",
              ".woff", ".woff2", ".db", ".sqlite", ".pyc", ".bin", ".lock", ".spec"}
MAX_TEXT_BYTES = 300_000


class Workspace:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.meta = self.root / ".geta"
        self._backup_root = self.meta / "backups" / time.strftime("%Y%m%d-%H%M%S")

    def ensure(self):
        self.meta.mkdir(parents=True, exist_ok=True)

    def resolve(self, rel) -> Path:
        s = str(rel or "").strip().strip("\"'`").replace("\\", "/")
        while s.startswith("./"):
            s = s[2:]
        if not s or s.startswith("/") or ":" in s:
            raise ValueError(f"مسیر نامعتبر: {rel}")
        p = (self.root / s).resolve()
        if p == self.root or self.root not in p.parents:
            raise ValueError(f"مسیر خارج از پوشه: {rel}")
        if ".geta" in p.relative_to(self.root).parts:
            raise ValueError(f"مسیر رزرو شده: {rel}")
        return p

    def rel(self, path) -> str:
        return self.resolve(path).relative_to(self.root).as_posix()

    def exists(self, rel) -> bool:
        try:
            return self.resolve(rel).is_file()
        except ValueError:
            return False

    def list_files(self, limit=5000):
        out = []
        if not self.root.is_dir():
            return out
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.endswith(".egg-info"))
            for fn in sorted(filenames):
                if fn.endswith((".geta-tmp", "-play.html")) or fn == "run_geta.bat":
                    continue
                out.append((Path(dirpath) / fn).relative_to(self.root).as_posix())
                if len(out) >= limit:
                    return out
        return out

    def is_text(self, rel) -> bool:
        p = self.resolve(rel)
        if p.suffix.lower() in BINARY_EXT:
            return False
        try:
            if p.stat().st_size > MAX_TEXT_BYTES:
                return False
            with open(p, "rb") as fh:
                return b"\0" not in fh.read(4096)
        except OSError:
            return False

    def read(self, rel) -> str:
        return self.resolve(rel).read_text(encoding="utf-8", errors="replace")

    def tree_text(self, limit=400) -> str:
        files = self.list_files()
        lines = []
        for rel in files[:limit]:
            try:
                size = (self.root / rel).stat().st_size
            except OSError:
                size = 0
            lines.append(f"{rel} ({size} B)")
        if len(files) > limit:
            lines.append(f"... and {len(files) - limit} more files")
        return "\n".join(lines) or "(empty folder)"

    def snapshot(self, paths=None, budget=45_000, per_file=12_000):
        parts, used, complete = [], 0, True
        for rel in (paths if paths is not None else self.list_files()):
            if not self.exists(rel) or not self.is_text(rel):
                continue
            body = self.read(rel)
            if len(body) > per_file:
                body = body[:per_file] + "\n... [truncated]"
                complete = False
            block = f"%%%FILE {rel}\n{body}\n%%%ENDFILE\n"
            if used + len(block) > budget:
                complete = False
                break
            parts.append(block)
            used += len(block)
        return "".join(parts), complete

    def backup(self, rel):
        p = self.resolve(rel)
        if not p.is_file():
            return None
        dst = self._backup_root / p.relative_to(self.root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            shutil.copy2(p, dst)
        return dst

    def write(self, rel, content: str):
        p = self.resolve(rel)
        if p.is_file():
            try:
                if b"\r\n" in p.read_bytes()[:20000]:
                    content = content.replace("\r\n", "\n").replace("\n", "\r\n")
            except OSError:
                pass
            self.backup(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".geta-tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        os.replace(tmp, p)

    def remove(self, rel):
        p = self.resolve(rel)
        if p.is_file():
            self.backup(rel)
            p.unlink()
