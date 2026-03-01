import datetime as dt
import json
import os
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
        self.root.option_add("*Font", "{Times New Roman} 11")
        style = ttk.Style(self.root)
        style.configure(".", font=("Times New Roman", 11))
        style.configure("Treeview.Heading", font=("Times New Roman", 11, "bold"))
        style.configure("Treeview", borderwidth=1, relief="solid")

        self.url_by_item = {}
        self.authors_by_item = {}
        self.summary_by_item = {}
        self.one_pager_by_item = {}
        self.one_pager_inflight = set()

        self._build_ui()
        self.root.bind_all("<Control-d>", self.download_selected_pdf)
        self.refresh_data()

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(14, 10))
        header.pack(fill="x")
        button_padx = 4

        title_font = tkfont.Font(family="Times New Roman", size=14, weight="bold")
        title_label = ttk.Label(
            header,
            text="Recent papers from arXiv: physics.optics",
            font=title_font,
        )
        title_label.pack(side="left")

        one_pager_btn = ttk.Button(
            header, text="One Pager (DeepSeek)", command=self.open_or_generate_one_pager
        )
        one_pager_btn.pack(side="right", padx=button_padx)

        download_btn = ttk.Button(
            header, text="Open PDF", command=self.download_selected_pdf
        )
        download_btn.pack(side="right", padx=button_padx)

        summary_btn = ttk.Button(
            header,
            text="Abstract",
            command=self.open_summary_window,
        )
        summary_btn.pack(side="right", padx=button_padx)

        refresh_btn = ttk.Button(header, text="Refresh", command=self.refresh_data)
        refresh_btn.pack(side="right", padx=button_padx)

        body = ttk.Frame(self.root, padding=(14, 4, 14, 14))
        body.pack(fill="both", expand=True)

        cols = ("title", "authors")
        self.tree = ttk.Treeview(body, columns=cols, show="tree headings")
        self.tree.heading("#0", text="Published Date")
        self.tree.heading("title", text="Title")
        self.tree.heading("authors", text="Authors")
        self.tree.column("#0", width=130, stretch=False)
        self.tree.column("title", width=520, stretch=True)
        self.tree.column("authors", width=280, stretch=True)
        self.tree.bind("<Double-1>", self.open_selected_link)
        self.tree.bind("<Return>", self.open_selected_link)
        self.tree.tag_configure("date_group", background="#E9EEF5")
        self.tree.tag_configure("paper_even", background="#FFFFFF")
        self.tree.tag_configure("paper_odd", background="#F6F8FB")

        vscroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        hscroll = ttk.Scrollbar(body, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vscroll.set, xscrollcommand=hscroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vscroll.grid(row=0, column=1, sticky="ns")
        hscroll.grid(row=1, column=0, sticky="ew")

        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Loading...")
        status = ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", padding=(14, 0, 14, 8)
        )
        status.pack(fill="x")

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
            font=tkfont.Font(family="Times New Roman", size=11, weight="bold"),
            wraplength=710,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 10))

        summary_text = tk.Text(container, wrap="word", font=("Times New Roman", 11))
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
            self._open_text_window("DeepSeek One-Pager", paper_title, cached)
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
        self.one_pager_by_item[item_id] = text
        self.status_var.set("One-pager ready.")
        self._open_text_window("DeepSeek One-Pager", title, text)

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
            "Write a one-page summary with exactly these sections:\n"
            "1. Problem\n"
            "2. Method\n"
            "3. Key Results\n"
            "4. Why It Matters\n"
            "5. Limitations\n"
            "6. Five Bullet Takeaways\n\n"
            "Requirements:\n"
            "- Be factual and concise.\n"
            "- If information is missing from the abstract, state that clearly.\n"
            "- Do not invent quantitative results."
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

    def _open_text_window(self, window_title, heading, body_text):
        popup = tk.Toplevel(self.root)
        popup.title(window_title)
        popup.geometry("860x560")
        popup.minsize(560, 360)

        container = ttk.Frame(popup, padding=12)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text=heading,
            font=tkfont.Font(family="Times New Roman", size=11, weight="bold"),
            wraplength=810,
            justify="left",
        ).pack(anchor="w", fill="x", pady=(0, 10))

        text_widget = tk.Text(container, wrap="word", font=("Times New Roman", 11))
        text_widget.pack(fill="both", expand=True)
        text_widget.insert("1.0", body_text)
        text_widget.configure(state="disabled")

def main():
    root = tk.Tk()
    ttk.Style(root).theme_use("clam")
    ArxivOpticsUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
