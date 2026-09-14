#!/usr/bin/env python3
"""Mondi.tech static publish build.

Regenerate the deployable site from the root *.dc.html sources (run from the repo root):

    python scripts/build-publish.py

Requires only the Python 3.8+ standard library. The script:

  1. hashes the source *.dc.html pages (they are opened read-only and re-verified at the end),
  2. renders each DC page to plain static HTML: <helmet> moves into <head>, the <x-dc> wrapper,
     support.js (React/ReactDOM/Babel from unpkg) and the DC logic script are removed, and the
     homepage's DC template constructs (sc-for, sc-if, {{ bindings }}) are expanded using data
     read from the page's own logic class,
  3. rewrites internal links and production URLs to the clean filenames in PAGES,
  4. copies every referenced local asset byte-for-byte (styles, fonts, images, scripts),
  5. writes robots.txt and sitemap.xml,
  6. validates the result, then replaces Publish/ with it. If validation fails, the existing
     Publish/ is left untouched and the script exits non-zero.

Interactions are provided by scripts/publish/site.js (copied to Publish/scripts/site.js).
Never hand-edit files in Publish/; edit the .dc.html sources (or this script) and rebuild.
"""

import hashlib
import html
import json
import posixpath
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / "Publish"
STAGING = ROOT / ".publish-staging"
SITE_JS = ROOT / "scripts" / "publish" / "site.js"
SITE = "https://mondi.tech"

# (source page, output file, public path)
ENGLISH_PAGES = [
    ("Mondi.tech Homepage.dc.html", "index.html", "/"),
    ("About.dc.html", "about.html", "/about.html"),
    ("Services.dc.html", "services.html", "/services.html"),
    ("We Write It For You.dc.html", "we-write-it-for-you.html", "/we-write-it-for-you.html"),
    ("We Help You Do It Better.dc.html", "we-help-you-do-it-better.html", "/we-help-you-do-it-better.html"),
    ("Success Stories.dc.html", "success-stories.html", "/success-stories.html"),
    ("Story Docs-as-code Migration.dc.html", "story-docs-as-code-migration.html", "/story-docs-as-code-migration.html"),
    ("Story Training Modules.dc.html", "story-training-modules.html", "/story-training-modules.html"),
    ("Contact.dc.html", "contact.html", "/contact.html"),
]
PAGES = []
for source, output, en_path in ENGLISH_PAGES:
    hu_path = "/hu/" if en_path == "/" else "/hu" + en_path
    PAGES.append(("en", source, output, en_path, en_path, hu_path))
    PAGES.append(("hu", "hu/" + source, "hu/" + output, hu_path, en_path, hu_path))
PUBLIC_PATH = {(lang, Path(src).name): path for lang, src, _, path, _, _ in PAGES}

SUPPORT_TAG = '<script src="./support.js"></script>\n'
SITE_JS_TAG = '<script src="./scripts/site.js" defer></script>\n'
# Impact cards start at opacity 0 in mondi.css and are revealed by site.js; keep them readable without JS.
NOSCRIPT_IMPACT = '<noscript><style>.impact-card{opacity:1;transform:none;transition:none}</style></noscript>\n'

# Self-hosted fonts referenced from styles/mondi.css; all must ship in Publish/assets/fonts/.
REQUIRED_FONTS = [
    "assets/fonts/Gotham-Medium.otf",
    "assets/fonts/Inter-Regular.woff2",
    "assets/fonts/Inter-Medium.woff2",
    "assets/fonts/Inter-SemiBold.woff2",
    "assets/fonts/Inter-Bold.woff2",
    "assets/fonts/PoltawskiNowy-Italic.woff2",
]
GOOGLE_FONTS_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")
CORRECT_PHONE_DISPLAY = "+36 20 482 6070"
CORRECT_PHONE_HREF = "tel:+36204826070"
OBSOLETE_PHONE_VALUES = ("+36 20 482 " + "4105", "+3620482" + "4105")

# Homepage renderVals() keys this build knows how to render statically.
KNOWN_LOGIC_KEYS = {
    "clients", "formOpen", "formToggleLabel", "toggleForm", "showCookie", "showPrefs", "prefsLabel",
    "acceptLabel", "categories", "acceptAll", "denyAll", "togglePrefs", "onDragStart", "onDragMove",
    "onDragEnd", "onHoverMove", "onHoverLeave", "rowRef", "impactRef",
}
# Source snippets whose behaviour is hand-ported in scripts/publish/site.js.
PORTED_LOGIC_SNIPPETS = [
    "entries.forEach(e => impact.classList.toggle('is-revealed', e.isIntersecting));",
    "r.scrollLeft += this.autoDir * 2.2;",
    "el.style.scrollSnapType = dir ? 'none' : 'x proximity';",
    "if (e.pointerType !== 'mouse' || !el()) return;",
    "if (Math.abs(dx) > 3) this.drag.moved = true; el().scrollLeft = this.drag.left - dx;",
    "const zone = Math.min(200, b.width * 0.22);",
    "const dir = e.clientX < b.left + zone ? -1 : e.clientX > b.right - zone ? 1 : 0;",
    "acceptAll: () => this.save(s.prefs ? s.cats : { functional: true, preferences: true, statistics: true, marketing: true }),",
    "denyAll: () => this.save({ functional: true, preferences: false, statistics: false, marketing: false }),",
]
CAROUSEL_HANDLERS = {
    ("onPointerDown", "onDragStart"), ("onPointerMove", "onDragMove"), ("onPointerUp", "onDragEnd"),
    ("onPointerLeave", "onDragEnd"), ("onPointerCancel", "onDragEnd"), ("onMouseMove", "onHoverMove"),
    ("onMouseLeave", "onHoverLeave"),
}


class BuildError(Exception):
    pass


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exists_exact(base, rel):
    """True if base/rel exists as a file with exactly this letter case (production servers are case-sensitive)."""
    cur = base
    for part in Path(rel).parts:
        try:
            names = {p.name for p in cur.iterdir()}
        except (FileNotFoundError, NotADirectoryError):
            return False
        if part not in names:
            return False
        cur = cur / part
    return cur.is_file()


def attr(value):
    return html.escape(value, quote=True)


def dedent2(text):
    return "\n".join(line[2:] if line.startswith("  ") else line for line in text.split("\n"))


# --------------------------------------------------------------------------- DC logic parsing

def parse_logic(page, js, data_props):
    """Extract the data the homepage template renders from its DC logic class."""
    def need(pattern, what, flags=0):
        m = re.search(pattern, js, flags)
        if not m:
            raise BuildError(f"{page}: could not find {what} in the DC logic class")
        return m

    body = need(r"renderVals\(\)\s*\{.*?return \{\n([ \t]+)(.*?)\n[ \t]*\};\n", "renderVals() return object", re.S)
    keys = set(re.findall(r"^" + body.group(1) + r"(\w+):", body.group(1) + body.group(2), re.M))
    unknown = keys - KNOWN_LOGIC_KEYS
    if unknown:
        raise BuildError(f"{page}: DC logic has render values this build cannot render statically: {sorted(unknown)}")
    missing = [s for s in PORTED_LOGIC_SNIPPETS if s not in js]
    if missing:
        raise BuildError(f"{page}: DC interaction logic changed; review scripts/publish/site.js. Missing: {missing}")

    state = need(r"state = \{(.*?)\};\n", "initial state").group(1)
    def state_bool(name):
        return need_in(state, rf"\b{name}:\s*(true|false)", f"state.{name}") == "true"
    def need_in(text, pattern, what):
        m = re.search(pattern, text)
        if not m:
            raise BuildError(f"{page}: could not find {what} in the DC logic class")
        return m.group(1)

    if need_in(state, r"\bcookie:\s*'(\w+)'", "state.cookie") != "pending":
        raise BuildError(f"{page}: unsupported initial cookie state")
    if state_bool("prefs"):
        raise BuildError(f"{page}: unsupported initial state prefs=true")
    cats_init = dict(re.findall(r"(\w+):\s*(true|false)", need_in(state, r"cats:\s*\{([^}]*)\}", "state.cats")))

    clients_block = need(r"clients: \[(.*?)\n\s*\],", "clients list", re.S).group(1)
    clients = [{"name": html.unescape(n), "src": s}
               for n, s in re.findall(r"\{\s*name:\s*'([^']*)',\s*src:\s*'([^']*)'\s*\}", clients_block)]
    if not clients or len(clients) != clients_block.count("{"):
        raise BuildError(f"{page}: could not parse every entry of the clients list")

    labels = re.findall(r"(\w+):\s*'([^']*)'", need(r"const labels = \{([^}]*)\}", "cookie category labels").group(1))
    locked = need(r"locked:\s*k\s*===\s*'(\w+)'", "locked cookie category").group(1)
    if set(k for k, _ in labels) != set(cats_init):
        raise BuildError(f"{page}: cookie labels and state.cats keys differ")
    categories = [{"label": label, "on": cats_init[k] == "true", "locked": k == locked,
                   "toggle": ("attr", "data-cookie-category", k)} for k, label in labels]

    ternaries = {}
    for key in ("formToggleLabel", "prefsLabel", "acceptLabel"):
        m = need(rf"\b{key}:\s*s\.(\w+)\s*\?\s*'([^']*)'\s*:\s*'([^']*)'", f"{key} labels")
        ternaries[key] = {"state": m.group(1), "on": m.group(2), "off": m.group(3)}

    key_get = need(r"localStorage\.getItem\('([^']+)'\)", "consent storage key").group(1)
    key_set = need(r"localStorage\.setItem\('([^']+)'", "consent storage key").group(1)
    if key_get != key_set:
        raise BuildError(f"{page}: consent storage keys differ ({key_get} / {key_set})")

    cookie_prop = (data_props or {}).get("cookieBanner", {})
    return {
        "clients": clients,
        "categories": categories,
        "labels": ternaries,
        "form_open_initial": state_bool("formOpen"),
        "consent_key": key_get,
        "cookie_banner": cookie_prop.get("default", True) is not False,
        "threshold": need(r"threshold:\s*([\d.]+)", "IntersectionObserver threshold").group(1),
    }


# --------------------------------------------------------------------------- template rendering

def expand_for(page, template, logic):
    pattern = re.compile(r"(?P<indent>[ \t]*)<sc-for list=\"\{\{\s*(?P<list>\w+)\s*\}\}\" as=\"(?P<as>\w+)\"[^>]*>\n"
                         r"(?P<body>.*?)\n(?P=indent)</sc-for>", re.S)

    def render(m):
        items = logic.get(m.group("list")) if logic else None
        if not isinstance(items, list):
            raise BuildError(f"{page}: no static data for sc-for list '{m.group('list')}'")
        name = re.escape(m.group("as"))
        body = dedent2(m.group("body"))
        out = []
        for item in items:
            def attr_binding(a):
                value = item[a.group(2)]
                if isinstance(value, bool):
                    return f" {a.group(1)}" if value else ""
                if isinstance(value, tuple):
                    return f' {value[1]}="{attr(value[2])}"'
                return f' {a.group(1)}="{attr(value)}"'
            chunk = re.sub(r"\s([A-Za-z][\w-]*)=\"\{\{\s*" + name + r"\.(\w+)\s*\}\}\"", attr_binding, body)
            chunk = re.sub(r"\{\{\s*" + name + r"\.(\w+)\s*\}\}", lambda t: html.escape(item[t.group(1)], quote=False), chunk)
            out.append(chunk)
        return "\n".join(out)

    return pattern.sub(render, template)


def expand_if(page, template, logic):
    # Innermost sc-if first.
    pattern = re.compile(r"(?P<indent>[ \t]*)<sc-if value=\"\{\{\s*(?P<var>\w+)\s*\}\}\"[^>]*>\n"
                         r"(?P<body>(?:(?!<sc-if\b).)*?)\n(?P=indent)</sc-if>", re.S)

    def render(m):
        var, body, indent = m.group("var"), m.group("body"), m.group("indent")
        if var == "formOpen":  # contact fields: present in HTML, collapsed by site.js
            return dedent2(body)
        if var == "showPrefs":  # cookie preferences: mounted by site.js on demand
            return re.sub(r"<(\w+)", r"<\1 data-cookie-prefs", dedent2(body), count=1)
        if var == "showCookie":  # cookie banner: inert template, shown by site.js
            if not logic["cookie_banner"]:
                return ""
            return (f'{indent}<template id="cookie-consent" data-consent-key="{attr(logic["consent_key"])}">\n'
                    f"{body}\n{indent}</template>")
        raise BuildError(f"{page}: no static rendering defined for <sc-if value=\"{{{{ {var} }}}}\">")

    while True:
        template, n = pattern.subn(render, template)
        if not n:
            return template


def render_bindings(page, template, logic):
    static_state = {"formOpen": True, "prefs": False}  # what the static HTML shows before site.js runs

    def label(m):
        spec = logic["labels"].get(m.group(4)) if logic else None
        if not spec:
            raise BuildError(f"{page}: no static text for {{{{ {m.group(4)} }}}}")
        text = spec["on"] if static_state[spec["state"]] else spec["off"]
        return (f'<{m.group(2)}{m.group(3)} data-label-on="{attr(spec["on"])}" data-label-off="{attr(spec["off"])}">'
                f"{html.escape(text, quote=False)}</{m.group(2)}>")

    template = re.sub(r"(<(\w+)([^<>]*)>)\{\{\s*(\w+)\s*\}\}(</\2>)", label, template)

    for tag in re.findall(r"<[a-z][^<>]*\{\{[^<>]*>", template):
        pairs = set(re.findall(r"\s([A-Za-z][\w-]*)=\"\{\{\s*(\w+)\s*\}\}\"", tag))
        if pairs & CAROUSEL_HANDLERS and not (CAROUSEL_HANDLERS <= pairs and ("ref", "rowRef") in pairs):
            raise BuildError(f"{page}: carousel handlers are not all on the rowRef element: {tag}")

    def binding(m):
        name, expr = m.group(1), m.group(2)
        if (name, expr) in CAROUSEL_HANDLERS:
            return ""
        replacements = {
            ("ref", "impactRef"): f' data-reveal data-reveal-threshold="{logic["threshold"]}"' if logic else None,
            ("ref", "rowRef"): " data-carousel",
            ("onClick", "toggleForm"): ' data-form-toggle="{}"'.format("open" if logic and logic["form_open_initial"] else "closed"),
            ("aria-expanded", "formOpen"): ' aria-expanded="true"',
            ("onClick", "acceptAll"): ' data-cookie-action="accept"',
            ("onClick", "denyAll"): ' data-cookie-action="deny"',
            ("onClick", "togglePrefs"): ' data-cookie-action="prefs"',
        }
        out = replacements.get((name, expr)) if logic else None
        if out is None:
            raise BuildError(f"{page}: no static rendering defined for {name}=\"{{{{ {expr} }}}}\"")
        return out

    return re.sub(r"\s([A-Za-z][\w-]*)=\"\{\{\s*([^}]+?)\s*\}\}\"", binding, template)


def render_page(src_name):
    raw = (ROOT / src_name).read_bytes().decode("utf-8").replace("\r\n", "\n")

    support_tag = ('<script src="../support.js"></script>\n'
                   if Path(src_name).parent != Path(".") else SUPPORT_TAG)
    if raw.count(support_tag) != 1:
        raise BuildError(f"{src_name}: expected exactly one support.js tag in <head>")
    body = re.fullmatch(r"(?s)(.*<body>\n)<x-dc>\n(.*)</x-dc>\n(<script type=\"text/x-dc\" data-dc-script([^>]*)>\n(.*?)</script>\n)(</body>\n</html>\n?)", raw)
    if not body:
        raise BuildError(f"{src_name}: unexpected document structure around <x-dc> / DC script")
    before, template, _, script_attrs, js, after = body.groups()

    helmet = re.match(r"<helmet>\n(.*?)</helmet>\n\n?", template, re.S)
    if not helmet:
        raise BuildError(f"{src_name}: expected <helmet> at the start of <x-dc>")
    head_links = helmet.group(1)
    template = template[helmet.end():]

    props_raw = re.search(r'data-props="([^"]*)"', script_attrs)
    data_props = json.loads(html.unescape(props_raw.group(1))) if props_raw else None
    trivial = re.fullmatch(r"\s*class Component extends DCLogic \{\s*\}\s*", js) is not None
    logic = None if trivial else parse_logic(src_name, js, data_props)
    if trivial and "{{" in template:
        raise BuildError(f"{src_name}: template bindings without a logic class")

    template = expand_for(src_name, template, logic)
    template = expand_if(src_name, template, logic)
    template = render_bindings(src_name, template, logic)
    for token in ("{{", "}}", "<sc-", "</sc-", "<helmet", "<x-dc", "</x-dc"):
        if token in template:
            raise BuildError(f"{src_name}: unrendered DC construct '{token}' remains")

    needs_js = re.search(r"\sdata-(reveal|carousel|form-toggle)\b|id=\"cookie-consent\"", template) is not None
    site_js_tag = ('<script src="../scripts/site.js" defer></script>\n'
                   if Path(src_name).parent != Path(".") else SITE_JS_TAG)
    head_extra = head_links + (site_js_tag if needs_js else "") + (NOSCRIPT_IMPACT if "data-reveal" in template else "")
    before = before.replace(support_tag, head_extra)
    return before + template + after, template


def rewrite_urls(src_name, lang, text):
    lookup = {name: path for (entry_lang, name), path in PUBLIC_PATH.items() if entry_lang == lang}

    def repl(m):
        prefix, ref, frag = m.group(1) or "", m.group(2), m.group(3) or ""
        name = unquote(ref)
        if name not in lookup:
            raise BuildError(f"{src_name}: link to unknown page '{ref}'")
        path = lookup[name]
        return (SITE + path if prefix.startswith("https://") else path) + frag

    return re.sub(r"(https://mondi\.tech/|\./)?([A-Za-z0-9%. _-]+\.dc\.html)(#[\w-]*)?", repl, text)


# --------------------------------------------------------------------------- HTML inspection

class PageScan(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.counts, self.meta, self.refs, self.ids = [], {}, {}, [], set()
        self.jsonld, self.text, self.visible_text, self.h1 = [], [], [], 0
        self.navs, self.in_jsonld = set(), False
        self.lang = None
        self.alternates = {}

    def bump(self, key, value=None):
        self.counts[key] = self.counts.get(key, 0) + 1
        if value is not None:
            self.meta.setdefault(key, value)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag not in self.VOID:
            self.stack.append(tag)
        if tag == "html":
            self.lang = a.get("lang")
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "title":
            self.bump("title")
        elif tag == "h1":
            self.h1 += 1
        elif tag == "nav":
            self.navs.add(a.get("aria-label"))
        elif tag == "meta":
            key = a.get("name") or a.get("property")
            if key in ("description",) or (key or "").startswith(("og:", "twitter:")):
                self.bump(key, a.get("content", ""))
        elif tag == "link" and a.get("rel") == "canonical":
            self.bump("canonical", a.get("href", ""))
        elif tag == "link" and a.get("rel") == "alternate" and a.get("hreflang"):
            code = a["hreflang"]
            self.bump("hreflang:" + code, a.get("href", ""))
            self.alternates[code] = a.get("href", "")
        elif tag == "script" and a.get("type") == "application/ld+json":
            self.in_jsonld = True
            self.jsonld.append("")
        for name in ("href", "src", "action"):
            if a.get(name):
                self.refs.append((tag, name, a[name]))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_jsonld = False
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        if self.in_jsonld:
            self.jsonld[-1] += data
            return
        if "title" in self.stack:
            self.meta["title"] = data
        if self.stack and self.stack[-1] in ("script", "style"):
            return
        chunk = " ".join(data.split())
        if chunk:
            self.text.append(chunk)
            if not {"template", "noscript", "head"} & set(self.stack):
                self.visible_text.append(chunk)


def scan(text):
    p = PageScan()
    p.feed(text)
    p.close()
    return p


def local_target(ref, page="index.html"):
    """Map a page-relative or root-relative reference to a path inside Publish/, or None if external."""
    parts = urlsplit(ref)
    if parts.scheme or ref.startswith(("//", "#")):
        return None
    path = unquote(parts.path)
    if path in ("", "/", "./"):
        return "index.html"
    if path.startswith("/"):
        target = posixpath.normpath(path.lstrip("/"))
    else:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(page), path))
    return posixpath.join(target, "index.html") if path.endswith("/") else target


# --------------------------------------------------------------------------- build + validate

def build(out):
    pages = {}
    published_paths = {path.lstrip("/") for _, _, _, path, _, _ in PAGES}
    for lang, src, dst, _, _, _ in PAGES:
        doc, _ = render_page(src)
        pages[dst] = rewrite_urls(src, lang, doc)
        (out / dst).parent.mkdir(parents=True, exist_ok=True)
        (out / dst).write_text(pages[dst], encoding="utf-8", newline="\n")

    # Collect and copy local assets referenced by the pages (and by their stylesheets).
    assets = set()
    for dst, doc in pages.items():
        p = scan(doc)
        for tag, name, ref in p.refs:
            target = local_target(ref, dst)
            if target and not target.endswith(".html"):
                assets.add(target)
        for absolute in re.findall(r"https://mondi\.tech/([^\"'\s<>]+)", doc):
            if absolute not in published_paths and not absolute.endswith((".html", ".xml")):
                assets.add(unquote(absolute))
    assets.discard("scripts/site.js")
    queue = sorted(assets)
    copied = set()
    while queue:
        rel = queue.pop()
        if rel in copied:
            continue
        if not exists_exact(ROOT, rel):
            raise BuildError(f"referenced asset not found in the repository (case-sensitive): {rel}")
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, out / rel)
        copied.add(rel)
        if rel.endswith(".css"):
            css = (ROOT / rel).read_text(encoding="utf-8")
            for url in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css):
                if not urlsplit(url).scheme and not url.startswith(("#", "data:")):
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), unquote(url)))
                    if target.startswith(".."):
                        raise BuildError(f"{rel}: url({url}) points outside the site root")
                    queue.append(target)

    missing_fonts = [f for f in REQUIRED_FONTS if f not in copied]
    if missing_fonts:
        raise BuildError(f"required self-hosted fonts are not referenced by the stylesheets: {missing_fonts}")

    (out / "scripts").mkdir(exist_ok=True)
    shutil.copyfile(SITE_JS, out / "scripts" / "site.js")
    (out / "robots.txt").write_text(f"User-agent: *\nAllow: /\n\nSitemap: {SITE}/sitemap.xml\n", encoding="utf-8", newline="\n")
    urls = "".join(f"  <url>\n    <loc>{SITE}{path}</loc>\n  </url>\n"
                   for _, _, _, path, _, _ in PAGES)
    (out / "sitemap.xml").write_text('<?xml version="1.0" encoding="UTF-8"?>\n'
                                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                                     f"{urls}</urlset>\n", encoding="utf-8", newline="\n")
    return sorted(copied)


def validate(out):
    errors = []
    err = errors.append
    canonical = {dst: SITE + path for _, _, dst, path, _, _ in PAGES}
    page_urls = set(canonical.values())

    html_files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.html"))
    if html_files != sorted(canonical):
        err(f"generated HTML set mismatch: {html_files}")
    for required in ("index.html", "robots.txt", "sitemap.xml", "styles/mondi.css", "scripts/site.js",
                     "assets/og-share-card.png", "assets/favicon.svg", *REQUIRED_FONTS):
        if not exists_exact(out, required):
            err(f"missing required file: {required}")
    for path in sorted(out.rglob("*")):
        if path.suffix in (".html", ".css", ".js", ".xml", ".txt"):
            text = path.read_text(encoding="utf-8")
            for host in GOOGLE_FONTS_HOSTS:
                if host in text:
                    err(f"{path.relative_to(out).as_posix()}: Google Fonts reference ({host}) remains")

    scans = {}
    for lang, src, dst, _, en_path, hu_path in PAGES:
        doc = (out / dst).read_text(encoding="utf-8")
        p = scans[dst] = scan(doc)
        if re.search(r"info@mondi\.tech|mailto:", doc, re.I):
            err(f"{dst}: public mailbox or mailto link remains")

        for pattern in (r"<x-dc", r"</x-dc", r"<helmet", r"<sc-", r"\{\{", r"\}\}", r"support\.js", r"data-dc-",
                        r"text/x-dc", r"DCLogic", r"unpkg", r"\breact(dom)?\b", r"\bbabel\b", r"\.dc\.html"):
            if re.search(pattern, doc, re.I):
                err(f"{dst}: forbidden runtime/source reference matches /{pattern}/")

        for key in ("title", "description", "canonical", "og:title", "og:description", "og:url", "og:image",
                    "og:type", "twitter:card", "twitter:title", "twitter:description", "twitter:image"):
            if p.counts.get(key, 0) != 1:
                err(f"{dst}: expected exactly one {key}, found {p.counts.get(key, 0)}")
        if p.meta.get("canonical") != canonical[dst]:
            err(f"{dst}: canonical {p.meta.get('canonical')} != {canonical[dst]}")
        if p.meta.get("og:url") != canonical[dst]:
            err(f"{dst}: og:url {p.meta.get('og:url')} != {canonical[dst]}")
        for key in ("og:image", "twitter:image"):
            if p.meta.get(key) != f"{SITE}/assets/og-share-card.png":
                err(f"{dst}: unexpected {key} {p.meta.get(key)}")

        if p.lang != lang:
            err(f"{dst}: html lang {p.lang!r} != {lang!r}")
        expected_alternates = {
            "en": SITE + en_path,
            "hu": SITE + hu_path,
            "x-default": SITE + en_path,
        }
        for code, expected in expected_alternates.items():
            if p.counts.get("hreflang:" + code, 0) != 1 or p.alternates.get(code) != expected:
                err(f"{dst}: hreflang {code} {p.alternates.get(code)!r} != {expected!r}")
        switchers = re.findall(
            r'<a href="([^"]+)" class="language-switcher[^"]*" aria-label="([^"]+)">.*?'
            r'<span>(HU|EN)</span></a>', doc, re.S)
        switch_target = hu_path if lang == "en" else en_path
        switch_label = "Switch language to Hungarian" if lang == "en" else "Váltás angol nyelvre"
        switch_text = "HU" if lang == "en" else "EN"
        if len(switchers) != 2 or any(item != (switch_target, switch_label, switch_text) for item in switchers):
            err(f"{dst}: language switchers do not match target {switch_target} / {switch_text}")

        if p.h1 != 1:
            err(f"{dst}: expected one <h1>, found {p.h1}")
        required_navs = {"Primary", "Footer"} if lang == "en" else {"Elsődleges", "Lábléc"}
        if not required_navs <= p.navs:
            err(f"{dst}: primary/footer navigation missing")
        visible = " ".join(p.visible_text)
        if len(visible) < 500 or "Mondi.tech Kft." not in visible:
            err(f"{dst}: static visible text looks incomplete ({len(visible)} chars)")
        if CORRECT_PHONE_DISPLAY not in visible or not any(ref == CORRECT_PHONE_HREF for _, _, ref in p.refs):
            err(f"{dst}: current phone number or telephone link is missing")
        for obsolete in OBSOLETE_PHONE_VALUES:
            if obsolete in doc:
                err(f"{dst}: obsolete phone value remains: {obsolete}")

        # Every text node of the source template must be present in the output.
        _, source_template = render_page_text_source(src)
        out_text = set(p.text)
        for chunk in scan(source_template).text:
            if "{{" not in chunk and chunk not in out_text:
                err(f"{dst}: source text missing from output: {chunk[:80]!r}")

        for i, block in enumerate(p.jsonld):
            try:
                data = json.loads(block)
            except ValueError as e:
                err(f"{dst}: JSON-LD block {i + 1} is invalid: {e}")
                continue
            for value in iter_strings(data):
                if value.startswith(SITE) and value not in page_urls and not exists_exact(out, unquote(value[len(SITE) + 1:])):
                    err(f"{dst}: JSON-LD URL does not match a published page or asset: {value}")
            if data.get("@type") in ("Service",) and data.get("url") != canonical[dst]:
                err(f"{dst}: Service url != canonical")
            if data.get("@type") == "Article" and data.get("mainEntityOfPage", {}).get("@id") != canonical[dst]:
                err(f"{dst}: Article mainEntityOfPage != canonical")
            if data.get("@type") == "BreadcrumbList" and data["itemListElement"][-1]["item"] != canonical[dst]:
                err(f"{dst}: last breadcrumb != canonical")

        for absolute in re.findall(r"https://mondi\.tech[^\"'\s<>]*", doc):
            if absolute not in page_urls and not exists_exact(out, unquote(absolute[len(SITE) + 1:])):
                err(f"{dst}: absolute URL does not resolve in Publish: {absolute}")

    for dst, p in scans.items():
        for tag, name, ref in p.refs:
            if ref.startswith(("mailto:", "tel:", SITE + "/", "https://www.linkedin.com/")):
                continue  # absolute mondi.tech URLs are checked above
            if ref.startswith("#"):
                if ref[1:] not in p.ids:
                    err(f"{dst}: in-page anchor {ref} has no target")
                continue
            target = local_target(ref, dst)
            if target is None:
                err(f"{dst}: unexpected external reference {ref}")
                continue
            if not exists_exact(out, target):
                err(f"{dst}: {tag}[{name}] {ref} does not resolve")
            frag = urlsplit(ref).fragment
            if frag and target in scans and frag not in scans[target].ids:
                err(f"{dst}: {ref} fragment has no target")
            if tag == "a" and target.endswith(".html") and not ref.startswith("/"):
                err(f"{dst}: page link is not root-relative: {ref}")

    for css in out.rglob("*.css"):
        for url in re.findall(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", css.read_text(encoding="utf-8")):
            if not urlsplit(url).scheme and not exists_exact(out, (css.parent / unquote(url)).resolve().relative_to(out.resolve()).as_posix()):
                err(f"{css.name}: url({url}) does not resolve")

    sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
    locs = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    if len(locs) != 18 or set(locs) != page_urls or "lastmod" in sitemap:
        err(f"sitemap.xml does not list exactly the 18 canonical URLs: {locs}")
    if f"Sitemap: {SITE}/sitemap.xml" not in (out / "robots.txt").read_text(encoding="utf-8"):
        err("robots.txt does not reference the sitemap")
    return errors


def render_page_text_source(src_name):
    raw = (ROOT / src_name).read_bytes().decode("utf-8").replace("\r\n", "\n")
    template = re.search(r"<x-dc>\n(.*)</x-dc>", raw, re.S).group(1)
    return raw, re.sub(r"<helmet>.*?</helmet>", "", template, flags=re.S)


def iter_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_strings(v)


def main():
    sources = sorted(ROOT.glob("*.dc.html")) + sorted((ROOT / "hu").glob("*.dc.html"))
    source_names = [p.relative_to(ROOT).as_posix() for p in sources]
    expected_sources = [src for _, src, _, _, _, _ in PAGES]
    if sorted(source_names) != sorted(expected_sources):
        raise BuildError(f"source pages differ from PAGES: {source_names}")
    before = {p.relative_to(ROOT).as_posix(): sha256(p) for p in sources}
    for source in sources:
        source_text = source.read_text(encoding="utf-8")
        for obsolete in OBSOLETE_PHONE_VALUES:
            if obsolete in source_text:
                raise BuildError(f"{source.relative_to(ROOT).as_posix()}: obsolete phone value remains: {obsolete}")

    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir()
    try:
        copied = build(STAGING)
        errors = validate(STAGING)
        after = {p.relative_to(ROOT).as_posix(): sha256(p) for p in sources}
        if after != before:
            errors.append("SOURCE PAGES CHANGED DURING BUILD")
        if errors:
            print("Publish validation FAILED; Publish/ was not changed:", file=sys.stderr)
            for e in errors:
                print("  - " + e, file=sys.stderr)
            return 1
        if PUBLISH.exists():
            if PUBLISH.resolve().parent != ROOT or PUBLISH.name != "Publish":
                raise BuildError("refusing to replace an unexpected Publish path")
            shutil.rmtree(PUBLISH)
        STAGING.rename(PUBLISH)
    finally:
        if STAGING.exists():
            shutil.rmtree(STAGING)

    print(f"Publish/ regenerated: {len(PAGES)} pages, {len(copied)} copied assets, scripts/site.js, robots.txt, sitemap.xml")
    for _, src, dst, path, _, _ in PAGES:
        print(f"  {src:40} -> Publish/{dst:36} {SITE}{path}")
    print("Validation passed; source .dc.html files verified unchanged (SHA-256).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BuildError as e:
        print(f"build-publish: {e}", file=sys.stderr)
        sys.exit(2)
