#!/usr/bin/env bash
# One-time setup: install Python dependencies and download the Chromium browser.
set -euo pipefail

echo "Installing Python dependencies…"
pip install -r requirements.txt

echo "Downloading Playwright's Chromium browser…"
python -m playwright install chromium --with-deps

echo ""
echo "Setup complete. Run the script with:"
echo "  python download_tax_bills.py \"<house number> <street name>\" --borough <borough>"
echo ""
echo "Example:"
echo "  python download_tax_bills.py \"123 Main Street\" --borough Manhattan"
