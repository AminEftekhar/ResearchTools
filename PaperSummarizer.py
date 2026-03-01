import datetime as dt
import asyncio
import json
import os
import re
import threading
import datetime as dt
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
from urllib.error import URLError
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse
from urllib.request import Request, urlopen
import webbrowser
import xml.etree.ElementTree as ET
try:
    import edge_tts
except Exception:
    edge_tts = None

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_QUERY = "cat:physics.optics"
MAX_RESULTS = 120
DEFAULT_OLLAMA_MODEL = "deepseek-r1:1.5b"
OLLAMA_API_URL = "http://localhost:11434/api/generate"
DEFAULT_PAPER_DOWNLOAD_DIR = r"C:\Users\Amin\OneDrive\Documents\Papers"
ONE_PAGER_DIR_NAME = "OnePagers"
AUDIO_DIR_NAME = "Audio"
LEGACY_APP_STATE_PATH = os.path.join(
    os.path.expanduser("~"),
    "AppData",
    "Roaming",
    "PaperSummarizer",
    "state.json",
)
APP_STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "paper_summarizer_state.json",
)

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def fetch_arxiv_articles(search_query=ARXIV_QUERY, max_results=MAX_RESULTS):
    """Fetch recent arXiv papers for a query and group by date."""
    query_url = (
        f"{ARXIV_API_URL}?search_query={quote_plus(search_query)}"
        f"&sortBy=submittedDate&sortOrder=descending&start=0&max_results={max_results}"
    )

    with urlopen(query_url, timeout=25) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    entries = root.findall("atom:entry", namespaces=ATOM_NS)

    grouped = {}
    for entry in entries:
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        link = entry.findtext("atom:id", default="", namespaces=ATOM_NS) or ""
        published_text = entry.findtext(
            "atom:published", default="", namespaces=ATOM_NS
        )
        authors = [
            (a.findtext("atom:name", default="", namespaces=ATOM_NS) or "").strip()
            for a in entry.findall("atom:author", namespaces=ATOM_NS)
        ]
        summary = (entry.findtext("atom:summary", default="", namespaces=ATOM_NS) or "").strip()

        try:
            published_dt = dt.datetime.strptime(published_text, "%Y-%m-%dT%H:%M:%SZ")
            date_key = published_dt.strftime("%Y-%m-%d")
        except ValueError:
            date_key = "Unknown date"

        grouped.setdefault(date_key, []).append(
            {
                "title": " ".join(title.split()),
                "url": link,
                "authors": ", ".join(a for a in authors if a) or "Unknown authors",
                "summary": " ".join(summary.split()) or "No summary available.",
            }
        )

    # Most recent first.
    sorted_dates = sorted(
        grouped.keys(),
        key=lambda d: (d != "Unknown date", d),
        reverse=True,
    )
    return [(date_key, grouped[date_key]) for date_key in sorted_dates]


class ArxivOpticsUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Recent arXiv Optics Papers")
        self.root.geometry("980x650")
        self.root.minsize(780, 500)
        self._apply_modern_style()

        self.tabs = {}
        self.tab_counter = 0
        self.plus_tab_id = None
        self.hovered_tab_id = None
        self.link_button_icon = None
        self.footer = None
        self.search_host = None
        self.status_label = None
        self._suppress_state_events = True
        self.status_display_max_chars = 72
        self.search_placeholder = "Enter paper names, authors, keywords..."
        self.search_placeholder_active = True

        self._build_ui()
        self.root.bind_all("<Control-d>", self.download_selected_pdf)
        self.root.bind_all("<Control-w>", self.remove_current_tab)
        self.root.protocol("WM_DELETE_WINDOW", self._on_app_close)
        restored = self._restore_tabs_state()
        if not restored:
            self.add_new_tab(query=ARXIV_QUERY, label="physics.optics", refresh=True, persist=False)
        self._suppress_state_events = False
        self._save_app_state()

    def _apply_modern_style(self):
        style = ttk.Style(self.root)
        if "clam" in set(style.theme_names()):
            style.theme_use("clam")

        bg = "#ECECEC"
        header_bg = "#E3E3E5"
        panel_bg = "#FFFFFF"
        text = "#1D1D1F"
        muted_text = "#6E6E73"

        self.root.configure(bg=bg)
        self.root.option_add("*Font", "{SF Pro Text} 10")

        style.configure("TFrame", background=bg)
        style.configure("Header.TFrame", background=header_bg)
        style.configure("Body.TFrame", background=panel_bg)
        style.configure("Footer.TFrame", background=bg)
        style.configure("TNotebook", background=bg, borderwidth=0, tabmargins=(6, 6, 6, 0))
        style.configure(
            "TNotebook.Tab",
            font=("SF Pro Text", 11, "bold"),
            padding=(14, 8),
            background="#DADAD6",
            foreground=text,
            borderwidth=1,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#FFFFFF"), ("active", "#E7E7E3"), ("!active", "#DADAD6")],
            foreground=[("selected", text), ("!selected", text)],
            padding=[("selected", (14, 8)), ("!selected", (14, 8))],
            expand=[("selected", (0, 0, 0, 0)), ("!selected", (0, 0, 0, 0))],
        )

        style.configure("TLabel", background=bg, foreground=text, font=("SF Pro Text", 10))
        style.configure("Title.TLabel", background=header_bg, foreground=text, font=("SF Pro Display", 16, "bold"))
        style.configure("Status.TLabel", background=bg, foreground=muted_text, font=("SF Pro Text", 10))

        style.configure(
            "Mac.TButton",
            font=("SF Pro Text", 10),
            padding=(12, 6),
            background="#F7F7F8",
            foreground=text,
            borderwidth=1,
            relief="solid",
        )
        style.map(
            "Mac.TButton",
            background=[("active", "#EBEBED"), ("pressed", "#E4E4E6")],
            foreground=[("disabled", "#A2A2A6"), ("!disabled", text)],
        )
        style.configure(
            "Icon.TButton",
            padding=(4, 2),
            background=header_bg,
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Icon.TButton",
            background=[("active", "#DCDDDF"), ("pressed", "#D5D6D8"), ("!disabled", header_bg)],
        )

        style.configure(
            "Mac.Treeview",
            background=panel_bg,
            fieldbackground=panel_bg,
            foreground=text,
            rowheight=30,
            borderwidth=0,
            relief="flat",
            font=("SF Pro Text", 10),
        )
        style.configure(
            "Mac.Treeview.Heading",
            font=("SF Pro Text", 10, "bold"),
            background="#F5F5F7",
            foreground=muted_text,
            relief="flat",
            borderwidth=0,
        )

    def _build_ui(self):
        header = ttk.Frame(self.root, style="Header.TFrame", padding=(14, 10))
        header.pack(fill="x")
        button_padx = 4

        traffic = tk.Canvas(header, width=52, height=14, bg="#E3E3E5", highlightthickness=0, bd=0)
        traffic.create_oval(2, 2, 12, 12, fill="#FF5F57", outline="#E0443E")
        traffic.create_oval(20, 2, 30, 12, fill="#FEBC2E", outline="#D6A026")
        traffic.create_oval(38, 2, 48, 12, fill="#28C840", outline="#1FA834")
        traffic.pack(side="left", padx=(0, 10))

        title_font = tkfont.Font(family="SF Pro Display", size=16, weight="bold")
        self.title_var = tk.StringVar(value="Recent papers from arXiv: physics.optics")
        title_label = ttk.Label(
            header,
            textvariable=self.title_var,
            font=title_font,
            style="Title.TLabel",
        )
        title_label.pack(side="left")

        self.link_button_icon = self._build_folder_icon()
        source_btn = tk.Button(
            header,
            image=self.link_button_icon,
            command=self.open_link_input_dialog,
            bg="#E3E3E5",
            activebackground="#DCDDDF",
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=4,
            pady=2,
            takefocus=0,
        )
        source_btn.pack(side="left", padx=(8, 0))

        right_panel = ttk.Frame(header, style="Header.TFrame")
        right_panel.pack(side="right", anchor="n")

        actions_row = ttk.Frame(right_panel, style="Header.TFrame")
        actions_row.pack(side="top", anchor="e")

        one_pager_btn = ttk.Button(
            actions_row,
            text="One Pager",
            command=self.open_or_generate_one_pager,
            style="Mac.TButton",
        )
        one_pager_btn.pack(side="right", padx=button_padx)

        podcast_btn = ttk.Button(
            actions_row,
            text="Podcast",
            command=self.create_podcast_for_selection,
            style="Mac.TButton",
        )
        podcast_btn.pack(side="right", padx=button_padx)

        download_btn = ttk.Button(
            actions_row, text="Open PDF", command=self.download_selected_pdf, style="Mac.TButton"
        )
        download_btn.pack(side="right", padx=button_padx)

        summary_btn = ttk.Button(
            actions_row,
            text="Abstract",
            command=self.open_summary_window,
            style="Mac.TButton",
        )
        summary_btn.pack(side="right", padx=button_padx)

        body = ttk.Frame(self.root, style="Body.TFrame", padding=(14, 8, 14, 14))
        body.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Loading...")

        tabs_row = ttk.Frame(body, style="Body.TFrame")
        tabs_row.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(tabs_row)
        self.notebook.pack(side="left", fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.notebook.bind("<Motion>", self._on_notebook_motion)
        self.notebook.bind("<Leave>", self._on_notebook_leave)
        self.notebook.bind("<Button-1>", self._on_notebook_click, add="+")
        footer = ttk.Frame(self.root, style="Footer.TFrame", padding=(14, 4, 14, 10))
        footer.pack(fill="x")
        footer.configure(height=50)
        self.footer = footer

        status = ttk.Label(footer, textvariable=self.status_var, anchor="w", style="Status.TLabel")
        status.pack(side="left", fill="x", expand=True)
        self.status_label = status

        search_host = ttk.Frame(self.root, style="Footer.TFrame")
        self.search_host = search_host

        self.search_var = tk.StringVar(value=self.search_placeholder)
        self.search_entry = tk.Entry(
            search_host,
            textvariable=self.search_var,
            width=34,
            fg="#7A7A7A",
            relief="solid",
            bd=1,
        )
        self.search_entry.pack(anchor="center", pady=0)
        self.search_entry.bind("<Return>", self.apply_search_filter)
        self.search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self.search_entry.bind("<FocusOut>", self._on_search_focus_out)

        footer_actions = ttk.Frame(footer, style="Footer.TFrame")
        footer_actions.pack(side="right")

        refresh_btn = ttk.Button(footer_actions, text="Refresh", command=self.refresh_data, style="Mac.TButton")
        refresh_btn.pack(side="right", padx=button_padx)

        open_folder_btn = ttk.Button(
            footer_actions, text="Open Papers Folder", command=self.open_papers_folder, style="Mac.TButton"
        )
        open_folder_btn.pack(side="right", padx=button_padx)

        self.root.bind("<Configure>", self._position_search_bar)
        self.root.after(20, self._position_search_bar)

    def _position_search_bar(self, _event=None):
        if not self.search_host or not self.footer:
            return
        self.root.update_idletasks()
        center_x = self.root.winfo_width() // 2
        footer_y = self.footer.winfo_y()
        footer_h = self.footer.winfo_height()
        center_y = footer_y + (footer_h // 2)
        self.search_host.place(x=center_x, y=center_y, anchor="center")
        self._sync_active_context_ui()

    def add_new_tab(self, query=ARXIV_QUERY, label="physics.optics", refresh=True, persist=True):
        label = self._clean_tab_label(label)
        self.tab_counter += 1
        tab = ttk.Frame(self.notebook, style="Body.TFrame", padding=(8, 8, 8, 8))
        cols = ("title", "authors")
        tree = ttk.Treeview(
            tab, columns=cols, show="tree headings", style="Mac.Treeview", selectmode="extended"
        )
        tree.heading("#0", text="Published Date")
        tree.heading("title", text="Title")
        tree.heading("authors", text="Authors")
        tree.column("#0", width=130, stretch=False)
        tree.column("title", width=520, stretch=True)
        tree.column("authors", width=280, stretch=True)
        tree.bind("<Double-1>", self.open_selected_link)
        tree.bind("<Return>", self.open_selected_link)
        tree.tag_configure("date_group", background="#F2F2F3", foreground="#3A3A3C")
        tree.tag_configure("paper_even", background="#FFFFFF")
        tree.tag_configure("paper_odd", background="#FAFAFB")

        vscroll = ttk.Scrollbar(tab, orient="vertical", command=tree.yview)
        hscroll = ttk.Scrollbar(tab, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)

        status = tk.StringVar(value="Ready.")
        ctx = {
            "frame": tab,
            "tree": tree,
            "status_var": status,
            "query": query,
            "label": label,
            "max_results": MAX_RESULTS,
            "search_query": "",
            "papers": [],
            "url_by_item": {},
            "authors_by_item": {},
            "summary_by_item": {},
            "one_pager_by_item": {},
            "one_pager_inflight": set(),
        }
        self.tabs[str(tab)] = ctx
        if self.plus_tab_id:
            plus_index = self.notebook.index(self.plus_tab_id)
            self.notebook.insert(plus_index, tab, text=label)
        else:
            self.notebook.add(tab, text=label)
        self.notebook.select(tab)
        self._ensure_plus_tab()
        self._refresh_tab_headers()
        self._sync_active_context_ui()
        if refresh:
            self.refresh_data()
        if persist:
            self._save_app_state()

    def remove_current_tab(self, _event=None):
        ctx = self._get_active_context()
        if not ctx:
            return

        if len(self.tabs) <= 1:
            messagebox.showinfo("Cannot remove tab", "At least one tab must remain open.")
            return

        tab_id = str(ctx["frame"])
        try:
            self.notebook.forget(ctx["frame"])
        except Exception:
            return
        self.tabs.pop(tab_id, None)
        self.hovered_tab_id = None
        self._refresh_tab_headers()
        self._sync_active_context_ui()
        self._save_app_state()

    def _get_active_context(self):
        if not hasattr(self, "notebook"):
            return None
        tab_id = self.notebook.select()
        if not tab_id:
            return None
        return self.tabs.get(tab_id)

    def _set_context_status(self, ctx, text):
        if not ctx:
            return
        full_text = str(text or "")
        ctx["status_var"].set(full_text)
        active = self._get_active_context()
        if active is ctx:
            self.status_var.set(self._truncate_status_text(full_text))

    def _sync_active_context_ui(self):
        ctx = self._get_active_context()
        if not ctx:
            self.title_var.set("Recent papers from arXiv")
            if hasattr(self, "status_var"):
                self.status_var.set("No active tab.")
            return
        self.title_var.set(f"Recent papers from arXiv: {ctx['label']}")
        if hasattr(self, "status_var"):
            self.status_var.set(self._truncate_status_text(ctx["status_var"].get()))
        if hasattr(self, "search_var"):
            self._set_search_entry_text(ctx.get("search_query", ""))

    def _truncate_status_text(self, text):
        message = str(text or "")
        if not message:
            return ""

        max_pixels = None
        if self.search_host and self.search_host.winfo_ismapped():
            # Keep space before the centered search bar so status never overlaps it.
            max_pixels = max(120, self.search_host.winfo_x() - 24)

        if not max_pixels:
            max_chars = max(20, int(self.status_display_max_chars))
            if len(message) <= max_chars:
                return message
            return message[: max_chars - 3].rstrip() + "..."

        try:
            font = tkfont.Font(font=self.status_label.cget("font")) if self.status_label else None
        except Exception:
            font = None

        if not font:
            max_chars = max(20, int(self.status_display_max_chars))
            if len(message) <= max_chars:
                return message
            return message[: max_chars - 3].rstrip() + "..."

        if font.measure(message) <= max_pixels:
            return message

        ellipsis = "..."
        lo, hi = 0, len(message)
        best = ellipsis
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = message[:mid].rstrip() + ellipsis
            if font.measure(candidate) <= max_pixels:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def _on_tab_changed(self, _event=None):
        selected = self.notebook.select()
        if selected and selected == self.plus_tab_id:
            self.add_new_tab()
            return
        self._refresh_tab_headers()
        self._sync_active_context_ui()
        if not self._suppress_state_events:
            self._save_app_state()

    def _ensure_plus_tab(self):
        if self.plus_tab_id and str(self.plus_tab_id) in self.notebook.tabs():
            # Keep "+" as the last tab.
            self.notebook.add(self.plus_tab_id, text="+")
            return
        plus_frame = ttk.Frame(self.notebook, style="Body.TFrame")
        self.plus_tab_id = str(plus_frame)
        self.notebook.add(plus_frame, text="+")
        self._refresh_tab_headers()

    def _refresh_tab_headers(self):
        if not hasattr(self, "notebook"):
            return
        for tab_id in self.notebook.tabs():
            if tab_id == self.plus_tab_id:
                self.notebook.tab(tab_id, text="+")
                continue
            ctx = self.tabs.get(tab_id)
            if not ctx:
                continue
            label = self._clean_tab_label(ctx.get("label", "physics.optics"))
            tab_text = f"{label}   x" if tab_id == self.hovered_tab_id else label
            self.notebook.tab(tab_id, text=tab_text)

    def _tab_id_at_xy(self, x, y):
        try:
            idx = self.notebook.index(f"@{x},{y}")
        except tk.TclError:
            return None
        tabs = self.notebook.tabs()
        if idx < 0 or idx >= len(tabs):
            return None
        return tabs[idx]

    def _on_notebook_motion(self, event):
        tab_id = self._tab_id_at_xy(event.x, event.y)
        if tab_id == self.plus_tab_id:
            tab_id = None
        if tab_id != self.hovered_tab_id:
            self.hovered_tab_id = tab_id
            self._refresh_tab_headers()

    def _on_notebook_leave(self, _event=None):
        if self.hovered_tab_id is not None:
            self.hovered_tab_id = None
            self._refresh_tab_headers()

    def _on_notebook_click(self, event):
        tab_id = self._tab_id_at_xy(event.x, event.y)
        if not tab_id or tab_id == self.plus_tab_id or tab_id != self.hovered_tab_id:
            return
        # Prevent accidental closes while switching tabs.
        if tab_id != self.notebook.select():
            return
        if len(self.tabs) <= 1:
            return
        try:
            idx = self.notebook.index(tab_id)
            x, y, w, h = self.notebook.bbox(idx)
        except tk.TclError:
            return
        close_font = tkfont.Font(family="SF Pro Text", size=11, weight="bold")
        close_width = close_font.measure("x") + 6
        if event.x < (x + w - close_width):
            return
        frame = self.tabs.get(tab_id, {}).get("frame")
        if not frame:
            return
        self.notebook.forget(frame)
        self.tabs.pop(tab_id, None)
        self.hovered_tab_id = None
        self._refresh_tab_headers()
        self._sync_active_context_ui()
        self._save_app_state()
        return "break"

    def refresh_data(self):
        ctx = self._get_active_context()
        if not ctx:
            return
        self._set_context_status(ctx, "Fetching latest entries...")
        self.root.update_idletasks()
        tree = ctx["tree"]
        tree.delete(*tree.get_children())
        ctx["papers"].clear()
        ctx["url_by_item"].clear()
        ctx["authors_by_item"].clear()
        ctx["summary_by_item"].clear()
        ctx["one_pager_by_item"].clear()
        ctx["one_pager_inflight"].clear()

        try:
            grouped = fetch_arxiv_articles(
                search_query=ctx["query"], max_results=ctx["max_results"]
            )
        except URLError as exc:
            self._set_context_status(ctx, "Network error while fetching papers.")
            messagebox.showerror("Fetch failed", f"Could not reach arXiv.\n\n{exc}")
            return
        except ET.ParseError as exc:
            self._set_context_status(ctx, "Response parsing failed.")
            messagebox.showerror("Parse failed", f"Could not parse arXiv response.\n\n{exc}")
            return
        except Exception as exc:
            self._set_context_status(ctx, "Unexpected error while fetching papers.")
            messagebox.showerror("Error", f"Unexpected error:\n\n{exc}")
            return

        total_count = 0
        for date_key, papers in grouped:
            for paper in papers:
                ctx["papers"].append(
                    {
                        "date": date_key,
                        "title": paper["title"],
                        "authors": paper["authors"],
                        "summary": paper["summary"],
                        "url": paper["url"],
                    }
                )
                total_count += 1

        self._render_tree_from_current_filter(ctx)

        self._set_context_status(
            ctx,
            f"Loaded {total_count} papers in {len(grouped)} publication date groups."
        )

    def apply_search_filter(self, _event=None):
        ctx = self._get_active_context()
        if not ctx:
            return
        ctx["search_query"] = self._get_search_text()
        self._render_tree_from_current_filter(ctx)

    def _set_search_entry_text(self, text):
        value = (text or "").strip()
        if not value:
            self.search_var.set(self.search_placeholder)
            self.search_entry.configure(fg="#7A7A7A")
            self.search_placeholder_active = True
        else:
            self.search_var.set(value)
            self.search_entry.configure(fg="#1D1D1F")
            self.search_placeholder_active = False

    def _on_search_focus_in(self, _event=None):
        if self.search_placeholder_active:
            self.search_var.set("")
            self.search_entry.configure(fg="#1D1D1F")
            self.search_placeholder_active = False

    def _on_search_focus_out(self, _event=None):
        if not self.search_var.get().strip():
            self._set_search_entry_text("")

    def _get_search_text(self):
        text = self.search_var.get().strip()
        if self.search_placeholder_active or text == self.search_placeholder:
            return ""
        return text

    def _render_tree_from_current_filter(self, ctx):
        tree = ctx["tree"]
        tree.delete(*tree.get_children())
        ctx["url_by_item"].clear()
        ctx["authors_by_item"].clear()
        ctx["summary_by_item"].clear()

        query = (ctx.get("search_query") or "").lower()
        if query:
            filtered = [
                p for p in ctx["papers"]
                if query in p["title"].lower()
                or query in p["authors"].lower()
                or query in p["summary"].lower()
            ]
        else:
            filtered = list(ctx["papers"])

        grouped = {}
        for p in filtered:
            grouped.setdefault(p["date"], []).append(p)

        sorted_dates = sorted(
            grouped.keys(),
            key=lambda d: (d != "Unknown date", d),
            reverse=True,
        )

        row_count = 0
        for date_key in sorted_dates:
            date_item = tree.insert("", "end", text=date_key, values=("", ""), tags=("date_group",))
            for paper in grouped[date_key]:
                row_tag = "paper_even" if row_count % 2 == 0 else "paper_odd"
                item_id = tree.insert(
                    date_item,
                    "end",
                    text="",
                    values=(paper["title"], paper["authors"]),
                    tags=(row_tag,),
                )
                ctx["url_by_item"][item_id] = paper["url"]
                ctx["authors_by_item"][item_id] = paper["authors"]
                ctx["summary_by_item"][item_id] = paper["summary"]
                row_count += 1

        for top_item in tree.get_children():
            tree.item(top_item, open=True)

        if query:
            self._set_context_status(
                ctx,
                f"Showing {len(filtered)} matching papers for '{ctx['search_query']}'.",
            )

    def open_link_input_dialog(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("Set arXiv Source")
        dialog.geometry("620x150")
        dialog.minsize(540, 140)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="Paste an arXiv link (list, search, abs, or pdf):",
        ).pack(anchor="w")

        link_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=link_var)
        entry.pack(fill="x", pady=(6, 10))
        entry.focus_set()

        try:
            clip = self.root.clipboard_get().strip()
            if clip.startswith("http://") or clip.startswith("https://"):
                link_var.set(clip)
                entry.icursor("end")
        except tk.TclError:
            pass

        actions = ttk.Frame(frame)
        actions.pack(fill="x")
        ttk.Button(
            actions,
            text="Apply",
            command=lambda: self._apply_source_link_from_text(link_var.get(), dialog),
            style="Mac.TButton",
        ).pack(side="right")
        ttk.Button(
            actions,
            text="Cancel",
            command=dialog.destroy,
            style="Mac.TButton",
        ).pack(side="right", padx=(0, 6))

    def _apply_source_link_from_text(self, link_text, dialog=None):
        ctx = self._get_active_context()
        if not ctx:
            return
        pasted = (link_text or "").strip()
        parsed = self._parse_arxiv_link_to_query(pasted)
        if not parsed:
            messagebox.showerror(
                "Unsupported link",
                "Could not parse an arXiv link.\n"
                "Supported examples:\n"
                "- https://arxiv.org/list/physics.optics/recent\n"
                "- https://arxiv.org/abs/2501.12345\n"
                "- https://arxiv.org/search/?query=metasurface",
            )
            return

        query, label = parsed
        ctx["query"] = query
        clean_label = self._clean_tab_label(label)
        ctx["label"] = clean_label
        self._refresh_tab_headers()
        self._set_context_status(ctx, f"Using source: {clean_label}")
        self._sync_active_context_ui()
        self._save_app_state()
        if dialog is not None:
            dialog.destroy()
        self.refresh_data()

    def _save_app_state(self):
        if self._suppress_state_events:
            return
        try:
            os.makedirs(os.path.dirname(APP_STATE_PATH), exist_ok=True)
            content_tabs = []
            tab_order = self.notebook.tabs() if hasattr(self, "notebook") else []
            for tab_id in tab_order:
                if tab_id == self.plus_tab_id:
                    continue
                ctx = self.tabs.get(tab_id)
                if not ctx:
                    continue
                content_tabs.append(
                    {
                        "query": ctx.get("query", ARXIV_QUERY),
                        "label": self._clean_tab_label(ctx.get("label", "physics.optics")),
                    }
                )
            selected_tab = self.notebook.select() if hasattr(self, "notebook") else ""
            selected_index = 0
            content_ids = [tid for tid in tab_order if tid != self.plus_tab_id]
            if selected_tab in content_ids:
                selected_index = content_ids.index(selected_tab)
            payload = {"tabs": content_tabs, "selected_index": selected_index}
            with open(APP_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            # Non-fatal: app should keep working even if persistence fails.
            pass

    def _restore_tabs_state(self):
        state_path = APP_STATE_PATH if os.path.exists(APP_STATE_PATH) else LEGACY_APP_STATE_PATH
        if not os.path.exists(state_path):
            return False
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            saved_tabs = payload.get("tabs", [])
            if not saved_tabs:
                return False
            selected_index = int(payload.get("selected_index", 0))
        except Exception:
            return False

        for tab_id in list(self.tabs.keys()):
            try:
                self.notebook.forget(tab_id)
            except Exception:
                pass
        self.tabs.clear()
        self.tab_counter = 0

        if self.plus_tab_id:
            try:
                self.notebook.forget(self.plus_tab_id)
            except Exception:
                pass
            self.plus_tab_id = None

        for tab_def in saved_tabs:
            query = str(tab_def.get("query", ARXIV_QUERY)).strip() or ARXIV_QUERY
            label = self._clean_tab_label(
                str(tab_def.get("label", "physics.optics")).strip() or "physics.optics"
            )
            self.add_new_tab(query=query, label=label, refresh=False, persist=False)

        self._ensure_plus_tab()
        content_ids = [tid for tid in self.notebook.tabs() if tid != self.plus_tab_id]
        if content_ids:
            selected_index = max(0, min(selected_index, len(content_ids) - 1))
            self.notebook.select(content_ids[selected_index])
            self._sync_active_context_ui()
            self.refresh_data()
            return True
        return False

    def _on_app_close(self):
        self._save_app_state()
        self.root.destroy()

    def _clean_tab_label(self, label):
        text = str(label or "").strip()
        if not text:
            return "physics.optics"
        text = re.sub(r"^\s*Tab\s*\d+\s*:\s*", "", text, flags=re.IGNORECASE)
        return text

    def _parse_arxiv_link_to_query(self, link_text):
        parsed = urlparse(link_text)
        host = parsed.netloc.lower()
        path = parsed.path.strip("/")

        if host not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            return None

        path_parts = path.split("/") if path else []
        if len(path_parts) >= 3 and path_parts[0] == "list":
            category = path_parts[1].strip()
            if category:
                return f"cat:{category}", category

        if len(path_parts) >= 2 and path_parts[0] in {"abs", "pdf"}:
            paper_id = path_parts[1].replace(".pdf", "").strip()
            if paper_id:
                return f"id:{paper_id}", f"paper {paper_id}"

        if path_parts and path_parts[0] == "search":
            params = parse_qs(parsed.query)
            raw_q = (params.get("query", [""])[0] or "").strip()
            if raw_q:
                decoded = unquote_plus(raw_q)
                return f"all:{decoded}", f"search {decoded}"

        if path_parts == ["api", "query"]:
            params = parse_qs(parsed.query)
            api_q = (params.get("search_query", [""])[0] or "").strip()
            if api_q:
                return unquote_plus(api_q), unquote_plus(api_q)

        return None

    def open_selected_link(self, _event=None):
        ctx = self._get_active_context()
        if not ctx:
            return
        selected = ctx["tree"].selection()
        if not selected:
            return
        item_id = selected[0]
        url = ctx["url_by_item"].get(item_id)
        if url:
            webbrowser.open(url)

    def download_selected_pdf(self, _event=None):
        ctx = self._get_active_context()
        if not ctx:
            return
        selected = ctx["tree"].selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select a paper first.")
            return

        item_id = selected[0]
        source_url = ctx["url_by_item"].get(item_id)
        if not source_url:
            messagebox.showinfo(
                "Download unavailable",
                "The selected row is not a paper.",
            )
            return

        pdf_url = self._build_arxiv_pdf_url(source_url)
        if not pdf_url:
            messagebox.showerror(
                "Invalid arXiv URL",
                "Could not derive a PDF URL from the selected paper.",
            )
            return

        paper_title = (ctx["tree"].item(item_id, "values")[0] or "").strip() or "Untitled paper"
        file_path = self._pdf_path_for_paper(paper_title, source_url)
        if os.path.exists(file_path):
            self._set_context_status(ctx, f"Opened existing PDF: {os.path.basename(file_path)}")
            try:
                os.startfile(file_path)
            except OSError as exc:
                messagebox.showerror("File error", f"Could not open PDF.\n\n{exc}")
            return

        self._set_context_status(ctx, "Downloading PDF...")
        self.root.update_idletasks()
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            req = Request(
                pdf_url,
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET",
            )
            with urlopen(req, timeout=90) as response:
                pdf_data = response.read()

            with open(file_path, "wb") as f:
                f.write(pdf_data)

            self._set_context_status(ctx, f"Downloaded PDF: {os.path.basename(file_path)}")
            os.startfile(file_path)
        except URLError as exc:
            self._set_context_status(ctx, "PDF download failed.")
            messagebox.showerror("Download failed", f"Could not download PDF.\n\n{exc}")
        except OSError as exc:
            self._set_context_status(ctx, "Could not save or open PDF.")
            messagebox.showerror("File error", f"Could not save/open PDF.\n\n{exc}")
        except Exception as exc:
            self._set_context_status(ctx, "Unexpected error during PDF download.")
            messagebox.showerror("Error", f"Unexpected error:\n\n{exc}")

    def open_papers_folder(self):
        try:
            papers_dir = os.getenv("PAPERS_DIR", DEFAULT_PAPER_DOWNLOAD_DIR).strip() or DEFAULT_PAPER_DOWNLOAD_DIR
            os.makedirs(papers_dir, exist_ok=True)
            os.startfile(papers_dir)
        except OSError as exc:
            messagebox.showerror("Folder error", f"Could not open papers folder.\n\n{exc}")

    def open_summary_window(self):
        ctx = self._get_active_context()
        if not ctx:
            return
        selected = ctx["tree"].selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select a paper first.")
            return

        item_id = selected[0]
        summary = ctx["summary_by_item"].get(item_id)
        if not summary:
            messagebox.showinfo(
                "Summary unavailable",
                "The selected row is not a paper, or the summary is missing.",
            )
            return

        paper_title = ctx["tree"].item(item_id, "values")[0]
        source_url = ctx["url_by_item"].get(item_id, "")

        popup = tk.Toplevel(self.root)
        popup.title("Paper Summary")
        popup.geometry("760x480")
        popup.minsize(520, 340)

        container = ttk.Frame(popup, padding=12)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=paper_title,
            font=tkfont.Font(family="SF Pro Display", size=12, weight="bold"),
            wraplength=710,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 10))

        summary_text = tk.Text(
            container,
            wrap="word",
            font=("SF Pro Text", 11),
            bg="#FFFFFF",
            fg="#1D1D1F",
            relief="flat",
        )
        summary_text.pack(fill="both", expand=True)
        summary_text.insert("1.0", summary)
        summary_text.configure(state="disabled")

        actions = ttk.Frame(container)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(
            actions,
            text="Text to Audio",
            command=lambda: self._export_text_to_audio_content(summary, paper_title, source_url),
            style="Mac.TButton",
        ).pack(side="right")

    def _export_text_to_audio_content(self, text_to_read, title, source_url):
        if edge_tts is None:
            messagebox.showerror(
                "TTS unavailable",
                "Install edge-tts to enable audio export.\n\nRun:\npy -3 -m pip install edge-tts",
            )
            return
        if not (text_to_read or "").strip():
            messagebox.showinfo(
                "No text available",
                "No text is available to convert to audio.",
            )
            return

        out_path = self._audio_path_for_paper(title, source_url)
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            voice = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural").strip() or "en-US-AriaNeural"
            rate = os.getenv("EDGE_TTS_RATE", "+0%").strip() or "+0%"
            self._save_audio_with_edge_tts(text_to_read, out_path, voice=voice, rate=rate)
            active_ctx = self._get_active_context()
            if active_ctx:
                self._set_context_status(active_ctx, f"Saved audio: {os.path.basename(out_path)}")
            os.startfile(out_path)
        except Exception as exc:
            messagebox.showerror("Audio export failed", str(exc))

    def _save_audio_with_edge_tts(self, text, out_path, voice, rate):
        async def _run():
            communicator = edge_tts.Communicate(text=text, voice=voice, rate=rate)
            await communicator.save(out_path)

        try:
            asyncio.run(_run())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(_run())
            finally:
                loop.close()

    def open_or_generate_one_pager(self):
        ctx = self._get_active_context()
        if not ctx:
            return
        selected = ctx["tree"].selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select a paper first.")
            return

        paper_items = [item_id for item_id in selected if item_id in ctx["summary_by_item"]]
        if not paper_items:
            messagebox.showinfo(
                "One pager unavailable",
                "Selected rows do not contain valid paper abstracts.",
            )
            return

        inflight = [item_id for item_id in paper_items if item_id in ctx["one_pager_inflight"]]
        if inflight:
            messagebox.showinfo(
                "In progress",
                "One-pager generation is already running for one or more selected papers.",
            )
            return

        if len(paper_items) == 1:
            item_id = paper_items[0]
            paper_title = ctx["tree"].item(item_id, "values")[0]
            source_url = ctx["url_by_item"].get(item_id, "")
            saved_text = self._load_onepager_from_disk(paper_title, source_url)
            if saved_text:
                normalized = self._normalize_one_pager_text(saved_text)
                ctx["one_pager_by_item"][item_id] = normalized
                self._set_context_status(ctx, "Opened saved one-pager.")
                self._open_one_pager_window(paper_title, normalized)
                return

            cached = ctx["one_pager_by_item"].get(item_id)
            if cached:
                normalized = self._normalize_one_pager_text(cached)
                ctx["one_pager_by_item"][item_id] = normalized
                self._open_one_pager_window(paper_title, normalized)
                return

            authors = ctx["authors_by_item"].get(item_id, "Unknown authors")
            abstract = ctx["summary_by_item"].get(item_id, "")
            ctx["one_pager_inflight"].add(item_id)
            self._set_context_status(ctx, "Generating one-pager...")

            worker = threading.Thread(
                target=self._generate_one_pager_worker,
                args=(str(ctx["frame"]), item_id, paper_title, authors, abstract),
                daemon=True,
            )
            worker.start()
            return

        for item_id in paper_items:
            ctx["one_pager_inflight"].add(item_id)
        self._set_context_status(ctx, f"Generating one-pagers for {len(paper_items)} papers...")
        worker = threading.Thread(
            target=self._generate_batch_one_pagers_worker,
            args=(str(ctx["frame"]), paper_items),
            daemon=True,
        )
        worker.start()

    def create_podcast_for_selection(self):
        ctx = self._get_active_context()
        if not ctx:
            return
        if edge_tts is None:
            messagebox.showerror(
                "TTS unavailable",
                "Install edge-tts to enable podcast export.\n\nRun:\npy -3 -m pip install edge-tts",
            )
            return

        selected = ctx["tree"].selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select one or more papers first.")
            return

        jobs = []
        for item_id in selected:
            abstract = (ctx["summary_by_item"].get(item_id, "") or "").strip()
            if not abstract:
                continue
            title = (ctx["tree"].item(item_id, "values")[0] or "").strip() or "paper"
            authors = (ctx["authors_by_item"].get(item_id, "") or "").strip() or "Unknown authors"
            source_url = (ctx["url_by_item"].get(item_id, "") or "").strip()
            jobs.append(
                {
                    "item_id": item_id,
                    "title": title,
                    "authors": authors,
                    "abstract": abstract,
                    "source_url": source_url,
                }
            )

        if not jobs:
            messagebox.showinfo(
                "Podcast unavailable",
                "Selected rows do not contain valid paper entries.",
            )
            return

        self._set_context_status(ctx, f"Preparing podcast for {len(jobs)} papers...")
        worker = threading.Thread(
            target=self._generate_podcast_worker,
            args=(str(ctx["frame"]), jobs),
            daemon=True,
        )
        worker.start()

    def _generate_podcast_worker(self, tab_id, jobs):
        ctx = self.tabs.get(tab_id)
        if not ctx:
            return

        sections = []
        total = len(jobs)
        for idx, job in enumerate(jobs, start=1):
            title = job["title"]
            abstract = job["abstract"]
            source_url = job["source_url"]
            item_id = job["item_id"]

            self.root.after(
                0,
                lambda i=idx, n=total: self._set_context_status(
                    self.tabs.get(tab_id), f"Podcast: processing {i}/{n}..."
                ),
            )

            one_pager = (ctx["one_pager_by_item"].get(item_id, "") or "").strip()
            if not one_pager:
                one_pager = self._load_onepager_from_disk(title, source_url)

            if not one_pager:
                try:
                    generated = self._request_one_pager_from_model(
                        title,
                        job["authors"],
                        abstract,
                    )
                    one_pager = self._normalize_one_pager_text(generated)
                    ctx["one_pager_by_item"][item_id] = one_pager
                    self._save_onepager_to_disk(title, source_url, one_pager)
                except Exception as exc:
                    one_pager = f"One-pager generation failed: {exc}"

            sections.append(
                "\n".join(
                    [
                        f"Title: {title}",
                        f"Abstract: {abstract}",
                        f"One Pager: {one_pager}",
                    ]
                )
            )

        full_text = "\n\n".join(sections).strip()
        if not full_text:
            self.root.after(
                0, lambda: messagebox.showerror("Podcast failed", "No text was available to synthesize.")
            )
            return

        out_path = self._podcast_audio_path(len(jobs))
        try:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            voice = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural").strip() or "en-US-AriaNeural"
            rate = os.getenv("EDGE_TTS_RATE", "+0%").strip() or "+0%"
            self._save_audio_with_edge_tts(full_text, out_path, voice=voice, rate=rate)
        except Exception as exc:
            self.root.after(0, lambda: self._on_podcast_error(tab_id, exc))
            return

        self.root.after(0, lambda: self._on_podcast_ready(tab_id, out_path, total))

    def _on_podcast_ready(self, tab_id, out_path, count):
        ctx = self.tabs.get(tab_id)
        if ctx:
            self._set_context_status(ctx, f"Podcast saved for {count} papers: {os.path.basename(out_path)}")
        try:
            os.startfile(out_path)
        except OSError:
            pass

    def _on_podcast_error(self, tab_id, exc):
        ctx = self.tabs.get(tab_id)
        if ctx:
            self._set_context_status(ctx, "Podcast generation failed.")
        messagebox.showerror("Podcast failed", str(exc))

    def _generate_batch_one_pagers_worker(self, tab_id, item_ids):
        ctx = self.tabs.get(tab_id)
        if not ctx:
            return

        aggregate = []
        for idx, item_id in enumerate(item_ids, start=1):
            title = ctx["tree"].item(item_id, "values")[0]
            authors = ctx["authors_by_item"].get(item_id, "Unknown authors")
            abstract = ctx["summary_by_item"].get(item_id, "")
            source_url = ctx["url_by_item"].get(item_id, "")

            self.root.after(
                0,
                lambda i=idx, n=len(item_ids): self._set_context_status(
                    self.tabs.get(tab_id),
                    f"Generating one-pager {i}/{n}...",
                ),
            )

            saved_text = self._load_onepager_from_disk(title, source_url)
            if saved_text:
                normalized = self._normalize_one_pager_text(saved_text)
                ctx["one_pager_by_item"][item_id] = normalized
                aggregate.append((title, normalized))
                continue

            cached = ctx["one_pager_by_item"].get(item_id)
            if cached:
                normalized = self._normalize_one_pager_text(cached)
                ctx["one_pager_by_item"][item_id] = normalized
                self._save_onepager_to_disk(title, source_url, normalized)
                aggregate.append((title, normalized))
                continue

            try:
                text = self._request_one_pager_from_model(title, authors, abstract)
                normalized = self._normalize_one_pager_text(text)
                ctx["one_pager_by_item"][item_id] = normalized
                self._save_onepager_to_disk(title, source_url, normalized)
                aggregate.append((title, normalized))
            except Exception as exc:
                aggregate.append((title, f"Generation failed:\n{exc}"))

        self.root.after(0, lambda: self._on_batch_one_pagers_ready(tab_id, item_ids, aggregate))

    def _generate_one_pager_worker(self, tab_id, item_id, title, authors, abstract):
        try:
            text = self._request_one_pager_from_model(title, authors, abstract)
        except Exception as exc:
            self.root.after(0, lambda: self._on_one_pager_error(tab_id, item_id, exc))
            return

        self.root.after(0, lambda: self._on_one_pager_ready(tab_id, item_id, title, text))

    def _on_one_pager_ready(self, tab_id, item_id, title, text):
        ctx = self.tabs.get(tab_id)
        if not ctx:
            return
        ctx["one_pager_inflight"].discard(item_id)
        normalized = self._normalize_one_pager_text(text)
        ctx["one_pager_by_item"][item_id] = normalized
        source_url = ctx["url_by_item"].get(item_id, "")
        self._save_onepager_to_disk(title, source_url, normalized)
        self._set_context_status(ctx, "One-pager ready.")
        self._open_one_pager_window(title, normalized)

    def _on_batch_one_pagers_ready(self, tab_id, item_ids, aggregate):
        ctx = self.tabs.get(tab_id)
        if not ctx:
            return
        for item_id in item_ids:
            ctx["one_pager_inflight"].discard(item_id)
        self._set_context_status(ctx, f"Ready: {len(aggregate)} one-pagers.")
        self._open_aggregate_one_pager_window(aggregate)

    def _on_one_pager_error(self, tab_id, item_id, exc):
        ctx = self.tabs.get(tab_id)
        if not ctx:
            return
        ctx["one_pager_inflight"].discard(item_id)
        self._set_context_status(ctx, "One-pager generation failed.")
        messagebox.showerror("One-pager failed", str(exc))

    def _build_one_pager_prompt(self, title, authors, abstract):
        clipped_abstract = abstract[:10000]
        return (
            "You are helping summarize a research paper for a technical reader.\n\n"
            f"Title: {title}\n"
            f"Authors: {authors}\n"
            f"Abstract: {clipped_abstract}\n\n"
            "Write a one-page summary with exactly these section headers (same words):\n"
            "1. Problem\n"
            "2. Method\n"
            "3. Key Results\n"
            "4. Why It Matters\n"
            "5. Limitations\n"
            "6. Takeaways\n\n"
            "Requirements:\n"
            "- Be factual and concise.\n"
            "- If information is missing from the abstract, state that clearly.\n"
            "- Do not invent quantitative results.\n"
            "- Output plain text only (no markdown, no **, no # headings).\n"
            "- Use ASCII math notation only: chi^(2), lambda, E_g, <=, >=, ~=, ->.\n"
            "- Keep equations on their own lines when possible.\n"
            "- Under 'Takeaways', output bullet lines starting with '- '."
        )

    def _request_one_pager_from_model(self, title, authors, abstract):
        model = os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL
        endpoint = os.getenv("OLLAMA_API_URL", OLLAMA_API_URL).strip() or OLLAMA_API_URL
        prompt = self._build_one_pager_prompt(title, authors, abstract)
        payload = json.dumps(
            {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 850},
            }
        )
        req = Request(
            endpoint,
            data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=180) as response:
            raw = response.read()
        data = json.loads(raw.decode("utf-8"))
        text = (data.get("response") or "").strip()
        if not text:
            raise RuntimeError("DeepSeek returned an empty response. Check Ollama/model.")
        return text

    def _build_arxiv_pdf_url(self, source_url):
        clean_url = (source_url or "").strip()
        if not clean_url:
            return ""
        if "/abs/" in clean_url:
            arxiv_id = clean_url.split("/abs/", 1)[1]
        elif "/pdf/" in clean_url:
            arxiv_id = clean_url.split("/pdf/", 1)[1].replace(".pdf", "")
        else:
            return ""
        arxiv_id = arxiv_id.strip("/")
        if not arxiv_id:
            return ""
        return f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    def _filename_from_pdf_url(self, pdf_url):
        arxiv_id = pdf_url.rsplit("/", 1)[-1]
        arxiv_id = arxiv_id.replace(".pdf", "").replace("/", "_")
        return f"{arxiv_id}.pdf"

    def _papers_dir(self):
        return os.getenv("PAPERS_DIR", DEFAULT_PAPER_DOWNLOAD_DIR).strip() or DEFAULT_PAPER_DOWNLOAD_DIR

    def _safe_filename(self, text, fallback):
        cleaned = re.sub(r'[<>:"/\\|?*]+', "_", (text or "").strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
        if not cleaned:
            cleaned = fallback
        if len(cleaned) > 150:
            cleaned = cleaned[:150].rstrip()
        return cleaned

    def _extract_arxiv_id(self, source_url):
        clean = (source_url or "").strip()
        if "/abs/" in clean:
            return clean.split("/abs/", 1)[1].strip("/")
        if "/pdf/" in clean:
            return clean.split("/pdf/", 1)[1].replace(".pdf", "").strip("/")
        return ""

    def _pdf_path_for_paper(self, paper_title, source_url):
        base_dir = self._papers_dir()
        paper_name = self._safe_filename(paper_title, "paper")
        return os.path.join(base_dir, f"{paper_name}.pdf")

    def _onepager_path_for_paper(self, paper_title, source_url):
        base_dir = self._papers_dir()
        onepager_dir = os.path.join(base_dir, ONE_PAGER_DIR_NAME)
        paper_name = self._safe_filename(paper_title, "paper")
        arxiv_id = self._safe_filename(self._extract_arxiv_id(source_url), "")
        if arxiv_id:
            filename = f"{paper_name} [{arxiv_id}].txt"
        else:
            filename = f"{paper_name}.txt"
        return os.path.join(onepager_dir, filename)

    def _audio_path_for_paper(self, paper_title, source_url):
        base_dir = self._papers_dir()
        audio_dir = os.path.join(base_dir, AUDIO_DIR_NAME)
        paper_name = self._safe_filename(paper_title, "paper")
        arxiv_id = self._safe_filename(self._extract_arxiv_id(source_url), "")
        if arxiv_id:
            filename = f"{paper_name} [{arxiv_id}].mp3"
        else:
            filename = f"{paper_name}.mp3"
        return os.path.join(audio_dir, filename)

    def _podcast_audio_path(self, paper_count):
        base_dir = self._papers_dir()
        audio_dir = os.path.join(base_dir, AUDIO_DIR_NAME)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(audio_dir, f"Podcast_{paper_count}papers_{stamp}.mp3")

    def _load_onepager_from_disk(self, paper_title, source_url):
        path = self._onepager_path_for_paper(paper_title, source_url)
        if not os.path.exists(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _save_onepager_to_disk(self, paper_title, source_url, text):
        path = self._onepager_path_for_paper(paper_title, source_url)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text or "")
        except OSError:
            pass

    def _open_aggregate_one_pager_window(self, aggregate_items):
        popup = tk.Toplevel(self.root)
        popup.title("One Pager Aggregate")
        popup.geometry("920x620")
        popup.minsize(620, 420)

        container = ttk.Frame(popup, padding=12)
        container.pack(fill="both", expand=True)

        text_widget = tk.Text(
            container,
            wrap="word",
            font=("SF Pro Text", 11),
            bg="#FFFFFF",
            fg="#1D1D1F",
            relief="flat",
        )
        text_widget.pack(fill="both", expand=True)
        text_widget.tag_configure("paper_title", font=("SF Pro Display", 13, "bold"))
        text_widget.tag_configure("separator", foreground="#808080")

        for idx, (title, body) in enumerate(aggregate_items, start=1):
            text_widget.insert("end", f"{idx}. {title}\n", "paper_title")
            text_widget.insert("end", f"{body}\n")
            if idx < len(aggregate_items):
                text_widget.insert("end", "\n" + ("-" * 70) + "\n\n", "separator")
        text_widget.configure(state="disabled")

    def _normalize_one_pager_text(self, text):
        cleaned = (text or "").replace("**", "").replace("###", "").replace("##", "")
        replacements = {
            "χ": "chi",
            "λ": "lambda",
            "ω": "omega",
            "Δ": "Delta",
            "≤": "<=",
            "≥": ">=",
            "≈": "~=",
            "≃": "~=",
            "→": "->",
            "×": "x",
            "·": "*",
            "−": "-",
            "–": "-",
            "—": "-",
        }
        for src, dst in replacements.items():
            cleaned = cleaned.replace(src, dst)
        cleaned = re.sub(r"(?m)^\s*#+\s*", "", cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        cleaned = "\n".join(line.rstrip() for line in cleaned.splitlines())
        sections = self._parse_one_pager_sections(cleaned.strip())
        sections = self._normalize_takeaways(sections)
        return self._compose_one_pager_text(sections)

    def _parse_one_pager_sections(self, text):
        section_order = [
            "Problem",
            "Method",
            "Key Results",
            "Why It Matters",
            "Limitations",
            "Takeaways",
        ]
        sections = {}
        current_title = None
        buffer = []
        heading_pattern = re.compile(
            r"^(?:\d+\.\s*)?(Problem|Method|Key Results|Why It Matters|Limitations|Takeaways|Five Bullet Takeaways)\s*:?\s*$",
            re.IGNORECASE,
        )
        inline_pattern = re.compile(
            r"^\d+\.\s*(Problem|Method|Key Results|Why It Matters|Limitations|Takeaways|Five Bullet Takeaways)\s*:\s*(.+)$",
            re.IGNORECASE,
        )

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                if current_title:
                    buffer.append("")
                continue

            if line.lower().startswith("summary of research paper"):
                continue

            inline_match = inline_pattern.match(line)
            if inline_match:
                if current_title is not None and buffer:
                    sections[current_title] = "\n".join(buffer).strip()
                matched = inline_match.group(1).lower()
                if matched == "five bullet takeaways":
                    matched = "takeaways"
                inline_title = next(
                    (title for title in section_order if title.lower() == matched),
                    None,
                )
                sections[inline_title] = inline_match.group(2).strip()
                current_title = None
                buffer = []
                continue

            heading_match = heading_pattern.match(line)
            if heading_match:
                if current_title is not None:
                    sections[current_title] = "\n".join(buffer).strip()
                matched = heading_match.group(1).lower()
                if matched == "five bullet takeaways":
                    matched = "takeaways"
                current_title = next(
                    (title for title in section_order if title.lower() == matched),
                    None,
                )
                buffer = []
                continue

            if current_title is None:
                # Fallback for model outputs that start writing content immediately.
                current_title = "Problem"
                buffer = []
            buffer.append(line)

        if current_title is not None:
            sections[current_title] = "\n".join(buffer).strip()
        return sections

    def _normalize_takeaways(self, sections):
        takeaways = []
        takeaways_text = sections.get("Takeaways", "")
        for line in takeaways_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                takeaways.append(stripped[2:].strip())
                continue
            numbered = re.match(r"^\d+\.\s+(.*)$", stripped)
            if numbered:
                takeaways.append(numbered.group(1).strip())

        deduped = []
        seen = set()
        for item in takeaways:
            key = item.lower()
            if key and key not in seen:
                seen.add(key)
                deduped.append(item)
        sections["Takeaways"] = "\n".join(f"- {item}" for item in deduped)
        return sections

    def _compose_one_pager_text(self, sections):
        ordered = [
            "Problem",
            "Method",
            "Key Results",
            "Why It Matters",
            "Limitations",
            "Takeaways",
        ]
        parts = []
        for title in ordered:
            body = (sections.get(title) or "").strip()
            if body:
                parts.append(f"{title}\n{body}")
        return "\n\n".join(parts).strip()

    def _open_one_pager_window(self, heading, body_text):
        popup = tk.Toplevel(self.root)
        popup.title("DeepSeek One-Pager")
        popup.geometry("860x560")
        popup.minsize(560, 360)
        popup.configure(bg="#F3F4F7")

        container = ttk.Frame(popup, padding=12, style="Body.TFrame")
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=heading,
            font=tkfont.Font(family="SF Pro Display", size=14, weight="bold"),
            wraplength=810,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 10))

        top_actions = ttk.Frame(container)
        top_actions.pack(fill="x", pady=(0, 6))
        ttk.Button(
            top_actions,
            text="Text to Audio",
            command=lambda: self._export_text_to_audio_content(body_text, heading, ""),
            style="Mac.TButton",
        ).pack(side="right")

        canvas = tk.Canvas(
            container,
            bg="#F3F4F7",
            highlightthickness=0,
            bd=0,
        )
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        sections_holder = tk.Frame(canvas, bg="#F3F4F7")
        canvas_window = canvas.create_window((0, 0), window=sections_holder, anchor="nw")

        def _on_holder_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfigure(canvas_window, width=event.width)

        sections_holder.bind("<Configure>", _on_holder_configure)
        canvas.bind("<Configure>", _on_canvas_configure)

        sections = self._parse_one_pager_sections(body_text)
        ordered_summary = ["Problem", "Method", "Key Results", "Why It Matters", "Limitations"]
        summary_lines = []
        for topic in ordered_summary:
            content = (sections.get(topic) or "").strip()
            if content:
                summary_lines.append((topic, content))

        if summary_lines:
            summary_card = self._create_card(sections_holder, "Summary")
            summary_body = tk.Frame(summary_card, bg="#FFFFFF")
            summary_body.pack(fill="x", padx=12, pady=(0, 12))
            for topic, content in summary_lines:
                row = tk.Frame(summary_body, bg="#FFFFFF")
                row.pack(fill="x", pady=(0, 10))
                headline = tk.Label(
                    row,
                    text=topic,
                    bg="#FFFFFF",
                    fg="#1D1D1F",
                    font=("SF Pro Display", 12, "bold"),
                    anchor="w",
                )
                headline.pack(fill="x", anchor="w", pady=(0, 3))
                body_label = tk.Label(
                    row,
                    text=self._strip_topic_prefix(content, topic),
                    bg="#FFFFFF",
                    fg="#2C2C2E",
                    font=("SF Pro Text", 11),
                    justify="left",
                    anchor="w",
                    wraplength=760,
                )
                body_label.pack(fill="x", anchor="w")

        takeaways = self._extract_takeaways(sections.get("Takeaways", ""))
        if takeaways:
            takeaway_card = self._create_card(sections_holder, "Takeaways")
            takeaway_body = tk.Frame(takeaway_card, bg="#FFFFFF")
            takeaway_body.pack(fill="x", padx=12, pady=(0, 12))
            for item in takeaways:
                row = tk.Frame(takeaway_body, bg="#FFFFFF")
                row.pack(fill="x", pady=(0, 7))
                dot = tk.Label(
                    row,
                    text="\u25AA",
                    bg="#FFFFFF",
                    fg="#2C2C2E",
                    font=("SF Pro Text", 12),
                    anchor="n",
                )
                dot.pack(side="left", padx=(0, 8))
                line = tk.Label(
                    row,
                    text=item,
                    bg="#FFFFFF",
                    fg="#2C2C2E",
                    font=("SF Pro Text", 11),
                    justify="left",
                    anchor="w",
                    wraplength=730,
                )
                line.pack(side="left", fill="x", expand=True)

    def _create_card(self, parent, title):
        card = tk.Frame(
            parent,
            bg="#FFFFFF",
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground="#DADCE3",
        )
        card.pack(fill="x", padx=4, pady=6)
        title_label = tk.Label(
            card,
            text=title,
            bg="#FFFFFF",
            fg="#1D1D1F",
            font=("SF Pro Display", 13, "bold"),
            anchor="w",
        )
        title_label.pack(fill="x", padx=12, pady=(10, 8))
        return card

    def _extract_takeaways(self, text):
        bullets = []
        for line in (text or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                bullets.append(stripped[2:].strip())
                continue
            numbered = re.match(r"^\d+\.\s+(.*)$", stripped)
            if numbered:
                bullets.append(numbered.group(1).strip())
        return [b for b in bullets if b]

    def _strip_topic_prefix(self, text, topic):
        if not text:
            return ""
        pattern = rf"^\s*(?:\d+\.\s*)?{re.escape(topic)}\s*:\s*"
        return re.sub(pattern, "", text.strip(), flags=re.IGNORECASE)

    def _build_folder_icon(self):
        icon = tk.PhotoImage(width=16, height=16)
        icon.put("#DDAA55", to=(1, 5, 14, 14))
        icon.put("#EFCF86", to=(2, 6, 13, 13))
        icon.put("#C99642", to=(1, 4, 7, 6))
        icon.put("#E8C06A", to=(2, 3, 8, 5))
        return icon

def main():
    root = tk.Tk()
    ArxivOpticsUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
