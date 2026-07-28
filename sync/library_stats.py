#!/usr/bin/env python3
"""
library_stats.py — inventory a papers folder: full PDFs vs abstract-only records
vs images, and how well each paper is covered.

Groups files by their base name (so Smith_Paper_2020.pdf, .md and .gif count as
one paper) and reports what you actually have.

Usage:
    python3 library_stats.py ~/literature/flash
    python3 library_stats.py ~/literature/flash --list        # name the gaps
    python3 library_stats.py ~/literature/flash --csv out.csv # per-paper table

Optional: `pip install pymupdf` lets it also flag PDFs with no extractable text
(scanned / image-only), which are the ones that need OCR.
"""

__version__ = "2026.07.27.4"

import csv
import sys
import pathlib
import collections

PDF_EXT = {".pdf"}
DOC_EXT = {".md", ".html", ".htm", ".txt", ".docx", ".doc", ".epub", ".rtf", ".pptx", ".csv"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif", ".svg"}

# marker written by proxify when it could not extract an abstract
NO_ABS_MARKERS = ("no abstract could be extracted",)


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def pdf_has_text(p, min_chars=100):
    """True/False if PyMuPDF is available, else None (unknown)."""
    try:
        import fitz
    except ImportError:
        return None
    try:
        d = fitz.open(str(p))
        n = sum(len(d[i].get_text("text").strip()) for i in range(min(3, d.page_count)))
        d.close()
        return n >= min_chars
    except Exception:
        return False


def md_kind(p):
    """'abstract' if the markdown carries a real abstract, else 'metadata-only'."""
    try:
        t = p.read_text(errors="ignore").lower()
    except OSError:
        return "metadata-only"
    return "metadata-only" if any(m in t for m in NO_ABS_MARKERS) else "abstract"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    show_list = "--list" in sys.argv
    csv_out = None
    if "--csv" in sys.argv:
        i = sys.argv.index("--csv")
        if i + 1 < len(sys.argv):
            csv_out = sys.argv[i + 1]
    root = pathlib.Path(args[0] if args else ".").expanduser()
    if not root.is_dir():
        sys.exit(f"not a folder: {root}")

    papers = collections.defaultdict(lambda: {"pdf": [], "doc": [], "img": [], "bytes": 0})
    counts = collections.Counter()
    total_bytes = 0

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        stem = p.stem
        # a sidecar image/text shares the paper's stem; strip our own suffixes
        for suf in ("_figures", "_image"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
        size = p.stat().st_size
        total_bytes += size
        rec = papers[stem]
        rec["bytes"] += size
        if ext in PDF_EXT:
            rec["pdf"].append(p); counts["pdf_files"] += 1
        elif ext in IMG_EXT:
            rec["img"].append(p); counts["image_files"] += 1
        elif ext in DOC_EXT:
            rec["doc"].append(p); counts["doc_files"] += 1
        else:
            counts["other_files"] += 1

    # classify each paper
    full, abs_only, meta_only, img_only = [], [], [], []
    md_abstract = md_meta = 0
    scanned_pdfs, unknown_pdfs = [], 0

    for stem, rec in papers.items():
        kinds = set()
        for d in rec["doc"]:
            if d.suffix.lower() == ".md":
                k = md_kind(d)
                kinds.add(k)
                md_abstract += (k == "abstract")
                md_meta += (k == "metadata-only")
            else:
                kinds.add("abstract")      # html/txt/docx treated as content
        if rec["pdf"]:
            full.append(stem)
            for pp in rec["pdf"]:
                t = pdf_has_text(pp)
                if t is False:
                    scanned_pdfs.append(pp.name)
                elif t is None:
                    unknown_pdfs += 1
        elif "abstract" in kinds:
            abs_only.append(stem)
        elif "metadata-only" in kinds:
            meta_only.append(stem)
        elif rec["img"]:
            img_only.append(stem)

    n = len(papers)
    print(f"\nLibrary: {root}")
    print(f"{'-'*58}")
    print(f"  unique papers (by base name) : {n}")
    print(f"  total files                  : {sum(counts.values())}  ({human(total_bytes)})")
    print()
    print(f"  full PDFs                    : {counts['pdf_files']:5d} file(s)")
    print(f"  text/abstract records        : {counts['doc_files']:5d} file(s)")
    print(f"      .md with a real abstract : {md_abstract:5d}")
    print(f"      .md metadata-only        : {md_meta:5d}")
    print(f"  images                       : {counts['image_files']:5d} file(s)")
    if counts["other_files"]:
        print(f"  other/ignored                : {counts['other_files']:5d} file(s)")
    print()
    print("  coverage per paper:")
    print(f"      have the full PDF        : {len(full):5d}  ({len(full)/n*100:.0f}%)")
    print(f"      abstract only (no PDF)   : {len(abs_only):5d}  ({len(abs_only)/n*100:.0f}%)")
    print(f"      metadata only (no text)  : {len(meta_only):5d}  ({len(meta_only)/n*100:.0f}%)")
    if img_only:
        print(f"      image only               : {len(img_only):5d}")
    if scanned_pdfs:
        print(f"\n  !! PDFs with no extractable text (need OCR): {len(scanned_pdfs)}")
        for nme in scanned_pdfs[:10]:
            print(f"       - {nme}")
        if len(scanned_pdfs) > 10:
            print(f"       … and {len(scanned_pdfs)-10} more")
    elif unknown_pdfs:
        print("\n  (install pymupdf to also check PDFs for extractable text)")

    if show_list:
        for label, group in (("METADATA ONLY (worth chasing the PDF)", meta_only),
                             ("ABSTRACT ONLY (no PDF)", abs_only)):
            if group:
                print(f"\n--- {label}: {len(group)} ---")
                for s in sorted(group):
                    print(f"  {s}")

    if csv_out:
        with open(csv_out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["paper", "has_pdf", "n_docs", "n_images", "coverage", "bytes"])
            for stem, rec in sorted(papers.items()):
                cov = ("full_pdf" if rec["pdf"] else
                       "abstract_only" if stem in abs_only else
                       "metadata_only" if stem in meta_only else "image_only")
                w.writerow([stem, bool(rec["pdf"]), len(rec["doc"]),
                            len(rec["img"]), cov, rec["bytes"]])
        print(f"\nwrote per-paper table to {csv_out}")
    print()


if __name__ == "__main__":
    main()
