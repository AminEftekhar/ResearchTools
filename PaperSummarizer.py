import datetime as dt
import json
import os
import re
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox, ttk
from urllib.error import URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
import webbrowser
import xml.etree.ElementTree as ET

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_QUERY = "cat:physics.optics"
MAX_RESULTS = 120
DEFAULT_OLLAMA_MODEL = "deepseek-r1:1.5b"
OLLAMA_API_URL = "http://localhost:11434/api/generate"
DEFAULT_PAPER_DOWNLOAD_DIR = r"C:\Users\Amin\OneDrive\Documents\Papers"

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def fetch_recent_optics_articles(max_results=MAX_RESULTS):
    """Fetch recent physics.optics papers from arXiv API and group by date."""
    query_url = (
        f"{ARXIV_API_URL}?search_query={quote_plus(ARXIV_QUERY)}"
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

        self.url_by_item = {}
        self.authors_by_item = {}
        self.summary_by_item = {}
        self.one_pager_by_item = {}
        self.one_pager_inflight = set()

        self._build_ui()
        self.root.bind_all("<Control-d>", self.download_selected_pdf)
        self.refresh_data()

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
        title_label = ttk.Label(
            header,
            text="Recent papers from arXiv: physics.optics",
            font=title_font,
            style="Title.TLabel",
        )
        title_label.pack(side="left")

        one_pager_btn = ttk.Button(
            header,
            text="One Pager (DeepSeek)",
            command=self.open_or_generate_one_pager,
            style="Mac.TButton",
        )
        one_pager_btn.pack(side="right", padx=button_padx)

        download_btn = ttk.Button(
            header, text="Open PDF", command=self.download_selected_pdf, style="Mac.TButton"
        )
        download_btn.pack(side="right", padx=button_padx)

        summary_btn = ttk.Button(
            header,
            text="Abstract",
            command=self.open_summary_window,
            style="Mac.TButton",
        )
        summary_btn.pack(side="right", padx=button_padx)

        body = ttk.Frame(self.root, style="Body.TFrame", padding=(14, 8, 14, 14))
        body.pack(fill="both", expand=True)

        cols = ("title", "authors")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings", style="Mac.Treeview")
        self.tree.heading("#0", text="Published Date")
        self.tree.heading("title", text="Title")
        self.tree.heading("authors", text="Authors")
        self.tree.column("#0", width=130, stretch=False)
        self.tree.column("title", width=520, stretch=True)
        self.tree.column("authors", width=280, stretch=True)
        self.tree.bind("<Double-1>", self.open_selected_link)
        self.tree.bind("<Return>", self.open_selected_link)
        self.tree.tag_configure("date_group", background="#F2F2F3", foreground="#3A3A3C")
        self.tree.tag_configure("paper_even", background="#FFFFFF")
        self.tree.tag_configure("paper_odd", background="#FAFAFB")

        vscroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")

        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Loading...")
        footer = ttk.Frame(self.root, style="Footer.TFrame", padding=(14, 4, 14, 10))
        footer.pack(fill="x")

        status = ttk.Label(footer, textvariable=self.status_var, anchor="w", style="Status.TLabel")
        status.pack(side="left", fill="x", expand=True)

        refresh_btn = ttk.Button(footer, text="Refresh", command=self.refresh_data, style="Mac.TButton")
        refresh_btn.pack(side="right", padx=button_padx)

        open_folder_btn = ttk.Button(
            footer, text="Open Papers Folder", command=self.open_papers_folder, style="Mac.TButton"
        )
        open_folder_btn.pack(side="right", padx=button_padx)

    def refresh_data(self):
        self.status_var.set("Fetching latest entries...")
        self.root.update_idletasks()
        self.tree.delete(*self.tree.get_children())
        self.url_by_item.clear()
        self.authors_by_item.clear()
        self.summary_by_item.clear()
        self.one_pager_by_item.clear()
        self.one_pager_inflight.clear()

        try:
            grouped = fetch_recent_optics_articles()
        except URLError as exc:
            self.status_var.set("Network error while fetching papers.")
            messagebox.showerror("Fetch failed", f"Could not reach arXiv.\n\n{exc}")
            return
        except ET.ParseError as exc:
            self.status_var.set("Response parsing failed.")
            messagebox.showerror("Parse failed", f"Could not parse arXiv response.\n\n{exc}")
            return
        except Exception as exc:
            self.status_var.set("Unexpected error while fetching papers.")
            messagebox.showerror("Error", f"Unexpected error:\n\n{exc}")
            return

        total_count = 0
        for date_key, papers in grouped:
            date_item = self.tree.insert(
                "", "end", text=date_key, values=("", ""), tags=("date_group",)
            )
            for paper in papers:
                row_tag = "paper_even" if total_count % 2 == 0 else "paper_odd"
                item_id = self.tree.insert(
                    date_item,
                    "end",
                    text="",
                    values=(paper["title"], paper["authors"]),
                    tags=(row_tag,),
                )
                self.url_by_item[item_id] = paper["url"]
                self.authors_by_item[item_id] = paper["authors"]
                self.summary_by_item[item_id] = paper["summary"]
                total_count += 1

        for top_item in self.tree.get_children():
            self.tree.item(top_item, open=True)

        self.status_var.set(
            f"Loaded {total_count} papers in {len(grouped)} publication date groups."
        )

    def open_selected_link(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        item_id = selected[0]
        url = self.url_by_item.get(item_id)
        if url:
            webbrowser.open(url)

    def download_selected_pdf(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select a paper first.")
            return

        item_id = selected[0]
        source_url = self.url_by_item.get(item_id)
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

        self.status_var.set("Downloading PDF...")
        self.root.update_idletasks()
        try:
            save_dir = os.getenv("PAPERS_DIR", DEFAULT_PAPER_DOWNLOAD_DIR).strip() or DEFAULT_PAPER_DOWNLOAD_DIR
            os.makedirs(save_dir, exist_ok=True)

            filename = self._filename_from_pdf_url(pdf_url)
            file_path = os.path.join(save_dir, filename)

            req = Request(
                pdf_url,
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET",
            )
            with urlopen(req, timeout=90) as response:
                pdf_data = response.read()

            with open(file_path, "wb") as f:
                f.write(pdf_data)

            self.status_var.set(f"Downloaded PDF: {filename}")
            os.startfile(file_path)
        except URLError as exc:
            self.status_var.set("PDF download failed.")
            messagebox.showerror("Download failed", f"Could not download PDF.\n\n{exc}")
        except OSError as exc:
            self.status_var.set("Could not save or open PDF.")
            messagebox.showerror("File error", f"Could not save/open PDF.\n\n{exc}")
        except Exception as exc:
            self.status_var.set("Unexpected error during PDF download.")
            messagebox.showerror("Error", f"Unexpected error:\n\n{exc}")

    def open_papers_folder(self):
        try:
            papers_dir = os.getenv("PAPERS_DIR", DEFAULT_PAPER_DOWNLOAD_DIR).strip() or DEFAULT_PAPER_DOWNLOAD_DIR
            os.makedirs(papers_dir, exist_ok=True)
            os.startfile(papers_dir)
        except OSError as exc:
            messagebox.showerror("Folder error", f"Could not open papers folder.\n\n{exc}")

    def open_summary_window(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select a paper first.")
            return

        item_id = selected[0]
        summary = self.summary_by_item.get(item_id)
        if not summary:
            messagebox.showinfo(
                "Summary unavailable",
                "The selected row is not a paper, or the summary is missing.",
            )
            return

        paper_title = self.tree.item(item_id, "values")[0]

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

    def open_or_generate_one_pager(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("No paper selected", "Select a paper first.")
            return

        item_id = selected[0]
        abstract = self.summary_by_item.get(item_id)
        if not abstract:
            messagebox.showinfo(
                "One pager unavailable",
                "The selected row is not a paper, or the abstract is missing.",
            )
            return

        paper_title = self.tree.item(item_id, "values")[0]
        cached = self.one_pager_by_item.get(item_id)
        if cached:
            normalized = self._normalize_one_pager_text(cached)
            self.one_pager_by_item[item_id] = normalized
            self._open_one_pager_window(paper_title, normalized)
            return

        if item_id in self.one_pager_inflight:
            messagebox.showinfo(
                "In progress",
                "One-pager generation is already running for this paper.",
            )
            return

        authors = self.authors_by_item.get(item_id, "Unknown authors")
        self.one_pager_inflight.add(item_id)
        self.status_var.set("Generating one-pager with DeepSeek...")

        worker = threading.Thread(
            target=self._generate_one_pager_worker,
            args=(item_id, paper_title, authors, abstract),
            daemon=True,
        )
        worker.start()

    def _generate_one_pager_worker(self, item_id, title, authors, abstract):
        try:
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
                raise RuntimeError(
                    "DeepSeek returned an empty response. Check Ollama and model availability."
                )
        except Exception as exc:
            self.root.after(0, lambda: self._on_one_pager_error(item_id, exc))
            return

        self.root.after(0, lambda: self._on_one_pager_ready(item_id, title, text))

    def _on_one_pager_ready(self, item_id, title, text):
        self.one_pager_inflight.discard(item_id)
        normalized = self._normalize_one_pager_text(text)
        self.one_pager_by_item[item_id] = normalized
        self.status_var.set("One-pager ready.")
        self._open_one_pager_window(title, normalized)

    def _on_one_pager_error(self, item_id, exc):
        self.one_pager_inflight.discard(item_id)
        self.status_var.set("One-pager generation failed.")
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

def main():
    root = tk.Tk()
    ArxivOpticsUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
