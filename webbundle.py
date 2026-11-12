"""پروژه‌ی وب چندفایلی را به یک HTML تکی تبدیل می‌کند که با دوبار کلیک (file://) اجرا شود."""
import base64
import json
import mimetypes
import re
from pathlib import Path

IMPORT_FROM_RE = re.compile(r'^[ \t]*import\s+([\w$\s{},*]+?)\s+from\s*(["\'])([^"\'\n]+)\2[ \t]*;?', re.M)
IMPORT_SIDE_RE = re.compile(r'^[ \t]*import\s*(["\'])([^"\'\n]+)\1[ \t]*;?', re.M)
DYN_IMPORT_RE = re.compile(r'\bimport\(\s*(["\'])([^"\'\n]+)\1\s*\)')
EXPORT_STAR_RE = re.compile(r'^[ \t]*export\s*\*\s*(?:as\s+([\w$]+)\s+)?from\s*(["\'])([^"\'\n]+)\2[ \t]*;?', re.M)
EXPORT_LIST_RE = re.compile(r'^[ \t]*export\s*\{([^}]*)\}(?:\s*from\s*(["\'])([^"\'\n]+)\2)?[ \t]*;?', re.M)
EXPORT_DEFAULT_DECL_RE = re.compile(r'^([ \t]*)export\s+default\s+((?:async\s+)?function\*?\s+([\w$]+)|class\s+([\w$]+))', re.M)
EXPORT_DEFAULT_RE = re.compile(r'^([ \t]*)export\s+default\s+', re.M)
EXPORT_DECL_RE = re.compile(r'^([ \t]*)export\s+((?:async\s+)?function\*?\s*([\w$]+)|class\s+([\w$]+)|(?:const|let|var)\s+([\w$]+))', re.M)
SCRIPT_RE = re.compile(r'<script\b([^>]*)>(.*?)</script\s*>', re.I | re.S)
LINK_RE = re.compile(r'<link\b[^>]*>', re.I)
CSS_URL_RE = re.compile(r'url\(\s*(["\']?)([^"\')]+)\1\s*\)', re.I)
CSS_IMPORT_RE = re.compile(r'@import\s+(?:url\()?\s*(["\'])([^"\']+)\1\s*\)?\s*;', re.I)
DATA_EXT = {".json", ".txt", ".csv", ".xml", ".glsl", ".frag", ".vert"}

RUNTIME_HEAD = """(function () {
"use strict";
var defs = {}, cache = {};
function __def(id, fn) { defs[id] = fn; }
function __req(id) {
  if (cache[id]) return cache[id];
  if (!defs[id]) throw new Error("Geta bundle: module not found: " + id);
  var exp = cache[id] = {};
  defs[id](__req, exp);
  return exp;
}
function __star(target, src) {
  Object.keys(src).forEach(function (k) {
    if (k !== "default" && !(k in target)) Object.defineProperty(target, k, { enumerable: true, get: function () { return src[k]; } });
  });
}
"""
RUNTIME_TAIL = """
function run() { ENTRIES.forEach(function (id) { __req(id); }); }
if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
})();"""

ERROR_OVERLAY = """<script>
(function () {
  function show(msg) {
    var d = document.getElementById("__geta_err");
    if (!d) {
      if (!document.body) return;
      d = document.createElement("pre"); d.id = "__geta_err";
      d.style.cssText = "position:fixed;left:8px;right:8px;bottom:8px;max-height:40vh;overflow:auto;z-index:2147483647;background:#1a0000;color:#ff8a8a;font:12px/1.5 Consolas,monospace;padding:10px;border:1px solid #ff3b3b;border-radius:8px;white-space:pre-wrap;direction:ltr";
      document.body.appendChild(d);
    }
    d.textContent += msg + "\\n";
  }
  window.addEventListener("error", function (e) { show("Error: " + (e.message || e.error) + (e.lineno ? "  (line " + e.lineno + ")" : "")); });
  window.addEventListener("unhandledrejection", function (e) { show("Unhandled: " + (e.reason && (e.reason.stack || e.reason.message) || e.reason)); });
})();
</script>"""

FETCH_SHIM = """<script>
(function () {
  var FILES = __FILES__;
  var base = location.href.replace(/[?#].*$/, "").replace(/[^/]*$/, "");
  var orig = window.fetch ? window.fetch.bind(window) : null;
  window.fetch = function (input, init) {
    try {
      var url = new URL(typeof input === "string" ? input : input.url, location.href).href.replace(/[?#].*$/, "");
      if (url.indexOf(base) === 0) {
        var key = decodeURIComponent(url.slice(base.length));
        if (Object.prototype.hasOwnProperty.call(FILES, key)) {
          var type = /\\.json$/i.test(key) ? "application/json" : "text/plain";
          return Promise.resolve(new Response(FILES[key], { status: 200, headers: { "Content-Type": type } }));
        }
      }
    } catch (e) {}
    return orig ? orig(input, init) : Promise.reject(new Error("fetch unavailable"));
  };
})();
</script>"""


class BundleError(Exception):
    pass


def _attr(tag, name):
    m = re.search(r'(?<![\w-])' + name + r'\s*=\s*(["\'])(.*?)\1', tag, re.I | re.S)
    if m:
        return m.group(2).strip()
    m = re.search(r'(?<![\w-])' + name + r'\s*=\s*([^\s>]+)', tag, re.I)
    return m.group(1).strip() if m else None


def _is_local(url):
    return bool(url) and not re.match(r'^(?:[a-z][a-z0-9+.-]*:|//|#)', url, re.I)


def _safe_js(code):
    return re.sub(r"</(script)", r"<\\/\1", code, flags=re.I)


def parse_clause(clause):
    c, default, ns, named = clause.strip(), None, None, []
    m = re.search(r'\{([^}]*)\}', c)
    if m:
        for part in m.group(1).split(","):
            part = part.strip()
            if part:
                bits = re.split(r"\s+as\s+", part)
                named.append((bits[0].strip(), bits[-1].strip()))
        c = c[:m.start()] + c[m.end():]
    m = re.search(r'\*\s*as\s+([\w$]+)', c)
    if m:
        ns = m.group(1)
        c = c[:m.start()] + c[m.end():]
    c = c.replace(",", " ").strip()
    if c:
        default = c.split()[0]
    return default, ns, named


def resolve_spec(from_rel, spec, root):
    if not spec.startswith(("./", "../", "/")):
        return None
    root = Path(root).resolve()
    spec = spec.split("?")[0].split("#")[0]
    target = root / spec.lstrip("/") if spec.startswith("/") else root / Path(from_rel).parent / spec
    for cand in (target, target.with_name(target.name + ".js"), target / "index.js"):
        cand = cand.resolve()
        if cand.is_file() and root in cand.parents:
            return cand.relative_to(root).as_posix()
    return None


def transform_module(rel, src, root):
    deps, getters, counter = [], [], [0]

    def req(spec):
        dep = resolve_spec(rel, spec, root)
        if dep is None:
            raise BundleError(f"ایمپورت '{spec}' در {rel} پیدا نشد یا خارجی است")
        deps.append(dep)
        return "__req(" + json.dumps(dep) + ")"

    def tmp():
        counter[0] += 1
        return f"__m{counter[0]}"

    def rep_import(m):
        d, ns, named = parse_clause(m.group(1))
        t = tmp()
        parts = [f"const {t} = {req(m.group(3))};"]
        if d:
            parts.append(f"const {d} = {t}.default;")
        if ns:
            parts.append(f"const {ns} = {t};")
        if named:
            inner = ", ".join(i if i == l else f"{i}: {l}" for i, l in named)
            parts.append("const { " + inner + " } = " + t + ";")
        return " ".join(parts)

    def rep_star(m):
        r = req(m.group(3))
        if m.group(1):
            return "Object.defineProperty(__exp, " + json.dumps(m.group(1)) + ", { enumerable: true, get: () => " + r + " });"
        return f"__star(__exp, {r});"

    def rep_list(m):
        pairs = []
        for p in m.group(1).split(","):
            if p.strip():
                bits = re.split(r"\s+as\s+", p.strip())
                pairs.append((bits[0].strip(), bits[-1].strip()))
        if m.group(3):
            t = tmp()
            out = [f"const {t} = {req(m.group(3))};"]
            for local, exported in pairs:
                out.append("Object.defineProperty(__exp, " + json.dumps(exported) +
                           ", { enumerable: true, get: () => " + t + "[" + json.dumps(local) + "] });")
            return " ".join(out)
        getters.extend((exported, local) for local, exported in pairs)
        return ""

    def rep_default_decl(m):
        getters.append(("default", m.group(3) or m.group(4)))
        return m.group(1) + m.group(2)

    def rep_decl(m):
        name = m.group(3) or m.group(4) or m.group(5)
        getters.append((name, name))
        return m.group(1) + m.group(2)

    code = IMPORT_FROM_RE.sub(rep_import, src)
    code = IMPORT_SIDE_RE.sub(lambda m: req(m.group(2)) + ";", code)
    code = DYN_IMPORT_RE.sub(lambda m: "Promise.resolve().then(() => " + req(m.group(2)) + ")", code)
    code = EXPORT_STAR_RE.sub(rep_star, code)
    code = EXPORT_LIST_RE.sub(rep_list, code)
    code = EXPORT_DEFAULT_DECL_RE.sub(rep_default_decl, code)
    code = EXPORT_DEFAULT_RE.sub(lambda m: m.group(1) + "__exp.default = ", code)
    code = EXPORT_DECL_RE.sub(rep_decl, code)
    code = code.replace("import.meta.url", "location.href").replace("import.meta", "({ url: location.href })")

    head = ['"use strict";'] + [
        "Object.defineProperty(__exp, " + json.dumps(e) + ", { enumerable: true, get: () => " + l + " });"
        for e, l in getters]
    wrapped = "__def(" + json.dumps(rel) + ", function (__req, __exp) {\n" + "\n".join(head) + "\n" + code + "\n});"
    return wrapped, deps


def _data_uri(path, limit=3_000_000):
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _inline_css(path, root, seen):
    if path in seen or not path.is_file():
        return ""
    seen.add(path)
    css = path.read_text(encoding="utf-8", errors="replace")

    def rep_import(m):
        if not _is_local(m.group(2)):
            return m.group(0)
        return _inline_css((path.parent / m.group(2)).resolve(), root, seen)

    def rep_url(m):
        u = m.group(2).strip()
        if not _is_local(u):
            return m.group(0)
        p = (path.parent / u.split("?")[0].split("#")[0]).resolve()
        uri = _data_uri(p)
        if uri:
            return f'url("{uri}")'
        try:
            return f'url("{p.relative_to(root).as_posix()}")'
        except ValueError:
            return m.group(0)

    css = CSS_IMPORT_RE.sub(rep_import, css)
    return CSS_URL_RE.sub(rep_url, css).replace("</style", "<\\/style")


def bundle_project(root, name="game"):
    """خروجی: (مسیر HTML تکی، لیست هشدارها). فایل در ریشه‌ی پروژه ساخته می‌شود."""
    root = Path(root).resolve()
    html_path = root / "index.html"
    if not html_path.is_file():
        cands = [p for p in root.glob("*.html") if not p.name.endswith("-play.html")]
        if not cands:
            raise BundleError("index.html در پوشه‌ی پروژه پیدا نشد")
        html_path = cands[0]
    html = html_path.read_text(encoding="utf-8", errors="replace")
    html_rel = html_path.relative_to(root).as_posix()
    warnings, modules, entries, deferred = [], {}, [], []
    state = {"placed": False, "inline": 0}

    def add_module(rel, src):
        if rel in modules:
            return
        modules[rel] = ""
        code, deps = transform_module(rel, src, root)
        modules[rel] = code
        for d in deps:
            if d not in modules:
                add_module(d, (root / d).read_text(encoding="utf-8", errors="replace"))

    def local_spec(src):
        return src if src.startswith(("./", "../", "/")) else "./" + src

    def rep_script(m):
        attrs, body = m.group(1), m.group(2)
        src, typ = _attr(attrs, "src"), (_attr(attrs, "type") or "").lower()
        if typ == "module":
            if src:
                if not _is_local(src):
                    warnings.append(f"ماژول خارجی {src} به صورت آنلاین حفظ شد")
                    return m.group(0)
                rel = resolve_spec(html_rel, local_spec(src), root)
                if not rel:
                    raise BundleError(f"فایل {src} پیدا نشد")
                add_module(rel, (root / rel).read_text(encoding="utf-8", errors="replace"))
            else:
                local_imports = [
                    spec for _, _, spec in IMPORT_FROM_RE.findall(body)
                    if spec.startswith(("./", "../", "/"))
                ] + [
                    spec for _, spec in IMPORT_SIDE_RE.findall(body)
                    if spec.startswith(("./", "../", "/"))
                ]
                if not local_imports:
                    return m.group(0)
                state["inline"] += 1
                rel = f"{html_rel}#inline{state['inline']}"
                add_module(rel, body)
            entries.append(rel)
            if state["placed"]:
                return ""
            state["placed"] = True
            return "<!--GETA_MODULES-->"
        if src and _is_local(src):
            rel = resolve_spec(html_rel, local_spec(src), root)
            if not rel:
                warnings.append(f"اسکریپت {src} پیدا نشد")
                return ""
            tag = "<script>\n" + _safe_js((root / rel).read_text(encoding="utf-8", errors="replace")) + "\n</script>"
            if re.search(r'(?<![\w-])(defer|async)(?![\w-])', attrs, re.I):
                deferred.append(tag)
                return ""
            return tag
        if src:
            warnings.append(f"اسکریپت خارجی {src} نیاز به اینترنت دارد")
        return m.group(0)

    def rep_link(m):
        tag = m.group(0)
        rel_attr, href = (_attr(tag, "rel") or "").lower(), _attr(tag, "href")
        if "stylesheet" not in rel_attr:
            return tag
        if not _is_local(href):
            warnings.append(f"استایل خارجی {href} نیاز به اینترنت دارد")
            return tag
        p = (html_path.parent / href.split("?")[0]).resolve()
        if not p.is_file():
            warnings.append(f"فایل استایل {href} پیدا نشد")
            return ""
        return "<style>\n" + _inline_css(p, root, set()) + "\n</style>"

    html = SCRIPT_RE.sub(rep_script, html)
    html = LINK_RE.sub(rep_link, html)

    if modules:
        runtime = (RUNTIME_HEAD + "\n".join(_safe_js(c) for c in modules.values()) +
                   "\nvar ENTRIES = " + json.dumps(entries) + ";" + RUNTIME_TAIL)
        html = html.replace("<!--GETA_MODULES-->", "<script>\n" + runtime + "\n</script>", 1)
    if deferred:
        block = "\n".join(deferred)
        idx = html.lower().rfind("</body>")
        html = html[:idx] + block + "\n" + html[idx:] if idx >= 0 else html + block

    inject = ERROR_OVERLAY
    if "fetch(" in html:
        files, total = {}, 0
        for p in root.rglob("*"):
            if (p.is_file() and p.suffix.lower() in DATA_EXT
                    and not ({".geta", "node_modules", ".git"} & set(p.relative_to(root).parts))):
                size = p.stat().st_size
                if size <= 1_000_000 and total + size <= 4_000_000:
                    files[p.relative_to(root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
                    total += size
        if files:
            inject += "\n" + FETCH_SHIM.replace("__FILES__", _safe_js(json.dumps(files, ensure_ascii=False)), 1)
    if re.search(r'<head\b[^>]*>', html, re.I):
        html = re.sub(r'<head\b[^>]*>', lambda m: m.group(0) + "\n" + inject, html, count=1, flags=re.I)
    else:
        html = inject + "\n" + html

    safe = re.sub(r'[^\w\-]+', "_", name or root.name).strip("_") or "game"
    out = root / f"{safe}-play.html"
    out.write_text(html, encoding="utf-8")
    return out, warnings
