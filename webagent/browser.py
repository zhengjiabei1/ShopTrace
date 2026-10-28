"""Single-tab browser actions using refs from the latest DOM observation."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import posixpath
import re
import shutil
from typing import Any
from urllib.parse import unquote, urlsplit


class BrowserError(ValueError):
    """Browser action or scope error."""


def _origin(value: str, *, origin_only=False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password
                or (origin_only and (parsed.path not in {"", "/"} or parsed.query or parsed.fragment))):
            raise ValueError()
        hostname = parsed.hostname.lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port and port != (443 if parsed.scheme == "https" else 80):
            host += f":{port}"
        return f"{parsed.scheme}://{host}"
    except (AttributeError, TypeError, ValueError) as exc:
        raise BrowserError("Only credential-free HTTP(S) browser URLs are supported.") from None


def _normalized_path(value: str) -> str:
    path = value or "/"
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if "\x00" in path:
        raise BrowserError("Invalid navigation path.")
    return "/" + posixpath.normpath(path.replace("\\", "/")).lstrip("/")


_OBSERVE = r"""({prefix, maximum, textMaximum}) => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const clip = (value, maximum) => Array.from(String(value || '')).slice(0, maximum).join('');
  const visible = node => {
    if (!node || node.closest('[hidden],[inert]')) return false;
    if (typeof node.checkVisibility === 'function') {
      return node.checkVisibility({checkOpacity:true, checkVisibilityCSS:true});
    }
    const style = getComputedStyle(node), box = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      style.visibility !== 'collapse' && style.opacity !== '0' && box.width > 0 && box.height > 0;
  };
  const label = node => {
    const labelledby = node.getAttribute('aria-labelledby');
    if (labelledby) {
      const text = clean(labelledby.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' '));
      if (text) return text;
    }
    const aria = clean(node.getAttribute('aria-label')); if (aria) return aria;
    const labels = clean(Array.from(node.labels || []).filter(visible).map(x => x.innerText).join(' '));
    if (labels) return labels;
    const text = clean(node.innerText); if (text) return text;
    if (['submit','button','reset'].includes(node.type) && node.value) return clean(node.value);
    return clean(node.getAttribute('placeholder') || node.getAttribute('title') || node.getAttribute('name') || node.id || node.tagName.toLowerCase());
  };
  document.querySelectorAll('[data-shoptrace-ref]').forEach(node => node.removeAttribute('data-shoptrace-ref'));
  const selector = 'a[href],button,input:not([type="hidden"]):not([type="file"]):not([type="password"]),textarea,select,summary,[role="button"],[role="link"],[role="checkbox"],[role="radio"],[role="combobox"],[contenteditable="true"],[tabindex]';
  const nodes = Array.from(document.querySelectorAll(selector)).filter(node =>
    visible(node) && !(node.tagName === 'INPUT' && ['hidden','file','password'].includes(node.type)));
  const elements = nodes.slice(0, maximum).map((node, i) => {
    const tag = node.tagName.toLowerCase(), ref = `${prefix}-e${i + 1}`;
    node.setAttribute('data-shoptrace-ref', ref);
    let role = node.getAttribute('role');
    if (!role) role = tag === 'a' ? 'link' : ['button','summary'].includes(tag) ? 'button' :
      tag === 'select' ? 'combobox' : ['checkbox','radio'].includes(node.type) ? node.type :
      ['input','textarea'].includes(tag) || node.isContentEditable ? 'textbox' : tag;
    const result = {ref, tag, role, name:clip(label(node), 300),
      disabled:!!node.disabled || node.getAttribute('aria-disabled') === 'true'};
    if (['input','textarea','select'].includes(tag)) result.value = clip(node.value, 1000);
    if (tag === 'input') result.input_type = node.type;
    if (tag === 'select') {
      result.options = Array.from(node.options).slice(0, 100).map(option => ({value:clip(option.value,500), label:clip(clean(option.label),300), disabled:option.disabled}));
      result.options_truncated = node.options.length > 100;
    }
    if (tag === 'a') result.href = node.href;
    const surrounding = node.closest('article,tr,[role="listitem"],fieldset');
    if (surrounding) result.context = clip(clean(surrounding.innerText), 700);
    if (node.getAttribute('aria-expanded')) result.expanded = node.getAttribute('aria-expanded');
    if (typeof node.checked === 'boolean' && ['checkbox','radio'].includes(node.type)) result.checked = node.checked;
    return result;
  });
  const text = document.body?.innerText || '';
  const textCharacters = Array.from(text).length;
  const loading = [...document.querySelectorAll('[aria-busy="true"]')].some(visible);
  return {title:document.title, text:clip(text,textMaximum), elements, loading,
    truncated:textCharacters > textMaximum || nodes.length > maximum || elements.some(element => element.options_truncated),
    text_characters:textCharacters, total_interactive_elements:nodes.length};
}"""


class BrowserSession:
    def __init__(self, start_url: str, artifact_dir: Path, *, allowed_origins: list[str],
                 allowed_path_prefixes: dict[str, list[str]] | None = None,
                 executable_path: str | None = None, headless=True, read_only: bool = False):
        if not allowed_origins:
            raise BrowserError("At least one explicit allowed origin is required.")
        self.allowed_origins = {_origin(origin, origin_only=True) for origin in allowed_origins}
        self.allowed_path_prefixes: dict[str, tuple[str, ...]] = {}
        for origin in self.allowed_origins:
            default = ["/sandbox/"] if _origin(start_url) == origin and urlsplit(start_url).path.startswith("/sandbox") else ["/"]
            candidates = next((prefixes for name, prefixes in (allowed_path_prefixes or {}).items()
                               if _origin(name, origin_only=True) == origin), default)
            if not isinstance(candidates, list) or not candidates or any(not isinstance(p, str) or not p.startswith("/") or "?" in p or "#" in p for p in candidates):
                raise BrowserError("Allowed path prefixes must be nonempty absolute paths.")
            self.allowed_path_prefixes[origin] = tuple(_normalized_path(prefix).rstrip("/") or "/" for prefix in candidates)
        self.start_url = start_url
        if not self._allowed(start_url):
            raise BrowserError("The start URL is outside the declared browser scope.")
        self.artifact_dir = Path(artifact_dir).resolve()
        self.executable_path = executable_path or os.environ.get("WEBAGENT_CHROMIUM_PATH") or shutil.which("chromium")
        self.headless = bool(headless)
        self.read_only = bool(read_only)
        self.page = None
        self._playwright = self._browser = self._context = None
        self._closed = False
        self._started = False
        self._counter = 0
        self._refs: dict[str, dict[str, Any]] = {}
        self._handles: dict[str, Any] = {}
        self._background: set[asyncio.Task] = set()
        self._blocked_requests = 0
        self._blocked_actions = 0
        self._last_allowed_url = start_url

    def _allowed(self, value: str, *, websocket=False) -> bool:
        try:
            if websocket:
                parsed = urlsplit(value)
                if parsed.scheme not in {"ws", "wss"}:
                    return False
                value = ("https" if parsed.scheme == "wss" else "http") + value[value.index(":"):]
            origin = _origin(value)
            if origin not in self.allowed_origins:
                return False
            path = _normalized_path(urlsplit(value).path)
            return any(prefix == "/" or path == prefix or path.startswith(prefix + "/")
                       for prefix in self.allowed_path_prefixes[origin])
        except (BrowserError, ValueError, TypeError):
            return False

    def _schedule(self, awaitable):
        task = asyncio.create_task(awaitable)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _route(self, route):
        request = route.request
        if not self._allowed(request.url) or (self.read_only and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}):
            self._blocked_requests += 1
            if request.is_navigation_request() and self.page is not None and request.frame == self.page.main_frame:
                self._blocked_actions += 1
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    async def _websocket(self, route):
        if not self.read_only and self._allowed(route.url, websocket=True):
            route.connect_to_server()
        else:
            self._blocked_requests += 1
            await route.close(code=1008, reason="Outside declared browser scope")

    async def _close_popup(self, page):
        self._blocked_actions += 1
        try:
            await page.close()
        except Exception:
            pass

    async def _cancel_download(self, download):
        self._blocked_actions += 1
        try:
            await download.cancel()
        except Exception:
            pass

    async def _cancel_filechooser(self, chooser):
        self._blocked_actions += 1
        try:
            await chooser.set_files([])
        except Exception:
            pass

    async def _guard_frame(self, frame):
        if self._closed or frame.url == "about:blank" or self._allowed(frame.url):
            return
        self._blocked_actions += 1
        if self.page is not None and frame != self.page.main_frame:
            try:
                element = await frame.frame_element()
                await element.evaluate("node => node.remove()")
            except Exception:
                pass

    def _proxy(self):
        address = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
        if not address:
            return None
        parsed = urlsplit(address)
        if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
            return None
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port:
            host += f":{parsed.port}"
        proxy = {"server": f"{parsed.scheme}://{host}",
                 "bypass": ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "localhost", "127.0.0.1", "[::1]"]))}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
        if parsed.password:
            proxy["password"] = unquote(parsed.password)
        return proxy

    async def start(self) -> dict:
        if self._closed or self._started:
            raise BrowserError("Create a new browser session for each run.")
        self._started = True
        try:
            from playwright.async_api import async_playwright
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            self._playwright = await async_playwright().start()
            env_vars = os.environ.copy()
            for name in ("LLM_API_KEY", "OPENAI_API_KEY"):
                env_vars.pop(name, None)
            options: dict[str, Any] = {"headless": self.headless, "args": ["--disable-dev-shm-usage"],
                                       "env": env_vars}
            if self.executable_path:
                options["executable_path"] = self.executable_path
            proxy = self._proxy()
            if proxy:
                options["proxy"] = proxy
            self._browser = await self._playwright.chromium.launch(**options)
            self._context = await self._browser.new_context(viewport={"width": 1280, "height": 900},
                locale="en-US", accept_downloads=False, service_workers="block")
            self._context.set_default_timeout(6000)
            self._context.set_default_navigation_timeout(15000)
            await self._context.route("**/*", self._route)
            await self._context.route_web_socket("**/*", self._websocket)
            self.page = await self._context.new_page()
            self._context.on("page", lambda page: self._schedule(self._close_popup(page)))
            self.page.on("download", lambda download: self._schedule(self._cancel_download(download)))
            self.page.on("filechooser", lambda chooser: self._schedule(self._cancel_filechooser(chooser)))
            self.page.on("framenavigated", lambda frame: self._schedule(self._guard_frame(frame)))
            await self.page.goto(self.start_url, wait_until="domcontentloaded")
            return await self.observe()
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise
        except Exception as exc:
            await self.close()
            if isinstance(exc, BrowserError):
                raise
            diagnostic = str(exc)
            if re.search(r"\b(?:ERR_CERT_[A-Z0-9_]+|CERTIFICATE_VERIFY_FAILED)\b", diagnostic):
                raise BrowserError("Browser TLS certificate validation failed; configure Chromium to trust the session proxy CA and keep TLS verification enabled.") from None
            if "ERR_TUNNEL_CONNECTION_FAILED" in diagnostic:
                raise BrowserError("Browser proxy tunnel failed; check the configured proxy and network access.") from None
            raise BrowserError("Browser startup failed; check Playwright, Chromium, and the task server.") from None

    def _require_open(self):
        if self._closed or not self._started or self.page is None or self.page.is_closed():
            raise BrowserError("The browser session is not open.")

    async def observe(self) -> dict:
        self._require_open()
        if not self._allowed(self.page.url):
            raise BrowserError("The current document is outside the declared browser scope.")
        self._counter += 1
        identifier = f"obs-{self._counter:04d}"
        try:
            from playwright.async_api import TimeoutError as WaitError
            try:
                await self.page.wait_for_function("""() => ![...document.querySelectorAll('[aria-busy="true"]')]
                    .some(node => !node.closest('[hidden],[inert]') && node.getClientRects().length > 0)""", timeout=1500)
            except WaitError:
                pass
            observed = await self.page.evaluate(_OBSERVE, {"prefix": identifier, "maximum": 160, "textMaximum": 12000})
            for handle in self._handles.values():
                try:
                    await handle.dispose()
                except Exception:
                    pass
            self._handles = {}
            bundle = await self.page.evaluate_handle("refs => Object.fromEntries(refs.map(ref => [ref, document.querySelector('[data-shoptrace-ref=\"' + ref + '\"]')]))", [row["ref"] for row in observed["elements"]])
            properties = await bundle.get_properties()
            for ref, handle in properties.items():
                element = handle.as_element()
                if element is not None:
                    self._handles[ref] = element
                else:
                    await handle.dispose()
            await bundle.dispose()
            self._refs = {row["ref"]: row for row in observed["elements"]}
            observed.update(observation_id=identifier, url=self.page.url,
                            read_only=self.read_only,
                            blocked_requests=self._blocked_requests,
                            screenshot=await self.screenshot(identifier + ".png"))
            self._last_allowed_url = self.page.url
            return observed
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise
        except Exception as exc:
            self._refs = {}
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError("The browser document could not be observed.") from None

    async def _target(self, ref):
        if not isinstance(ref, str) or ref not in self._refs:
            raise BrowserError("Use an element reference from the latest observation.")
        metadata = self._refs[ref]
        locator = self._handles.get(ref)
        if locator is None or not await locator.evaluate("node => node.isConnected") or not await locator.is_visible():
            raise BrowserError("The observed element is no longer visible; observe again.")
        if metadata.get("disabled") or not await locator.is_enabled():
            raise BrowserError("The observed element is disabled.")
        return locator, metadata

    async def _guard_click(self, locator):
        target = await locator.evaluate("node => ({href:node.closest('a[href]')?.href || null, target:node.closest('a[href]')?.target || '', download:node.closest('a[download]') !== null, formAction:node.form?.action || null, submit:node.type === 'submit'})")
        if target["download"]:
            raise BrowserError("Downloads are outside this browser action interface.")
        if target["target"] and target["target"] not in {"_self", "_top", "_parent"}:
            raise BrowserError("New browser tabs are outside this single-tab session.")
        if target["href"] and not self._allowed(target["href"]):
            raise BrowserError("The link is outside the declared browser scope.")
        if target["submit"] and target["formAction"] and not self._allowed(target["formAction"]):
            raise BrowserError("The form target is outside the declared browser scope.")

    async def act(self, action: dict) -> dict:
        self._require_open()
        fields = {"click": {"type", "ref"}, "fill": {"type", "ref", "text"},
                  "select": {"type", "ref", "value"}, "press": {"type", "ref", "key"},
                  "scroll": {"type", "direction"}, "back": {"type"}, "wait": {"type", "milliseconds"}}
        if not isinstance(action, dict) or not isinstance(action.get("type"), str) or action["type"] not in fields or set(action) != fields[action["type"]]:
            raise BrowserError("Browser action fields do not match the closed action schema.")
        kind = action["type"]
        locator = metadata = None
        before = self._blocked_actions
        try:
            if "ref" in action:
                locator, metadata = await self._target(action["ref"])
            if kind == "click":
                await self._guard_click(locator)
                await locator.click()
            elif kind == "fill":
                if not isinstance(action["text"], str) or len(action["text"]) > 2000:
                    raise BrowserError("Fill text must be a string of at most 2000 characters.")
                if metadata["tag"] not in {"input", "textarea"} and metadata["role"] != "textbox":
                    raise BrowserError("Only observed text fields can be filled.")
                if metadata.get("input_type") in {"file", "password", "hidden"}:
                    raise BrowserError("This input type is outside the action interface.")
                await locator.fill(action["text"])
            elif kind == "select":
                if not isinstance(action["value"], str) or len(action["value"]) > 500:
                    raise BrowserError("Select values must be bounded strings.")
                if metadata["tag"] != "select" or not any(option["value"] == action["value"] and not option["disabled"] for option in metadata.get("options", [])):
                    raise BrowserError("Select a currently observed, enabled option.")
                await locator.select_option(action["value"])
            elif kind == "press":
                if not isinstance(action["key"], str) or action["key"] not in {"Enter", "Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Space"}:
                    raise BrowserError("This keyboard shortcut is not supported.")
                if action["key"] == "Enter":
                    await self._guard_click(locator)
                    form_url = await locator.evaluate("node => node.form?.action || null")
                    if form_url and not self._allowed(form_url):
                        raise BrowserError("The form target is outside the declared browser scope.")
                await locator.press(action["key"])
            elif kind == "scroll":
                if not isinstance(action["direction"], str) or action["direction"] not in {"up", "down"}:
                    raise BrowserError("Scroll direction must be up or down.")
                await self.page.evaluate("direction => window.scrollBy({top:direction * window.innerHeight * .8, behavior:'instant'})", 1 if action["direction"] == "down" else -1)
            elif kind == "back":
                await self.page.go_back(wait_until="domcontentloaded")
            elif kind == "wait":
                wait_ms = action["milliseconds"]
                if type(wait_ms) is not int or not 0 <= wait_ms <= 2000:
                    raise BrowserError("Wait must be an integer between 0 and 2000 milliseconds.")
                await asyncio.sleep(wait_ms / 1000)
            await asyncio.sleep(0.04)
            if self._blocked_actions != before or not self._allowed(self.page.url):
                if not self._allowed(self.page.url):
                    await self.page.goto(self._last_allowed_url, wait_until="domcontentloaded")
                raise BrowserError("The action attempted an out-of-scope navigation, popup, or download.")
            return await self.observe()
        except asyncio.CancelledError:
            await asyncio.shield(self.close())
            raise
        except BrowserError:
            raise
        except Exception:
            if self.page is not None and not self.page.is_closed() and not self._allowed(self.page.url):
                try:
                    await self.page.goto(self._last_allowed_url, wait_until="domcontentloaded")
                except Exception:
                    pass
            raise BrowserError("The browser action failed; no automatic action retry was performed.") from None

    async def screenshot(self, name: str) -> str:
        self._require_open()
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,110}\.png", name):
            raise BrowserError("Screenshot names must be local PNG filenames.")
        dest = self.artifact_dir / name
        if dest.exists() or dest.is_symlink():
            raise BrowserError("Refusing to overwrite an existing screenshot.")
        try:
            await self.page.screenshot(path=str(dest), animations="disabled", caret="hide", full_page=False)
            return name
        except Exception:
            raise BrowserError("The browser screenshot could not be saved.") from None

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._refs = {}
        self._handles = {}
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    await asyncio.wait_for(resource.close(), timeout=5)
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=5)
            except Exception:
                pass
        if self._background:
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*list(self._background), return_exceptions=True)
