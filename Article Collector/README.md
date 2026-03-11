# Article Collector

Desktop Windows app for tracking new articles from Slavoj Zizek's Substack and opening the originals.

## Features

- Uses `https://substack.com/@slavojzizek` as the visible source page, while relying on the publication feed and post page underneath for reliable article discovery.
- Marks unseen articles in bold.
- Opens the original article on Substack when you click the title.
- Runs in a native Windows window using `pywebview` while keeping the same app UI.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
python app.py
```

This now opens the app in its own desktop window.

## Notes

- `state.json` is created automatically and stores which articles are still considered new.
- If `pywebview` is missing, `python app.py` will stop with an install message instead of opening the app.
- The local FastAPI server still runs on `http://127.0.0.1:8000` behind the desktop window.
