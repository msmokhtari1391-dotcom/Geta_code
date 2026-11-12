"""بررسی‌های قطعی (بدون هوش مصنوعی) برای پروژه‌های پایتون: import های محلی و نام‌های گمشده."""
import ast
import os
import re
from pathlib import Path

SKIP_DIRS = {".geta", "dist", "build", "__pycache__", "browser_profiles", "debug", "venv", ".venv"}


def _py_files(root):
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for fn in filenames:
            if fn.endswith(".py"):
                out.append(Path(dirpath, fn).relative_to(root).as_posix())
    return sorted(out)


def _public_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        names.add(x.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):  # تعریف‌های داخل if/try سطح بالا
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    names.add(sub.id)
    return names


def _module_path(root, dotted):
    """فایل یا پوشه‌ی ماژول را پیدا می‌کند؛ اگه پیدا نشد None."""
    rel = dotted.replace(".", "/")
    if (root / (rel + ".py")).is_file():
        return root / (rel + ".py")
    if (root / rel / "__init__.py").is_file():
        return root / rel / "__init__.py"
    if (root / rel).is_dir():
        return root / rel  # بسته‌ی بدون __init__ (namespace)
    return None


def _external_names(root):
    """ماژول‌های بیرونی: استاندارد پایتون، requirements.txt، و هرچی روی همین پایتون نصبه."""
    import importlib.util
    import sys
    ext = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    req = Path(root) / "requirements.txt"
    if req.is_file():
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            name = re.split(r"[<>=!~\[; ]", line.strip(), maxsplit=1)[0].strip()
            if name and not name.startswith("#"):
                ext.add(name.replace("-", "_").lower())
                ext.add(name)
    return ext


def _is_external(top, ext):
    import importlib.util
    if top in ext or top.lower() in ext:
        return True
    try:
        return importlib.util.find_spec(top) is not None
    except (ImportError, ValueError):
        return False


def find_issues(root):
    """فهرست مشکلات import محلی: ماژول گمشده یا نام گمشده داخل ماژول محلی.
    هر import که بیرونی نباشه (نه استاندارد، نه requirements، نه نصب‌شده) محلی حساب میشه."""
    root = Path(root)
    ext = _external_names(root)
    issues = []
    cache = {}
    for rel in _py_files(root):
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue  # خطای سینتکس جداگانه بررسی می‌شه
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [(node.module, [a.name for a in node.names])]
            elif isinstance(node, ast.Import):
                mods = [(a.name, []) for a in node.names]
            else:
                continue
            for mod, names in mods:
                if _is_external(mod.split(".")[0], ext):
                    continue  # کتابخانه‌ی بیرونی یا استاندارد
                target = _module_path(root, mod)
                if target is None:
                    issues.append({"file": rel, "kind": "module_missing", "module": mod, "names": names})
                    continue
                if not names or target.is_dir():
                    continue
                if mod not in cache:
                    try:
                        cache[mod] = _public_names(ast.parse(target.read_text(encoding="utf-8", errors="replace")))
                    except SyntaxError:
                        cache[mod] = None
                if cache[mod] is None:
                    continue
                for name in names:
                    if name == "*" or name in cache[mod]:
                        continue
                    if _module_path(root, mod + "." + name) is not None:
                        continue  # زیرماژول
                    issues.append({"file": rel, "kind": "name_missing", "module": mod, "name": name})
    return issues


def manifest(root, paths):
    """خلاصه‌ی کوتاه هر فایل: تعداد خط و نام‌های تعریف‌شده. برای اینکه همه‌ی فایل‌ها توی پرامپت جا بشن."""
    root = Path(root)
    lines = []
    for rel in paths:
        p = root / rel
        if not p.is_file() or p.suffix != ".py":
            if p.is_file():
                lines.append(f"- {rel} ({sum(1 for _ in p.open(encoding='utf-8', errors='replace'))} خط)")
            else:
                lines.append(f"- {rel} (وجود ندارد)")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        try:
            names = sorted(_public_names(ast.parse(text)))
        except SyntaxError:
            names = ["(خطای سینتکس)"]
        lines.append(f"- {rel} ({text.count(chr(10)) + 1} خط) تعریف‌ها: {', '.join(names[:40])}")
    return "\n".join(lines)


def js_balance_issue(code):
    """بررسی سبک: پرانتز/آکولاد/کروشه و رشته‌ها بسته‌اند؟ (فایل نصفه‌مونده رو می‌گیره)."""
    stack, i, n = [], 0, len(code)
    pairs = {")": "(", "]": "[", "}": "{"}
    while i < n:
        ch = code[i]
        nxt = code[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = code.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and nxt == "*":
            j = code.find("*/", i + 2)
            if j < 0:
                return "کامنت بلاک بسته نشده"
            i = j + 2
            continue
        if ch in "\"'`":
            q = ch
            i += 1
            while i < n and code[i] != q:
                if code[i] == "\\":
                    i += 1
                elif q != "`" and code[i] == "\n":
                    return "رشته‌ی بسته‌نشده"
                i += 1
            if i >= n:
                return "رشته‌ی بسته‌نشده (فایل وسط کار قطع شده)"
            i += 1
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return f"پرانتز یا آکولاد اضافه/جاافتاده نزدیک «{ch}»"
            stack.pop()
        i += 1
    if stack:
        return f"{len(stack)} پرانتز یا آکولاد بسته‌نشده (احتمالاً فایل قطع شده)"
    return ""


def html_issues(text):
    """خطاهای ساختاری HTML: کامل بودن تگ‌ها، اسکریپت‌های بسته و JS داخلش."""
    import re
    low = text.lower()
    issues = []
    if "<html" not in low or "</html>" not in low:
        issues.append("سند HTML کامل نیست (تگ <html> یا </html> نیست)")
    if "<body" not in low or "</body>" not in low:
        issues.append("تگ <body> کامل نیست")
    opens = len(re.findall(r"<script\b", low))
    closes = low.count("</script>")
    if opens != closes:
        issues.append(f"تعداد <script> ({opens}) با </script> ({closes}) برابر نیست")
    for m in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.S | re.I):
        msg = js_balance_issue(m.group(1))
        if msg:
            issues.append("داخل <script>: " + msg)
            break
    return issues
