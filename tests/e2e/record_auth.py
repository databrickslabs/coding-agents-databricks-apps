"""Record Databricks Apps SSO state and exit once redirected to CoDA."""

from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--output", default=str(Path(__file__).with_name("auth.json")))
    args = parser.parse_args()
    target_host = urlparse(args.url).netloc

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(args.url)
        page.wait_for_function(
            "host => location.host === host && !location.pathname.startsWith('/login')",
            arg=target_host,
            timeout=300_000,
        )
        context.storage_state(path=args.output)
        browser.close()
    print(f"Auth state saved to {args.output}")


if __name__ == "__main__":
    main()
