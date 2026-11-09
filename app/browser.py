"""The model's web browser: agent-browser (github.com/vercel-labs/agent-browser) driving a private,
headless Chrome.

Five tools: search the web, open a page, click something on it, type into it, and go back.
Each page view gives the model the page's readable text and a short numbered list of what it
can click or type into, picked from agent-browser's accessibility snapshot: form fields,
buttons in dialogs (cookie banners), links in the article, then other buttons. The model
names elements by those numbers, which a 2B model handles better than agent-browser's own
@e refs. Each step saves a screenshot so the page can show what the model looked at.

It's private: its own agent-browser session and config file (so never your Chrome profile,
saved logins or agent-browser settings), a fresh profile each launch, and every request,
loopback included, goes through app/netguard.py's proxy, which refuses this computer and the
local network. Commands go to agent-browser as JSON on stdin, so nothing the model types is
ever parsed by a shell.
"""

import asyncio
import json
import os
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path

from app import netguard
from app.netguard import REFUSAL, is_local  # noqa: F401  (is_local: the smoke test checks it)

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "outputs"
CONFIG = ROOT / "data" / "agent-browser.json"
SESSION = "lfm3-lab"
TEXT_LIMIT = 3000
RESULT_LIMIT = 5
INSTALL = "The web browser needs agent-browser. Install it with: npm install -g agent-browser && agent-browser install"

READ_PAGE = """(([textLimit, linkLimit]) => {
  // The article itself if the page marks it, else the main area, else the whole page.
  const pick = ['#mw-content-text', 'article', '[role=main]', 'main', '#content', '#main']
    .map(s => document.querySelector(s)).find(el => el && el.innerText.trim().length > 300);
  const root = pick || document.body;
  const text = ((root && root.innerText) || '').split('\\n').map(l => l.replace(/\\s+/g, ' ').trim())
    .filter(l => l.length > 1).join('\\n');
  const here = location.href.split('#')[0];
  const seen = new Set(), links = [];
  for (const a of (root ? root.querySelectorAll('a[href]') : [])) {
    const href = a.href;
    if (!/^https?:/.test(href) || seen.has(href) || href.split('#')[0] === here) continue;
    const box = a.getBoundingClientRect();
    if (!box.width && !box.height) continue;
    seen.add(href);
    links.push(href);
    if (links.length >= linkLimit) break;
  }
  return {title: document.title, url: location.href, text: text.slice(0, textLimit),
          more: text.length > textLimit, links};
})([%d, 60])""" % TEXT_LIMIT

# Wait for the page to finish loading, but never long: agent-browser's own `wait --load` waits
# for the next load event (25 s when the page has already loaded), and `wait --fn` ignores its
# timeout. This returns once the page is complete, after 2.5 s if it's usable, or at 10 s.
SETTLE = """new Promise(resolve => {
  const start = Date.now();
  (function tick() {
    const state = document.readyState, waited = Date.now() - start;
    if (state === 'complete' || (state === 'interactive' && waited > 2500) || waited > 10000) return resolve(state);
    setTimeout(tick, 100);
  })();
})"""

DUCKDUCKGO = """[...document.querySelectorAll('.result')].map(r => {
  if (r.classList.contains('result--ad')) return null;
  const a = r.querySelector('.result__a'), s = r.querySelector('.result__snippet');
  if (!a) return null;
  let url = a.href;
  try { const u = new URL(url); if (u.searchParams.get('uddg')) url = u.searchParams.get('uddg'); } catch (e) {}
  if (url.includes('duckduckgo.com/y.js')) return null;
  return {title: a.innerText.trim(), url, snippet: s ? s.innerText.trim() : ''};
}).filter(Boolean)"""

BING = """[...document.querySelectorAll('li.b_algo')].map(li => {
  const a = li.querySelector('h2 a'), p = li.querySelector('.b_caption p, p');
  if (!a) return null;
  let url = a.href;
  try {
    const u = new URL(url), v = u.searchParams.get('u');
    if (u.hostname.endsWith('bing.com') && v && v.startsWith('a1'))
      url = atob(v.slice(2).replace(/-/g, '+').replace(/_/g, '/'));
  } catch (e) {}
  return {title: (a.innerText || a.textContent).trim(), url, snippet: p ? p.innerText.trim() : ''};
}).filter(Boolean)"""

# What the model is told each kind of element is, and which it can type into.
KINDS = {"textbox": "text box", "searchbox": "search box", "combobox": "text box", "spinbutton": "number box",
         "checkbox": "checkbox", "radio": "choice", "switch": "switch", "button": "button", "link": "link",
         "tab": "tab", "menuitem": "menu item", "option": "option"}
FIELDS = {"textbox", "searchbox", "combobox", "spinbutton"}
TOGGLES = {"checkbox", "radio", "switch"}
OUTSIDE = {"navigation", "banner", "contentinfo", "complementary", "menubar", "menu"}
NOISE = re.compile(r"^(jump (up|to)\b.*|edit|\[?\d+\]?|\^|[a-z]|skip to .*)$", re.I)
SNAPSHOT_LINE = re.compile(r'^(\s*)- ([\w-]+)(?: "((?:[^"\\]|\\.)*)")?(.*)$')


def _binary():
    """agent-browser's executable. On Windows npm installs a .cmd shim; the native .exe beside it
    starts faster (either is safe: the model's text never reaches the command line)."""
    found = shutil.which("agent-browser")
    if found and os.name == "nt" and found.lower().endswith((".cmd", ".bat", ".ps1")):
        exes = sorted(Path(found).parent.glob("node_modules/agent-browser/bin/agent-browser-win32-*.exe"))
        if exes:
            return str(exes[0])
    return found


def elements_from(snapshot, page_url, content_links):
    """The few elements worth offering, from `snapshot -i -c -u` text: [(ref, role, name, checked)].

    Fields and dialog buttons first, then links in the readable part of the page (in reading
    order), then other buttons and toggles, then whatever links are left, up to 30.
    """
    here = page_url.split("#")[0]
    parsed, stack, seen = [], [], set()
    for line in snapshot.splitlines():
        m = SNAPSHOT_LINE.match(line)
        if not m:
            continue
        indent, role, name, rest = len(m.group(1)), m.group(2), (m.group(3) or "").replace('\\"', '"'), m.group(4)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        ancestors = {r for _, r in stack}
        stack.append((indent, role))
        ref = re.search(r"\bref=(e\d+)", rest)
        if role not in KINDS or not ref:
            continue
        name = re.sub(r"\s+", " ", name).strip()
        url = (re.search(r"\burl=([^\s\]]+?)(?:,\s|\]$)", rest) or [None, ""])[1]
        if role == "link" and (len(name) < 2 or NOISE.match(name) or not url or url.split("#")[0] == here
                               or not url.startswith("http")):
            continue
        if role != "link" and not name and role not in FIELDS:
            continue
        key = (role, name.lower(), url)
        if key in seen:
            continue
        seen.add(key)
        checked = (re.search(r"\bchecked=(\w+)", rest) or [None, None])[1]
        parsed.append({"i": len(parsed), "ref": ref.group(1), "role": role, "name": name[:90], "url": url,
                       "dialog": bool(ancestors & {"dialog", "alertdialog"}), "outside": bool(ancestors & OUTSIDE),
                       "checked": checked})

    order = {u: i for i, u in enumerate(content_links)}
    picked = []

    def take(items, limit):
        for e in items:
            if len(picked) >= 30 or limit <= 0:
                return
            if e not in picked:
                picked.append(e)
                limit -= 1

    take([e for e in parsed if e["dialog"]], 8)
    take(sorted((e for e in parsed if e["role"] in FIELDS), key=lambda e: e["outside"]), 6)
    take(sorted((e for e in parsed if e["role"] == "link" and e["url"] in order), key=lambda e: order[e["url"]]), 15)
    take([e for e in parsed if e["role"] != "link" and not e["outside"]], 6)
    if len(picked) < 15:
        take([e for e in parsed if e["role"] == "link"], 20 - len(picked))
    picked.sort(key=lambda e: e["i"])
    return [(e["ref"], e["role"], e["name"], e["checked"]) for e in picked]


NET_ERRORS = {"TUNNEL_CONNECTION_FAILED": "the site couldn't be reached, or the address doesn't exist.",
              "NAME_NOT_RESOLVED": "the address doesn't exist.", "TIMED_OUT": "the site took too long to answer.",
              "CONNECTION_REFUSED": "the site refused the connection.", "CERT": "the site's certificate isn't valid.",
              "SSL": "the site's secure connection failed."}


def _why(failure):
    text = str(failure or "no answer")
    return next((why for code, why in NET_ERRORS.items() if code in text), text.splitlines()[0][:160] + ".")


def describe(n, role, name, checked):
    state = {"true": " (on)", "false": " (off)"}.get(checked, "") if role in TOGGLES else ""
    return f"{n} {KINDS[role]}: {name or '(no label)'}{state}"


class BrowserError(Exception):
    pass


class Browser:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.proxy = netguard.Proxy()
        self.binary = None
        self.user_agent = None
        self.ready = False
        self.results = []   # the last search's results, so the model can open one by number
        self.elements = []  # the open page's elements [(ref, role, name, checked)], numbered from 1
        self.target = None  # what the last click or typing went to, for the page and the model
        self.headed = os.environ.get("LFM3_BROWSER_HEADED") == "1"

    # ---- agent-browser ------------------------------------------------------------
    def _env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("AGENT_BROWSER_") and k.upper() not in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        env.update({
            "AGENT_BROWSER_SESSION": SESSION,
            "AGENT_BROWSER_CONFIG": str(CONFIG),
            "AGENT_BROWSER_PROXY": self.proxy.url,
            # Chrome sends loopback requests around a proxy unless told not to.
            "AGENT_BROWSER_PROXY_BYPASS": "<-loopback>",
            "AGENT_BROWSER_ARGS": "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "AGENT_BROWSER_SCREENSHOT_FORMAT": "jpeg",
            "AGENT_BROWSER_SCREENSHOT_QUALITY": "72",
        })
        if self.headed:
            env["AGENT_BROWSER_HEADED"] = "true"
        if self.user_agent:
            env["AGENT_BROWSER_USER_AGENT"] = self.user_agent
        return env

    async def _run(self, *commands, timeout=60):
        """Run commands in one agent-browser batch: a list of (ok, result-or-error) per command."""
        proc = await asyncio.create_subprocess_exec(
            self.binary, "--json", "batch", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self._env())
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps([list(c) for c in commands]).encode()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise BrowserError("The browser took too long to answer.")
        try:
            steps = json.loads(out)
        except ValueError:
            raise BrowserError("The browser failed: " + (err.decode(errors="replace").strip()[-200:] or "no output"))
        return [(bool(s.get("success")), s.get("result") if s.get("success") else (s.get("error") or "failed"))
                for s in steps]

    async def _ensure(self):
        if self.ready:
            return
        self.binary = self.binary or _binary()
        if not self.binary:
            raise BrowserError(INSTALL)
        await self.proxy.start()
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        if not CONFIG.exists():
            CONFIG.write_text("{}\n")
        # A browser left over from an earlier run would still use that run's proxy.
        await self._run(["close"], timeout=20)
        # Headless Chrome says so in its user agent, and search engines then show no results.
        steps = await self._run(["open"], ["set", "viewport", "1280", "800"], ["eval", "navigator.userAgent"], timeout=60)
        agent = steps[-1][1].get("result") if steps and steps[-1][0] else None
        if isinstance(agent, str):
            self.user_agent = agent.replace("HeadlessChrome", "Chrome")
        self.ready = True

    async def close(self):
        try:
            if self.ready:
                await self._run(["close"], timeout=20)
        except Exception:
            pass
        self.ready = False
        await self.proxy.stop()

    def _shot_path(self):
        SHOTS.mkdir(parents=True, exist_ok=True)
        return SHOTS / f"page-{uuid.uuid4().hex[:10]}.jpg"

    # ---- a page, as the model sees it --------------------------------------------
    async def _act(self, *commands):
        """Do something to the page, then read it: a page view for the model."""
        started = time.time()
        shot = self._shot_path()
        steps = await self._run(
            *commands, ["eval", SETTLE], ["wait", "300"],
            ["eval", READ_PAGE], ["snapshot", "-i", "-c", "-u"], ["screenshot", str(shot)])
        acted, page, snap, shot_ok = steps[:len(commands)], steps[-3], steps[-2], steps[-1]
        shot_name = shot.name if shot_ok[0] and shot.exists() else None
        failed = next((r for ok, r in acted if not ok), None)
        info = page[1].get("result") if page[0] and isinstance(page[1], dict) else None
        if not isinstance(info, dict):
            self.elements = []
            return {"error": f"The page couldn't be read ({str(failed or page[1])[:160]}).", "_screenshot": shot_name}
        url, text = info.get("url") or "", (info.get("text") or "").strip()
        # The page itself was refused: the proxy's own page (http), or Chrome's error page after
        # a refused tunnel (https). A public page that merely embeds something local still loads.
        if text == REFUSAL or (url.startswith("chrome-error:") and any(t >= started for t, _ in netguard.refused)):
            self.elements = []
            return {"error": REFUSAL, "_screenshot": shot_name}
        if url.startswith("chrome-error:"):
            self.elements = []
            return {"error": "The page didn't load: " + _why(failed or info.get("title")), "_screenshot": shot_name}
        self.elements = elements_from(snap[1].get("snapshot", "") if snap[0] else "", url, info.get("links") or [])
        view = {"url": url, "title": info.get("title") or "", "text": info.get("text") or "(The page has no readable text.)",
                "text_cut_off": bool(info.get("more")),
                "elements": [describe(n, role, name, checked) for n, (_, role, name, checked) in enumerate(self.elements, 1)],
                "_screenshot": shot_name}
        if failed:
            view["error"] = f"That didn't work ({str(failed).splitlines()[0][:160]}). This is the page as it is now."
        return view

    def _element(self, number, fields_only=False):
        try:
            n = int(str(number).strip().lstrip("#"))
        except (TypeError, ValueError):
            raise BrowserError("Give the number of an element from the open page.")
        if not self.elements:
            raise BrowserError("No page is open. Open a page first.")
        if not 1 <= n <= len(self.elements):
            raise BrowserError(f"The open page has elements 1 to {len(self.elements)}.")
        ref, role, name, _ = self.elements[n - 1]
        self.target = f"{KINDS[role]} “{name}”" if name else KINDS[role]
        if fields_only and role not in FIELDS:
            fields = [str(i) for i, e in enumerate(self.elements, 1) if e[1] in FIELDS]
            raise BrowserError(f"Element {n} is a {KINDS[role]}, not a text box."
                               + (f" Text boxes: {', '.join(fields)}." if fields else " The page has no text boxes."))
        return "@" + ref

    # ---- tools ---------------------------------------------------------------------
    async def search(self, query):
        query = str(query or "").strip()
        if not query:
            return {"error": "Give a search query."}
        async with self.lock:
            await self._ensure()
            results, engine, shot = [], None, None
            for engine, url, script in [
                ("DuckDuckGo", "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query), DUCKDUCKGO),
                ("Bing", "https://www.bing.com/search?q=" + urllib.parse.quote(query), BING),
            ]:
                path = self._shot_path()
                steps = await self._run(["open", url], ["eval", script], ["screenshot", str(path)])
                found = steps[1][1].get("result") if steps[1][0] and isinstance(steps[1][1], dict) else None
                results = [r for r in (found or []) if str(r.get("url", "")).startswith("http")]
                shot = path.name if steps[2][0] and path.exists() else shot
                if results:
                    break
            self.results = results[:RESULT_LIMIT]
            self.elements = []
        if not self.results:
            return {"query": query, "error": "The search returned no results.", "_screenshot": shot}
        return {"query": query, "engine": engine,
                "results": [{"n": i + 1, "title": r["title"], "url": r["url"], "snippet": r["snippet"][:240]}
                            for i, r in enumerate(self.results)],
                "_screenshot": shot}

    async def open(self, url):
        target = str(url or "").strip()
        if re.fullmatch(r"#?\d{1,2}", target):  # a search result number
            n = int(target.lstrip("#"))
            if not 1 <= n <= len(self.results):
                return {"error": f"There's no search result {n}. Search first, or give a full address."}
            target = self.results[n - 1]["url"]
        if not re.match(r"^https?://", target, re.I):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) and not target.lower().startswith("www."):
                return {"error": "Only http and https addresses can be opened."}
            target = "https://" + target
        refused = await netguard.check(target)
        if refused:
            return {"url": target, "error": refused}
        async with self.lock:
            await self._ensure()
            return await self._act(["open", target])

    async def click(self, number):
        async with self.lock:
            try:
                ref = self._element(number)
            except BrowserError as exc:
                return {"error": str(exc)}
            await self._ensure()
            return {"clicked": self.target, **await self._act(["click", ref], ["wait", "600"])}

    async def type_text(self, number, text, submit=False):
        if isinstance(submit, str):
            submit = submit.strip().lower() in ("true", "yes", "1")
        async with self.lock:
            try:
                ref = self._element(number, fields_only=True)
            except BrowserError as exc:
                return {"error": str(exc)}
            await self._ensure()
            steps = [["fill", ref, str(text or "")]]
            if submit:
                steps += [["press", "Enter"], ["wait", "600"]]
            return {"typed_into": self.target, **await self._act(*steps)}

    async def back(self):
        async with self.lock:
            if not self.ready:
                return {"error": "No page is open."}
            return await self._act(["back"], ["wait", "300"])


browser = Browser()


async def _guarded(call):
    try:
        return await call
    except BrowserError as exc:
        return {"error": str(exc)}


async def web_search(query: str):
    return await _guarded(browser.search(query))


async def open_page(url: str):
    return await _guarded(browser.open(url))


async def click(number: int):
    return await _guarded(browser.click(number))


async def type_text(number: int, text: str, submit: bool = False):
    return await _guarded(browser.type_text(number, text, submit))


async def go_back():
    return await _guarded(browser.back())
