# Magpie

Quietly collects social media screenshots into a tidy local archive.

## Requirements

- Python 3.9+

## Quickstart

1. Create and activate a virtual environment:
   - `python3 -m venv .venv`
   - `source .venv/bin/activate`
2. Install dependencies:
   - `pip install -r requirements.txt`
3. Install Playwright browser:
   - `playwright install chromium`
4. Run capture:
   - `python -m magpie.cli capture --accounts accounts.example.txt --start-date 2026/03/01 --end-date 2026/03/03`

## Usage

CLI:
- `python -m magpie.cli capture --accounts PATH [--output-dir PATH] [--start-date YYYY/MM/DD] [--end-date YYYY/MM/DD] [--max-posts-per-account INT]`

Supported account domains:
- `x.com`
- `instagram.com`
- `reddit.com`

Output screenshots are saved under:
- `./output/screenshots/<platform>__<account_slug>/YYYYMMDD__NNN__<post_url_slug>.png`
