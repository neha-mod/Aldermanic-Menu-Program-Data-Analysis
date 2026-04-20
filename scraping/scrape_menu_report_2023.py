"""
scrape_menu_report.py
---------------------
Scrapes the Chicago Aldermanic Menu Program Q4 2023 PDF.
Extracts per-project rows with clean, separated columns:
    ward | menu_package | address | cost | cost_numeric

Output: quarterly_menu_report_Q4_2023.csv

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
SPLIT_B = 820   # left edge of Cost column (2023 PDF cost words start at ~855)

COST_RE = re.compile(r"\$([\d,]+\.\d{2})$")

NOISE_EXACT = {
    "Return to TOC", "CDOT CONSTRUCTION MANAGEMENT",
    "2023 Menu Ward Detail Report",
    "Menu Package Locations Estimated 2023 Cost",
    "Estimated 2023 Cost",
    "Aldermanic Menu Program",
    "Annual Allocation and Project Overview",
    "Q4 2023 Update",
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
    re.compile(r"^\s*\d{1,2}\s+202[34]\s+\$"),
    re.compile(r"^MAYOR\s+", re.I),
    re.compile(r"^Ward \d+\.+\d+$"),
    re.compile(r"^Menu Package\s+Locations"),
    re.compile(r"^Return to TOC"),
]

# Noise phrases that can bleed into address strings — strip them out
_ADDR_NOISE_RE = re.compile(
    r"\s*(?:2023|2024) Menu Ward Detail Report\s*"
    r"|CDOT CONSTRUCTION MANAGEMENT\s*",
    re.I
)

def is_noise(t):
    t = t.strip()
    if not t or t in NOISE_EXACT:
        return True
    return any(p.search(t) for p in NOISE_RE)

def clean(s):
    s = _ADDR_NOISE_RE.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()

def w2t(words):
    return clean(" ".join(w["text"] for w in words))

WARD_SKIP = -99  # Sentinel: currently inside a ward whose projects should be excluded

EXCLUDED_WARDS = {99}  # Ward numbers to filter out entirely

def parse_ward(text_a):
    # 1. Clean the input string
    text_a = text_a.strip()

    # 2. Check for the specific "doubled" signature
    # If found, use slicing [::2] to take every second character
    if "WW" in text_a and "aa" in text_a:
        text_a = text_a[::2]

    # 3. Now run your standard regex on the (now cleaned) string
    # We don't need the aggressive de-duper anymore because [::2] handled it
    m = re.match(r'^Ward:\s*(\d+)\s*$', text_a)

    if m:
        n = int(m.group(1))
        if n in EXCLUDED_WARDS:
            return WARD_SKIP   # signal: enter skip mode
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
def _looks_like_new_address(text):
    """Heuristic: does *text* look like the start of a new address rather than
    a short overflow fragment (like "ST", "AVE", "PL", "E)") from the previous
    record?

    New-address signals:
      • Starts with a digit  (e.g. "2500 W AUGUSTA BLVD; ...")
      • Starts with "ON "    (e.g. "ON S FEDERAL ST FROM ...")
      • Contains a semicolon (compound address list)
      • Length > 25 characters (overflow fragments are typically ≤ one or two
        short words)
    """
    t = text.strip()
    if len(t) > 25:
        return True
    if re.match(r"^\d", t):
        return True
    if t.startswith("ON "):
        return True
    if ";" in t:
        return True
    return False


# Trailing tokens that signal the address was cut mid-phrase and continues
# on the next line.  Matches patterns like:
#   "...& W", "...ON E", "...TO N", "...FROM S", "(100", "& N", etc.
_TRUNCATED_TAIL_RE = re.compile(
    r"(?:"
    r"[&;]\s*[NSEW]"         # "& W", "; N" — cross-street cut before street name
    r"|(?:ON|TO|FROM)\s+[NSEW]"  # "ON S", "TO N" — direction cut before street
    r"|\(\d+"                # "(100" — parenthetical cut before closing paren
    r"|\d+\s+[NSEW]"        # "1400 N" — address number + direction, no street yet
    r")\s*$"
)

def _looks_truncated(text):
    """Does *text* look like an address that was cut off mid-phrase?"""
    return bool(_TRUNCATED_TAIL_RE.search(text.strip()))

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
    last_cost_had_addr = False  # did the last cost row already have its address inline?
    # post_loc: col-B lines after the last cost row (passed in, mutated in place)
    #
    # The PDF physically interleaves address lines from one record with the
    # package/cost lines of the next.  Example layout (page 9, ward 3):
    #
    #   top=164  A=""                    B="ON S CLARK ST..."   C=""    ← addr line 1 of rec N
    #   top=170  A="Speed Indicator..."  B=""                   C="$X"  ← cost of rec N (A has pkg)
    #   top=174  A=""                    B="1446 S CLARK ST"    C=""    ← addr line 2 of rec N
    #   top=194  A=""                    B="ON S FEDERAL ST..."  C=""   ← addr line 3 of rec N
    #   top=200  A="Street Light Res."   B=""                   C="$X"  ← cost of rec N+1
    #   top=204  A=""                    B="TO W 14TH ST..."    C=""    ← addr tail of rec N+1
    #
    # The rule that disambiguates post_loc vs pre_loc:
    #   • A col-B-only line that arrives BEFORE any col-A has been seen since
    #     the last cost row → it is a PRE-address for the upcoming record,
    #     so route it to pre_loc (not post_loc).
    #   • A col-B-only line that arrives AFTER a new col-A has been buffered
    #     → it is the pre-address continuation, route to pre_loc.
    #   • post_loc is only used when a col-B line arrives with NO col-A in
    #     the buffer AND no prior col-B yet in pre_loc — i.e. it could be
    #     overflow from the previous record.  We defer the decision to the
    #     next cost row.
    #
    # At the cost row:
    #   • If text_b is present → address is right here; commit post_loc to
    #     last_rec first, then use pre_loc + text_b.
    #   • If text_b is absent and pre_loc has content → pre_loc IS the address.
    #     commit post_loc to last_rec first.
    #   • If text_b is absent and pre_loc is empty but post_loc has content →
    #     post_loc is the pre-address (Pattern 2 sandwich).

    def commit_post():
        """Flush post_loc into the last record's address (post-overflow)."""
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

        # ── Skip rows belonging to excluded wards (e.g. ward 99) ─────────
        if current_ward == WARD_SKIP:
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
                # Address is on this line.  post_loc belongs to previous record.
                commit_post()
                loc = clean(" ".join(pre_loc + [text_b]))
                # Only mark the address as "complete" if it doesn't end
                # mid-phrase.  A truncated cost-row address typically ends
                # with a short directional/preposition fragment like
                # "ON E", "& W", "& S", "(100", "TO N", "FROM S", etc.
                # In that case, the next col-B line is still overflow.
                last_cost_had_addr = not _looks_truncated(loc)
            elif pre_loc:
                # pre_loc was built up before this cost row — it is the address.
                # post_loc (if any) belongs to the previous record.
                commit_post()
                loc = clean(" ".join(pre_loc))
                last_cost_had_addr = False
            elif post_loc:
                # Pattern 2 sandwich: post_loc lines are actually the pre-address
                # for THIS record (no col-A arrived to trigger a pre_loc route).
                loc = clean(" ".join(post_loc))
                post_loc.clear()
                last_cost_had_addr = False
            else:
                loc = ""
                last_cost_had_addr = False

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
            if text_a:
                # A new package name is starting. DO NOT commit post_loc here.
                # The next cost row will decide whether post_loc is overflow for
                # the last record or pre-address for this one.
                pre_pkg.append(text_a)
                if text_b:
                    pre_loc.append(text_b)
            elif text_b:
                if pre_pkg or pre_loc:
                    # Already building a pre-address block → accumulate in pre_loc.
                    pre_loc.append(text_b)
                elif last_cost_had_addr and not post_loc:
                    # The last cost row already had its address on the same
                    # line (text_b was present), so this col-B-only line
                    # cannot be overflow for that record.  It must be the
                    # pre-address for the next record.
                    pre_loc.append(text_b)
                else:
                    # No new package seen yet. Could be overflow from last record
                    # or pre-address for next. Defer via post_loc.
                    #
                    # Key insight: if post_loc already has content and this new
                    # line looks like the START of a new address (long line,
                    # starts with a number, cross-street, or "ON "), then
                    # everything already in post_loc is overflow for the
                    # previous record, and this line begins the pre-address
                    # of the next record.  Short fragments (≤25 chars, no
                    # digits at the start) are continuations of the previous
                    # overflow instead.
                    if post_loc and _looks_like_new_address(text_b):
                        commit_post()
                        pre_loc.append(text_b)
                    else:
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

DEFAULT_PDF = "Menu Report 2023.pdf"
DEFAULT_OUT = "quarterly_menu_report_Q4_2023.csv"

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
