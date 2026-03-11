import datetime as dt
import asyncio
import json
import os
import re
import threading
import webbrowser
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote_plus, urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import uvicorn

try:
    import webview
except Exception:
    webview = None
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
TRANSCRIPTS_DIR_NAME = "Transcripts"
LEGACY_APP_STATE_PATH = os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "PaperSummarizer", "state.json")
APP_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_summarizer_state.json")
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def fetch_arxiv_articles(search_query=ARXIV_QUERY, max_results=MAX_RESULTS):
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
        published_text = entry.findtext("atom:published", default="", namespaces=ATOM_NS)
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
    sorted_dates = sorted(grouped.keys(), key=lambda d: (d != "Unknown date", d), reverse=True)
    return [(date_key, grouped[date_key]) for date_key in sorted_dates]


def wait_for_local_server(url, timeout_seconds=15.0):
    deadline = dt.datetime.now().timestamp() + timeout_seconds
    last_error = None
    while dt.datetime.now().timestamp() < deadline:
        try:
            with urlopen(url, timeout=1.5):
                return
        except Exception as exc:
            last_error = exc
        threading.Event().wait(0.25)
    raise RuntimeError(f"Timed out waiting for local server at {url}") from last_error


class PaperSummarizerService:
    def load_state(self):
        for path in (APP_STATE_PATH, LEGACY_APP_STATE_PATH):
            if not os.path.exists(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass
        return {}

    def save_state(self, payload):
        try:
            with open(APP_STATE_PATH, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
        except OSError:
            pass

    def current_query(self):
        state = self.load_state()
        query = str(state.get("last_query", "")).strip()
        if query:
            return query
        tabs = state.get("tabs", [])
        if tabs and isinstance(tabs[0], dict):
            query = str(tabs[0].get("query", "")).strip()
            if query:
                return query
        return ARXIV_QUERY

    def set_current_query(self, query):
        state = self.load_state()
        state["last_query"] = query
        self.save_state(state)

    def resolve_query(self, raw_query):
        value = (raw_query or "").strip()
        if not value:
            query = self.current_query()
            return query, "physics.optics" if query == ARXIV_QUERY else query
        if value.startswith("http://") or value.startswith("https://"):
            parsed = self.parse_arxiv_link_to_query(value)
            if not parsed:
                raise ValueError("Unsupported arXiv link. Use a list/search/abs/pdf URL or a raw arXiv query.")
            return parsed
        if value.startswith("cat:"):
            return value, value.split("cat:", 1)[1] or value
        return value, value

    def parse_arxiv_link_to_query(self, link_text):
        parsed = urlparse((link_text or "").strip())
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").strip("/")
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
            raw_q = (parse_qs(parsed.query).get("query", [""])[0] or "").strip()
            if raw_q:
                decoded = unquote_plus(raw_q)
                return f"all:{decoded}", f"search {decoded}"
        return None

    def papers_dir(self):
        return os.getenv("PAPERS_DIR", DEFAULT_PAPER_DOWNLOAD_DIR).strip() or DEFAULT_PAPER_DOWNLOAD_DIR

    def safe_filename(self, text, fallback):
        cleaned = re.sub(r'[<>:"/\\|?*]+', "_", (text or "").strip())
        cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
        cleaned = cleaned or fallback
        return cleaned[:150].rstrip()

    def extract_arxiv_id(self, source_url):
        clean = (source_url or "").strip()
        if "/abs/" in clean:
            return clean.split("/abs/", 1)[1].strip("/")
        if "/pdf/" in clean:
            return clean.split("/pdf/", 1)[1].replace(".pdf", "").strip("/")
        return ""

    def pdf_path(self, title, source_url):
        return os.path.join(self.papers_dir(), f"{self.safe_filename(title, 'paper')}.pdf")

    def onepager_path(self, title, source_url):
        base = os.path.join(self.papers_dir(), ONE_PAGER_DIR_NAME)
        name = self.safe_filename(title, "paper")
        arxiv_id = self.safe_filename(self.extract_arxiv_id(source_url), "")
        return os.path.join(base, f"{name} [{arxiv_id}].txt" if arxiv_id else f"{name}.txt")

    def audio_path(self, title, source_url):
        base = os.path.join(self.papers_dir(), AUDIO_DIR_NAME)
        name = self.safe_filename(title, "paper")
        arxiv_id = self.safe_filename(self.extract_arxiv_id(source_url), "")
        return os.path.join(base, f"{name} [{arxiv_id}].mp3" if arxiv_id else f"{name}.mp3")

    def podcast_audio_path(self, paper_count):
        base = os.path.join(self.papers_dir(), AUDIO_DIR_NAME)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        return os.path.join(base, f"Podcast_{paper_count}papers_{stamp}.mp3")

    def podcast_transcript_path(self, podcast_audio_path):
        audio_dir = os.path.dirname(podcast_audio_path)
        transcript_dir = os.path.join(audio_dir, TRANSCRIPTS_DIR_NAME)
        stem = os.path.splitext(os.path.basename(podcast_audio_path))[0]
        return os.path.join(transcript_dir, f"{stem}.txt")

    def fetch_papers(self, raw_query):
        query, label = self.resolve_query(raw_query)
        grouped = fetch_arxiv_articles(query, MAX_RESULTS)
        papers = []
        for date_key, items in grouped:
            for item in items:
                papers.append(
                    {
                        "id": item["url"],
                        "date": date_key,
                        "title": item["title"],
                        "authors": item["authors"],
                        "summary": item["summary"],
                        "url": item["url"],
                    }
                )
        self.set_current_query(query)
        return {
            "query": query,
            "label": re.sub(r"\s+", " ", label).strip() or "papers",
            "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "ollama_model": os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL,
            "papers": papers,
        }

    def build_one_pager_prompt(self, title, authors, abstract):
        return (
            "You are helping summarize a research paper for a technical reader.\n\n"
            f"Title: {title}\nAuthors: {authors}\nAbstract: {(abstract or '')[:10000]}\n\n"
            "Write a one-page summary with exactly these section headers:\n"
            "1. Problem\n2. Method\n3. Key Results\n4. Why It Matters\n5. Limitations\n6. Takeaways\n"
            "Use plain text only. Under Takeaways, use bullet lines starting with '- '."
        )

    def request_one_pager(self, title, authors, abstract):
        payload = json.dumps(
            {
                "model": os.getenv("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL).strip() or DEFAULT_OLLAMA_MODEL,
                "prompt": self.build_one_pager_prompt(title, authors, abstract),
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": 850},
            }
        )
        endpoint = os.getenv("OLLAMA_API_URL", OLLAMA_API_URL).strip() or OLLAMA_API_URL
        req = Request(endpoint, data=payload.encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=180) as response:
            raw = response.read()
        text = (json.loads(raw.decode("utf-8")).get("response") or "").strip()
        if not text:
            raise RuntimeError("DeepSeek returned an empty response. Check Ollama/model.")
        return self.normalize_one_pager(text)

    def normalize_one_pager(self, text):
        cleaned = (text or "").replace("**", "").replace("###", "").replace("##", "")
        cleaned = re.sub(r"(?m)^\s*#+\s*", "", cleaned)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return "\n".join(line.rstrip() for line in cleaned.splitlines()).strip()

    def get_one_pager(self, paper):
        title = (paper.get("title") or "").strip() or "Untitled paper"
        authors = (paper.get("authors") or "").strip() or "Unknown authors"
        abstract = (paper.get("summary") or "").strip()
        source_url = (paper.get("url") or "").strip()
        if not abstract:
            raise ValueError("This paper does not have an abstract available.")
        path = self.onepager_path(title, source_url)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                return {"text": handle.read().strip(), "path": path, "cached": True}
        text = self.request_one_pager(title, authors, abstract)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return {"text": text, "path": path, "cached": False}

    def get_or_create_one_pager_text(self, paper):
        title = (paper.get("title") or "").strip() or "Untitled paper"
        source_url = (paper.get("url") or "").strip()
        path = self.onepager_path(title, source_url)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        return self.get_one_pager(paper)["text"]

    def save_audio_with_edge_tts(self, text, out_path, voice, rate):
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

    def build_podcast_transcript_text(self, paper_blocks):
        chunks = []
        separator = "=" * 96
        for index, block in enumerate(paper_blocks, start=1):
            chunks.append(
                "\n".join(
                    [
                        f"PAPER {index}",
                        f"Title: {(block.get('title') or '').strip()}",
                        "",
                        "Abstract",
                        (block.get("abstract") or "").strip(),
                        "",
                        "One Pager",
                        (block.get("one_pager") or "").strip(),
                    ]
                )
            )
        return f"\n\n{separator}\n\n".join(chunks)

    def create_podcast(self, papers):
        if edge_tts is None:
            raise ValueError("Podcast export requires edge-tts. Install it with: python -m pip install edge-tts")

        selected = []
        for paper in papers:
            abstract = (paper.get("summary") or "").strip()
            if not abstract:
                continue
            selected.append(
                {
                    "title": (paper.get("title") or "").strip() or "paper",
                    "authors": (paper.get("authors") or "").strip() or "Unknown authors",
                    "abstract": abstract,
                    "url": (paper.get("url") or "").strip(),
                }
            )

        if not selected:
            raise ValueError("Select one or more valid papers first.")

        paper_blocks = []
        for paper in selected:
            try:
                one_pager = self.get_or_create_one_pager_text(paper)
            except Exception as exc:
                one_pager = f"One-pager generation failed: {exc}"
            paper_blocks.append(
                {
                    "title": paper["title"],
                    "abstract": paper["abstract"],
                    "one_pager": one_pager,
                }
            )

        full_text = self.build_podcast_transcript_text(paper_blocks).strip()
        if not full_text:
            raise ValueError("No text was available to synthesize.")

        out_path = self.podcast_audio_path(len(selected))
        transcript_path = self.podcast_transcript_path(out_path)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        os.makedirs(os.path.dirname(transcript_path), exist_ok=True)
        with open(transcript_path, "w", encoding="utf-8") as handle:
            handle.write(full_text)

        voice = os.getenv("EDGE_TTS_VOICE", "en-US-AriaNeural").strip() or "en-US-AriaNeural"
        rate = os.getenv("EDGE_TTS_RATE", "+0%").strip() or "+0%"
        self.save_audio_with_edge_tts(full_text, out_path, voice=voice, rate=rate)
        os.startfile(out_path)
        return {
            "audio_path": out_path,
            "transcript_path": transcript_path,
            "count": len(selected),
            "titles": [paper["title"] for paper in selected],
        }

    def ensure_pdf(self, paper):
        title = (paper.get("title") or "").strip() or "Untitled paper"
        source_url = (paper.get("url") or "").strip()
        if "/abs/" in source_url:
            pdf_url = f"https://arxiv.org/pdf/{source_url.split('/abs/', 1)[1].strip('/')}.pdf"
        elif "/pdf/" in source_url:
            pdf_url = f"https://arxiv.org/pdf/{source_url.split('/pdf/', 1)[1].replace('.pdf', '').strip('/')}.pdf"
        else:
            raise ValueError("Could not derive a PDF URL from the selected paper.")
        path = self.pdf_path(title, source_url)
        cached = os.path.exists(path)
        if not cached:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            req = Request(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
            with urlopen(req, timeout=90) as response:
                with open(path, "wb") as handle:
                    handle.write(response.read())
        os.startfile(path)
        return {"path": path, "cached": cached}

    def open_folder(self):
        folder = self.papers_dir()
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)
        return {"path": folder}


INDEX_HTML = """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Paper Summarizer</title><style>
:root{--bg:#eef2f5;--bg2:#dfe8ec;--w:rgba(252,253,255,.82);--p:rgba(255,255,255,.92);--l:rgba(21,53,75,.12);--i:#163042;--m:#647786;--a:#0d6c74;--a2:#094f57;--soft:rgba(13,108,116,.1);--soft2:rgba(13,108,116,.16);--s:0 24px 60px rgba(18,40,56,.14)}
*{box-sizing:border-box}body{margin:0;color:var(--i);font-family:"Segoe UI","Helvetica Neue",sans-serif;background:radial-gradient(circle at 0 0,rgba(255,255,255,.92),transparent 28%),radial-gradient(circle at 100% 15%,rgba(116,171,179,.24),transparent 22%),linear-gradient(145deg,var(--bg),var(--bg2))}
.shell{max-width:1420px;margin:0 auto;padding:28px 18px 36px}.window{border:1px solid rgba(255,255,255,.55);border-radius:28px;background:var(--w);box-shadow:var(--s);backdrop-filter:blur(20px);overflow:hidden}
.topbar,.toolbar{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:16px 22px;border-bottom:1px solid var(--l)}.topbar{background:linear-gradient(180deg,rgba(255,255,255,.8),rgba(247,250,252,.58))}.toolbar{background:rgba(248,251,252,.72);flex-wrap:wrap}
.traffic{display:flex;gap:8px}.traffic span{width:11px;height:11px;border-radius:50%;display:inline-block}.traffic span:nth-child(1){background:#f17f74}.traffic span:nth-child(2){background:#e8b860}.traffic span:nth-child(3){background:#5fc07c}
.eyebrow{margin:0 0 2px;color:var(--m);font-size:.78rem;text-transform:uppercase;letter-spacing:.12em;font-weight:700}h1{margin:0;font-size:clamp(1.5rem,3vw,2.5rem);line-height:1.05;letter-spacing:-.04em}.sub{margin-top:8px;color:var(--m);max-width:72ch;font-size:.96rem}
.input{min-height:46px;border-radius:999px;border:1px solid var(--l);background:rgba(255,255,255,.95);padding:0 18px;color:var(--i);font:inherit;outline:none}.query{flex:1 1 360px}.search{flex:1 1 260px}
.btn{border:1px solid transparent;border-radius:999px;padding:11px 16px;font:inherit;font-weight:700;cursor:pointer;text-decoration:none}.pri{background:var(--a);color:#f8feff}.sec{background:rgba(255,255,255,.72);border-color:var(--l);color:var(--i)}.ghost{background:transparent;border-color:var(--l);color:var(--m)}
.content{display:grid;grid-template-columns:340px minmax(0,1fr);min-height:76vh}.side{padding:18px;border-right:1px solid var(--l);background:linear-gradient(180deg,rgba(247,250,252,.72),rgba(242,247,248,.48));display:grid;grid-template-rows:minmax(0,1fr) auto;gap:14px;align-content:start}
.grid{display:grid;gap:12px}.stat{padding:16px;border-radius:18px;border:1px solid var(--l);background:var(--p)}.sl{color:var(--m);font-size:.76rem;text-transform:uppercase;letter-spacing:.08em;font-weight:700}.sv{margin-top:6px;font-size:1.35rem;font-weight:800;letter-spacing:-.04em}.sn{margin-top:6px;color:var(--m);font-size:.86rem;line-height:1.45;overflow-wrap:anywhere}
.pick{padding:16px;border-radius:18px;border:1px solid var(--l);background:var(--p);display:flex;flex-direction:column;min-height:0}.pickhead{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px}.picktitle{font-size:.94rem;font-weight:800}.mini{font-size:.78rem;color:var(--m)}
.pickactions{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.miniBtn{padding:7px 10px;border-radius:999px;border:1px solid var(--l);background:#fff;color:var(--i);font:inherit;font-size:.79rem;cursor:pointer}
.articlelist{display:grid;gap:8px;overflow:auto;max-height:none;flex:1;padding-right:4px}.item{display:grid;grid-template-columns:auto 1fr;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:14px;border:1px solid var(--l);background:rgba(255,255,255,.72);cursor:pointer}.item.active,.item.selected{border-color:rgba(13,108,116,.3);background:var(--soft)}.item input{margin-top:2px}.itemtitle{font-size:.84rem;line-height:1.3;font-weight:650}
.main{padding:22px}.head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}.lt{font-size:1.04rem;font-weight:800;letter-spacing:-.03em}.ls,.status,.meta{color:var(--m);font-size:.88rem}.status{text-align:right;min-height:22px}
.list{display:grid;gap:14px}.empty{padding:28px;border:1px dashed rgba(21,53,75,.22);border-radius:18px;background:rgba(255,255,255,.66);color:var(--m);text-align:center}
.card{padding:18px;border-radius:22px;border:1px solid var(--l);background:var(--p)}.card.selected{border-color:rgba(13,108,116,.34);box-shadow:inset 0 0 0 1px var(--soft2)}.cardhead{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:8px}.pill{display:inline-flex;align-items:center;padding:4px 10px;border-radius:999px;font-size:.76rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;background:rgba(13,108,116,.1);color:var(--a2)}
.checkrow{display:flex;align-items:center;gap:10px}.title{font-size:1.14rem;line-height:1.38;font-weight:750;margin:0 0 8px}.meta{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px}.preview{margin:0 0 14px;color:#284456;font-size:.96rem;line-height:1.55}.actions{display:flex;gap:10px;flex-wrap:wrap}
.modalwrap{position:fixed;inset:0;background:rgba(15,31,43,.4);display:none;align-items:center;justify-content:center;padding:24px}.modalwrap.open{display:flex}.modal{width:min(920px,100%);max-height:88vh;overflow:auto;padding:24px;border-radius:28px;background:rgba(246,250,252,.95);box-shadow:var(--s)}.mbody{white-space:pre-wrap;line-height:1.65;color:#243f50;background:rgba(255,255,255,.7);border:1px solid var(--l);border-radius:18px;padding:18px}
@media(max-width:1100px){.content{grid-template-columns:1fr}.side{border-right:0;border-bottom:1px solid var(--l)}.articlelist{max-height:24vh;flex:none}.pick{min-height:auto}} @media(max-width:760px){.topbar,.toolbar,.head{display:grid}}
</style></head><body><main class='shell'><section class='window'><header class='topbar'><div><div class='traffic'><span></span><span></span><span></span></div></div><div style='flex:1'><div class='eyebrow'>Paper Summarizer</div><h1>arXiv Research Console</h1><div class='sub'>Browse all loaded titles in the compact left rail, multi-select papers, and create one podcast from the selected set.</div></div><button class='btn pri' id='refreshButton'>Refresh</button></header>
<section class='toolbar'><input class='input query' id='queryInput' placeholder='cat:physics.optics or paste an arXiv URL'><input class='input search' id='searchInput' placeholder='Filter loaded papers...'><div style='display:flex;gap:10px;flex-wrap:wrap'><button class='btn sec' id='applyQueryButton'>Set Source</button><button class='btn sec' id='podcastButton'>Create Podcast</button><button class='btn sec' id='openFolderButton'>Open Papers Folder</button></div></section>
<section class='content'><aside class='side'><div class='pick'><div class='pickhead'><div class='picktitle'>Article List</div><div class='mini' id='listCount'>0 shown • 0 selected</div></div><div class='pickactions'><button class='miniBtn' id='selectAllButton'>Select all shown</button><button class='miniBtn' id='clearSelectionButton'>Clear</button></div><div class='articlelist' id='articleList'></div></div><div class='grid'><div class='stat'><div class='sl'>Source</div><div class='sn' id='sourceLabel'>Loading...</div></div><div class='stat'><div class='sl'>Model</div><div class='sn' id='modelName'>-</div></div><div class='stat'><div class='sl'>Papers</div><div class='sv' id='count'>0</div><div class='sn'>Loaded from the current arXiv source query.</div></div><div class='stat'><div class='sl'>Last Check</div><div class='sn' id='checked'>Checking source...</div></div></div></aside>
<section class='main'><div class='head'><div><div class='lt'>All Articles</div><div class='ls'>Every loaded article stays visible on the right. Use the left rail for a compact full list and selection.</div></div><div class='status' id='status'>Ready.</div></div><section class='list' id='papers'></section></section></section></section></main>
<section class='modalwrap' id='modalWrap'><article class='modal'><div style='display:flex;justify-content:space-between;gap:16px;margin-bottom:18px'><div><h2 id='modalTitle' style='margin:0'></h2><div id='modalMeta' class='ls' style='margin-top:8px'></div></div><button class='btn sec' id='closeModalButton'>Close</button></div><div class='mbody' id='modalBody'></div></article></section>
<script>
let allPapers=[],currentQuery="",currentLabel="",selectedIds=new Set(),activeId="";
const el=id=>document.getElementById(id);
const setStatus=m=>el("status").textContent=m;
const truncate=(t,n=260)=>{t=(t||"").trim();return t.length<=n?t:t.slice(0,n-3)+"...";}
async function fetchJson(url,options={}){const r=await fetch(url,options);if(!r.ok){let d="Request failed";try{d=(await r.json()).detail||d}catch{d=await r.text()||d}throw new Error(d)}return r.json()}
function openModal(title,body,meta=""){el("modalTitle").textContent=title;el("modalBody").textContent=body;el("modalMeta").textContent=meta;el("modalWrap").classList.add("open")}
function closeModal(){el("modalWrap").classList.remove("open")}
function filteredPapers(){const q=el("searchInput").value.trim().toLowerCase();if(!q)return allPapers;return allPapers.filter(p=>p.title.toLowerCase().includes(q)||p.authors.toLowerCase().includes(q)||p.summary.toLowerCase().includes(q)||p.date.toLowerCase().includes(q))}
function updateListSummary(shownCount){el("listCount").textContent=`${shownCount} shown • ${selectedIds.size} selected`}
function toggleSelect(id,checked){if(checked)selectedIds.add(id);else selectedIds.delete(id);updateSelectedCount();renderArticleList();renderPapers()}
function focusPaper(id){activeId=id;renderArticleList();renderPapers()}
function selectedPapers(){return allPapers.filter(p=>selectedIds.has(p.id))}
function updateSelectedCount(){updateListSummary(filteredPapers().length)}
function renderArticleList(){const papers=filteredPapers(),container=el("articleList");container.innerHTML="";updateListSummary(papers.length);if(!papers.length){const empty=document.createElement("div");empty.className="empty";empty.textContent="No papers match the current filter.";container.append(empty);return}for(const paper of papers){const item=document.createElement("label");item.className=`item ${selectedIds.has(paper.id)?"selected":""} ${activeId===paper.id?"active":""}`;const check=document.createElement("input");check.type="checkbox";check.checked=selectedIds.has(paper.id);check.addEventListener("click",e=>e.stopPropagation());check.addEventListener("change",e=>toggleSelect(paper.id,e.target.checked));const body=document.createElement("div");const title=document.createElement("div");title.className="itemtitle";title.textContent=paper.title;body.append(title);item.append(check,body);item.addEventListener("click",()=>focusPaper(paper.id));container.append(item)}}
function renderPapers(){const papers=filteredPapers(),container=el("papers");container.innerHTML="";if(!papers.length){const empty=document.createElement("div");empty.className="empty";empty.textContent="No papers match the current filter.";container.append(empty);return}for(const paper of papers){const card=document.createElement("article");card.className=`card ${selectedIds.has(paper.id)?"selected":""}`;const head=document.createElement("div");head.className="cardhead";const left=document.createElement("div");left.className="checkrow";const checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=selectedIds.has(paper.id);checkbox.addEventListener("change",e=>toggleSelect(paper.id,e.target.checked));const pill=document.createElement("span");pill.className="pill";pill.textContent=paper.date||"Unknown date";left.append(checkbox,pill);const focusBtn=document.createElement("button");focusBtn.className="btn ghost";focusBtn.type="button";focusBtn.textContent="Focus";focusBtn.addEventListener("click",()=>focusPaper(paper.id));head.append(left,focusBtn);const title=document.createElement("h3");title.className="title";title.textContent=paper.title;const meta=document.createElement("div");meta.className="meta";meta.innerHTML=`<span>${paper.authors}</span><span>${currentLabel||currentQuery}</span>`;const preview=document.createElement("p");preview.className="preview";preview.textContent=truncate(paper.summary||"No abstract available.");const actions=document.createElement("div");actions.className="actions";for(const [label,handler] of [["Abstract",()=>openModal(paper.title,paper.summary||"No abstract available.",`${paper.authors} | ${paper.date}`)],["One-Pager",()=>onePager(paper)],["Open PDF",()=>openPdf(paper)]]){const b=document.createElement("button");b.className="btn sec";b.textContent=label;b.type="button";b.addEventListener("click",handler);actions.append(b)}const link=document.createElement("a");link.className="btn pri";link.href=paper.url;link.target="_blank";link.rel="noreferrer";link.textContent="Open arXiv";actions.append(link);card.append(head,title,meta,preview,actions);container.append(card)}}
async function refresh(queryOverride=null){const button=el("refreshButton");button.disabled=true;setStatus("Refreshing papers...");try{const query=queryOverride??el("queryInput").value.trim()??currentQuery;const data=await fetchJson(`/api/papers?query=${encodeURIComponent(query||"")}`);currentQuery=data.query;currentLabel=data.label;allPapers=data.papers;activeId=allPapers[0]?.id||"";selectedIds=new Set([...selectedIds].filter(id=>allPapers.some(p=>p.id===id)));el("queryInput").value=data.query;el("count").textContent=String(data.papers.length);el("sourceLabel").textContent=data.label||data.query||"Unknown";el("checked").textContent=data.checked_at?new Date(data.checked_at).toLocaleString():"Not checked yet";el("modelName").textContent=data.ollama_model||"-";updateSelectedCount();renderArticleList();renderPapers();setStatus(`Loaded ${data.papers.length} papers.`)}catch(error){setStatus("Refresh failed.");throw error}finally{button.disabled=false}}
async function onePager(paper){setStatus("Preparing one-pager...");try{const data=await fetchJson("/api/papers/onepager",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(paper)});openModal(paper.title,data.text,`${data.cached?"Loaded saved one-pager":"Generated new one-pager"}${data.path?` | ${data.path}`:""}`);setStatus(data.cached?"Opened saved one-pager.":"Generated one-pager.")}catch(error){setStatus(error.message)}}
async function openPdf(paper){setStatus("Preparing PDF...");try{const data=await fetchJson("/api/papers/pdf",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(paper)});setStatus(data.cached?"Opened saved PDF.":"Downloaded PDF.")}catch(error){setStatus(error.message)}}
async function createPodcast(){const papers=selectedPapers();if(!papers.length){setStatus("Select one or more papers first.");return}setStatus(`Creating podcast for ${papers.length} papers...`);try{const data=await fetchJson("/api/podcast",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({papers})});openModal(`Podcast ready: ${data.count} papers`,data.titles.join("\\n"),`Audio: ${data.audio_path} | Transcript: ${data.transcript_path}`);setStatus(`Podcast saved for ${data.count} papers.`)}catch(error){setStatus(error.message)}}
async function openFolder(){setStatus("Opening papers folder...");try{await fetchJson("/api/folder/open",{method:"POST"});setStatus("Opened papers folder.")}catch(error){setStatus(error.message)}}
el("refreshButton").addEventListener("click",()=>refresh());el("applyQueryButton").addEventListener("click",()=>refresh(el("queryInput").value.trim()));el("podcastButton").addEventListener("click",createPodcast);el("openFolderButton").addEventListener("click",openFolder);el("searchInput").addEventListener("input",()=>{renderArticleList();renderPapers()});el("selectAllButton").addEventListener("click",()=>{for(const paper of filteredPapers())selectedIds.add(paper.id);updateSelectedCount();renderArticleList();renderPapers()});el("clearSelectionButton").addEventListener("click",()=>{selectedIds.clear();updateSelectedCount();renderArticleList();renderPapers()});el("closeModalButton").addEventListener("click",closeModal);el("modalWrap").addEventListener("click",e=>{if(e.target.id==="modalWrap")closeModal()});document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal()});refresh().catch(error=>{el("count").textContent="0";el("checked").textContent="Failed";setStatus(error.message)});
</script></body></html>"""


service = PaperSummarizerService()
app = FastAPI(title="Paper Summarizer")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(INDEX_HTML)


@app.get("/api/papers")
async def get_papers(query: str = ""):
    try:
        return service.fetch_papers(query)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/papers/onepager")
async def create_one_pager(payload: dict[str, Any]):
    try:
        return service.get_one_pager(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/papers/pdf")
async def open_or_download_pdf(payload: dict[str, Any]):
    try:
        return service.ensure_pdf(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/podcast")
async def create_podcast(payload: dict[str, Any]):
    try:
        return service.create_podcast(payload.get("papers", []))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/folder/open")
async def open_papers_folder():
    try:
        return service.open_folder()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def run_paper_summarizer_web_app():
    url = "http://127.0.0.1:8001"

    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=8001, reload=False)

    server_thread = threading.Thread(target=run_server, name="paper-summarizer-server", daemon=True)
    server_thread.start()
    wait_for_local_server(url)
    if webview is None:
        print("pywebview is unavailable. Opening Paper Summarizer in your default browser instead.")
        webbrowser.open(url)
        server_thread.join()
        return
    try:
        webview.create_window("Paper Summarizer", url, width=1380, height=940)
        webview.start()
    except Exception as exc:
        print(
            "pywebview failed to start its native Windows backend. "
            "Opening Paper Summarizer in your default browser instead.\n"
            f"Reason: {exc}\n"
            "If you want the desktop window later, install the missing dependency with:\n"
            "python -m pip install pycparser"
        )
        webbrowser.open(url)
        server_thread.join()


if __name__ == "__main__":
    run_paper_summarizer_web_app()
