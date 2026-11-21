# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Headless-Chrome page checks (G3 browser smoke, the R1 corpus, G5 keyboard/AX basics).

Works on any page bundle that keeps the contract's DOM ids (section 7.3) and texts (section 9):
the fixture now, ``plugin/sidebar`` once lane B lands. Each scenario opens windows through the
real launch flow (R18 hello + launch, bootstrap file, exchange) against a ``targets.Target``.

    python3 scripts/web_smoke/browser_smoke.py --bundle <ui folder> [--evidence <folder>] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import contract  # noqa: E402
import corpus  # noqa: E402
from cdp import Chrome, Page  # noqa: E402
from httpraw import LauncherClient, doc  # noqa: E402

# Installed before any page script: records CSP violations, the fragment seen when each vendored
# library defines its global, window.open calls. Never stores the launch code itself.
PRELOAD = r"""
(() => {
  if (window.__smoke) return;
  const rec = window.__smoke = {csp: [], lib: {}, opens: 0, startHadLaunch: /^#launch=/.test(location.hash)};
  document.addEventListener('securitypolicyviolation', (e) => rec.csp.push({
    directive: e.violatedDirective, blocked: String(e.blockedURI).slice(0, 60), sample: String(e.sample || '').slice(0, 40),
    source: String(e.sourceFile || '').replace(/^https?:\/\/[^/]+/, '')}), true);
  for (const name of ['marked', 'DOMPurify']) {
    let value;
    Object.defineProperty(window, name, {configurable: true, enumerable: true, get() { return value; },
      set(v) { if (!(name in rec.lib)) rec.lib[name] = {hashEmpty: location.hash === '', path: location.pathname}; value = v; }});
  }
  window.open = function () { rec.opens += 1; return null; };
})();
"""

INSPECT_REPLY = r"""
(() => {
  const m = document.getElementById('messages');
  const all = Array.from(m.querySelectorAll('*'));
  const forbidden = %s;
  const tags = all.map((e) => e.tagName.toLowerCase());
  const attrs = [];
  for (const e of all) for (const a of Array.from(e.attributes)) attrs.push([e.tagName.toLowerCase(), a.name, a.value]);
  const allowedUrls = %s;
  const inj = attrs.filter(([t, n, v]) => (/INJ/.test(v) && !(['title', 'aria-label'].includes(n) && allowedUrls.includes(v)))
    || /^on/i.test(n));
  const ids = %s;
  const outside = ids.every((id) => { const el = document.getElementById(id);
    return el && document.querySelectorAll('#' + id).length === 1 && (id === 'messages' ? el === m : !m.contains(el)); });
  const composer = document.getElementById('send-button');
  return {
    forbiddenTags: forbidden.filter((t) => tags.includes(t)),
    idAttrs: all.filter((e) => e.hasAttribute('id')).length,
    nameAttrs: all.filter((e) => e.hasAttribute('name')).length,
    styleAttrs: all.filter((e) => e.hasAttribute('style')).length,
    injClassOrAttr: inj.slice(0, 8),
    hrefAnchors: all.filter((e) => e.tagName === 'A' && e.hasAttribute('href')).length,
    userBubbles: all.filter((e) => e.classList && e.classList.contains('user')).length,
    idsOutsideAndUnique: outside,
    sendIsComposer: !!composer && !m.contains(composer) && composer.closest('footer, .composer') !== null,
    globals: Object.keys(window).filter((k) => k.startsWith('__INJ')),
    bodyBg: getComputedStyle(document.body).backgroundColor,
    text: m.innerText.length,
    linkTextsShown: %s.filter((u) => m.innerText.includes(u)).length,
    controls: all.filter((e) => ['BUTTON'].includes(e.tagName)).map((e) => [e.textContent.trim(),
      e.getAttribute('title') || e.getAttribute('aria-label') || '']).slice(0, 12),
  };
})()
""" % (json.dumps(list(corpus.FORBIDDEN_TAGS)), json.dumps(list(corpus.ALLOWED_LINKS)), json.dumps(list(contract.DOM_IDS)),
       json.dumps(list(corpus.ALLOWED_LINKS)))


class Scenario:
    def __init__(self, sid: str, gate: str, title: str, fn: Callable[["BCtx"], None], needs: tuple[str, ...] = ()) -> None:
        self.id, self.gate, self.title, self.fn, self.needs = sid, gate, title, fn, needs


SCENARIOS: list[Scenario] = []


def scenario(sid: str, gate: str, title: str, needs: tuple[str, ...] = ()) -> Callable:
    def deco(fn: Callable[["BCtx"], None]) -> Callable[["BCtx"], None]:
        SCENARIOS.append(Scenario(sid, gate, title, fn, needs))
        return fn
    return deco


class BCtx:
    def __init__(self, target: Any, chrome: Chrome, evidence: Optional[Path], tag: str) -> None:
        self.t, self.chrome, self.evidence, self.tag = target, chrome, evidence, tag
        self.checks: list[dict[str, Any]] = []
        self.pages: list[Page] = []
        self.boot_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"web-smoke-boot-{secrets.token_hex(4)}"
        self.boot_dir.mkdir(mode=0o700)
        self.users_sent: dict[str, int] = {}

    def check(self, label: str, ok: Any, expected: Any = None, observed: Any = None) -> bool:
        self.checks.append({"label": label, "ok": bool(ok), "expected": expected, "observed": observed})
        return bool(ok)

    def launch(self, n: int, mode: str = "app") -> tuple[str, str, LauncherClient]:
        client = LauncherClient(self.t.port, self.t.token)
        client.hello()
        resp = client.launch(doc(n, title=f"doc-{n}.ods"), mode=mode, bounds=None if mode == "tab" else None)
        if resp.status != 201:
            raise AssertionError(f"launch answered {resp.status} {resp.code}")
        data = resp.json()
        return data["launch_id"], data["url"], client

    def bootstrap(self, url: str) -> Path:
        path = self.boot_dir / f"launch-{secrets.token_hex(16)}.html"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(contract.bootstrap_html(url))
        return path

    def open_window(self, n: int, *, wait: bool = True) -> tuple[Page, str, LauncherClient]:
        launch_id, url, client = self.launch(n)
        page = self.new_page(self.bootstrap(url).as_uri())
        if wait:
            self.wait_connected(page)
        return page, launch_id, client

    def new_page(self, url: str) -> Page:
        page = Page(self.chrome, url, preload=PRELOAD)
        self.pages.append(page)
        return page

    def wait_connected(self, page: Page, timeout: float = 10.0) -> bool:
        return bool(page.wait_for("location.pathname === '/ui/index.html' && document.readyState === 'complete' && "
                                  "!!(document.getElementById('bridge-session-id') || {}).textContent", timeout))

    def send(self, page: Page, text: str) -> None:
        page.eval("document.getElementById('prompt-input').focus()")
        page.type_text(text)
        page.key("Enter")

    def wait_text(self, page: Page, needle: str, timeout: float = 15.0, where: str = "document.body") -> bool:
        return bool(page.wait_for(f"({where}).innerText.includes({json.dumps(needle)})", timeout))

    def shot(self, page: Page, name: str) -> None:
        if self.evidence is not None:
            page.screenshot(self.evidence / f"{self.tag}-{name}.png")

    def csp(self, page: Page) -> list:
        """Violations raised by page code. Reports whose source is the vendored sanitizer come from
        DOMPurify's inert parsing document (nothing loads or runs there) and are listed apart."""
        return [v for v in self.csp_all(page) if not v.get("source", "").endswith("/vendor/purify.js")]

    def csp_sanitizer(self, page: Page) -> list:
        return [v for v in self.csp_all(page) if v.get("source", "").endswith("/vendor/purify.js")]

    def csp_all(self, page: Page) -> list:
        return page.eval("window.__smoke ? window.__smoke.csp : null") or []

    def stops_for_last_run(self) -> int:
        fake = self.t.fake
        with fake.lock:
            creates = [c for c in fake.calls if c["kind"] == "create"]
            if not creates:
                return 0
            sid = creates[-1]["session_id"]
            return sum(1 for c in fake.calls if c["kind"] == "stop" and c["session_id"] == sid)

    def close(self) -> None:
        for page in self.pages:
            page.close()
        for f in self.boot_dir.glob("*"):
            f.unlink()
        self.boot_dir.rmdir()


# ======================================================================== G3: launch & exchange
@scenario("g3.exchange", "G3", "Bootstrap launch: exchange, fragment stripped before any library runs, DOM ids, no CSP violations")
def _(b: BCtx) -> None:
    page, launch_id, client = b.open_window(1)
    b.check("connected (bridge-session-id shows chat_label)", b.wait_connected(page), True, page.eval(
        "(document.getElementById('bridge-session-id')||{}).textContent"))
    info = page.eval("({path: location.pathname, hash: location.hash, hlen: history.length, smoke: window.__smoke && "
                     "{start: window.__smoke.startHadLaunch, lib: window.__smoke.lib}})")
    b.check("URL is /ui/index.html with no fragment", info["path"] == "/ui/index.html" and info["hash"] == "", "/ui/index.html",
            info["path"] + ("#…" if info["hash"] else ""))
    b.check("the launch fragment was present at document start", info["smoke"]["start"] is True, True, info["smoke"]["start"])
    libs = info["smoke"]["lib"]
    b.check("marked and DOMPurify defined with the fragment already gone",
            set(libs) == {"marked", "DOMPurify"} and all(v["hashEmpty"] for v in libs.values()), "both, hash empty", libs)
    b.check("history.length is 1 (meta refresh replaced the bootstrap entry)", info["hlen"] == 1, 1, info["hlen"])
    state = client.status(launch_id).json().get("state")
    b.check("launch state exchanged", state == "exchanged", "exchanged", state)
    ids = page.eval("(%s).map((id) => [id, document.querySelectorAll('#' + id).length])" % json.dumps(list(contract.DOM_IDS)))
    missing = [i for i, n in ids if n != 1]
    b.check("every section 7.3 id exists exactly once", not missing, [], missing)
    radios = page.eval("['omnia-palette','omnia-mode'].map((n) => document.querySelectorAll('input[type=radio][name=' + n + ']').length)")
    b.check("radio groups omnia-palette and omnia-mode", all(radios), "both", radios)
    roles = page.eval("[document.getElementById('messages').getAttribute('role'), "
                      "document.getElementById('connection-status').getAttribute('role')]")
    b.check("#messages role=log, #connection-status role=status", roles == ["log", "status"], ["log", "status"], roles)
    storage = page.eval("({ls: Object.keys(localStorage), ss: sessionStorage.length, cookie: document.cookie, name: window.name})")
    b.check("no token in storage (only omnia.theme may be in localStorage)", set(storage["ls"]) <= {"omnia.theme"}
            and storage["ss"] == 0 and storage["cookie"] == "" and storage["name"] == "", "empty", storage)
    b.check("zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    b.shot(page, "connected")


@scenario("g3.markdown", "G3", "A Markdown reply renders (table, code, list, quote) once; status returns; zero CSP violations", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    page.eval("""window.__mut = {batches: []};
      new MutationObserver((ms) => { const t = []; for (const m of ms) {
          if (m.type === 'childList') for (const n of m.addedNodes) t.push(n.textContent);
          else if (m.type === 'characterData') t.push('CD:' + m.target.textContent); }
        if (t.length) window.__mut.batches.push(t.join(' ')); })
        .observe(document.getElementById('messages'), {childList: true, subtree: true, characterData: true});""")
    b.send(page, "Summarize [md]")
    b.check("reply shown", b.wait_text(page, "446 units", where="document.getElementById('messages')"), True, False)
    shape = page.eval("""(() => { const m = document.getElementById('messages');
      return {table: m.querySelectorAll('table').length, td: m.querySelectorAll('td').length,
              pre: (m.querySelector('pre code') || {}).textContent, quote: m.querySelectorAll('blockquote').length,
              ol: m.querySelectorAll('ol').length, del: m.querySelectorAll('del').length,
              right: Array.from(m.querySelectorAll('td')).slice(1, 2).map((td) => td.getAttribute('align'))}; })()""")
    b.check("table with cells", shape["table"] == 1 and shape["td"] >= 12, "1 table, 12 td", shape)
    b.check("code block kept", "=SUM(B2:B5)" in (shape["pre"] or ""), "=SUM(B2:B5)", shape["pre"])
    b.check("blockquote, ordered list, strike-through kept", shape["quote"] == 1 and shape["ol"] >= 1 and shape["del"] == 1,
            "kept", shape)
    b.check("column alignment kept (align attribute)", shape["right"] == ["right"], ["right"], shape["right"])
    time.sleep(0.5)
    batches = page.eval("window.__mut.batches")
    reply_batches = [t for t in batches if any(f in t for f in ("Sales by region", "446 units", "=SUM(B2:B5)",
                                                                   "Nothing in the document"))]
    b.check("reply text arrives in one DOM insertion and is never edited (read once by screen readers)",
            len(reply_batches) == 1 and not any(t.startswith("CD:") for t in batches), "1 batch",
            {"batches": len(batches), "reply_batches": len(reply_batches)})
    b.check("composer has focus after the reply", page.eval("document.activeElement && document.activeElement.id") == "prompt-input",
            "prompt-input", page.eval("document.activeElement && document.activeElement.id"))
    b.check("zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    b.shot(page, "markdown")


@scenario("g3.r1_corpus", "G3", "R1 corpus: no control, style, image, id, name or class from a reply; lookups hit the real composer; links never navigate", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    nav_mark = len(b.chrome.events)
    b.send(page, "Show me [inject]")
    b.check("corpus reply shown", b.wait_text(page, "INJ-cell", where="document.getElementById('messages')"), True, False)
    time.sleep(0.5)
    r = page.eval(INSPECT_REPLY)
    b.check("no forbidden tag inside #messages", r["forbiddenTags"] == [], [], r["forbiddenTags"])
    b.check("no id / name / style attribute inside #messages", r["idAttrs"] == 0 and r["nameAttrs"] == 0 and r["styleAttrs"] == 0,
            "0 0 0", [r["idAttrs"], r["nameAttrs"], r["styleAttrs"]])
    b.check("no reply-supplied class or attribute (INJ marker, on*)", r["injClassOrAttr"] == [], [], r["injClassOrAttr"])
    b.check("no navigating <a href> left", r["hrefAnchors"] == 0, 0, r["hrefAnchors"])
    b.check("no fake user bubble (user bubbles = messages sent)", r["userBubbles"] == 1, 1, r["userBubbles"])
    b.check("section 7.3 ids unique and outside #messages", r["idsOutsideAndUnique"], True, r["idsOutsideAndUnique"])
    b.check("getElementById('send-button') is the composer's button", r["sendIsComposer"], True, r["sendIsComposer"])
    b.check("no injected script ran", r["globals"] == [], [], r["globals"])
    b.check("page background not restyled", "255, 0, 0" not in r["bodyBg"], "not red", r["bodyBg"])
    b.check("zero securitypolicyviolation events from page code", b.csp(page) == [], [], b.csp(page)[:5])
    sani = b.csp_sanitizer(page)
    b.check("sanitizer inert-parse CSP reports (nothing loaded or run; see REPORT)", True, "info",
            sorted({v["directive"] for v in sani}) + [len(sani)])
    b.shot(page, "r1-corpus")
    before = page.eval("location.href")
    clicked = page.eval("""(() => { const m = document.getElementById('messages'); let n = 0;
      for (const e of Array.from(m.querySelectorAll('button, [role=button], [tabindex]'))) { e.click(); n++; }
      for (const e of Array.from(m.querySelectorAll('span, code'))) { if (/example\\.invalid|mailto:/.test(e.textContent)) { e.click(); n++; } }
      return n; })()""")
    time.sleep(1.0)
    navs = [m for m in b.chrome.events[nav_mark:] if m.get("method") == "Page.frameNavigated"
            and m.get("sessionId") == page.session and not m["params"]["frame"].get("parentId")]
    created = [m for m in b.chrome.events[nav_mark:] if m.get("method") == "Target.targetCreated"]
    b.check("clicking every link control: no navigation, no new window, no window.open",
            page.eval("location.href") == before and not navs and not created and page.eval("window.__smoke.opens") == 0,
            "none", {"clicked": clicked, "navigations": len(navs), "targets": len(created)})
    shown = page.eval("(%s).filter((u) => document.getElementById('messages').innerText.includes(u)).length"
                      % json.dumps(list(corpus.ALLOWED_LINKS)))
    b.check("after a click every allowed link shows its full URL (R1: click shows the URL and Copy link)",
            shown == len(corpus.ALLOWED_LINKS), len(corpus.ALLOWED_LINKS), shown)
    copy_buttons = page.eval("Array.from(document.getElementById('messages').querySelectorAll('button'))"
                             ".filter((e) => /^(Copy link|Copied)/.test(e.textContent.trim())).length")
    b.check("a Copy link button is offered for the links", copy_buttons >= 1, ">= 1", copy_buttons)
    stray = [c for c in r["controls"] if not (c[0] in ("Copy link", "Copied") or c[1] in corpus.ALLOWED_LINKS
                                              or c[0] in corpus.ALLOWED_LINKS)]
    b.check("every reply control is a link control (carries an allowed URL) or Copy link", stray == [], [], stray)


PROFILE_PROBE = r"""
(() => {
  const md = '| L | R | C |\n| :-- | --: | :-: |\n| a | 1 | x |\n\n3. three\n4. four\n\n[ok](https://example.invalid/p)';
  const html = window.marked.parse(md, {async: false, breaks: true});
  const contract = {ALLOWED_TAGS: ['p','br','strong','em','del','code','pre','blockquote','ul','ol','li','h1','h2','h3',
    'h4','h5','h6','table','thead','tbody','tr','th','td','hr','a'], ALLOWED_ATTR: ['href','align','start'],
    ADD_URI_SAFE_ATTR: ['align', 'start'], ALLOW_DATA_ATTR: false, ALLOW_ARIA_ATTR: false,
    ALLOWED_URI_REGEXP: /^(?:https?|mailto):/i, RETURN_TRUSTED_TYPE: true};
  const without = Object.assign({}, contract); delete without.ADD_URI_SAFE_ATTR;
  const run = (cfg) => { const d = document.createElement('div'); d.innerHTML = window.DOMPurify.sanitize(html, cfg);
    return {align: Array.from(d.querySelectorAll('td')).map((td) => td.getAttribute('align')),
            start: (d.querySelector('ol') || {getAttribute: () => null}).getAttribute('start'),
            href: (d.querySelector('a') || {getAttribute: () => null}).getAttribute('href')}; };
  return {marked: (html.match(/<td[^>]*>/g) || []).slice(0, 3), contract: run(contract),
          withoutUriSafe: run(without)};
})()
"""


@scenario("g3.r1_profile", "G3", "The section 7.2 sanitizer profile keeps table alignment and list start (R1 says it must)")
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    r = page.eval(PROFILE_PROBE)
    want = {"align": ["left", "right", "center"], "start": "3", "href": "https://example.invalid/p"}
    b.check("marked emits align attributes", r["marked"] == ['<td align="left">', '<td align="right">', '<td align="center">'],
            "align=left/right/center", r["marked"])
    b.check("contract profile (exact 7.2 options, with ADD_URI_SAFE_ATTR) keeps align and start", r["contract"] == want,
            want, r["contract"])
    b.check("info: without ADD_URI_SAFE_ATTR DOMPurify strips them", True, "info", r["withoutUriSafe"])
    if b.evidence is not None:
        (b.evidence / f"{b.tag}-sanitizer-profile.json").write_text(json.dumps(r, indent=1))


@scenario("g3.stop_error_uncertain", "G3", "Stop (Esc), error and uncertain outcomes show the contract texts; focus returns to the composer", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    b.send(page, "Long job [hang]")
    b.check("working status shown", b.wait_text(page, contract.WORKING_TEXT, 5), contract.WORKING_TEXT,
            page.eval("document.getElementById('connection-status').textContent"))
    b.check("Stop enabled while working", page.wait_for("!document.getElementById('stop-button').disabled", 5), True, False)
    page.key("Escape")
    b.check("Esc stops: USER_STOPPED shown", b.wait_text(page, "Stopped. Check the document for changes made before stopping.", 8),
            True, False)
    b.check("the stop reached the Runs API", b.stops_for_last_run() >= 1, ">= 1", b.stops_for_last_run())
    b.check("focus back in the composer", page.eval("document.activeElement && document.activeElement.id") == "prompt-input",
            "prompt-input", page.eval("document.activeElement && document.activeElement.id"))
    b.send(page, "Break [fail]")
    b.check("error text shown", b.wait_text(page, "Hermes could not finish this reply", 10), True, False)
    b.send(page, "Unsure [unknown]")
    b.check("uncertain text shown", b.wait_text(page, "It is not known whether Hermes carried out this request", 10), True, False)
    b.send(page, "Approve [approval]")
    b.check("approval wording (R20)", b.wait_text(page, contract.APPROVAL_TEXT, 6), True,
            page.eval("document.getElementById('connection-status').textContent"))
    page.eval("document.getElementById('stop-button').click()")
    b.wait_text(page, "Stopped.", 8)
    b.check("zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    b.shot(page, "outcomes")


@scenario("g3.readiness", "G3", "Chat readiness states reach the page (state poll on focus)", needs=("ready",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    seen = {}
    for reason in ("no_api_key", "bad_api_key", "plugin_inactive", None):
        b.t.set_ready(reason)
        page.eval("window.dispatchEvent(new Event('focus'))")
        time.sleep(1.0)
        seen[str(reason)] = page.eval("['connection-status', 'bridge-status', 'desk-connect-text']"
                                      ".map((id) => (document.getElementById(id) || {}).textContent || '').join(' | ')")
    b.check("the page shows a different text for each state", len(set(seen.values())) == 4, 4, seen)


@scenario("g3.reload_duplicate", "G3", "Reload shows reconnect and stops the run; a duplicated tab is disconnected", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    b.send(page, "Long job [hang]")
    page.wait_for("!document.getElementById('stop-button').disabled", 5)
    time.sleep(0.5)
    page.send("Page.reload")
    time.sleep(1.0)
    b.check("reload shows the reconnect text", b.wait_text(page, contract.PAGE_TEXTS[None], 5), True,
            page.eval("document.body.innerText.slice(0, 200)"))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and b.stops_for_last_run() == 0:
        time.sleep(0.2)
    b.check("the run was asked to stop (pagehide close or window gone)", b.stops_for_last_run() >= 1, ">= 1",
            b.stops_for_last_run())
    dup = b.new_page(f"{b.t.origin}/ui/index.html")
    b.check("a duplicated tab shows the reconnect text", b.wait_text(dup, contract.PAGE_TEXTS[None], 5), True,
            dup.eval("document.body.innerText.slice(0, 200)"))


@scenario("g3.expired_double", "G3", "An expired link and a double-used link show their texts; the first window is disconnected")
def _(b: BCtx) -> None:
    ttl = b.t.timing["code_ttl"]
    if ttl > 40:
        raise SkipScenario("code life too long for the expired-link step on this target")
    _lid, url, _c = b.launch(7)
    time.sleep(ttl + 1.0)
    late = b.new_page(b.bootstrap(url).as_uri())
    b.check("expired link text", b.wait_text(late, "already used or has expired", 6), True,
            late.eval("document.body.innerText.slice(0, 200)"))
    _lid, url, _c = b.launch(8)
    first = b.new_page(b.bootstrap(url).as_uri())
    b.wait_connected(first)
    second = b.new_page(url)
    refused = second.wait_for("document.body.innerText.includes('already used or has expired') || document.body.innerText"
                              f".includes({json.dumps(contract.PAGE_TEXTS['double_use'])})", 6)
    b.check("the second presentation is refused (launch_expired or double-use text; section 6.1 gives no text for it)",
            bool(refused), True, second.eval("document.body.innerText.slice(0, 200)"))
    first.eval("window.dispatchEvent(new Event('focus'))")
    b.check("the first window shows the double-use text", b.wait_text(first, contract.PAGE_TEXTS["double_use"], 8), True,
            first.eval("document.body.innerText.slice(0, 200)"))


@scenario("g3.settings", "G3/G5", "Settings dialog under form-action 'none': opens, traps focus, Esc and Done close it, focus returns")
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    page.eval("document.getElementById('settings-button').focus()")
    page.key("Enter")
    b.check("dialog opens from the keyboard", page.wait_for("document.getElementById('settings-dialog').open", 3), True, False)
    stops = []
    for _ in range(12):
        page.key("Tab")
        stops.append(page.eval("""(() => { const a = document.activeElement, d = document.getElementById('settings-dialog');
          if (!a || a === document.body || a === document.documentElement) return 'browser';
          return d.contains(a) ? 'dialog' : 'outside:' + (a.id || a.tagName); })()"""))
    behind = [s for s in stops if s.startswith("outside:")]
    b.check("Tab never reaches page content behind the open dialog", behind == [], [], stops)
    b.check("Tab wraps inside the dialog without a browser stop", stops.count("browser") == 0, "12/12 in the dialog",
            f"{stops.count('dialog')}/12 in the dialog, {stops.count('browser')} browser stop(s)")
    page.key("Escape")
    b.check("Esc closes the dialog", page.wait_for("!document.getElementById('settings-dialog').open", 3), True, False)
    b.check("focus returns to the settings button", page.eval("document.activeElement.id") == "settings-button",
            "settings-button", page.eval("document.activeElement.id"))
    page.eval("document.getElementById('settings-button').click()")
    page.wait_for("document.getElementById('settings-dialog').open", 3)
    page.eval("document.getElementById('save-settings-button').click()")
    b.check("Done (form method=dialog) closes it", page.wait_for("!document.getElementById('settings-dialog').open", 3), True, False)
    b.check("zero securitypolicyviolation events (form-action 'none')", b.csp(page) == [], [], b.csp(page))


@scenario("g5.keyboard", "G5", "Keyboard: Tab order, Enter sends, Shift+Enter newline, focus stays in the composer", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    page.eval("document.activeElement && document.activeElement.blur(); document.body.focus()")
    order = []
    for _ in range(10):
        page.key("Tab")
        order.append(page.eval("document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null"))
    key_ids = [i for i in order if i in ("settings-button", "new-chat-button", "prompt-input", "send-button")]
    if "settings-button" in key_ids:   # the sequence is cyclic; start at the first control of the page
        start = key_ids.index("settings-button")
        key_ids = key_ids[start:] + key_ids[:start]
    firsts = list(dict.fromkeys(key_ids))
    b.check("Tab order: settings, New chat, composer, Send", firsts == ["settings-button", "new-chat-button", "prompt-input",
            "send-button"], ["settings-button", "new-chat-button", "prompt-input", "send-button"], order)
    page.eval("document.getElementById('prompt-input').focus()")
    page.type_text("line one")
    page.key("Enter", shift=True)
    page.type_text("line two")
    value = page.eval("document.getElementById('prompt-input').value")
    b.check("Shift+Enter inserts a newline and sends nothing", value == "line one\nline two"
            and page.eval("document.querySelectorAll('#messages .user').length") == 0, "line one\\nline two", value)
    page.key("Enter")
    b.check("Enter sends", page.wait_for("document.querySelectorAll('#messages .user').length === 1", 3), True, False)
    b.check("composer cleared after send", page.eval("document.getElementById('prompt-input').value") == "", "", "?")
    b.wait_text(page, "You said", 8)
    b.check("focus in the composer after the reply", page.eval("document.activeElement.id") == "prompt-input",
            "prompt-input", page.eval("document.activeElement.id"))


@scenario("g5.ax_tree", "G5", "AX tree: main landmark, named controls, one status, a log; reduced motion honoured")
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    page.send("Accessibility.enable")
    nodes = page.send("Accessibility.getFullAXTree")["nodes"]
    live = [n for n in nodes if not n.get("ignored")]
    role = lambda n: (n.get("role") or {}).get("value")  # noqa: E731
    name = lambda n: (n.get("name") or {}).get("value") or ""  # noqa: E731
    roles = [role(n) for n in live]
    b.check("a main landmark", "main" in roles, "main", sorted({r for r in roles if r})[:30])
    b.check("exactly one status", roles.count("status") == 1, 1, roles.count("status"))
    b.check("a log (the transcript)", roles.count("log") == 1, 1, roles.count("log"))
    unnamed = [role(n) for n in live if role(n) in ("button", "textbox", "radio", "checkbox", "combobox") and not name(n).strip()]
    b.check("every control has a name", not unnamed, [], unnamed)
    page.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})
    moving = page.eval("""Array.from(document.querySelectorAll('*')).filter((e) => { const s = getComputedStyle(e);
      return parseFloat(s.transitionDuration) > 0.01 || (s.animationName !== 'none' && parseFloat(s.animationDuration) > 0.01); })
      .map((e) => e.id || e.className || e.tagName).slice(0, 10)""")
    b.check("no transition or animation under prefers-reduced-motion: reduce", moving == [], [], moving)
    if b.evidence is not None:
        (b.evidence / f"{b.tag}-ax-roles.json").write_text(json.dumps(
            [{"role": role(n), "name": name(n)[:60]} for n in live if role(n) not in ("generic", "none", "StaticText",
                                                                                     "InlineTextBox", "LineBreak")], indent=1))


LIVE_WATCH = r"""
(() => {
  const st = document.getElementById('connection-status'), log = document.getElementById('messages');
  const rec = window.__live = {status: [], statusRewrites: 0, last: st.textContent, logBatches: 0};
  new MutationObserver(() => { const t = st.textContent;
      if (t === rec.last) rec.statusRewrites += 1; else rec.status.push(t);
      rec.last = t; }).observe(st, {childList: true, subtree: true, characterData: true});
  new MutationObserver(() => { rec.logBatches += 1; }).observe(log, {childList: true, subtree: true, characterData: true});
})()
"""


@scenario("g5.live_regions", "G5", "Live regions: the status is written only when it changes (idle polls stay silent), the reply lands in the log, not the status", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    time.sleep(0.5)
    page.eval(LIVE_WATCH)
    time.sleep(7.0)   # two or more idle state polls (every 3 s while visible)
    idle = page.eval("({status: window.__live.status.slice(), rewrites: window.__live.statusRewrites, log: window.__live.logBatches})")
    b.check("idle state polls write nothing to the status or the log (no repeated announcements)",
            idle["status"] == [] and idle["rewrites"] == 0 and idle["log"] == 0, "no writes", idle)
    b.send(page, "Summarize [md]")
    b.wait_text(page, "446 units", where="document.getElementById('messages')")
    time.sleep(1.5)
    live = page.eval("({status: window.__live.status.slice(), rewrites: window.__live.statusRewrites})")
    b.check("during a turn the status announces the working text once", live["status"].count(contract.WORKING_TEXT) == 1,
            1, live["status"])
    b.check("no status write repeats the text already shown", live["rewrites"] == 0, 0, live["rewrites"])
    b.check("consecutive status texts differ", all(x != y for x, y in zip(live["status"], live["status"][1:])), True, live["status"])
    attrs = page.eval("""(() => { const st = document.getElementById('connection-status'), log = document.getElementById('messages');
      return {statusHasReply: st.textContent.includes('446 units'), logLive: log.getAttribute('aria-live'),
              logBusy: log.getAttribute('aria-busy'), statusLive: st.getAttribute('aria-live'),
              assertive: document.querySelectorAll('[aria-live=assertive], [role=alert]').length}; })()""")
    b.check("the reply is not read through the status", not attrs["statusHasReply"], False, attrs["statusHasReply"])
    b.check("the log is not silenced (aria-live is not off) and not left busy", attrs["logLive"] != "off"
            and attrs["logBusy"] != "true", "live, not busy", attrs)
    b.check("the status is not silenced (aria-live is not off)", attrs["statusLive"] != "off", "not off", attrs["statusLive"])
    b.check("no assertive region or alert interrupts the reader in normal use", attrs["assertive"] == 0, 0, attrs["assertive"])


@scenario("g3.three_windows", "G3", "Four windows with concurrent turns (one long-poll each, shared connection pool) can all Stop", needs=("fake",))
def _(b: BCtx) -> None:
    pages = [b.open_window(n)[0] for n in (11, 12, 13, 14)]
    for i, page in enumerate(pages):
        b.send(page, f"window {i} [hang]")
    ok = [bool(p.wait_for("!document.getElementById('stop-button').disabled", 6)) for p in pages]
    b.check("all four working", all(ok), [True] * 4, ok)
    time.sleep(1.0)
    t0 = time.monotonic()
    for page in pages:
        page.eval("document.getElementById('stop-button').click()")
    stopped = [b.wait_text(p, "Stopped. Check the document", 10) for p in pages]
    b.check("every window shows the stopped outcome", all(stopped), [True] * 4, stopped)
    b.check("all stopped within 10 s", time.monotonic() - t0 < 10, "< 10 s", round(time.monotonic() - t0, 2))
    with b.t.fake.lock:
        stops = len({c["session_id"] for c in b.t.fake.calls if c["kind"] == "stop"})
    b.check("four runs stopped at the Runs API", stops >= 4, ">= 4", stops)
    b.check("a fifth page load is served meanwhile", True, "info", "covered by probes budget.stop_with_parked_polls")


@scenario("g3.unload", "G3", "Unloading the plugin while a turn is followed shows the turned-off text", needs=("unload", "fake"))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(1)
    b.send(page, "Long job [hang]")
    page.wait_for("!document.getElementById('stop-button').disabled", 5)
    time.sleep(0.8)
    remaining = b.t.unload(10.0)
    b.check("unload left no run", remaining == 0, 0, remaining)
    b.check("the page shows the turned-off text", b.wait_text(page, contract.PAGE_TEXTS["turned_off"], 6), True,
            page.eval("document.body.innerText.slice(0, 200)"))


class SkipScenario(Exception):
    pass


class DropChatAnswers:
    """Fail the browser's next ``drop`` answers to ``POST /api/chat`` on one page AFTER the plugin
    has answered (CDP Fetch at the response stage): the plugin admitted the message, the page sees a
    network error. Later answers pass. Answers are sent from a thread of their own (the CDP reader
    thread must not wait for its own replies)."""

    def __init__(self, page: Page, drop: int) -> None:
        import threading
        self.page, self.drop, self.paused = page, drop, 0
        self.failed = 0
        self._lock = threading.Lock()
        self._threading = threading
        page.chrome.listeners.append(self._on_event)
        page.send("Fetch.enable", {"patterns": [{"urlPattern": "*/api/chat", "requestStage": "Response"}]})

    def _on_event(self, msg: dict) -> None:
        if msg.get("method") != "Fetch.requestPaused" or msg.get("sessionId") != self.page.session:
            return
        request_id = msg["params"]["requestId"]
        with self._lock:
            self.paused += 1
            fail = self.paused <= self.drop
            if fail:
                self.failed += 1

        def answer() -> None:
            try:
                if fail:
                    self.page.send("Fetch.failRequest", {"requestId": request_id, "errorReason": "ConnectionReset"})
                else:
                    self.page.send("Fetch.continueRequest", {"requestId": request_id})
            except Exception:  # noqa: BLE001 - the page may be gone; the scenario's checks report it
                pass
        self._threading.Thread(target=answer, daemon=True).start()

    def close(self) -> None:
        if self._on_event in self.page.chrome.listeners:
            self.page.chrome.listeners.remove(self._on_event)


def _creations(b: BCtx) -> int:
    with b.t.fake.lock:
        return sum(1 for c in b.t.fake.calls if c["kind"] == "create")


REPLY_COUNT = ("Array.from(document.querySelectorAll('#messages .message.assistant'))"
               ".filter((m) => m.innerText.includes(%s)).length")


def _lost_send(b: BCtx, n: int, drop: int, message: str, needle: str, label: str) -> None:
    page, _lid, _c = b.open_window(n)
    created = _creations(b)
    dropper = DropChatAnswers(page, drop)
    try:
        b.send(page, message)
        count = REPLY_COUNT % json.dumps(needle)
        shown = page.wait_for(f"{count} >= 1", 15)
        b.check(f"{label}: the result is shown although the answer to Send was lost", bool(shown), True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        b.check(f"{label}: {drop} answer(s) to Send were dropped", dropper.failed == drop, drop, dropper.failed)
        time.sleep(7.0)   # two or more state polls (every 3 s) after the result was shown
        b.check(f"{label}: shown exactly once, also after later state polls", page.eval(count) == 1, 1, page.eval(count))
        runs = _creations(b) - created
        b.check(f"{label}: one run for one Send (no blind resend)", runs == 1, 1, runs)
        disabled = page.eval("document.getElementById('prompt-input').disabled")
        b.check(f"{label}: the composer is usable again", disabled is False, False, disabled)
        b.check(f"{label}: zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    finally:
        dropper.close()


@scenario("g3.lost_send", "G3", "A lost answer to Send: the page retries with the same send_id and shows the reply once",
          needs=("fake",))
def _(b: BCtx) -> None:
    _lost_send(b, 21, 1, "Lost answer [md]", "Sales by region", "one lost answer")


@scenario("g3.lost_send_unknown", "G3", "Every answer to Send lost and the reply done before the next state poll: shown once from the state poll",
          needs=("fake",))
def _(b: BCtx) -> None:
    _lost_send(b, 22, 3, "All answers lost [md]", "Sales by region", "completed")
    _lost_send(b, 23, 3, "All answers lost [fail]", "Hermes could not finish this reply", "failed")


class FetchControl:
    """CDP Fetch on one page for several URL patterns at once. Each paused request or response is
    passed to ``decide(stage, params)`` (stage ``request`` or ``response``), which answers
    ``continue``, ``fail`` (the page sees a network error) or ``hold`` (kept until ``release``).
    Answers are sent from threads of their own (the CDP reader must not wait for its replies)."""

    def __init__(self, page: Page, patterns: list, decide: Callable[[str, dict], str]) -> None:
        import threading
        self.page, self.decide = page, decide
        self.held: list[str] = []
        self.lock = threading.Condition()
        self._threading = threading
        page.chrome.listeners.append(self._on_event)
        page.send("Fetch.enable", {"patterns": patterns})

    def _answer(self, method: str, request_id: str, extra: Optional[dict] = None) -> None:
        def run() -> None:
            try:
                self.page.send(method, dict({"requestId": request_id}, **(extra or {})))
            except Exception:  # noqa: BLE001 - the page may be gone; the scenario's checks report it
                pass
        self._threading.Thread(target=run, daemon=True).start()

    def _on_event(self, msg: dict) -> None:
        if msg.get("method") != "Fetch.requestPaused" or msg.get("sessionId") != self.page.session:
            return
        params = msg["params"]
        stage = "response" if ("responseStatusCode" in params or "responseErrorReason" in params) else "request"
        action = self.decide(stage, params)
        if action == "hold":
            with self.lock:
                self.held.append(params["requestId"])
                self.lock.notify_all()
        elif action == "fail":
            self._answer("Fetch.failRequest", params["requestId"], {"errorReason": "ConnectionReset"})
        else:
            self._answer("Fetch.continueRequest", params["requestId"])

    def wait_held(self, count: int = 1, timeout: float = 10.0) -> bool:
        with self.lock:
            return self.lock.wait_for(lambda: len(self.held) >= count, timeout=timeout)

    def release_all(self) -> None:
        with self.lock:
            held, self.held = self.held, []
        for request_id in held:
            self._answer("Fetch.continueRequest", request_id)

    def close(self) -> None:
        self.release_all()
        if self._on_event in self.page.chrome.listeners:
            self.page.chrome.listeners.remove(self._on_event)


def _deliver_out_of_band(b: BCtx, params: dict, answers: list) -> None:
    """Send the page's own paused ``/api/chat`` request to the endpoint from here, as a request
    that reached it while the browser saw the connection fail."""
    import threading
    from httpraw import request as raw
    headers = {k.lower(): v for k, v in params["request"].get("headers", {}).items()}
    send = [("Origin", b.t.origin), ("Sec-Fetch-Site", "same-origin"),
            ("Sec-Fetch-Mode", "cors"), ("Sec-Fetch-Dest", "empty"), ("Content-Type", "application/json"),
            ("Accept", "application/json"), ("X-Sidecar-Session", headers.get("x-sidecar-session", ""))]
    body = params["request"].get("postData", "").encode("utf-8")

    def run() -> None:
        try:
            answers.append(raw(b.t.port, "POST", "/api/chat", send, body, timeout=60))
        except Exception as exc:  # noqa: BLE001 - reported by the scenario's checks
            answers.append(exc)
    threading.Thread(target=run, daemon=True).start()


NO_NON_DELIVERY = "did not reach Hermes"
UNDECIDED_TEXT = "It is not known yet whether Hermes received this message"
COMPOSER_STATE = ("({send: document.getElementById('send-button').disabled, input: document.getElementById('prompt-input')"
                  ".disabled, newChat: document.getElementById('new-chat-button').disabled, retry: "
                  "!document.getElementById('desk-connect').hidden && !document.getElementById('desk-connect-button').hidden})")


def _undecided_send(b: BCtx, n: int, message: str, needle: str, label: str, *, try_again: bool = False,
                    stop: bool = False) -> None:
    """critic2 R2: the first request of a Send reaches the plugin but its admission is held; the
    browser sees that request fail and its two retries fail before reaching the plugin. State
    polls answer meanwhile without the turn. The page must not say the message did not arrive,
    must not offer a fresh-id Send or New chat, and must show the result once when the admission
    completes, from a state poll or (``try_again``) from Try again with the same send_id."""
    page, _lid, _c = b.open_window(n)
    created = _creations(b)
    oob: list = []
    seen = {"chat": 0}

    def decide(stage: str, params: dict) -> str:
        if "/api/chat" not in params["request"]["url"]:
            return "continue"
        seen["chat"] += 1
        if seen["chat"] == 1:
            _deliver_out_of_band(b, params, oob)
            return "fail"
        return "fail" if seen["chat"] <= 3 else "continue"
    release = b.t.hold_admission()
    control = FetchControl(page, [{"urlPattern": "*/api/chat", "requestStage": "Request"}], decide)
    try:
        b.send(page, message)
        undecided = b.wait_text(page, UNDECIDED_TEXT, 10)
        b.check(f"{label}: the page says delivery is not known yet", undecided, True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        time.sleep(7.0)   # two or more state polls (every 3 s) answer while the admission is held
        body = page.eval("document.body.innerText")
        b.check(f"{label}: no claim that the message did not reach Hermes", NO_NON_DELIVERY not in body, False,
                body[-300:])
        state = page.eval(COMPOSER_STATE)
        b.check(f"{label}: no fresh Send and no New chat while undecided; Try again offered",
                state == {"send": True, "input": True, "newChat": True, "retry": True}, "all true", state)
        b.check(f"{label}: nothing was created while the admission was held", _creations(b) - created == 0, 0,
                _creations(b) - created)
        if try_again:
            page.eval("document.getElementById('desk-connect-button').click()")
            with control.lock:
                control.lock.wait_for(lambda: seen["chat"] >= 4, timeout=5)
            time.sleep(0.5)   # the Try again request is waiting in the plugin for the admission
        release()
        count = REPLY_COUNT % json.dumps(needle)
        if stop:
            working = page.wait_for("!document.getElementById('stop-button').disabled", 15)
            b.check(f"{label}: the turn is followed and Stop is offered", bool(working), True, False)
            page.eval("document.getElementById('stop-button').click()")
        shown = page.wait_for(f"{count} >= 1", 20)
        b.check(f"{label}: the result is shown once the admission completes", bool(shown), True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        time.sleep(7.0)
        b.check(f"{label}: shown exactly once, also after later state polls", page.eval(count) == 1, 1, page.eval(count))
        runs = _creations(b) - created
        b.check(f"{label}: one run for one Send", runs == 1, 1, runs)
        first = oob[0] if oob else None
        b.check(f"{label}: the out-of-band first request was admitted (202)",
                getattr(first, "status", None) == 202, 202, getattr(first, "status", first))
        if try_again:
            b.check(f"{label}: Try again sent the same request (4 requests in all)", seen["chat"] == 4, 4, seen["chat"])
        body = page.eval("document.body.innerText")
        b.check(f"{label}: never a non-delivery claim", NO_NON_DELIVERY not in body, False, body[-200:])
        disabled = page.eval("document.getElementById('prompt-input').disabled")
        b.check(f"{label}: the composer is usable again", disabled is False, False, disabled)
        b.check(f"{label}: zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    finally:
        release()
        control.close()


@scenario("g3.send_undecided", "G3", "A Send still being admitted after all its answers were lost: no 'did not reach', "
          "one run, the result shown once (completed, failed, not known)", needs=("fake", "admission"))
def _(b: BCtx) -> None:
    _undecided_send(b, 24, "Held admission [md]", "Sales by region", "completed")
    _undecided_send(b, 25, "Held admission [fail]", "Hermes could not finish this reply", "failed")
    _undecided_send(b, 26, "Held admission [unknown]", "It is not known whether Hermes carried out", "not known")


@scenario("g3.send_try_again", "G3", "Try again on an undecided Send re-sends the same send_id, which waits for the "
          "held admission: one run, then Stop", needs=("fake", "admission"))
def _(b: BCtx) -> None:
    _undecided_send(b, 27, "Held then stopped [hang]", "Stopped. Check the document", "stopped",
                    try_again=True, stop=True)


@scenario("g3.stale_state_after_send", "G3", "A state answer from before Send, delivered after its retries ran out, "
          "decides nothing; Try again then delivers it once", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(28)
    created = _creations(b)
    seen = {"chat": 0, "state": 0}

    def decide(stage: str, params: dict) -> str:
        url = params["request"]["url"]
        if "/api/state" in url:
            seen["state"] += 1
            return "hold" if seen["state"] == 1 else "continue"
        seen["chat"] += 1
        return "fail" if seen["chat"] <= 3 else "continue"   # the Send never reaches the plugin
    control = FetchControl(page, [{"urlPattern": "*/api/state", "requestStage": "Response"},
                                  {"urlPattern": "*/api/chat", "requestStage": "Request"}], decide)
    try:
        b.check("a state answer from before the Send is held", control.wait_held(1, 10), True, False)
        b.send(page, "Reordered state [md]")
        b.check("retries ran out: delivery not known", b.wait_text(page, UNDECIDED_TEXT, 10), True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        control.release_all()   # the old answer (no turn) arrives now
        time.sleep(4.0)
        body = page.eval("document.body.innerText")
        b.check("the stale state answer made no non-delivery claim", NO_NON_DELIVERY not in body, False, body[-300:])
        state = page.eval(COMPOSER_STATE)
        b.check("no fresh Send and no New chat; Try again offered",
                state == {"send": True, "input": True, "newChat": True, "retry": True}, "all true", state)
        page.eval("document.getElementById('desk-connect-button').click()")
        count = REPLY_COUNT % json.dumps("Sales by region")
        shown = page.wait_for(f"{count} >= 1", 15)
        b.check("Try again delivered it and the reply is shown", bool(shown), True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        time.sleep(7.0)
        b.check("shown exactly once", page.eval(count) == 1, 1, page.eval(count))
        runs = _creations(b) - created
        b.check("one run for one Send", runs == 1, 1, runs)
        b.check("zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    finally:
        control.close()


@scenario("g3.unreachable_try_again", "G3", "With a Send undecided: an outage that ends leaves no 'not reachable' "
          "status, and a Try again that gets through before any state poll does clears the 'not reachable' "
          "banner; one run", needs=("fake",))
def _(b: BCtx) -> None:
    """dev-critic round 3 N3 (the real page; the fixture page has no 'not reachable' banner)."""
    if b.tag.startswith("fixture"):
        raise SkipScenario("the fixture page has no 'not reachable' banner")
    page, _lid, _c = b.open_window(30)
    created = _creations(b)
    mode, seen = {"state": "continue"}, {"chat": 0}
    status = "document.getElementById('connection-status').textContent"
    banner = "document.getElementById('desk-connect').hidden ? null : document.getElementById('desk-connect-text').textContent"

    def decide(stage: str, params: dict) -> str:
        if "/api/state" in params["request"]["url"]:
            return mode["state"]
        seen["chat"] += 1
        return "fail" if seen["chat"] <= 3 else "continue"   # the Send never reaches the plugin, Try again does
    control = FetchControl(page, [{"urlPattern": "*/api/state", "requestStage": "Request"},
                                  {"urlPattern": "*/api/chat", "requestStage": "Request"}], decide)
    try:
        b.send(page, "Unreachable then Try again [hang]")
        b.check("retries ran out: delivery not known", b.wait_text(page, UNDECIDED_TEXT, 10), True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        mode["state"] = "fail"
        down = page.wait_for(f"{status} === 'Hermes not reachable' && ({banner} || '').includes('Hermes is not reachable')", 10)
        b.check("state polls fail: the status and the banner say not reachable", bool(down), True, page.eval(status))
        mode["state"] = "continue"
        up = page.wait_for(f"{status} !== 'Hermes not reachable'", 10)
        b.check("the outage ends: the status no longer says not reachable", bool(up), True, page.eval(status))
        b.check("the Try again of the undecided Send is still offered",
                page.eval(COMPOSER_STATE).get("retry") is True, True, page.eval(COMPOSER_STATE))
        mode["state"] = "fail"
        down = page.wait_for(f"({banner} || '').includes('Hermes is not reachable')", 10)
        b.check("a second outage: the banner says not reachable", bool(down), True, page.eval(banner))
        mode["state"] = "hold"   # from now on no state poll is answered at all
        page.eval("document.getElementById('desk-connect-button').click()")
        working = page.wait_for("!document.getElementById('stop-button').disabled", 15)
        b.check("Try again got through: the turn runs", bool(working), True, page.eval(status))
        shown = page.eval(banner)
        b.check("the stale 'not reachable' banner is gone", shown is None or "not reachable" not in shown,
                "no 'not reachable' banner", shown)
        b.check("the status is not 'not reachable'", page.eval(status) != "Hermes not reachable",
                "not 'Hermes not reachable'", page.eval(status))
        mode["state"] = "continue"
        control.release_all()
        page.eval("document.getElementById('stop-button').click()")
        b.check("Stop ends the turn", b.wait_text(page, "Stopped. Check the document", 15), True,
                page.eval("document.getElementById('messages').innerText.slice(-300)"))
        runs = _creations(b) - created
        b.check("one run for one Send", runs == 1, 1, runs)
        b.check("zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    finally:
        control.close()


@scenario("g3.new_chat_stale_state", "G3", "A state answer from before New chat, delivered after it, neither shows the "
          "cleared reply again nor an unknown-turn error", needs=("fake",))
def _(b: BCtx) -> None:
    page, _lid, _c = b.open_window(29)
    b.send(page, "Before New chat [md]")
    count = REPLY_COUNT % json.dumps("Sales by region")
    b.check("the reply is shown", bool(page.wait_for(f"{count} >= 1", 15)), True, False)
    page.wait_for("!document.getElementById('new-chat-button').disabled", 5)
    seen = {"state": 0}

    def decide(stage: str, params: dict) -> str:
        seen["state"] += 1
        return "hold" if seen["state"] == 1 else "continue"
    control = FetchControl(page, [{"urlPattern": "*/api/state", "requestStage": "Response"}], decide)
    try:
        b.check("a state answer naming the old turn is held", control.wait_held(1, 10), True, False)
        page.eval("document.getElementById('new-chat-button').click()")
        cleared = page.wait_for("document.getElementById('messages').children.length === 0", 5)
        b.check("New chat cleared the log", bool(cleared), True, False)
        control.release_all()
        time.sleep(4.0)
        body = page.eval("document.body.innerText")
        b.check("no unknown-turn error", "not the window's current one" not in body, False, body[-300:])
        b.check("the cleared reply is not shown again", page.eval(count) == 0, 0, page.eval(count))
        status = page.eval("document.getElementById('connection-status').textContent")
        b.check("the status is not an error", status != "Error", "not Error", status)
        b.check("zero securitypolicyviolation events", b.csp(page) == [], [], b.csp(page))
    finally:
        control.close()


def run_scenarios(make_target: Callable[[], Any], *, evidence: Optional[Path], tag: str, only: list[str] = (),
                  headless: bool = True) -> list[dict[str, Any]]:
    results = []
    chrome = Chrome(headless=headless)
    try:
        for s in SCENARIOS:
            if only and not any(s.id.startswith(o) for o in only):
                continue
            started = time.monotonic()
            r: dict[str, Any] = {"id": s.id, "gate": s.gate, "title": s.title, "bundle": tag}
            target = make_target()
            b = BCtx(target, chrome, evidence, tag + "-" + s.id.replace(".", "-"))
            try:
                missing = [n for n in s.needs if n not in target.capabilities]
                if missing:
                    raise SkipScenario("target lacks " + ", ".join(missing))
                s.fn(b)
                r["status"] = "pass" if all(c["ok"] for c in b.checks) else "fail"
            except SkipScenario as exc:
                r["status"], r["note"] = "skip", str(exc)
            except Exception as exc:  # noqa: BLE001 - a crashed scenario is an error result with its trace
                r["status"], r["note"] = "error", f"{type(exc).__name__}: {exc}"
                r["trace"] = traceback.format_exc()[-1500:]
            finally:
                b.close()
                target.close()
            r["checks"] = b.checks
            r["seconds"] = round(time.monotonic() - started, 2)
            results.append(r)
            failed = [c["label"] for c in b.checks if not c["ok"]]
            print(f"{r['status'].upper():5} {tag}:{r['id']:28} {r['seconds']:6.1f}s {r.get('note', '')}"
                  + (f"  FAILED: {failed[:4]}" if failed else ""), flush=True)
        results.append({"id": "browser", "chrome": chrome.version.get("product"), "status": "info"})
    finally:
        chrome.close()
    return results


def main(argv: Optional[list[str]] = None) -> int:
    import targets
    ap = argparse.ArgumentParser(description="web_smoke headless-Chrome checks")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--tag", default="bundle")
    ap.add_argument("--evidence")
    ap.add_argument("--json")
    ap.add_argument("--only", action="append", default=[])
    ap.add_argument("--repo", default=str(HERE.parents[1]))
    args = ap.parse_args(argv)
    evidence = Path(args.evidence) if args.evidence else None
    if evidence:
        evidence.mkdir(parents=True, exist_ok=True)
    results = run_scenarios(lambda: targets.StubTarget(Path(args.bundle), repo=Path(args.repo)), evidence=evidence,
                            tag=args.tag, only=args.only)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1, default=str))
    return 0 if all(r["status"] in ("pass", "skip", "info") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
