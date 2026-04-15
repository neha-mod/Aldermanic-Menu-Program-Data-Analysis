"""
scrape_menu_report.py
---------------------
Scrapes the Chicago Aldermanic Menu Program Q4 2024 PDF.
Extracts per-project rows with clean, separated columns:
    ward | menu_package | address | cost | cost_numeric

Output: quarterly_menu_report_Q4_2024.csv

Usage:
    python scrape_menu_report.py
    python scrape_menu_report.py --pdf path/to/file.pdf
    python scrape_menu_report.py --output results.csv

Requirements:
    pip install pdfplumber
"""

import re, csv, argparse
from pathlib import Path
from collections import defaultdict

try:
    import pdfplumber
except ImportError:
    raise SystemExit("pdfplumber not found.  Run: pip install pdfplumber")

# ── Column x-boundaries (PDF points) ─────────────────────────────────────────
SPLIT_A = 270   # right edge of Menu Package column
SPLIT_B = 750   # left edge of Cost column

COST_RE = re.compile(r"\$([\d,]+\.\d{2})$")

NOISE_EXACT = {
    "Return to TOC", "CDOT CONSTRUCTION MANAGEMENT",
    "2024 Menu Ward Detail Report",
    "Menu Package Locations Estimated 2024 Cost",
    "Estimated 2024 Cost", "Aldermanic Menu Program",
    "Annual Allocation and Project Overview", "Q4 2024 Update",
    "Table of Contents", "How to Read this Report", "Ward Balance Summary",
    "Locations",
}
NOISE_RE = [
    re.compile(r"^\d{1,2}/\d{1,2}/\d{4}\s+\d+:\d+:\d+\s+(AM|PM)"),
    re.compile(r"^Page:\s*\d+\s+of\s+\d+", re.I),
    re.compile(r"^MENU BUDGET", re.I),
    re.compile(r"^WARD COMMITTED", re.I),
    re.compile(r"^WARD \d{4} BALANCE", re.I),
    re.compile(r"^Ward\s+Year\s+Ward Budget"),
    re.compile(r"^\s*\d{1,2}\s+2024\s+\$"),
    re.compile(r"^MAYOR\s+", re.I),
    re.compile(r"^Ward \d+\.+\d+$"),
    re.compile(r"^Menu Package\s+Locations"),
    re.compile(r"^Return to TOC"),
]

def is_noise(t):
    t = t.strip()
    if not t or t in NOISE_EXACT:
        return True
    return any(p.search(t) for p in NOISE_RE)

def clean(s):
    return re.sub(r"\s+", " ", s).strip()

def w2t(words):
    return clean(" ".join(w["text"] for w in words))

def parse_ward(text_a):
    # 1. Clean the input string
    text_a = text_a.strip()

    # 2. Check for the specific "doubled" signature
    # If found, use slicing [::2] to take every second character
    if "WW" in text_a and "aa" in text_a:
        text_a = text_a[::2]

    # 3. Now run your standard regex on the (now cleaned) string
    # We don't need the aggressive de-duper anymore because [::2] handled it
    m = re.match(r'^Ward:\s*(\d{1,2})\s*$', text_a)
    
    if m:
        n = int(m.group(1))
        return n if 1 <= n <= 50 else None
    return None

# ── Per-page extraction ───────────────────────────────────────────────────────
#
# The PDF has three multi-line address patterns:
#
#   Pattern 1 – address wraps AFTER the cost line (simple post-overflow):
#       A='Pkg'   B='address line 1'  C='$X'
#       A=''      B='address line 2'  C=''
#
#   Pattern 2 – address starts BEFORE the cost line (pre-overflow sandwich):
#       A=''      B='address line 1'  C=''
#       A='Pkg'   B=''                C='$X'   ← B is empty on cost row
#       A=''      B='address line 2'  C=''     ← optional post piece
#
#   Pattern 3 – package name wraps, address is on the cost line:
#       A='pkg line 1'  B=''        C=''
#       A=''            B='address' C='$X'

def extract_page(page, current_ward, last_rec=None, post_loc=None):
    """
    Returns (records, current_ward, last_rec, post_loc).
    last_rec and post_loc are threaded across pages so that address lines
    that overflow a page break are correctly attached to their record.
    """
    if post_loc is None:
        post_loc = []

    words = page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False)
    if not words:
        return [], current_ward, last_rec, post_loc

    bands = defaultdict(list)
    for w in words:
        bands[round(w["top"] / 2) * 2].append(w)

    records = []
    pre_pkg = []   # col-A lines buffered before the next cost row
    pre_loc = []   # col-B lines buffered before the next cost row
    # post_loc: col-B lines after the last cost row (passed in, mutated in place)

    def commit_post():
        """Flush post_loc into the last record's address (Pattern 1 overflow)."""
        if last_rec and post_loc:
            extra = clean(" ".join(post_loc))
            last_rec["address"] = clean(last_rec["address"] + " " + extra)
        post_loc.clear()

    for top in sorted(bands):
        row_words = sorted(bands[top], key=lambda w: w["x0"])
        col_a = [w for w in row_words if w["x0"] < SPLIT_A]
        col_b = [w for w in row_words if SPLIT_A <= w["x0"] < SPLIT_B]
        col_c = [w for w in row_words if w["x0"] >= SPLIT_B]

        text_a = w2t(col_a)
        text_b = w2t(col_b)
        text_c = w2t(col_c)
        full   = clean(f"{text_a} {text_b} {text_c}")

        # ── Ward header ───────────────────────────────────────────────────
        ward_num = parse_ward(text_a) if text_a else None
        if ward_num is not None:
            commit_post()
            current_ward = ward_num
            pre_pkg.clear(); pre_loc.clear(); last_rec = None
            continue

        if is_noise(full):
            continue

        has_cost = bool(COST_RE.search(text_c))

        # ── Cost row ──────────────────────────────────────────────────────
        if has_cost and current_ward is not None:
            cm       = COST_RE.search(text_c)
            cost_str = cm.group(0)
            cost_val = float(cm.group(1).replace(",", ""))

            pkg = clean(" ".join(pre_pkg + ([text_a] if text_a else [])))

            if text_b:
                # Address is on this line; any post_loc belongs to previous record.
                commit_post()
                loc = clean(" ".join(pre_loc + [text_b]))
            elif post_loc:
                # Pattern 2: col-B was empty here but post_loc has buffered lines
                # that are actually the pre-address for THIS record, not overflow
                # from the previous one.
                loc = clean(" ".join(pre_loc + post_loc))
                post_loc.clear()
            else:
                commit_post()
                loc = clean(" ".join(pre_loc))

            rec = {
                "ward":         current_ward,
                "menu_package": pkg,
                "address":      loc,
                "cost":         cost_str,
                "cost_numeric": cost_val,
            }
            records.append(rec)
            last_rec = rec
            pre_pkg.clear(); pre_loc.clear()

        # ── Non-cost row ──────────────────────────────────────────────────
        else:
            if last_rec is None:
                # Before any cost row on this page
                if text_a: pre_pkg.append(text_a)
                if text_b: pre_loc.append(text_b)
            elif text_a:
                # New package starting — commit any post-overflow first
                commit_post()
                pre_pkg.append(text_a)
                if text_b: pre_loc.append(text_b)
            elif text_b:
                # Only col-B: could be post-overflow for current record or
                # pre-address for the next. Buffer in post_loc; the cost-row
                # logic above resolves which it is.
                post_loc.append(text_b)

    commit_post()
    return records, current_ward, last_rec, post_loc


# ── Full PDF ──────────────────────────────────────────────────────────────────

def extract(pdf_path):
    all_records = []
    current_ward = None
    last_rec     = None
    post_loc     = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        print(f"  PDF loaded — {total} pages")
        for n, page in enumerate(pdf.pages, 1):
            if n % 20 == 0 or n == total:
                print(f"  Processing page {n}/{total}...")
            recs, current_ward, last_rec, post_loc = extract_page(
                page, current_ward, last_rec, post_loc
            )
            all_records.extend(recs)
    return all_records


# ── CSV ───────────────────────────────────────────────────────────────────────

FIELDS = ["ward", "menu_package", "address", "cost", "cost_numeric"]

def write_csv(records, out_path):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(records)
    print(f"\n  Saved {len(records):,} rows → {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

DEFAULT_PDF = "Quarterly Menu Reports Q4 2024 (1).pdf"
DEFAULT_OUT = "quarterly_menu_report_Q4_2024.csv"

def main():
    ap = argparse.ArgumentParser(description="Scrape Chicago Aldermanic Menu Program PDF")
    ap.add_argument("--pdf",    default=DEFAULT_PDF)
    ap.add_argument("--output", default=DEFAULT_OUT)
    ap.add_argument("--ward",   metavar="N", type=int, nargs="+",
                    help="Only output records for these ward number(s)")
    args = ap.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    print(f"\nScraping: {pdf_path}")
    records = extract(str(pdf_path))
    if not records:
        raise SystemExit("No records extracted.")

    if args.ward:
        keep = set(args.ward)
        records = [r for r in records if r["ward"] in keep]
        if not records:
            raise SystemExit(f"No records found for ward(s): {sorted(keep)}")

    records.sort(key=lambda r: (r["ward"], r["menu_package"].lower()))
    write_csv(records, args.output)

    wards      = sorted({r["ward"] for r in records})
    total_cost = sum(r["cost_numeric"] for r in records)
    blank_addr = sum(1 for r in records if not r["address"])
    print(f"\n  Wards captured   : {len(wards)}  ({min(wards)}-{max(wards)})")
    print(f"  Total rows       : {len(records):,}")
    print(f"  Blank addresses  : {blank_addr}")
    print(f"  Total $ value    : ${total_cost:,.2f}")
    print("\nDone.")

if __name__ == "__main__":
    main()
