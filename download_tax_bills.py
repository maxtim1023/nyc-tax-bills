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
    Only matches <a>, <button>, or role=tab/link/menuitem — never inputs.
    Returns None if not found.
    """
    for frame in [page.main_frame, *page.frames]:
        for sel in ["a", "button", "[role='tab']", "[role='link']", "[role='menuitem']"]:
            try:
                loc = frame.locator(sel).filter(has_text=text).first
                if loc.is_visible(timeout=timeout_ms):
                    return loc
            except Exception:
                pass
    return None



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



def click_agree_if_present(page: Page, context: BrowserContext) -> Page:
    """Click an Agree/Accept/Continue button if a disclaimer page is shown."""
    try:
        page.screenshot(path="debug_after_step4.png", full_page=True)
        print("   Screenshot saved → debug_after_step4.png")
    except Exception:
        pass

    for text in ["I Agree to the Terms", "Agree", "I Agree", "Accept", "Continue", "I Accept", "OK"]:
        loc = _first_visible(page, text, timeout_ms=5_000)
        if loc is not None:
            print(f"   Found '{text}' — clicking…")
            loc.click()
            wait_stable(page)
            return page

    print("   (no agree button found — continuing)")
    return page


def fill_address_form(page: Page, house_number: str, street_name: str, borough_code: str) -> None:
    """Fill the address search form. Raises if required fields are not found."""
    # Save screenshot so we can see the page state
    try:
        page.screenshot(path="debug_pts_home.png", full_page=True)
        print("   Screenshot saved → debug_pts_home.png")
    except Exception:
        pass

    # Wait for ANY text input to appear (ASP.NET IDs won't contain 'house'/'street')
    try:
        page.wait_for_selector("input[type='text'], input:not([type])", timeout=10_000)
    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"No form inputs appeared after Agree.\n"
            f"  URL  : {page.url}\n"
            f"  Title: {page.title()!r}"
        )

    # Dump every visible input/select so we know exact names and IDs
    print("   Visible form fields:")
    all_text_inputs = []
    for frame in [page.main_frame, *page.frames]:
        for loc in frame.locator("input, select").all():
            try:
                if not loc.is_visible(timeout=200):
                    continue
                t    = loc.get_attribute("type") or "text"
                name = loc.get_attribute("name") or ""
                id_  = loc.get_attribute("id") or ""
                ph   = loc.get_attribute("placeholder") or ""
                print(f"     · type={t!r}  name={name!r}  id={id_!r}  placeholder={ph!r}")
                if t in ("text", "") and name != "search-terms":
                    all_text_inputs.append(loc)
            except Exception:
                pass

    filled_house = False
    filled_street = False

    # ── Try by visible label (most reliable for ASP.NET forms) ────────────
    for label_text in ["House Number", "House No", "Bldg No", "Building Number", "Low"]:
        try:
            loc = page.get_by_label(label_text, exact=False).first
            if loc.is_visible(timeout=1_000):
                loc.fill(house_number)
                print(f"   House number filled via label {label_text!r}")
                filled_house = True
                break
        except Exception:
            pass

    for label_text in ["Street Name", "Street", "Address"]:
        try:
            loc = page.get_by_label(label_text, exact=False).first
            if loc.is_visible(timeout=1_000):
                loc.fill(street_name)
                print(f"   Street name filled via label {label_text!r}")
                filled_street = True
                break
        except Exception:
            pass

    for label_text in ["Borough", "Boro"]:
        try:
            loc = page.get_by_label(label_text, exact=False).first
            if loc.is_visible(timeout=1_000):
                loc.select_option(value=borough_code)
                print(f"   Borough set via label {label_text!r}")
                break
        except Exception:
            pass

    # ── Fall back to name/id attribute selectors ───────────────────────────
    if not filled_house:
        for sel in ["input[name*='housenum' i]", "input[id*='housenum' i]",
                    "input[name*='house' i]",    "input[id*='house' i]",
                    "input[name*='low' i]",       "input[id*='low' i]"]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.fill(house_number)
                    print(f"   House number filled via {sel!r}")
                    filled_house = True
                    break
            except Exception:
                pass

    if not filled_street:
        for sel in ["input[name*='street' i]", "input[id*='street' i]",
                    "input[name*='addr' i]",    "input[id*='addr' i]"]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.fill(street_name)
                    print(f"   Street name filled via {sel!r}")
                    filled_street = True
                    break
            except Exception:
                pass

    # ── Last resort: fill first two visible text inputs positionally ───────
    if not filled_house and len(all_text_inputs) >= 1:
        all_text_inputs[0].fill(house_number)
        print("   House number filled positionally (1st text input)")
        filled_house = True
    if not filled_street and len(all_text_inputs) >= 2:
        all_text_inputs[1].fill(street_name)
        print("   Street name filled positionally (2nd text input)")
        filled_street = True

    if not filled_house:
        raise RuntimeError(
            "Could not find house-number input. Check debug_pts_home.png and the field list above."
        )
    if not filled_street:
        raise RuntimeError(
            "Could not find street-name input. Check debug_pts_home.png and the field list above."
        )


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
    print(f"Borough  : {borough} (code {borough_code})")
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

        print("4. Clicking 'Property Address Search'…")
        page = click_text_robust(page, context, "Property Address Search")
        log_state(page, "4")

        print("4b. Clicking 'Agree' (disclaimer)…")
        page = click_agree_if_present(page, context)
        log_state(page, "4b")

        # The agree step sets a session cookie but may land on Home.aspx.
        # Navigate directly to the address search form URL.
        print("4c. Navigating to address search form…")
        page.goto(
            "https://a836-pts-access.nyc.gov/care/search/commonsearch.aspx?mode=address",
            timeout=30_000,
        )
        wait_stable(page)
        log_state(page, "4c")

        print(f"5. Entering address: {house_number} {street_name} (borough: {borough})…")
        fill_address_form(page, house_number, street_name, borough_code)

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
        "--borough", "-b", default="Bronx",
        help="NYC borough name or 1-5 (default: Bronx)",
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
