"""A headless Chrome the model drives through three tools: search, open a page, follow a link.

It runs in the app's own event loop (Playwright's async API), starts on first use and keeps
one tab. Each step saves a screenshot so the page can show what the model looked at. The
model gets text: search results, or a page's readable text plus its links, numbered.

It uses its own empty profile, so it's never signed in to anything, and it refuses
addresses on this Mac or the local network, so a web page can't steer the model into
reading local services.
"""

import asyncio
import ipaddress
import os
import re
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "data" / "outputs"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
TEXT_LIMIT = 3000
LINK_LIMIT = 15
RESULT_LIMIT = 5

READ_PAGE = """([textLimit, linkLimit]) => {
  // The article itself if the page marks it, else the main area, else the whole page.
  const pick = ['#mw-content-text', 'article', '[role=main]', 'main', '#content', '#main']
    .map(s => document.querySelector(s)).find(el => el && el.innerText.trim().length > 300);
  const root = pick || document.body;
  const text = (root.innerText || '').split('\\n').map(l => l.replace(/\\s+/g, ' ').trim())
    .filter(l => l.length > 1).join('\\n');
  const here = location.href.split('#')[0];
  const seen = new Set(), links = [];
  for (const a of root.querySelectorAll('a[href]')) {
    const label = (a.innerText || a.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    const href = a.href;
    if (label.length < 3 || !/^https?:/.test(href) || seen.has(href) || href.split('#')[0] === here) continue;
    const box = a.getBoundingClientRect();
    if (!box.width && !box.height) continue;
    seen.add(href);
    links.push({text: label.slice(0, 90), url: href});
    if (links.length >= linkLimit) break;
  }
  return {title: document.title, url: location.href, text: text.slice(0, textLimit),
          more: text.length > textLimit, links};
}"""

DUCKDUCKGO = """() => [...document.querySelectorAll('.result')].map(r => {
  if (r.classList.contains('result--ad')) return null;
  const a = r.querySelector('.result__a'), s = r.querySelector('.result__snippet');
  if (!a) return null;
  let url = a.href;
  try { const u = new URL(url); if (u.searchParams.get('uddg')) url = u.searchParams.get('uddg'); } catch (e) {}
  if (url.includes('duckduckgo.com/y.js')) return null;
  return {title: a.innerText.trim(), url, snippet: s ? s.innerText.trim() : ''};
}).filter(Boolean)"""

BING = """() => [...document.querySelectorAll('li.b_algo')].map(li => {
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


def is_local(host):
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


class Browser:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.playwright = self.browser = self.page = None
        self.results = []  # the last search's results, so the model can open one by number
        self.links = []    # the open page's links, so the model can follow one by number
        self.headed = os.environ.get("LFM3_BROWSER_HEADED") == "1"

    async def _ensure(self):
        if self.page is not None and not self.page.is_closed():
            return
        from playwright.async_api import async_playwright

        if self.playwright is None:
            self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.launch(channel="chrome", headless=not self.headed)
        except Exception:
            # No Google Chrome: fall back to Playwright's own Chromium (`playwright install chromium`).
            self.browser = await self.playwright.chromium.launch(headless=not self.headed)
        context = await self.browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                                 viewport={"width": 1280, "height": 800})
        await context.route("**/*", self._guard)
        self.page = await context.new_page()

    async def _guard(self, route):
        host = urllib.parse.urlsplit(route.request.url).hostname
        if is_local(host):
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    async def close(self):
        try:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
        self.playwright = self.browser = self.page = None

    async def _screenshot(self):
        name = f"page-{uuid.uuid4().hex[:10]}.jpg"
        SHOTS.mkdir(parents=True, exist_ok=True)
        try:
            await self.page.screenshot(path=SHOTS / name, type="jpeg", quality=72)
            return name
        except Exception:
            return None

    async def _goto(self, url):
        await self.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=2_500)
        except Exception:
            pass  # busy pages never go idle; what has loaded is enough to read

    # ---- tools -----------------------------------------------------------------
    async def search(self, query):
        query = str(query or "").strip()
        if not query:
            return {"error": "Give a search query."}
        async with self.lock:
            await self._ensure()
            results, engine = [], None
            for engine, url, script in [
                ("DuckDuckGo", "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query), DUCKDUCKGO),
                ("Bing", "https://www.bing.com/search?q=" + urllib.parse.quote(query), BING),
            ]:
                try:
                    await self._goto(url)
                    results = [r for r in await self.page.evaluate(script) if r["url"].startswith("http")]
                except Exception:
                    results = []
                if results:
                    break
            self.results = results[:RESULT_LIMIT]
            shot = await self._screenshot()
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
        if is_local(urllib.parse.urlsplit(target).hostname):
            return {"error": "Addresses on this Mac or the local network can't be opened."}
        return await self._read(target)

    async def follow(self, number):
        try:
            n = int(str(number).strip().lstrip("#"))
        except ValueError:
            return {"error": "Give the number of a link from the open page."}
        if not self.links:
            return {"error": "No page is open. Open a page first."}
        if not 1 <= n <= len(self.links):
            return {"error": f"The open page has links 1 to {len(self.links)}."}
        return await self._read(self.links[n - 1]["url"])

    async def _read(self, url):
        async with self.lock:
            await self._ensure()
            try:
                await self._goto(url)
            except Exception as exc:
                shot = await self._screenshot()
                return {"url": url, "error": f"The page didn't load ({str(exc).splitlines()[0][:120]}).",
                        "_screenshot": shot}
            page = await self.page.evaluate(READ_PAGE, [TEXT_LIMIT, LINK_LIMIT])
            self.links = page["links"]
            shot = await self._screenshot()
        return {"url": page["url"], "title": page["title"], "text": page["text"] or "(The page has no readable text.)",
                "text_cut_off": page["more"],
                "links": [{"n": i + 1, "text": link["text"]} for i, link in enumerate(page["links"])],
                "_screenshot": shot}


browser = Browser()


async def web_search(query: str):
    return await browser.search(query)


async def open_page(url: str):
    return await browser.open(url)


async def click_link(number: int):
    return await browser.follow(number)
