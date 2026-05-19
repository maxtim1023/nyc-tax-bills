#!/usr/bin/env python3
"""Download NYC property tax bills for a given address using Playwright."""

import argparse
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# NYC borough name → code mapping
BOROUGH_MAP = {
    "manhattan": "1", "mn": "1", "new york": "1", "ny": "1", "1": "1",
    "bronx": "2", "the bronx": "2", "bx": "2", "2": "2",
    "brooklyn": "3", "bk": "3", "kings": "3", "3": "3",
    "queens": "4", "qn": "4", "4": "4",
    "staten island": "5", "si": "5", "richmond": "5", "5": "5",
}

# Bills to download: (human label, regex to match on page, output filename)
TARGET_BILLS = [
    ("June 2025",     re.compile(r"jun\w*[\s/\-]*2025", re.IGNORECASE), "tax_bill_june_2025.pdf"),
    ("November 2025", re.compile(r"nov\w*[\s/\-]*2025", re.IGNORECASE), "tax_bill_november_2025.pdf"),
]


def parse_address(raw: str) -> tuple[str, str]:
    """Split '123 Main Street' → ('123', 'Main Street')."""
    parts = raw.strip().split(None, 1)
    if len(parts) < 2:
        raise ValueError(
            f"Address must include a house number and a street name (got: {raw!r})"
        )
    return parts[0], parts[1]


def resolve_borough(value: str) -> str:
    key = value.strip().lower()
    code = BOROUGH_MAP.get(key)
    if not code:
        raise ValueError(
            f"Unrecognised borough {value!r}. "
            f"Use one of: Manhattan, Bronx, Brooklyn, Queens, 'Staten Island', or 1-5."
        )
    return code


def click_text(page: Page, text: str, timeout: int = 30_000) -> None:
    """Click the first visible element whose text matches exactly or contains the given string."""
    locator = page.get_by_text(text, exact=False).first
    locator.wait_for(state="visible", timeout=timeout)
    locator.click()


def wait_stable(page: Page, timeout: int = 30_000) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)


def fill_address_form(page: Page, house_number: str, street_name: str, borough_code: str) -> None:
    """
    Fill the DOF property address search form.
    The form has three fields: borough (select), house number, street name.
    We probe for them by name/id/label because the selectors can vary.
    """
    # Borough dropdown — try by label text first, then common names/ids
    borough_selectors = [
        "select[name*='boro' i]",
        "select[id*='boro' i]",
        "select[name*='borough' i]",
        "select[id*='borough' i]",
        "select",  # last resort: first select on the page
    ]
    for sel in borough_selectors:
        loc = page.locator(sel).first
        if loc.is_visible():
            loc.select_option(value=borough_code)
            break

    # House-number input
    house_selectors = [
        "input[name*='housenum' i]",
        "input[id*='housenum' i]",
        "input[name*='house' i]",
        "input[id*='house' i]",
        "input[placeholder*='house' i]",
        "input[placeholder*='number' i]",
    ]
    for sel in house_selectors:
        loc = page.locator(sel).first
        if loc.is_visible():
            loc.fill(house_number)
            break

    # Street-name input
    street_selectors = [
        "input[name*='street' i]",
        "input[id*='street' i]",
        "input[placeholder*='street' i]",
    ]
    for sel in street_selectors:
        loc = page.locator(sel).first
        if loc.is_visible():
            loc.fill(street_name)
            break


def find_bill_link(page: Page, pattern: re.Pattern):
    """Return a Locator for a link whose text matches *pattern*, or None."""
    # Collect all <a> tags and check text content
    anchors = page.locator("a").all()
    for anchor in anchors:
        try:
            text = anchor.inner_text(timeout=500).strip()
        except Exception:
            continue
        if pattern.search(text):
            return anchor

    # Also check table cells (sometimes the date is plain text that triggers a JS click)
    cells = page.locator("td, th").all()
    for cell in cells:
        try:
            text = cell.inner_text(timeout=500).strip()
        except Exception:
            continue
        if pattern.search(text):
            return cell

    return None


def download_pdf(context: BrowserContext, page: Page, element, output_path: Path) -> None:
    """
    Click *element* and save the resulting PDF.
    Handles three cases:
      1. A new browser tab/popup opens with the PDF URL.
      2. The browser triggers a file-download event.
      3. The current page navigates to the PDF.
    """
    # --- Try popup / new-tab first ---
    try:
        with context.expect_page(timeout=8_000) as popup_info:
            element.click()
        popup = popup_info.value
        popup.wait_for_load_state("domcontentloaded", timeout=30_000)
        pdf_url = popup.url

        # Fetch the PDF bytes using the same session context
        response = context.request.get(pdf_url, timeout=60_000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} fetching {pdf_url}")
        output_path.write_bytes(response.body())
        popup.close()
        return
    except PlaywrightTimeoutError:
        pass  # No popup opened — fall through

    # --- Try download event ---
    try:
        with page.expect_download(timeout=8_000) as dl_info:
            element.click()
        download = dl_info.value
        download.save_as(output_path)
        return
    except PlaywrightTimeoutError:
        pass  # No download triggered — fall through

    # --- Try same-page navigation ---
    element.click()
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    pdf_url = page.url
    response = context.request.get(pdf_url, timeout=60_000)
    if response.ok:
        output_path.write_bytes(response.body())
    else:
        raise RuntimeError(
            f"Could not download PDF (HTTP {response.status}) from {pdf_url}"
        )


def run(address: str, borough: str, output_dir: Path, headless: bool) -> None:
    house_number, street_name = parse_address(address)
    borough_code = resolve_borough(borough)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Address  : {house_number} {street_name}")
    print(f"Borough  : {borough} (code {borough_code})")
    print(f"Output   : {output_dir.resolve()}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # ── Step 1: NYC Finance homepage ───────────────────────────────────
        print("1. Opening NYC Finance homepage…")
        page.goto("https://www.nyc.gov/site/finance/index.page", timeout=60_000)
        wait_stable(page)

        # ── Step 2: "Property Tax Bills and Payments" ──────────────────────
        print("2. Clicking 'Property Tax Bills and Payments'…")
        click_text(page, "Property Tax Bills and Payments")
        wait_stable(page)

        # ── Step 3: "View Property Tax Bills and Notices" ──────────────────
        print("3. Clicking 'View Property Tax Bills and Notices'…")
        click_text(page, "View Property Tax Bills and Notices")
        wait_stable(page)

        # ── Step 4: "Property Address Search" ─────────────────────────────
        print("4. Clicking 'Property Address Search'…")
        click_text(page, "Property Address Search")
        wait_stable(page)

        # ── Step 5: Fill in address ────────────────────────────────────────
        print(f"5. Entering address: {house_number} {street_name} (borough {borough_code})…")
        fill_address_form(page, house_number, street_name, borough_code)

        # ── Step 6: Click Search ───────────────────────────────────────────
        print("6. Clicking Search…")
        search_btn = (
            page.get_by_role("button", name=re.compile(r"search", re.IGNORECASE)).first
        )
        search_btn.click()
        wait_stable(page)

        # ── Step 7: "Property Tax Bills" ──────────────────────────────────
        print("7. Clicking 'Property Tax Bills'…")
        click_text(page, "Property Tax Bills")
        wait_stable(page)

        # ── Step 8: Download each bill ────────────────────────────────────
        for label, pattern, filename in TARGET_BILLS:
            output_path = output_dir / filename
            print(f"8. Downloading {label} bill → {output_path.name}…")

            element = find_bill_link(page, pattern)
            if element is None:
                print(f"   ✗ Could not find a link matching '{label}' on the page.")
                print(f"     Page title: {page.title()!r}")
                print(f"     Page URL:   {page.url}")
                continue

            try:
                download_pdf(context, page, element, output_path)
                size_kb = output_path.stat().st_size // 1024
                print(f"   ✓ Saved {output_path.name} ({size_kb} KB)")
            except Exception as exc:
                print(f"   ✗ Failed to download {label} bill: {exc}")

        browser.close()

    print()
    print(f"Done. Bills saved to: {output_dir.resolve()}/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download NYC property tax bills for a given address.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python download_tax_bills.py "123 Main Street"
  python download_tax_bills.py "456 Broadway" --borough Manhattan
  python download_tax_bills.py "789 Flatbush Ave" --borough Brooklyn --output-dir ~/tax-bills
  python download_tax_bills.py "100 Church St" --visible
""",
    )
    parser.add_argument(
        "address",
        help="Property address as 'HOUSE_NUMBER STREET_NAME', e.g. '123 Main Street'",
    )
    parser.add_argument(
        "--borough", "-b",
        default="Manhattan",
        metavar="BOROUGH",
        help=(
            "NYC borough name or code 1-5 "
            "(Manhattan=1, Bronx=2, Brooklyn=3, Queens=4, Staten Island=5). "
            "Default: Manhattan"
        ),
    )
    parser.add_argument(
        "--output-dir", "-o",
        default="bills",
        metavar="DIR",
        help="Directory to save downloaded PDFs (default: ./bills)",
    )
    parser.add_argument(
        "--visible",
        action="store_true",
        help="Show the browser window (useful for debugging)",
    )

    args = parser.parse_args()

    try:
        run(
            address=args.address,
            borough=args.borough,
            output_dir=Path(args.output_dir),
            headless=not args.visible,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
