#!/usr/bin/env python3
"""Download NYC property tax bills for a given address using Playwright."""

import argparse
import re
import sys
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BOROUGH_MAP = {
    "manhattan": "1", "mn": "1", "new york": "1", "ny": "1", "1": "1",
    "bronx": "2", "the bronx": "2", "bx": "2", "2": "2",
    "brooklyn": "3", "bk": "3", "kings": "3", "3": "3",
    "queens": "4", "qn": "4", "4": "4",
    "staten island": "5", "si": "5", "richmond": "5", "5": "5",
}

TARGET_BILLS = [
    ("June 2025",     re.compile(r"jun\w*[\s/\-]*2025", re.IGNORECASE), "tax_bill_june_2025.pdf"),
    ("November 2025", re.compile(r"nov\w*[\s/\-]*2025", re.IGNORECASE), "tax_bill_november_2025.pdf"),
]

# Fallback: navigate here directly when the nyc.gov nav can't reach the form
DIRECT_SEARCH_URL = "https://webapps.nyc.gov/NYCPROP/findmyland.html"


# ── Helpers ────────────────────────────────────────────────────────────────────

def parse_address(raw: str) -> tuple[str, str]:
    parts = raw.strip().split(None, 1)
    if len(parts) < 2:
        raise ValueError(f"Address must include a house number and street name (got: {raw!r})")
    return parts[0], parts[1]


def resolve_borough(value: str) -> str:
    code = BOROUGH_MAP.get(value.strip().lower())
    if not code:
        raise ValueError(
            f"Unrecognised borough {value!r}. "
            "Use one of: Manhattan, Bronx, Brooklyn, Queens, 'Staten Island', or 1-5."
        )
    return code


def log_state(page: Page, label: str) -> None:
    print(f"   URL  : {page.url}")
    print(f"   Title: {page.title()!r}")


def wait_stable(page: Page, timeout: int = 30_000) -> None:
    page.wait_for_load_state("domcontentloaded", timeout=timeout)
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def _click_and_follow(
    loc,
    page: Page,
    context: BrowserContext,
    click_timeout: int = 30_000,
) -> Page:
    """
    Click *loc*, then return whichever page is now active.
    Uses context.expect_page() to reliably catch new-tab opens.
    """
    try:
        with context.expect_page(timeout=5_000) as popup_info:
            loc.click(timeout=click_timeout)
        new_page = popup_info.value
        wait_stable(new_page)
        print("   (link opened a new tab → switching)")
        return new_page
    except PlaywrightTimeoutError:
        # No new tab: same-page navigation
        wait_stable(page)
        return page


def _first_visible(page: Page, text: str, timeout_ms: int = 3_000):
    """
    Return the first visible locator whose text contains *text*,
    searching the main frame then all child frames.
    Returns None if not found.
    """
    for frame in [page.main_frame, *page.frames]:
        try:
            loc = frame.get_by_text(text, exact=False).first
            if loc.is_visible(timeout=timeout_ms):
                return loc
        except Exception:
            pass
    return None


def _address_form_visible(page: Page) -> bool:
    """Return True when a house-number input is visible anywhere on the page."""
    selectors = [
        "input[name*='housenum' i]",
        "input[id*='housenum' i]",
        "input[name*='house' i]",
        "input[id*='house' i]",
        "input[placeholder*='house' i]",
        "input[placeholder*='number' i]",
        # more generic: any labelled text input near 'house' text
    ]
    for frame in [page.main_frame, *page.frames]:
        for sel in selectors:
            try:
                if frame.locator(sel).first.is_visible(timeout=500):
                    return True
            except Exception:
                pass
    return False


def _dump_links(page: Page) -> list[str]:
    """Return text of every visible link/tab/button on the page (for debugging)."""
    found = []
    for frame in [page.main_frame, *page.frames]:
        for sel in ["a", "button", "[role='tab']", "[role='link']"]:
            try:
                for loc in frame.locator(sel).all():
                    try:
                        if loc.is_visible(timeout=200):
                            txt = loc.inner_text(timeout=200).strip()
                            if txt:
                                found.append(txt)
                    except Exception:
                        pass
            except Exception:
                pass
    return found


# ── Navigation steps ───────────────────────────────────────────────────────────

def click_text_robust(page: Page, context: BrowserContext, text: str) -> Page:
    loc = _first_visible(page, text)
    if loc is None:
        raise RuntimeError(
            f"Cannot find element with text {text!r}.\n"
            f"  URL  : {page.url}\n"
            f"  Title: {page.title()!r}\n"
            f"  Hint : re-run with --visible to watch the browser."
        )
    return _click_and_follow(loc, page, context)


def navigate_to_address_search(page: Page, context: BrowserContext) -> Page:
    """
    Reach the property address-search form.  Strategy (in order):
      1. Form already visible — done.
      2. Click a tab/link whose text suggests address search.
      3. Navigate directly to the search URL as a last resort.
    Saves a screenshot + link list before attempting anything so the caller
    can inspect the page state regardless of outcome.
    """
    # Give any JS a moment to finish rendering after the previous navigation
    try:
        page.wait_for_timeout(1_500)
    except Exception:
        pass

    # ── Diagnose: always print visible links at this point ──────────────────
    links = _dump_links(page)
    print("   Visible links/tabs on page:")
    if links:
        for t in links:
            print(f"     · {t!r}")
    else:
        print("     (none found)")

    # Save a screenshot so the user can see the page visually
    try:
        shot = Path("step4_page.png")
        page.screenshot(path=str(shot), full_page=True)
        print(f"   Screenshot saved → {shot.resolve()}")
    except Exception as e:
        print(f"   (screenshot failed: {e})")

    # 1 — form already on screen
    if _address_form_visible(page):
        print("   Address form is already visible — no tab click needed.")
        return page

    # 2 — click a matching tab/link
    candidates = [
        "Property Address Search",
        "Address Search",
        "Search by Address",
        "By Address",
        "Address",
    ]
    # Also try any visible link whose text contains "address" or "propert"
    broad_pattern = re.compile(r"address|propert", re.IGNORECASE)
    for lnk_text in links:
        if broad_pattern.search(lnk_text) and lnk_text not in candidates:
            candidates.append(lnk_text)

    for text in candidates:
        loc = _first_visible(page, text, timeout_ms=2_000)
        if loc is None:
            continue
        print(f"   Clicking '{text}'…")
        page = _click_and_follow(loc, page, context)
        try:
            page.wait_for_timeout(1_000)
        except Exception:
            pass
        if _address_form_visible(page):
            return page

    # 3 — direct URL fallback
    print(f"   Tab search unsuccessful; navigating directly to {DIRECT_SEARCH_URL}")
    page.goto(DIRECT_SEARCH_URL, timeout=30_000)
    wait_stable(page)
    try:
        page.wait_for_timeout(1_500)
    except Exception:
        pass

    if not _address_form_visible(page):
        try:
            page.screenshot(path="step4_direct_debug.png", full_page=True)
        except Exception:
            pass
        raise RuntimeError(
            f"Could not reach the address-search form even after direct navigation.\n"
            f"  URL  : {page.url}\n"
            f"  Title: {page.title()!r}\n"
            f"  Hint : open step4_direct_debug.png to see what the browser shows."
        )

    return page


def click_agree_if_present(page: Page, context: BrowserContext) -> Page:
    """Click an Agree/Accept/Continue button if a disclaimer page is shown."""
    for text in ["Agree", "I Agree", "Accept", "Continue", "I Accept"]:
        loc = _first_visible(page, text, timeout_ms=4_000)
        if loc is not None:
            print(f"   Found '{text}' button — clicking…")
            return _click_and_follow(loc, page, context)
    print("   (no disclaimer button found — continuing)")
    return page


def fill_address_form(page: Page, house_number: str, street_name: str) -> None:
    """Fill house number and street name across all frames. Borough is left as-is."""
    for frame in [page.main_frame, *page.frames]:
        for sel in [
            "input[name*='housenum' i]",
            "input[id*='housenum' i]",
            "input[name*='house' i]",
            "input[id*='house' i]",
            "input[placeholder*='house' i]",
            "input[placeholder*='number' i]",
        ]:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.fill(house_number)
                    break
            except Exception:
                pass

        for sel in [
            "input[name*='street' i]",
            "input[id*='street' i]",
            "input[placeholder*='street' i]",
        ]:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.fill(street_name)
                    break
            except Exception:
                pass


def click_search_button(page: Page, context: BrowserContext) -> Page:
    for frame in [page.main_frame, *page.frames]:
        for sel in [
            "input[type='submit']",
            "button[type='submit']",
        ]:
            try:
                loc = frame.locator(sel).first
                if loc.is_visible(timeout=500):
                    return _click_and_follow(loc, page, context)
            except Exception:
                pass

    # Try by text
    for text in ["Search", "Find", "Submit", "Go"]:
        loc = _first_visible(page, text, timeout_ms=1_500)
        if loc is not None:
            return _click_and_follow(loc, page, context)

    raise RuntimeError(
        f"Could not find a Search/Find button.\n  URL: {page.url}\n  Title: {page.title()!r}"
    )


def find_bill_link(page: Page, pattern: re.Pattern):
    """Return a locator whose text matches *pattern* across all frames."""
    for frame in [page.main_frame, *page.frames]:
        for sel in ["a", "td", "th", "button"]:
            try:
                for loc in frame.locator(sel).all():
                    try:
                        text = loc.inner_text(timeout=300).strip()
                    except Exception:
                        continue
                    if pattern.search(text):
                        return loc
            except Exception:
                pass
    return None


def download_pdf(context: BrowserContext, page: Page, element, output_path: Path) -> None:
    """Click *element* and save the resulting PDF via popup, download-event, or navigation."""
    try:
        with context.expect_page(timeout=8_000) as popup_info:
            element.click()
        popup = popup_info.value
        wait_stable(popup)
        pdf_url = popup.url
        response = context.request.get(pdf_url, timeout=60_000)
        if not response.ok:
            raise RuntimeError(f"HTTP {response.status} fetching {pdf_url}")
        output_path.write_bytes(response.body())
        popup.close()
        return
    except PlaywrightTimeoutError:
        pass

    try:
        with page.expect_download(timeout=8_000) as dl_info:
            element.click()
        download = dl_info.value
        download.save_as(output_path)
        return
    except PlaywrightTimeoutError:
        pass

    element.click()
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    pdf_url = page.url
    response = context.request.get(pdf_url, timeout=60_000)
    if response.ok:
        output_path.write_bytes(response.body())
    else:
        raise RuntimeError(f"Could not download PDF (HTTP {response.status}) from {pdf_url}")


# ── Main flow ──────────────────────────────────────────────────────────────────

def run(address: str, borough: str, output_dir: Path, headless: bool) -> None:
    house_number, street_name = parse_address(address)
    borough_code = resolve_borough(borough)  # kept for reference; not injected into form
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Address  : {house_number} {street_name}")
    print(f"Borough  : {borough}")
    print(f"Output   : {output_dir.resolve()}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print("1. Opening NYC Finance homepage…")
        page.goto("https://www.nyc.gov/site/finance/index.page", timeout=60_000)
        wait_stable(page)
        log_state(page, "1")

        print("2. Clicking 'Property Tax Bills and Payments'…")
        page = click_text_robust(page, context, "Property Tax Bills and Payments")
        log_state(page, "2")

        print("3. Clicking 'View Property Tax Bills and Notices'…")
        page = click_text_robust(page, context, "View Property Tax Bills and Notices")
        log_state(page, "3")

        print("4. Navigating to address-search form…")
        page = navigate_to_address_search(page, context)
        log_state(page, "4")

        print("4b. Clicking 'Agree' (disclaimer)…")
        page = click_agree_if_present(page, context)

        print(f"5. Entering address: {house_number} {street_name}…")
        fill_address_form(page, house_number, street_name)

        print("6. Clicking Search…")
        page = click_search_button(page, context)
        log_state(page, "6")

        print("7. Clicking 'Property Tax Bills'…")
        page = click_text_robust(page, context, "Property Tax Bills")
        log_state(page, "7")

        for label, pattern, filename in TARGET_BILLS:
            output_path = output_dir / filename
            print(f"8. Downloading {label} bill → {output_path.name}…")
            element = find_bill_link(page, pattern)
            if element is None:
                print(f"   ✗ No link found matching '{label}'.")
                print(f"     URL  : {page.url}")
                print(f"     Title: {page.title()!r}")
                try:
                    print(f"     Body : {page.inner_text('body', timeout=3_000)[:400]!r}")
                except Exception:
                    pass
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


# ── CLI ────────────────────────────────────────────────────────────────────────

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
    parser.add_argument("address", help="Property address, e.g. '123 Main Street'")
    parser.add_argument(
        "--borough", "-b", default="Manhattan",
        help="NYC borough name or 1-5 (default: Manhattan)",
    )
    parser.add_argument(
        "--output-dir", "-o", default="bills",
        help="Directory to save PDFs (default: ./bills)",
    )
    parser.add_argument(
        "--visible", action="store_true",
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
    except (ValueError, RuntimeError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
