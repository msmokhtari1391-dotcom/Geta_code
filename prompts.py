"""پرامپت‌های معمار، کدنویس و بازبین برای ساخت جدید و ویرایش (با محافظت از کد در برابر تخریب Markdown مرورگر)."""

import os

FORMAT_BY_EXT = {".py": "python", ".pyw": "python", ".html": "html", ".htm": "html", ".css": "css",
                 ".js": "javascript", ".json": "json", ".md": "markdown", ".txt": "text", ".bat": "batch",
                 ".ps1": "powershell", ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".csv": "csv", ".sql": "sql"}


def format_of(path):
    ext = os.path.splitext(path)[1].lower()
    return FORMAT_BY_EXT.get(ext, ext.lstrip(".") or "text")


PLAN_FORMAT = """Reply with ONLY one JSON object inside a ```json code block and nothing else.
Keys required in the JSON object:
- "summary": short description of the architecture and how files connect.
- "files": list of objects, each with {"path": "...", "action": "create", "goal": "..."}.
- "run": command to run the project (e.g. "index.html" or "python main.py").

CRITICAL ARCHITECTURE RULES:
- If the request names files or folders (for example engine/game.py, screens/menu.py), create EXACTLY those paths with that folder structure. Do not merge them and do not skip any of them.
- If the request names no files, keep the list cohesive. There is no limit on the number of files; a large project may have many.
- Every file must stay under 300 lines. A larger module must be split into several files with clear names (for example world/tilemap.py and world/maps.py).
- Order files so foundational modules come BEFORE files that import them (`main.py` last).
- Browser games and web apps: plan SEVERAL source files, e.g. index.html (markup only), css/style.css, and js/ files split by responsibility (config.js, input.js, entities.js, render.js, game.js). The system bundles them into ONE HTML file automatically. Never put a whole game into a single file.
- Do NOT include empty `__init__.py` files or `README.md` unless explicitly requested.
- For browser/HTML5 games or web apps: prefer a single self-contained `index.html` (or at most 3 files: `index.html`, `style.css`, `game.js`).
- For Python desktop apps/games: include `requirements.txt` if external packages (like pygame) are needed.
- Order files so foundational modules come BEFORE files that import them (`main.py` last).
- Use real file names and forward slashes. No placeholders."""


def architect_new(task, name, tree):
    return f"""You are the software architect. Design a complete, runnable project.
Request:
{task}

Project name: {name}
Current folder contents (may be empty):
{tree}

Use action "create" for every file.
{PLAN_FORMAT}"""


def architect_select(task, tree):
    return f"""You are the software architect. A change is requested in an existing project.
Request:
{task}

Project files:
{tree}

Which files must you read to plan this change? Reply with ONLY JSON:
{{"read": ["path1", "path2"]}}  (at most 25 files)"""


def architect_edit(task, tree, contents):
    return f"""You are the software architect. Plan changes to an existing project.
Request:
{task}

Project files:
{tree}

Contents of relevant files:
{contents}

List ONLY files that must be created, modified or deleted. For "modify", the goal must describe precisely what changes.
{PLAN_FORMAT}"""


def _plan_list(files):
    return "\n".join(f"- {f['path']} [{f['action']}]: {f['goal']}" for f in files)


def coder(task, summary, files, item, current, context_files=""):
    cur = ""
    if current is not None:
        cur = (f"\nCurrent content of this file (apply the change, keep everything else working):\n"
               f"%%%CURRENT\n{current}\n%%%END_CURRENT\n")
    ctx = ""
    if context_files and context_files.strip():
        ctx = (f"\nAlready written project files (match their exact class/function names, exports, and DOM IDs):\n"
               f"{context_files}\n")
    lang = format_of(item["path"])
    return f"""You are the coder. Write the COMPLETE content of one file.
Overall request:
{task}

Architecture:
{summary}

All files in the plan:
{_plan_list(files)}
{ctx}
Your file: {item['path']} ({item['action']})
Goal: {item['goal']}
{cur}
CRITICAL OUTPUT FORMAT:
You MUST wrap the file content inside a fenced code block (```{lang} ... ```) between @@@FILE and @@@END so that indentation, `#` comments, and `__dunder__` names are preserved 100% intact:

@@@FILE {item['path']} | format: {lang}
```{lang}
(write the complete, valid {lang} code of {item['path']} here)
```
@@@END

Keep the path exactly as given above. No explanations outside the block, no placeholders, never write "rest unchanged". The full file.
Rules that break the project if ignored:
- Never comment out a line that is part of a multi-line statement (imports, function calls, dict or list literals). Every opening bracket must be closed.
- Import only names that exist in the "Already written project files" list above; if a name is missing there, define it in your file instead.
- The file must end properly (the last statement complete). Keep it under 300 lines; if the goal needs more, split it into more files in the plan instead."""


def reviewer(task, files, item, content, context_files=""):
    ctx = ""
    if context_files and context_files.strip():
        ctx = f"\nOther project files for reference (verify imports/exports match):\n{context_files}\n"
    lang = format_of(item["path"])
    return f"""You are the code reviewer. Check this file for bugs, syntax errors, missing imports,
mismatches with other project files, and placeholders.
IMPORTANT:
- If the file is already complete, runnable, and has no critical bugs, reply ONLY with: @@@OK
- Do NOT rewrite a working file just for minor style preferences.
- Only if there is a real bug, syntax error, or broken import, reply with the full corrected file wrapped in ```{lang} ... ``` inside @@@FILE ... @@@END.

Overall request:
{task}

All files in the plan:
{_plan_list(files)}
{ctx}
File: {item['path']}
Goal: {item['goal']}
@@@FILE {item['path']} | format: {lang}
```{lang}
{content}
```
@@@END

If the file is correct reply exactly: @@@OK
Otherwise reply with the full corrected file in the same @@@FILE ... @@@END format (with ```{lang} code fence), no explanations."""


def refiner(task):
    return f"""You are a senior product designer and prompt writer for an AI that builds software projects.
The user wrote a short request. Expand it into a precise build brief in the SAME language as the request.
Cover: goal, target platform, main features, screens or levels, controls, visual style, content that must be real (no placeholders), and acceptance criteria.
If the request names a final format (html, exe, zip), state it clearly.
Do NOT write code and do NOT reply with JSON. Plain text only, at most 25 short lines.

User request:
{task}
"""


def orchestrator_integrate(brief, fmt, files_text, entry_hint):
    return f"""You are the project integrator of an automatic build system.
Several AI coders wrote the files below. Your job:
1. Check that every import or reference matches a real file name, class/function name, and path.
2. Find missing files or missing pieces needed to run or build the project.
3. Choose the entry file (the one that starts the program, e.g. index.html or main.py).
4. Make sure the requested final output can be built from these files: {fmt or "auto-detect best format"}.
Fix ONLY what is necessary. If the code already works properly, return an empty list `[]` for `fixes`.
A fix must contain the FULL corrected file content.
Build target, choose exactly one: "html" (one self-contained HTML file for a web/HTML/JS project),
"exe" (Windows executable if user requested exe), "zip" (if user requested zip), "none" (runnable source files).
Reply with ONLY one JSON object, no other text:
{{"entry": "main_entry_file", "build": "html|exe|zip|none", "fixes": [], "notes": "short summary in Persian"}}

Project brief:
{brief}

Entry hint from earlier planning: {entry_hint or "none"}

Files:
{files_text}
"""


def orchestrator_repair(brief, fmt, files_text, build_log, entry):
    return f"""You are the build repair assistant of an automatic build system.
The build or execution of the project below failed. Read the error log, find the real cause, and fix the files.
Fix ONLY what is necessary. A fix must contain the FULL corrected file content.
Reply with ONLY one JSON object, no other text:
{{"fixes": [{{"path": "file_path", "content": "full_code"}}], "notes": "short summary in Persian"}}

Requested final output: {fmt or "source code"}. Entry file: {entry or "unknown"}

Project brief:
{brief}

Error / Build log (last lines):
{build_log}

Files:
{files_text}
"""
