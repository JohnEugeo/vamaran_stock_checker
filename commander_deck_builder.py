"""Vamaren Stock Checker — dark mode, image/name toggle, deck URL import."""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import asyncio
import json
import os
import sys
import time
import webbrowser
import re
import queue
import unicodedata
import urllib.parse
import urllib.request
from collections import Counter
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageTk, ImageDraw
import aiohttp

# ----------------------------- Config ---------------------------------------

MAX_CARDS = 100
CONCURRENCY_LIMIT = 6
SCRYFALL_CARD = "https://api.scryfall.com/cards/named?fuzzy="
STORE_BASE = "https://vamaren.tcgplayerpro.com"
# Same JSON endpoint the storefront's search page uses. Returns
# products.totalItems, which matches the visible "N results for" count
# (in-stock items only, since the site defaults to the Available filter).
STORE_API = f"{STORE_BASE}/api/catalog/search"
# Max store listings to scan per card when verifying an exact name match
STOCK_SCAN_LIMIT = 250

# The store's bot protection 403s non-browser user agents.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
STORE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/json, text/plain, */*",
    "Referer": f"{STORE_BASE}/search/products",
    "Origin": STORE_BASE,
}

APP_TITLE = "Vamaren Stock Checker"
APP_VERSION = "1.0.0"
REPO_URL = "https://github.com/JohnEugeo/vamaran_stock_checker"
VERSION_URL = ("https://raw.githubusercontent.com/JohnEugeo/"
               "vamaran_stock_checker/main/VERSION")
UPDATE_INTERVAL_S = 24 * 60 * 60  # check once a day

IS_FROZEN = getattr(sys, "frozen", False)  # running as a packaged .exe


def _data_dir() -> Path:
    """Writable directory for cache/settings (survives exe restarts)."""
    if IS_FROZEN:
        d = Path(os.environ.get("LOCALAPPDATA",
                                Path.home())) / "VamarenStockChecker"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path(__file__).parent


def _bundled(name: str) -> Path:
    """Path of a resource bundled inside the exe (read-only)."""
    if IS_FROZEN and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / name
    return Path(__file__).with_name(name)


UPDATE_STAMP_FILE = _data_dir() / ".last_update_check"
LOGO_FILE = _data_dir() / "vamaren_logo.png"
BUNDLED_LOGO = _bundled("vamaren_logo.png")
# Official store logo (fallback download if the local file is missing)
LOGO_URL = ("https://storefronts-assets.tcgplayer.com/media/"
            "41efd73f-64c7-475f-ab12-3567c58e6c51/"
            "46c91cb9-cfc8-4daa-b317-c3123015fa0b/Vamaren_1.png")
LOGO_SIZE = 44

CARD_W, CARD_H = 160, 223
HOVER_W, HOVER_H = 300, 419

# Dark palette
C = {
    "bg":        "#1e1e1e",
    "panel":     "#252526",
    "card":      "#2d2d30",
    "border":    "#3e3e42",
    "text":      "#d4d4d4",
    "muted":     "#9d9d9d",
    "accent":    "#0e639c",
    "accent_hi": "#1177bb",
    "danger":    "#a1260d",
    "danger_hi": "#c93b1c",
    "green":     "#2ea043",
    "red":       "#da3633",
    "input_bg":  "#1b1b1c",
}

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)


def store_search_url(name: str) -> str:
    q = urllib.parse.quote_plus(name)
    return f"{STORE_BASE}/search/products?q={q}&productTypeName=Cards"


def parse_version(s: str) -> tuple:
    """'1.2.3' -> (1, 2, 3) for comparison. Tolerates 'v' prefixes etc."""
    nums = re.findall(r"\d+", s or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def normalize_card_name(name: str) -> str:
    """Case/accent/whitespace-insensitive form for comparing card names."""
    name = unicodedata.normalize("NFKD", name)
    name = "".join(ch for ch in name if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", name).strip().casefold()


def strip_listing_suffixes(product_name: str) -> str:
    """Remove trailing parenthesized suffixes from a store listing name.

    "Sol Ring (252)" -> "Sol Ring"
    "Lightning Bolt (2289) (Rainbow Foil)" -> "Lightning Bolt"
    """
    prev = None
    while prev != product_name:
        prev = product_name
        product_name = re.sub(r"\s*\([^()]*\)\s*$", "", product_name)
    return product_name.strip()


def normalize_set_name(s: str) -> str:
    """Loose form for comparing set names between TCGplayer and Scryfall.

    e.g. store "Universes Beyond: Warhammer 40,000" should match
    Scryfall "Warhammer 40,000 Commander".
    """
    s = s.casefold()
    for junk in ("universes beyond", "magic: the gathering", "commander"):
        s = s.replace(junk, " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return " ".join(s.split())


def set_names_match(a: str, b: str) -> bool:
    """True if two set names loosely refer to the same set."""
    if not a or not b:
        return False
    na, nb = normalize_set_name(a), normalize_set_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


# ----------------------------- Decklist parsing -----------------------------

def parse_decklist_line(line: str):
    """Parse one decklist line -> dict or None (for comments/headers/blanks)."""
    line = line.strip()
    if not line or line.startswith(("//", "#")):
        return None
    if line.lower() in ("deck", "sideboard", "commander", "maybeboard"):
        return None

    qty, foil, set_code = 1, False, None

    m = re.match(r"^(\d+)\s*[xX]?\s+(.+)$", line)
    if m:
        qty = int(m.group(1))
        rest = m.group(2)
    else:
        rest = line

    if re.search(r"\bfoil\b", rest, re.IGNORECASE):
        foil = True
        rest = re.sub(r"\bfoil\b", "", rest, flags=re.IGNORECASE).strip()

    m = re.search(r"\(([^)]*)\)", rest)
    if m:
        set_code = m.group(1)
        rest = re.sub(r"\([^)]*\)", "", rest).strip()

    # Strip trailing collector numbers ("Sol Ring (CMM) 464")
    rest = re.sub(r"\s+\d+\s*$", "", rest).strip()
    if not rest:
        return None
    return {"name": rest, "qty": max(1, qty), "set_code": set_code, "foil": foil}


# ----------------------------- Deck URL import ------------------------------

class DeckImportError(Exception):
    """User-facing error for deck URL imports."""


IMPORT_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _http_text(session, url):
    """GET a URL, return (status, text)."""
    async with session.get(url, headers=IMPORT_HEADERS,
                           timeout=aiohttp.ClientTimeout(total=25)) as r:
        return r.status, await r.text()


async def _browser_fetch(url, wait_ms=2500):
    """Fetch a page with headless Chromium (passes most bot checks).

    Returns (status, body_text, html).
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise DeckImportError(
            "Importing from this site needs the 'playwright' package:\n"
            "pip install playwright && playwright install chromium")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            ctx = await browser.new_context(user_agent=BROWSER_UA)
            page = await ctx.new_page()
            resp = await page.goto(url, timeout=30000,
                                   wait_until="domcontentloaded")
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            body = await page.inner_text("body")
            html = await page.content()
            return (resp.status if resp else 0), body, html
        finally:
            await browser.close()


def _looks_like_decklist(text):
    return bool(re.search(r"^\s*\d+\s*[xX]?\s+\S", text, re.M))


async def _import_moxfield(session, url):
    m = re.search(r"moxfield\.com/decks/([\w-]+)", url)
    if not m:
        raise DeckImportError("Could not find a deck id in that Moxfield URL.")
    api = f"https://api2.moxfield.com/v2/decks/all/{m.group(1)}"
    # Moxfield's API blocks plain HTTP clients; go through the browser.
    status, body, _ = await _browser_fetch(api, wait_ms=500)
    if status == 404:
        raise DeckImportError("Moxfield deck not found (is it private?).")
    try:
        data = json.loads(body)
    except ValueError:
        raise DeckImportError("Moxfield returned an unexpected response.")
    lines = []
    for board in ("commanders", "companions", "mainboard"):
        for name, info in data.get(board, {}).items():
            lines.append(f"{info.get('quantity', 1)} {name}")
    if not lines:
        raise DeckImportError("That Moxfield deck appears to be empty.")
    return "\n".join(lines)


async def _import_archidekt(session, url):
    m = re.search(r"archidekt\.com/decks/(\d+)", url)
    if not m:
        raise DeckImportError("Could not find a deck id in that Archidekt URL.")
    status, text = await _http_text(
        session, f"https://archidekt.com/api/decks/{m.group(1)}/")
    if status == 404:
        raise DeckImportError("Archidekt deck not found (is it private?).")
    if status != 200:
        raise DeckImportError(f"Archidekt returned HTTP {status}.")
    data = json.loads(text)
    skip = {"sideboard", "maybeboard", "considering", "wishlist"}
    lines = []
    for c in data.get("cards", []):
        cats = {str(x).casefold() for x in (c.get("categories") or [])}
        if cats & skip:
            continue
        name = (c.get("card", {}).get("oracleCard", {}) or {}).get("name")
        if name:
            lines.append(f"{c.get('quantity', 1)} {name}")
    if not lines:
        raise DeckImportError("That Archidekt deck appears to be empty.")
    return "\n".join(lines)


async def _import_mtggoldfish(session, url):
    m = re.search(r"mtggoldfish\.com/deck/(\d+)", url)
    deck_id = m.group(1) if m else None
    if not deck_id:
        # Archetype pages embed a /deck/download/<id> link
        status, html = await _http_text(session, url)
        if status != 200:
            raise DeckImportError(f"MTGGoldfish returned HTTP {status}.")
        m = re.search(r"/deck/download/(\d+)", html)
        if not m:
            raise DeckImportError(
                "Could not find a decklist on that MTGGoldfish page.")
        deck_id = m.group(1)
    status, text = await _http_text(
        session, f"https://www.mtggoldfish.com/deck/download/{deck_id}")
    if status != 200 or not _looks_like_decklist(text):
        raise DeckImportError("Could not download that MTGGoldfish deck.")
    return text.strip()


async def _import_aetherhub(session, url):
    if "aetherhub.com/Deck/" not in url and "aetherhub.com/deck/" not in url:
        raise DeckImportError("That does not look like an Aetherhub deck URL.")
    status, _, html = await _browser_fetch(url, wait_ms=3500)
    if status != 200:
        raise DeckImportError(f"Aetherhub returned HTTP {status}.")
    # The visual tab lists one card link per copy
    m = re.search(r'id="tab_visual_\d+"(.*?)(?:<div class="tab-pane|$)',
                  html, re.S)
    if not m:
        raise DeckImportError("Could not find the decklist on that page.")
    names = re.findall(r'data-card-name="([^"]+)"', m.group(1))
    if not names:
        raise DeckImportError("That Aetherhub deck appears to be empty.")
    return "\n".join(f"{qty} {name}" for name, qty in Counter(names).items())


async def _import_tappedout(session, url):
    base = url.split("?")[0].rstrip("/")
    txt_url = base + "/?fmt=txt"
    status, text = await _http_text(session, txt_url)
    if status != 200 or not _looks_like_decklist(text):
        status, text, _ = await _browser_fetch(txt_url)
    if status == 200 and _looks_like_decklist(text):
        return text.strip()
    raise DeckImportError(
        "TappedOut is blocking automated access right now.\n"
        "Open the deck, use Export > Text, and paste the list instead.")


async def _import_deckstats(session, url):
    for candidate in (url + ("&" if "?" in url else "?") + "export_txt=1",):
        status, text = await _http_text(session, candidate)
        if status == 200 and _looks_like_decklist(text):
            return text.strip()
        status, text, _ = await _browser_fetch(candidate)
        if status == 200 and _looks_like_decklist(text):
            return text.strip()
    raise DeckImportError(
        "Deckstats is blocking automated access right now.\n"
        "Open the deck, use Export, and paste the list instead.")


DECK_IMPORTERS = {
    "moxfield.com": _import_moxfield,
    "archidekt.com": _import_archidekt,
    "mtggoldfish.com": _import_mtggoldfish,
    "aetherhub.com": _import_aetherhub,
    "tappedout.net": _import_tappedout,
    "deckstats.net": _import_deckstats,
}


async def import_deck_from_url(url):
    """Fetch a decklist (text) from a supported deck site URL."""
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    host = urllib.parse.urlparse(url).netloc.casefold()
    for domain, importer in DECK_IMPORTERS.items():
        if host == domain or host.endswith("." + domain):
            async with aiohttp.ClientSession() as session:
                return await importer(session, url)
    raise DeckImportError(
        "Unsupported site. Supported: " + ", ".join(sorted(DECK_IMPORTERS)))


# ----------------------------- Hover preview --------------------------------

class HoverPreview:
    """Delayed hover popup rendered from in-memory image bytes (no network)."""

    def __init__(self, widget, image_bytes):
        self.widget = widget
        self.image_bytes = image_bytes
        self.popup = None
        self.photo = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _e=None):
        self._cancel()
        self._after_id = self.widget.after(350, self._show)

    def _cancel(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _show(self):
        if self.popup or not self.image_bytes:
            return
        try:
            img = Image.open(BytesIO(self.image_bytes)).resize(
                (HOVER_W, HOVER_H), Image.LANCZOS)
            self.photo = ImageTk.PhotoImage(img)
            self.popup = tk.Toplevel(self.widget)
            self.popup.overrideredirect(True)
            self.popup.attributes("-topmost", True)
            x = self.widget.winfo_pointerx() + 16
            y = self.widget.winfo_pointery() + 12
            # Keep on screen
            sw = self.widget.winfo_screenwidth()
            sh = self.widget.winfo_screenheight()
            x = min(x, sw - HOVER_W - 10)
            y = min(y, sh - HOVER_H - 10)
            self.popup.geometry(f"+{x}+{y}")
            tk.Label(self.popup, image=self.photo, bd=1,
                     relief="solid", bg=C["border"]).pack()
        except Exception:
            self._hide()

    def _hide(self, _e=None):
        self._cancel()
        if self.popup:
            try:
                self.popup.destroy()
            except tk.TclError:
                pass
            self.popup = None
            self.photo = None


# ----------------------------- Styled widgets -------------------------------

def flat_button(parent, text, command, bg, hover_bg):
    btn = tk.Button(parent, text=text, command=command, bg=bg, fg="white",
                    activebackground=hover_bg, activeforeground="white",
                    relief="flat", bd=0, padx=14, pady=5, font=FONT_BOLD,
                    cursor="hand2")
    btn.bind("<Enter>", lambda e: btn.config(bg=hover_bg))
    btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn


# ----------------------------- Main app -------------------------------------

class CommanderApp:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.configure(bg=C["bg"])

        self.deck_cards = []
        self.cancel_event = threading.Event()
        self.build_thread = None
        self.import_thread = None
        self.cart_thread = None
        self.update_thread = None
        self.msg_queue = queue.Queue()
        self.show_images = tk.BooleanVar(value=True)
        self.import_mode = tk.BooleanVar(value=False)
        self._logo_photo = None
        self._resize_after = None
        self._render_after = None
        self._last_per_row = None

        self._init_style()
        self._create_gui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._process_queue)
        self.root.after(3000, self._auto_update_check)

    # ---- Style / GUI ----

    def _init_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Dark.Horizontal.TProgressbar",
                        troughcolor=C["panel"], bordercolor=C["border"],
                        background=C["accent_hi"], lightcolor=C["accent_hi"],
                        darkcolor=C["accent_hi"], thickness=8)

    def _create_gui(self):
        # ---- Header: logo + app title ----
        header = tk.Frame(self.root, bg=C["panel"], padx=12, pady=8)
        header.pack(fill="x")

        self.logo_label = tk.Label(header, bg=C["panel"])
        self.logo_label.pack(side="left", padx=(0, 10))
        self._load_logo()

        tk.Label(header, text=APP_TITLE, font=("Segoe UI", 16, "bold"),
                 bg=C["panel"], fg="#ffffff").pack(side="left")
        tk.Label(header, text=f"v{APP_VERSION}  ·  powered by Vamaren TCG",
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"], padx=10,
                 ).pack(side="left", pady=(6, 0))

        self.update_btn = flat_button(header, "Check for Updates",
                                      lambda: self.check_for_updates(True),
                                      C["card"], C["border"])
        self.update_btn.pack(side="right")

        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

        top = tk.Frame(self.root, bg=C["panel"], padx=12, pady=10)
        top.pack(fill="x")

        label_row = tk.Frame(top, bg=C["panel"])
        label_row.pack(fill="x")
        tk.Label(label_row, text="Paste decklist  (e.g.  1 Sol Ring)",
                 font=FONT_BOLD, bg=C["panel"], fg=C["text"]).pack(side="left")
        tk.Checkbutton(
            label_row, text="Import deck list", variable=self.import_mode,
            command=self._toggle_import_row, bg=C["panel"], fg=C["text"],
            activebackground=C["panel"], activeforeground=C["text"],
            selectcolor=C["input_bg"], font=FONT_SMALL, cursor="hand2"
        ).pack(side="right")

        # ---- Deck URL import row (hidden until toggled) ----
        self.import_frame = tk.Frame(top, bg=C["panel"])
        self.url_entry = tk.Entry(
            self.import_frame, font=FONT, bg=C["input_bg"], fg=C["text"],
            insertbackground=C["text"], relief="flat",
            highlightthickness=1, highlightbackground=C["border"],
            highlightcolor=C["accent"])
        self.url_entry.pack(side="left", fill="x", expand=True,
                            ipady=5, padx=(0, 8))
        self.url_entry.bind("<Return>", lambda e: self.start_import())
        self.import_btn = flat_button(self.import_frame, "Import",
                                      self.start_import,
                                      C["accent"], C["accent_hi"])
        self.import_btn.pack(side="left")
        tk.Label(self.import_frame,
                 text="Moxfield, Archidekt, MTGGoldfish, Aetherhub, …",
                 font=FONT_SMALL, bg=C["panel"], fg=C["muted"]
                 ).pack(side="left", padx=10)

        self.decklist_text = tk.Text(
            top, height=9, font=FONT, bg=C["input_bg"], fg=C["text"],
            insertbackground=C["text"], relief="flat", bd=6,
            selectbackground=C["accent"], undo=True)
        self.decklist_text.pack(fill="x", pady=(6, 8))

        bar = tk.Frame(top, bg=C["panel"])
        bar.pack(fill="x")

        self.build_btn = flat_button(bar, "Check Stock", self.start_build,
                                     C["accent"], C["accent_hi"])
        self.build_btn.pack(side="left")

        self.cancel_btn = flat_button(bar, "Cancel", self.cancel_build,
                                      C["danger"], C["danger_hi"])
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.cart_btn = flat_button(bar, "Add to Cart",
                                    self.start_add_to_cart,
                                    C["green"], "#3fb950")
        self.cart_btn.pack(side="left", padx=(8, 0))

        self.select_all_btn = flat_button(bar, "Select All",
                                          self._toggle_select_all,
                                          C["card"], C["border"])
        self.select_all_btn.pack(side="left", padx=(8, 0))

        tk.Checkbutton(
            bar, text="Show card images", variable=self.show_images,
            command=self._redisplay, bg=C["panel"], fg=C["text"],
            activebackground=C["panel"], activeforeground=C["text"],
            selectcolor=C["input_bg"], font=FONT, cursor="hand2"
        ).pack(side="left", padx=16)

        self.status_label = tk.Label(bar, text="Ready", font=FONT_SMALL,
                                     bg=C["panel"], fg=C["muted"])
        self.status_label.pack(side="right")

        self.progress = ttk.Progressbar(top, length=400, maximum=100,
                                        style="Dark.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(8, 0))

        stats = tk.Frame(self.root, bg=C["bg"], padx=12, pady=6)
        stats.pack(fill="x")
        self.stats_label = tk.Label(stats, text="Deck stats: —", font=FONT,
                                    bg=C["bg"], fg=C["muted"], anchor="w",
                                    justify="left")
        self.stats_label.pack(anchor="w")

        # Scrollable card area
        wrap = tk.Frame(self.root, bg=C["bg"])
        wrap.pack(fill="both", expand=True)
        self.card_canvas = tk.Canvas(wrap, bg=C["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(wrap, orient="vertical",
                                 command=self.card_canvas.yview,
                                 bg=C["panel"], troughcolor=C["bg"],
                                 activebackground=C["border"])
        scrollbar.pack(side="right", fill="y")
        self.card_canvas.pack(side="left", fill="both", expand=True)
        self.card_canvas.configure(yscrollcommand=scrollbar.set)

        self.card_frame = tk.Frame(self.card_canvas, bg=C["bg"])
        self._canvas_window = self.card_canvas.create_window(
            (0, 0), window=self.card_frame, anchor="nw")

        self.card_frame.bind(
            "<Configure>",
            lambda e: self.card_canvas.configure(
                scrollregion=self.card_canvas.bbox("all")))
        self.card_canvas.bind("<Configure>", self._on_canvas_resize)
        self.card_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    # ---- Update checker ----

    def check_for_updates(self, manual=False):
        """Compare the running version against the repo's VERSION file."""
        if self.update_thread and self.update_thread.is_alive():
            return
        if manual:
            self._set_status("Checking for updates…")
        self.update_thread = threading.Thread(
            target=self._run_update_check, args=(manual,), daemon=True)
        self.update_thread.start()

    def _run_update_check(self, manual):
        try:
            req = urllib.request.Request(
                VERSION_URL, headers={"User-Agent": BROWSER_UA,
                                      "Cache-Control": "no-cache"})
            remote = urllib.request.urlopen(
                req, timeout=15).read().decode("utf-8", "ignore").strip()
            try:
                UPDATE_STAMP_FILE.write_text(str(int(time.time())))
            except OSError:
                pass
            if parse_version(remote) > parse_version(APP_VERSION):
                self.msg_queue.put(("update_available", remote))
            elif manual:
                self.msg_queue.put(("update_none", remote))
        except Exception as e:
            if manual:
                self.msg_queue.put(("update_error", str(e)))

    def _auto_update_check(self):
        """Runs shortly after startup, then once a day while open."""
        try:
            last = int(UPDATE_STAMP_FILE.read_text().strip())
        except (OSError, ValueError):
            last = 0
        if time.time() - last >= UPDATE_INTERVAL_S:
            self.check_for_updates(manual=False)
        self.root.after(UPDATE_INTERVAL_S * 1000, self._auto_update_check)

    # ---- Logo ----

    def _load_logo(self):
        for path in (BUNDLED_LOGO, LOGO_FILE):
            if path.exists():
                self._set_logo_from_bytes(path.read_bytes())
                return
        threading.Thread(target=self._download_logo, daemon=True).start()

    def _download_logo(self):
        try:
            req = urllib.request.Request(LOGO_URL,
                                         headers={"User-Agent": BROWSER_UA})
            data = urllib.request.urlopen(req, timeout=15).read()
            LOGO_FILE.write_bytes(data)
            self.msg_queue.put(("logo", data))
        except Exception:
            pass  # header just shows text without the logo

    def _set_logo_from_bytes(self, data):
        try:
            img = Image.open(BytesIO(data)).resize(
                (LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
            self._logo_photo = ImageTk.PhotoImage(img)
            self.logo_label.config(image=self._logo_photo)
        except Exception:
            pass

    # ---- Deck URL import ----

    def _toggle_import_row(self):
        if self.import_mode.get():
            self.import_frame.pack(fill="x", pady=(6, 0),
                                   before=self.decklist_text)
            self.url_entry.focus_set()
        else:
            self.import_frame.pack_forget()

    def start_import(self):
        if self.import_thread and self.import_thread.is_alive():
            return
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Input", "Paste a deck URL first.")
            return
        self.import_btn.config(state="disabled")
        self._set_status("Importing deck…")
        self.import_thread = threading.Thread(
            target=self._run_import, args=(url,), daemon=True)
        self.import_thread.start()

    def _run_import(self, url):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            text = loop.run_until_complete(import_deck_from_url(url))
            self.msg_queue.put(("decklist", text))
        except DeckImportError as e:
            self.msg_queue.put(("import_error", str(e)))
        except Exception as e:
            self.msg_queue.put(("import_error", f"Import failed: {e}"))
        finally:
            loop.close()
            self.msg_queue.put(("import_finished", None))

    def _on_mousewheel(self, event):
        self.card_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_canvas_resize(self, event):
        self.card_canvas.itemconfigure(self._canvas_window, width=event.width)
        if self._resize_after:
            self.root.after_cancel(self._resize_after)
        self._resize_after = self.root.after(200, self._relayout_if_needed)

    def _relayout_if_needed(self):
        self._resize_after = None
        if self.deck_cards and self._per_row() != self._last_per_row:
            self._redisplay()

    # ---- Build orchestration ----

    def start_build(self):
        if self.build_thread and self.build_thread.is_alive():
            messagebox.showwarning("Running",
                                   "A stock check is already in progress.")
            return
        text = self.decklist_text.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning("Input", "Paste a decklist first.")
            return

        entries = {}
        for line in text.splitlines():
            parsed = parse_decklist_line(line)
            if parsed:
                # Same card from different sets stays separate
                key = (parsed["name"].lower(),
                       (parsed["set_code"] or "").lower())
                if key in entries:
                    entries[key]["qty"] += parsed["qty"]
                else:
                    entries[key] = parsed
        entries = list(entries.values())[:MAX_CARDS]
        if not entries:
            messagebox.showwarning("Input", "No valid card lines found.")
            return

        self.cancel_event.clear()
        self._cancel_scheduled_render()
        self.progress["value"] = 0
        self.deck_cards = []
        self._update_cart_button()
        for w in self.card_frame.winfo_children():
            w.destroy()
        self._set_status("Starting…")
        self.build_btn.config(state="disabled")

        self.build_thread = threading.Thread(
            target=self._run_async_build, args=(entries,), daemon=True)
        self.build_thread.start()

    def cancel_build(self):
        self.cancel_event.set()
        self._set_status("Cancelling…")

    def _run_async_build(self, entries):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._build(entries))
        except Exception as e:
            self.msg_queue.put(("error", f"Build failed: {e}"))
        finally:
            loop.close()
            self.msg_queue.put(("finished", None))

    # ---- Async workers ----

    @staticmethod
    async def _scryfall_named(session, name, set_code=None):
        """Scryfall named-card lookup, optionally pinned to a set code."""
        url = SCRYFALL_CARD + urllib.parse.quote(name)
        if set_code:
            url += "&set=" + urllib.parse.quote(set_code)
        try:
            async with session.get(url) as r:
                if r.status == 200:
                    return await r.json()
        except Exception:
            pass
        return None

    @staticmethod
    async def _fetch_card_image(session, data):
        """Download the 'normal' image for a Scryfall card object."""
        img_uris = data.get("image_uris")
        if not img_uris and data.get("card_faces"):
            img_uris = data["card_faces"][0].get("image_uris")
        if img_uris and img_uris.get("normal"):
            try:
                async with session.get(img_uris["normal"]) as r:
                    if r.status == 200:
                        return await r.read()
            except Exception:
                pass
        return None

    async def _fetch_card(self, session, entry):
        card = {"name": entry["name"], "qty": entry["qty"],
                "foil": entry["foil"], "set_code": entry.get("set_code"),
                "set_name": None, "cmc": 0, "type_line": "Unknown",
                "image_bytes": None, "in_stock": False}
        data = None
        # Requested printing first ("Swamp (SIS)"), then any printing
        if entry.get("set_code"):
            data = await self._scryfall_named(session, entry["name"],
                                              entry["set_code"])
        if data is None:
            data = await self._scryfall_named(session, entry["name"])
        if data:
            card["name"] = data.get("name", entry["name"])
            card["cmc"] = data.get("cmc", 0)
            card["type_line"] = data.get("type_line", "Unknown")
            card["set_name"] = data.get("set_name")
            card["image_bytes"] = await self._fetch_card_image(session, data)
        return card

    async def _find_stock_art(self, session, name, stock_sets):
        """Image of a printing that is actually in stock at the store.

        Returns (image_bytes, set_code) or None.
        """
        url = ("https://api.scryfall.com/cards/search"
               "?unique=prints&order=released&q="
               + urllib.parse.quote(f'!"{name}"'))
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        except Exception:
            return None
        for printing in data.get("data", []):
            set_name = printing.get("set_name", "")
            if any(set_names_match(set_name, s) for s in stock_sets):
                img = await self._fetch_card_image(session, printing)
                if img:
                    return img, printing.get("set", ""), set_name
        return None

    async def _store_search_page(self, session, query, offset, size):
        """One page of results from the store search API, or None on error."""
        payload = {
            "query": query,
            "context": None,
            "filters": {"productTypeName": ["Cards"]},
            "from": offset,
            "size": size,
            "sort": [{"field": "in-stock-price-sort", "order": "desc"}],
        }
        try:
            async with session.post(STORE_API, json=payload,
                                    headers=STORE_HEADERS,
                                    timeout=aiohttp.ClientTimeout(
                                        total=15)) as r:
                if r.status != 200:
                    print(f"Stock check {query!r}: HTTP {r.status}")
                    return None
                data = await r.json()
                return data.get("products", {})
        except Exception as e:
            print(f"Stock check error for {query!r}: {e}")
            return None

    async def _check_stock(self, session, name):
        """Return (in_stock, matched_products) for this card.

        matched_products is a list of store listings (dicts with id, name,
        setName, lowestPrice) whose product name exactly matches the card.
        The store API matches substrings ("Snap" also returns "Snapping
        Drake"), so a nonzero result count alone is not enough — every
        returned product name is compared against the card name after
        stripping listing suffixes like "(252)" or "(Rainbow Foil)".
        """
        # Accept the full name or the front face (stores list double-faced
        # cards by front face only, but split cards as "Fire // Ice").
        targets = {normalize_card_name(name)}
        if " // " in name:
            targets.add(normalize_card_name(name.split(" // ")[0]))

        # Full name first; DFC full names return 0, so retry with front face.
        queries = [name]
        if " // " in name:
            queries.append(name.split(" // ")[0])

        matched = []
        for query in queries:
            offset = 0
            while offset < STOCK_SCAN_LIMIT:
                products = await self._store_search_page(
                    session, query, offset, 50)
                if products is None:
                    break
                items = products.get("items", [])
                for item in items:
                    base = strip_listing_suffixes(item.get("name", ""))
                    if normalize_card_name(base) in targets:
                        matched.append({
                            "id": item.get("id"),
                            "name": item.get("name", ""),
                            "setName": item.get("setName") or "",
                            "lowestPrice": item.get("lowestPrice"),
                        })
                offset += len(items)
                if not items or offset >= products.get("totalItems", 0):
                    break
            if matched:
                break
        return bool(matched), matched

    async def _build(self, entries):
        total = len(entries)
        done = 0
        results = []

        headers = {"User-Agent": "CommanderDeckBuilder/2.0"}
        async with aiohttp.ClientSession(headers=headers) as session:
            sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

            async def process(entry):
                async with sem:
                    if self.cancel_event.is_set():
                        return None
                    card = await self._fetch_card(session, entry)
                    if self.cancel_event.is_set():
                        return None
                    in_stock, products = await self._check_stock(
                        session, card["name"])
                    card["in_stock"] = in_stock
                    card["stock_products"] = products
                    stock_sets = [p["setName"] for p in products
                                  if p["setName"]]
                    # A specific set was requested but the store stocks a
                    # different printing -> show the in-stock art instead
                    if (in_stock and card.get("set_code") and stock_sets
                            and not any(
                                set_names_match(card.get("set_name") or "", s)
                                for s in stock_sets)):
                        swap = await self._find_stock_art(
                            session, card["name"], stock_sets)
                        if swap:
                            card["image_bytes"], code, sname = swap
                            card["displayed_set_code"] = code
                            card["displayed_set_name"] = sname
                            card["_photo"] = None
                    return card

            tasks = [asyncio.ensure_future(process(e)) for e in entries]
            for fut in asyncio.as_completed(tasks):
                card = await fut
                if self.cancel_event.is_set():
                    for t in tasks:
                        t.cancel()
                    self.msg_queue.put(("status", "Cancelled"))
                    return
                if card:
                    results.append(card)
                    # Stream each result so the grid fills in live
                    self.msg_queue.put(("card", card))
                done += 1
                self.msg_queue.put(("progress",
                                    (done / total * 100,
                                     f"Fetching… {done}/{total}")))

        results.sort(key=lambda c: (c.get("cmc", 0), c["name"]))
        self.msg_queue.put(("done", results))

    # ---- Add to cart ----

    @staticmethod
    def _pick_product(products, prefer_sets):
        """Choose the store listing to buy: preferred set, else cheapest."""
        for pref in prefer_sets:
            for p in products:
                if pref and set_names_match(pref, p["setName"]):
                    return p
        def price(p):
            v = p.get("lowestPrice")
            return v if isinstance(v, (int, float)) else float("inf")
        return min(products, key=price) if products else None

    @staticmethod
    def _pick_sku(skus, want_foil):
        """Cheapest available SKU, preferring the requested finish."""
        avail = [s for s in skus if (s.get("quantity") or 0) > 0]
        if not avail:
            return None
        def price(s):
            v = s.get("price")
            return v if isinstance(v, (int, float)) else float("inf")
        matching = [s for s in avail if bool(s.get("isFoil")) == want_foil]
        return min(matching or avail, key=price)

    async def _cart_worker(self, selections, headless=False, keep_open=True):
        """Add selected cards to a real store cart in a visible browser.

        The cart is tied to browser cookies, so items are added inside a
        Chromium window the user keeps to complete checkout.
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            self.msg_queue.put(("cart_error",
                                "Add to cart needs the 'playwright' package:"
                                "\npip install playwright && "
                                "playwright install chromium"))
            return
        added, problems = 0, []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=headless, args=["--start-maximized"])
            ctx = await browser.new_context(user_agent=BROWSER_UA,
                                            no_viewport=not headless)
            page = await ctx.new_page()
            await page.goto(STORE_BASE, timeout=30000,
                            wait_until="domcontentloaded")

            for i, sel in enumerate(selections):
                self.msg_queue.put((
                    "status",
                    f"Adding to cart… {i + 1}/{len(selections)}"))
                try:
                    product = self._pick_product(sel["products"],
                                                 sel["prefer_sets"])
                    if not product or not product.get("id"):
                        problems.append(f"{sel['name']}: no listing found")
                        continue
                    r = await page.request.get(
                        f"{STORE_BASE}/api/inventory/skus"
                        f"?productIds={product['id']}")
                    if r.status != 200:
                        problems.append(f"{sel['name']}: sku lookup failed")
                        continue
                    groups = await r.json()
                    skus = groups[0].get("skus", []) if groups else []
                    sku = self._pick_sku(skus, sel["foil"])
                    if not sku:
                        problems.append(f"{sel['name']}: sold out")
                        continue
                    qty = min(sel["qty"], sku.get("quantity") or 1)
                    resp = await page.request.post(
                        f"{STORE_BASE}/api/cart/items",
                        data={"skuId": sku["skuId"],
                              "storePriceCustomId":
                                  sku.get("storePriceCustomId") or None,
                              "condition": sku.get("conditionName",
                                                   "Near Mint"),
                              "quantity": qty})
                    body = {}
                    try:
                        body = await resp.json()
                    except Exception:
                        pass
                    if resp.status == 200 and not body.get("errors"):
                        added += 1
                        if qty < sel["qty"]:
                            problems.append(
                                f"{sel['name']}: only {qty} of "
                                f"{sel['qty']} available")
                    else:
                        problems.append(f"{sel['name']}: store rejected add")
                except Exception as e:
                    problems.append(f"{sel['name']}: {e}")

            # Store cart page (prompts TCGplayer sign-in for checkout;
            # the cart itself is already attached to this browser session)
            await page.goto(f"{STORE_BASE}/tcgplayer/cart", timeout=30000,
                            wait_until="domcontentloaded")
            self.msg_queue.put(("cart_done", (added, len(selections),
                                              problems)))
            if keep_open:
                # Keep the window alive until the user closes it
                disconnected = asyncio.Event()
                browser.on("disconnected",
                           lambda *_: disconnected.set())
                try:
                    while len(ctx.pages) > 0 and not disconnected.is_set():
                        await asyncio.sleep(0.5)
                except Exception:
                    pass
            await browser.close()

    def _selected_cards(self):
        return [c for c in self.deck_cards
                if c.get("_sel") is not None and c["_sel"].get()]

    def start_add_to_cart(self):
        if self.cart_thread and self.cart_thread.is_alive():
            messagebox.showwarning(
                "Running", "Already adding to cart — check the browser "
                "window.")
            return
        cards = self._selected_cards()
        if not cards:
            messagebox.showwarning(
                "Nothing selected",
                "Tick the checkbox on the cards you want first.")
            return
        selections = []
        for c in cards:
            selections.append({
                "name": c["name"],
                "qty": c["qty"],
                "foil": bool(c.get("foil")),
                "products": c.get("stock_products") or [],
                "prefer_sets": [s for s in (c.get("displayed_set_name"),
                                            c.get("set_name")) if s],
            })
        self.cart_btn.config(state="disabled")
        self._set_status("Opening store…")
        self.cart_thread = threading.Thread(
            target=self._run_cart, args=(selections,), daemon=True)
        self.cart_thread.start()

    def _run_cart(self, selections):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._cart_worker(selections))
        except Exception as e:
            self.msg_queue.put(("cart_error", f"Add to cart failed: {e}"))
        finally:
            loop.close()
            self.msg_queue.put(("cart_finished", None))

    def _toggle_select_all(self):
        stockable = [c for c in self.deck_cards if c.get("in_stock")]
        all_on = stockable and all(
            c.get("_sel") and c["_sel"].get() for c in stockable)
        for c in stockable:
            if c.get("_sel") is None:
                c["_sel"] = tk.BooleanVar(master=self.root, value=False)
            c["_sel"].set(not all_on)
        self._update_cart_button()

    def _sel_var(self, card):
        if card.get("_sel") is None:
            card["_sel"] = tk.BooleanVar(master=self.root, value=False)
        return card["_sel"]

    def _update_cart_button(self):
        n = len(self._selected_cards())
        self.cart_btn.config(
            text=f"Add to Cart ({n})" if n else "Add to Cart")

    # ---- UI-thread message pump ----

    def _process_queue(self):
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "progress":
                    pct, status = payload
                    self.progress["value"] = pct
                    self._set_status(status)
                elif kind == "status":
                    self._set_status(payload)
                elif kind == "card":
                    self.deck_cards.append(payload)
                    self._schedule_render()
                elif kind == "done":
                    self._cancel_scheduled_render()
                    self.deck_cards = payload
                    self.display_deck(payload)
                    self._set_status(f"Done — {len(payload)} unique cards")
                elif kind == "error":
                    self._set_status("Error")
                    messagebox.showerror("Error", payload)
                elif kind == "finished":
                    self.build_btn.config(state="normal")
                elif kind == "logo":
                    self._set_logo_from_bytes(payload)
                elif kind == "decklist":
                    self.decklist_text.delete("1.0", "end")
                    self.decklist_text.insert("1.0", payload)
                    n = len([l for l in payload.splitlines() if l.strip()])
                    self._set_status(f"Imported {n} lines")
                elif kind == "import_error":
                    self._set_status("Import failed")
                    messagebox.showerror("Import failed", payload)
                elif kind == "import_finished":
                    self.import_btn.config(state="normal")
                elif kind == "cart_done":
                    added, total, problems = payload
                    self._set_status(
                        f"Cart: {added}/{total} added — finish checkout in "
                        "the browser window")
                    if problems:
                        messagebox.showwarning(
                            "Add to Cart",
                            f"Added {added} of {total} cards.\n\nIssues:\n"
                            + "\n".join(problems[:15]))
                elif kind == "cart_error":
                    self._set_status("Add to cart failed")
                    messagebox.showerror("Add to Cart", payload)
                elif kind == "cart_finished":
                    self.cart_btn.config(state="normal")
                    self._update_cart_button()
                elif kind == "update_available":
                    self._set_status(f"Update available: v{payload}")
                    if messagebox.askyesno(
                            "Update available",
                            f"Version {payload} is available "
                            f"(you have v{APP_VERSION}).\n\n"
                            "Open the download page?"):
                        webbrowser.open(REPO_URL)
                elif kind == "update_none":
                    self._set_status(f"Up to date (v{APP_VERSION})")
                elif kind == "update_error":
                    self._set_status("Update check failed")
        except queue.Empty:
            pass
        self.root.after(100, self._process_queue)

    def _set_status(self, text):
        self.status_label.config(text=text)

    # ---- Rendering ----

    def _per_row(self):
        width = self.card_canvas.winfo_width() or 1000
        if self.show_images.get():
            return max(1, width // (CARD_W + 22))
        return max(1, width // 320)

    def _schedule_render(self):
        """Throttled re-render used while results stream in live."""
        if self._render_after is None:
            self._render_after = self.root.after(400, self._render_now)

    def _cancel_scheduled_render(self):
        if self._render_after is not None:
            self.root.after_cancel(self._render_after)
            self._render_after = None

    def _render_now(self):
        self._render_after = None
        self.deck_cards.sort(key=lambda c: (c.get("cmc", 0), c["name"]))
        self.display_deck(self.deck_cards)

    def _redisplay(self):
        """Re-render immediately (image/name toggle). Works at any time,
        including while a stock check is still streaming results."""
        self._cancel_scheduled_render()
        if self.deck_cards:
            self.display_deck(self.deck_cards)
        else:
            self._set_status("No results yet — run a stock check first")

    @staticmethod
    def _primary_type(type_line):
        """'Legendary Creature — Goblin // Land' -> 'Creature'."""
        if not type_line:
            return "Unknown"
        front = type_line.split("//")[0].split("\u2014")[0]
        words = front.split()
        return words[-1] if words else "Unknown"

    def display_deck(self, deck):
        for w in self.card_frame.winfo_children():
            w.destroy()

        per_row = self._per_row()
        self._last_per_row = per_row
        type_counts = {}
        in_stock_count = 0

        for idx, card in enumerate(deck):
            tkey = self._primary_type(card.get("type_line"))
            type_counts[tkey] = type_counts.get(tkey, 0) + card["qty"]
            if card.get("in_stock"):
                in_stock_count += 1

            if self.show_images.get():
                self._render_image_card(card, idx, per_row)
            else:
                self._render_name_card(card, idx, per_row)

        total_qty = sum(c["qty"] for c in deck)
        stats = f"{total_qty} cards  |  in stock: {in_stock_count}/{len(deck)}  |  " \
                + "  ".join(f"{k}: {v}" for k, v in sorted(type_counts.items()))
        self.stats_label.config(text=stats)

    def _render_image_card(self, card, idx, per_row):
        frame = tk.Frame(self.card_frame, bg=C["card"], bd=1,
                         relief="solid", highlightthickness=0)
        frame.grid(row=idx // per_row, column=idx % per_row, padx=6, pady=6)

        link = store_search_url(card["name"])
        widget = None
        photo = card.get("_photo")  # cached thumbnail (instant re-render)
        if photo is None and card.get("image_bytes"):
            try:
                img = Image.open(BytesIO(card["image_bytes"])).resize(
                    (CARD_W, CARD_H), Image.LANCZOS)
                self._draw_stock_badge(img, card.get("in_stock"))
                photo = ImageTk.PhotoImage(img)
                card["_photo"] = photo
            except Exception:
                photo = None
        if photo is not None:
            widget = tk.Button(frame, image=photo, bd=0, relief="flat",
                               bg=C["card"], activebackground=C["card"],
                               cursor="hand2",
                               command=lambda u=link: webbrowser.open(u))
            widget.image = photo
            widget.pack()
            HoverPreview(widget, card["image_bytes"])

        if widget is None:
            widget = tk.Button(frame, text=card["name"], font=FONT,
                               bg=C["card"], fg=C["text"], bd=0, relief="flat",
                               activebackground=C["border"],
                               activeforeground=C["text"], cursor="hand2",
                               wraplength=CARD_W - 10, width=18, height=12,
                               command=lambda u=link: webbrowser.open(u))
            widget.pack()

        qty_text = f"{card['qty']}x"
        shown_set = card.get("displayed_set_code") or card.get("set_code")
        if shown_set:
            qty_text += f"  ({shown_set.upper()})"
            if card.get("displayed_set_code"):
                qty_text += " in stock"
        if card.get("foil"):
            qty_text += "  ✦ foil"

        bottom = tk.Frame(frame, bg=C["card"])
        bottom.pack(fill="x", pady=(0, 3))
        if card.get("in_stock"):
            tk.Checkbutton(
                bottom, text="Buy", variable=self._sel_var(card),
                command=self._update_cart_button, bg=C["card"],
                fg=C["text"], activebackground=C["card"],
                activeforeground=C["text"], selectcolor=C["input_bg"],
                font=FONT_SMALL, cursor="hand2").pack(side="left", padx=4)
        tk.Label(bottom, text=qty_text, font=FONT_SMALL, bg=C["card"],
                 fg=C["muted"]).pack(side="right", padx=6)

    def _render_name_card(self, card, idx, per_row):
        frame = tk.Frame(self.card_frame, bg=C["card"], padx=8, pady=5)
        frame.grid(row=idx // per_row, column=idx % per_row,
                   padx=4, pady=3, sticky="ew")

        if card.get("in_stock"):
            tk.Checkbutton(
                frame, variable=self._sel_var(card),
                command=self._update_cart_button, bg=C["card"],
                activebackground=C["card"], selectcolor=C["input_bg"],
                cursor="hand2").pack(side="left")

        dot_color = C["green"] if card.get("in_stock") else C["red"]
        tk.Label(frame, text="●", fg=dot_color, bg=C["card"],
                 font=FONT).pack(side="left")

        link = store_search_url(card["name"])
        name_text = f"{card['qty']}x {card['name']}"
        shown_set = card.get("displayed_set_code") or card.get("set_code")
        if shown_set:
            name_text += f"  ({shown_set.upper()})"
        name_lbl = tk.Label(frame, text=name_text,
                            font=FONT, bg=C["card"], fg=C["text"],
                            cursor="hand2", anchor="w")
        name_lbl.pack(side="left", padx=(6, 0), fill="x", expand=True)
        name_lbl.bind("<Button-1>", lambda e, u=link: webbrowser.open(u))
        name_lbl.bind("<Enter>",
                      lambda e: name_lbl.config(fg=C["accent_hi"]))
        name_lbl.bind("<Leave>", lambda e: name_lbl.config(fg=C["text"]))
        if card.get("image_bytes"):
            HoverPreview(name_lbl, card["image_bytes"])

    @staticmethod
    def _draw_stock_badge(img, in_stock):
        draw = ImageDraw.Draw(img)
        color = C["green"] if in_stock else C["red"]
        symbol = "✔" if in_stock else "✕"
        draw.ellipse([6, 6, 28, 28], fill=color, outline="white", width=1)
        draw.text((12, 8), symbol, fill="white")

    # ---- Shutdown ----

    def _on_close(self):
        self.cancel_event.set()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    root.geometry("1200x800")
    root.minsize(800, 600)
    app = CommanderApp(root)
    root.mainloop()
