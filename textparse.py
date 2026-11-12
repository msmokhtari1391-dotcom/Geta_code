"""استخراج فوق‌مقاوم فایل و JSON از متن پاسخ چت‌بات‌ها (حتی با سینتکس غیراستاندارد یا تخریب مارک‌داون مرورگر)."""
import ast
import json
import re

FILE_RE = re.compile(r"@@@FILE[^\n]*\n(.*?)\n?[ \t]*@@@END", re.S)
FILE_HEADER_RE = re.compile(r"@@@FILE\s+([^\s|\n]+)", re.I)
FORMAT_RE = re.compile(r"@@@FILE[^\n]*?format:\s*([\w.+-]+)", re.I)
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)

LANG_LABELS = {"python", "py", "javascript", "js", "typescript", "ts", "html", "css", "json",
               "bash", "sh", "shell", "powershell", "bat", "cmd", "text", "plaintext", "txt",
               "toml", "yaml", "yml", "ini", "xml", "sql", "markdown", "md", "jsx", "tsx",
               "c", "cpp", "c++", "csharp", "c#", "java", "go", "rust", "php", "dockerfile"}
UI_LABELS = {"copy", "copy code", "copied!", "copied", "download", "run", "edit",
             "کپی", "کپی کد", "دانلود", "اجرا"}

TEMPLATE_MARKERS = (
    "tic-tac-toe game: main.py runs the loop",
    "class board with draw()",
    "...full file content...",
    "relative/path/of/entry_file",
    '{"path": "...", "action": "create", "goal": "..."}',
)

TRIPLE_DQ = chr(34) * 3
TRIPLE_SQ = chr(39) * 3

def _strip_ui_labels(body: str) -> str:
    lines = body.split("\n")
    i = removed = 0
    while i < len(lines) and removed < 3:
        s = lines[i].strip().lower()
        if s == "":
            i += 1
        elif s in LANG_LABELS or s in UI_LABELS:
            i += 1
            removed += 1
        else:
            break
    return "\n".join(lines[i:])

def strip_echo(reply: str, prompt: str) -> str:
    reply = re.sub(r"You're using the free version of ChatGPT[^\n]*", "", reply or "").strip()
    if not reply:
        return ""
    for marker in ("ChatGPT said:", "Assistant:", "پاسخ:"):
        if marker in reply:
            part = reply.split(marker)[-1].strip()
            if part:
                reply = part
    prompt_str = (prompt or "").strip()
    if not prompt_str:
        return reply
    if reply.startswith(prompt_str):
        after = reply[len(prompt_str):].lstrip()
        if after:
            return after
    p_lines = [ln.strip() for ln in prompt_str.splitlines() if len(ln.strip()) > 15]
    if p_lines and p_lines[0] in reply[:400]:
        for tail_ln in reversed(p_lines):
            if tail_ln.startswith(("@@@", "%%%", "{", "<", "- ", "```")):
                continue
            pos = reply.find(tail_ln)
            if 0 <= pos <= len(prompt_str) + 800:
                after = reply[pos + len(tail_ln):].lstrip()
                if after:
                    return after
    return reply

def declared_format(text: str) -> str:
    matches = list(FORMAT_RE.finditer(text or ""))
    return matches[-1].group(1).lower() if matches else "—"

def repair_markdown_corrupted_python(code: str) -> str:
    try:
        ast.parse(code)
        return code
    except SyntaxError:
        pass
    fixed = re.sub(r"\bPath\(\s*file\s*\)", "Path(__file__)", code)
    fixed = re.sub(r"^(\s*if\s+)name(\s*==\s*['\"]__main__['\"]\s*:)", r"\1__name__\2", fixed, flags=re.M)
    fixed = re.sub(r"\bdef\s+init\s*\(\s*self\b", "def __init__(self", fixed)
    lines = fixed.splitlines()
    out = []
    in_triple = False
    PYTHON_STARTS = (
        "import ", "from ", "def ", "async ", "class ", "if ", "elif ", "else:", "for ", "while ",
        "try:", "except", "finally:", "with ", "return", "yield", "raise", "pass", "break", "continue",
        "global ", "nonlocal ", "assert ", "del ", "@", "#", TRIPLE_DQ, TRIPLE_SQ, ")", "]", "}"
    )
    for ln in lines:
        s = ln.strip()
        for q in (TRIPLE_DQ, TRIPLE_SQ):
            if q in s and s.count(q) % 2 == 1:
                in_triple = not in_triple
        if in_triple or not s:
            out.append(ln)
            continue
        if re.match(r"^[-=_*]{5,}$", s):
            out.append("# " + ln)
            continue
        is_assign = bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.,\s]*\s*[:+\-*/]?=", s))
        is_call_or_expr = bool(re.match(r"^[A-Za-z_][A-Za-z0-9_.]*\s*[\(\[\{]", s))
        is_dict_or_tuple = s.startswith((chr(34), chr(39), "{", "[", "(")) or s.endswith((",", ":", "\\"))
        if not (s.startswith(PYTHON_STARTS) or is_assign or is_call_or_expr or is_dict_or_tuple):
            out.append("# " + ln)
        else:
            out.append(ln)
    return "\n".join(out) + "\n"

def extract_file(text: str) -> str:
    text = text or ""
    fmt = declared_format(text)
    matches = [
        m.group(1) for m in FILE_RE.finditer(text)
        if "...full file content..." not in m.group(1).lower()
        and "(write the complete" not in m.group(1).lower()
    ]
    if matches:
        body = matches[-1]
    elif "@@@FILE" in text:
        idx = text.rfind("@@@FILE")
        nl = text.find("\n", idx)
        body = text[nl + 1:] if nl >= 0 else text
        body = re.sub(r"\n?[ \t]*@@@END.*$", "", body, flags=re.S)
    else:
        body = text
    blocks = [
        b for b in FENCE_RE.findall(body)
        if "...full file content..." not in b.lower()
        and "(write the complete" not in b.lower()
    ]
    if blocks:
        longest = max(blocks, key=len)
        if len(longest) >= len(body.strip()) * 0.45:
            body = longest
    body = _strip_ui_labels(body).strip("\n")
    if "...full file content..." in body.lower() or "(write the complete" in body.lower():
        return ""
    if fmt == "python" and body.strip():
        body = repair_markdown_corrupted_python(body).strip("\n")
    return body + "\n" if body.strip() else ""

def parse_review(text: str):
    if not text:
        return None
    s = text.strip()
    if "@@@OK" in s and "@@@FILE" not in s and len(s) < 250:
        return None
    if s.upper() == "OK":
        return None
    if "@@@FILE" in s or "```" in s:
        fixed = extract_file(s)
        return fixed or None
    return None

def _balanced_objects(text):
    for start in (i for i, c in enumerate(text) if c == "{"):
        depth, in_str, esc = 0, False, False
        quote_char = chr(34)
        for j in range(start, len(text)):
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == quote_char:
                    in_str = False
            elif c in (chr(34), chr(39)):
                in_str = True
                quote_char = c
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start:j + 1]
                    break

def _is_template_obj(raw_str: str) -> bool:
    low = raw_str.lower()
    if any(m in low for m in TEMPLATE_MARKERS):
        return True
    if '"path": "..."' in low or '"path":"..."' in low:
        return True
    return False

def _try_parse_dict(raw: str):
    raw = raw.strip()
    cleaned_comma = re.sub(r",\s*([}\]])", r"\1", raw)
    fixed_brace = re.sub(r'("goal"\s*:\s*"(?:[^"\\]|\\.)*")\s*\]', r"\1}\n  ]", cleaned_comma)
    for cand in (raw, cleaned_comma, fixed_brace):
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    try:
        py_cand = re.sub(r"\btrue\b", "True", fixed_brace)
        py_cand = re.sub(r"\bfalse\b", "False", py_cand)
        py_cand = re.sub(r"\bnull\b", "None", py_cand)
        obj = ast.literal_eval(py_cand)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return None

def _normalize_plan_dict(obj: dict) -> dict:
    if not isinstance(obj, dict):
        return obj
    if "files" not in obj:
        for alt in ("plan", "file_list", "project_files", "items", "modules"):
            if alt in obj and isinstance(obj[alt], list):
                obj["files"] = obj[alt]
                break
    if "files" in obj and isinstance(obj["files"], list):
        norm_files = []
        for item in obj["files"]:
            if isinstance(item, str) and item.strip():
                norm_files.append({"path": item.strip(), "action": "create", "goal": f"Implementation of {item.strip()}"})
            elif isinstance(item, dict):
                path = item.get("path") or item.get("file") or item.get("filename") or item.get("name") or ""
                if path and path != "...":
                    norm_files.append({
                        "path": str(path).strip(),
                        "action": str(item.get("action") or "create").lower(),
                        "goal": str(item.get("goal") or item.get("purpose") or item.get("description") or f"Write {path}"),
                    })
        if norm_files:
            obj["files"] = norm_files
    return obj

def _salvage_plan_from_text(text: str):
    files = []
    seen = set()
    pattern = re.compile(
        r"(?:^|[\n*•\-])\s*[`'\"]?([a-zA-Z0-9_./\-]+\.(?:py|pyw|html|htm|css|js|json|txt|md))[`'\"]?\s*(?:[:—\-]\s*([^\n]+))?",
        re.M
    )
    for m in pattern.finditer(text or ""):
        path = m.group(1).strip().strip("`'\"")
        goal = (m.group(2) or "").strip()
        low = path.lower()
        if low in seen or "path.ext" in low or "relative/path" in low or low in ("e.g.", "i.e."):
            continue
        seen.add(low)
        files.append({"path": path, "action": "create", "goal": goal or f"Complete implementation of {path}"})
    if files:
        run_cmd = "index.html" if any(f["path"].endswith(".html") for f in files) else "python main.py"
        return {"summary": "Auto-extracted project plan from architect response", "files": files, "run": run_cmd}
    return None

def extract_json(text: str, required_key: str):
    text = text or ""
    candidates = FENCE_RE.findall(text) + list(_balanced_objects(text))
    s_idx, e_idx = text.find("{"), text.rfind("}")
    if 0 <= s_idx < e_idx:
        candidates.append(text[s_idx:e_idx + 1])
    for raw in reversed(candidates):
        raw = raw.strip()
        if _is_template_obj(raw):
            continue
        obj = _try_parse_dict(raw)
        if not isinstance(obj, dict):
            continue
        if required_key == "files":
            obj = _normalize_plan_dict(obj)
        if required_key in obj:
            if required_key == "files" and not obj.get("files"):
                continue
            return obj
    if required_key == "files":
        salvaged = _salvage_plan_from_text(text)
        if salvaged:
            return salvaged
    return None
