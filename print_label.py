#!/usr/bin/env python3
"""print_label.py - turn awkward shipping-label PDFs into clean 4x6 prints.

Works with the Nelko/Beeprt PL70e thermal printer already configured in CUPS,
but any CUPS printer with a 4x6 page size will do.

Typical use:

    ./print_label.py ~/Downloads/eBayReturnLabel_5334688644.pdf --preview
    ./print_label.py ~/Downloads/eBayReturnLabel_5334688644.pdf

The first command writes a 4x6 PDF plus a PNG preview without printing.
The second prints it.  Run --list to see what the tool detected on each page.
Pass a directory to convert every PDF inside it; --all-labels keeps every
detected label page (one 4x6 page each).  Run --help for all options.

Tests:  ./.venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import functools
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

__version__ = "1.1"

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "label-print.json"
LABEL_SIZE_IN = (4.0, 6.0)  # portrait: 4 inches wide, 6 inches long
PT_PER_IN = 72.0
PREVIEW_DPI = 150
FIT_CHOICES = ("contain", "fill", "stretch")

LABEL_PATTERNS = (
    r"\busps\b", r"\bups\b", r"\bfedex\b", r"\bdhl\b",
    r"\bcanada post\b", r"\broyal mail\b", r"\bpurolator\b",
    r"\bontrac\b", r"\bamazon\s+(?:shipping|label)\b",
    r"\bshipping\s+label\b", r"\breturn\s+label\b",
    r"\bwaybill\b", r"\bairbill\b", r"\bconsignment\b",
    r"\bground advantage\b", r"\bpriority mail\b", r"\bfirst class\b",
    r"\bparcel\b", r"\btracking\s*(?:#|number|no\.?)",
    r"\bpostage\b", r"\bpitney bowes\b", r"\bdelivery confirmation\b",
    r"\bship to\b", r"\brma\b", r"\blabel\b",
)
DOC_WORDS = (
    "packing slip", "invoice", "receipt", "return reason", "item number",
    "order summary", "thank you for your order",
)
PRINTER_HINTS = ("nelko", "pl70", "beeprt", "label", "thermal", "pl80", "pl50")
# Files this tool writes; directory mode skips them so a re-run doesn't re-crop
# its own output.
GENERATED_RE = re.compile(r"_4x6(?:_p[\d_]+)?\.pdf$", re.I)


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    """Read the saved defaults, dropping anything we can't use."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    cfg: dict = {}
    if raw.get("fit") in FIT_CHOICES:
        cfg["fit"] = raw["fit"]
    try:
        if raw.get("margin") is not None:
            cfg["margin"] = float(raw["margin"])
    except (TypeError, ValueError):
        pass
    if isinstance(raw.get("printer"), str) and raw["printer"].strip():
        cfg["printer"] = raw["printer"].strip()
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# PDF analysis
# ---------------------------------------------------------------------------

@dataclass
class PageInfo:
    number: int                  # 1-based page number
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    crop: pymupdf.Rect | None = None
    rotation: int = 0            # degrees clockwise to make content upright
    frame: bool = False

    @property
    def crop_size(self) -> tuple[float, float]:
        if not self.crop:
            return (0.0, 0.0)
        return (self.crop.width, self.crop.height)


def _union(rects: list[pymupdf.Rect]) -> pymupdf.Rect | None:
    valid = [r for r in rects if r.is_valid]
    if not valid:
        return None
    # Rect's | operator ignores degenerate (line) rectangles, so do it by hand.
    return pymupdf.Rect(
        min(r.x0 for r in valid), min(r.y0 for r in valid),
        max(r.x1 for r in valid), max(r.y1 for r in valid),
    )


@dataclass
class PageContent:
    """Everything we need from a page, extracted in one pass."""

    text: str
    blocks: list[dict] = field(default_factory=list)
    spans: list[pymupdf.Rect] = field(default_factory=list)
    images: list[tuple[pymupdf.Rect, int, int]] = field(default_factory=list)
    drawings: list[pymupdf.Rect] = field(default_factory=list)

    @property
    def ink(self) -> list[pymupdf.Rect]:
        """Rectangles that carry visible content (text spans and images)."""
        return self.spans + [r for r, _, _ in self.images]


def _page_content(page: pymupdf.Page) -> PageContent:
    content = PageContent(text=page.get_text("text"), drawings=[
        pymupdf.Rect(d["rect"]) for d in page.get_drawings()
    ])
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        content.blocks.append(block)
        for line in block["lines"]:
            for span in line["spans"]:
                if span["text"].strip():
                    content.spans.append(pymupdf.Rect(span["bbox"]))
    for image in page.get_images(full=True):
        xref, iw, ih = image[0], image[2], image[3]
        for rect in page.get_image_rects(xref):
            content.images.append((pymupdf.Rect(rect), iw, ih))
    return content


def _frame_rect(page: pymupdf.Page, content: PageContent) -> tuple[pymupdf.Rect | None, int]:
    """Look for a large box drawn with vector lines - usually the label outline."""
    # Frames are often emitted as many separate horizontal/vertical lines,
    # so keep zero-width lines too, but drop degenerate points.
    rects = [r for r in content.drawings if r.width >= 1 or r.height >= 1]
    if not rects:
        return None, 0
    box = _union(rects)
    if not box:
        return None, 0
    # A frame may be one rectangle path or dozens of separate line strokes.
    # Reject full-bleed background rectangles (they aren't the label outline).
    area_ok = 0.03 * page.rect.get_area() <= box.get_area() <= 0.9 * page.rect.get_area()
    if not (area_ok and box.width >= 100 and box.height >= 60):
        return None, 0
    # Require most of the real content to live inside the box; otherwise this
    # is decoration, not a label frame.  Count text spans and images rather
    # than area so small annotations outside the frame don't dominate.
    ink = content.ink
    inside = sum(1 for r in ink if box.contains(r) or box.contains(r.tl) or box.contains(r.br))
    coverage = inside / len(ink) if ink else 1.0
    return (box, len(rects)) if coverage >= 0.55 else (None, len(rects))


def _detect_rotation(content: PageContent) -> int:
    """Return degrees clockwise needed to make text/barcode upright."""
    votes: dict[int, float] = {0: 0.0, 90: 0.0, 180: 0.0, 270: 0.0}
    for block in content.blocks:
        for line in block["lines"]:
            direction = line.get("dir") or (1, 0)
            angle = math.degrees(math.atan2(direction[1], direction[0]))
            bucket = round(angle / 90.0) * 90 % 360
            weight = sum(len(span["text"].strip()) for span in line["spans"])
            votes[bucket] += max(weight, 1)
    best = max(votes, key=votes.get)
    if votes[best] > 3:
        return best

    # No useful text: a barcode image that is stored wide but placed tall has
    # been rotated.  Direction is a guess, but --rotate can override it.
    for rect, iw, ih in content.images:
        native = iw / ih if ih else 0
        placed = rect.width / rect.height if rect.height else 0
        if native >= 2 and placed and not 0.6 <= placed / native <= 1.7:
            return 90
    return 0


def analyze_page(page: pymupdf.Page, number: int) -> PageInfo:
    info = PageInfo(number=number)
    content = _page_content(page)
    lower = content.text.lower()

    frame, n_lines = _frame_rect(page, content)
    info.frame = frame is not None

    for pattern in LABEL_PATTERNS:
        match = re.search(pattern, lower)
        if match:
            info.score += 2
            info.reasons.append(f"+2 '{match.group(0).strip()}'")
            break
    if any(word in lower for word in DOC_WORDS):
        info.score -= 12
        info.reasons.append("-12 document text (packing slip/invoice)")

    if frame is not None:
        info.score += 5
        info.reasons.append(f"+5 label frame ({n_lines} vector paths)")
    if any(iw / max(ih, 1) >= 2.5 or ih / max(iw, 1) >= 2.5 for _, iw, ih in content.images):
        info.score += 3
        info.reasons.append("+3 barcode-like image")
    used = (_union(content.ink) or page.rect).get_area()
    if len(content.text) < 2000 and used < 0.75 * page.rect.get_area():
        info.score += 2
        info.reasons.append("+2 content occupies part of sheet")

    # Crop: prefer the drawn label frame; otherwise the union of all content.
    crop = frame or _union(content.ink) or page.rect
    # Include the stroke width of the frame and round outward.
    pad = 1.5
    crop = pymupdf.Rect(
        max(0, crop.x0 - pad), max(0, crop.y0 - pad),
        min(page.rect.x1, crop.x1 + pad), min(page.rect.y1, crop.y1 + pad),
    )
    info.crop = crop
    info.rotation = _detect_rotation(content)
    return info


def parse_pages(spec: str | None, count: int) -> list[int]:
    """1-based page numbers from '2', '1,3' or '1-3'."""
    if not spec:
        return []
    result: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(value) for value in part.split("-", 1))
            else:
                a = b = int(part)
        except ValueError:
            raise ValueError(f"invalid page {part!r}; use 1, 3 or 1-3") from None
        if a > b:
            raise ValueError(f"invalid page range {part!r}: start is after end")
        result.extend(range(a, b + 1))
    for n in result:
        if not 1 <= n <= count:
            raise ValueError(f"page {n} out of range (document has {count} pages)")
    return list(dict.fromkeys(result))


# ---------------------------------------------------------------------------
# output PDF
# ---------------------------------------------------------------------------

def _fit_rect(crop: pymupdf.Rect, rotation: int, fit: str, margin_pt: float) -> pymupdf.Rect:
    page_w, page_h = LABEL_SIZE_IN[0] * PT_PER_IN, LABEL_SIZE_IN[1] * PT_PER_IN
    avail_w, avail_h = page_w - 2 * margin_pt, page_h - 2 * margin_pt
    w, h = crop.width, crop.height
    if rotation % 180:
        w, h = h, w
    if w <= 0 or h <= 0:
        raise ValueError("empty crop region")

    if fit == "stretch":
        sx, sy = avail_w / w, avail_h / h
    else:
        scale = (min if fit == "contain" else max)(avail_w / w, avail_h / h)
        sx = sy = scale

    out_w, out_h = w * sx, h * sy
    x0 = margin_pt + (avail_w - out_w) / 2
    y0 = margin_pt + (avail_h - out_h) / 2
    return pymupdf.Rect(x0, y0, x0 + out_w, y0 + out_h)


def build_pdf(
    src_doc: pymupdf.Document,
    selections: list[tuple[int, PageInfo, int | None]],
    output: Path,
    *,
    margin_in: float,
    fit: str,
) -> None:
    out = pymupdf.open()
    page_w = LABEL_SIZE_IN[0] * PT_PER_IN
    page_h = LABEL_SIZE_IN[1] * PT_PER_IN
    margin_pt = margin_in * PT_PER_IN
    if margin_pt < 0:
        raise ValueError("margin cannot be negative")
    if 2 * margin_pt >= min(page_w, page_h):
        raise ValueError("margin is too large")

    for number, info, rotation_override in selections:
        rotation = info.rotation if rotation_override is None else rotation_override
        if info.crop is None or info.crop.is_empty:
            raise ValueError(f"page {number} has an empty crop region")
        target = _fit_rect(info.crop, rotation, fit, margin_pt)
        dst = out.new_page(width=page_w, height=page_h)
        dst.show_pdf_page(
            target, src_doc, number - 1,
            clip=info.crop, rotate=rotation, keep_proportion=False,
        )

    if out.page_count == 0:
        out.close()
        raise ValueError("no pages to write")

    out.set_metadata({
        "title": "4x6 shipping label",
        "producer": "print_label.py",
        "creator": "print_label.py",
    })
    out.save(str(output), garbage=4, deflate=True)
    out.close()


def write_previews(pdf: Path, first: Path, dpi: int = PREVIEW_DPI) -> list[Path]:
    """Render every page of the 4x6 PDF to PNG, starting at *first*."""
    first = first if first.suffix else first.with_suffix(".png")
    written: list[Path] = []
    with pymupdf.open(str(pdf)) as doc:
        for index in range(doc.page_count):
            target = first if index == 0 else first.with_name(f"{first.stem}_p{index + 1}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            doc.load_page(index).get_pixmap(dpi=dpi, alpha=False).save(str(target))
            written.append(target)
    return written


# ---------------------------------------------------------------------------
# CUPS
# ---------------------------------------------------------------------------

def cups_printers() -> list[str]:
    try:
        out = subprocess.run(
            ["lpstat", "-p"], capture_output=True, text=True, check=False, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    printers = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "printer":
            printers.append(parts[1])
    return printers


@functools.lru_cache(maxsize=None)
def lpoptions(printer: str) -> str:
    try:
        return subprocess.run(
            ["lpoptions", "-p", printer, "-l"],
            capture_output=True, text=True, check=False, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _option_line(printer: str, name: str) -> str:
    prefix = name.lower() + "/"
    return next((l for l in lpoptions(printer).splitlines() if l.lower().startswith(prefix)), "")


def option_name(printer: str, name: str) -> str:
    """The printer's own keyword for an option ('' when the queue lacks it)."""
    line = _option_line(printer, name)
    return line.split("/", 1)[0].strip() if line else ""


def option_choices(printer: str, name: str) -> list[str]:
    line = _option_line(printer, name)
    return [v.lstrip("*") for v in line.partition(":")[2].split()]


def _four_by_six(printer: str) -> str | None:
    """The printer's own 4x6 media choice, if it has one."""
    return next(
        (c for c in option_choices(printer, "pagesize") if re.fullmatch(r"w288h432|4x6", c, re.I)),
        None,
    )


def printer_page_size(printer: str) -> bool:
    """True if the queue offers a 4x6 (288x432 pt) media size."""
    return _four_by_six(printer) is not None


def choose_printer(requested: str | None) -> str:
    printers = cups_printers()
    if requested:
        if requested not in printers:
            raise ValueError(f"printer '{requested}' not found; available: {', '.join(printers) or 'none'}")
        return requested
    capable = [p for p in printers if printer_page_size(p)]
    if capable:
        return sorted(capable, key=lambda p: (not any(h in p.lower() for h in PRINTER_HINTS), p))[0]
    if printers:
        return printers[0]
    raise ValueError("no CUPS printers found")


def page_size_option(printer: str) -> str:
    return _four_by_six(printer) or "w288h432"


def _dpi_choice(printer: str, wanted: int = 203) -> str | None:
    """The printer's own value for a ~203 dpi resolution, if it offers one."""
    for choice in option_choices(printer, "resolution"):
        if choice.lower().startswith(f"{wanted}dpi") or choice.lower().startswith(f"{wanted}x{wanted}dpi"):
            return choice
    return None


def print_pdf(pdf: Path, printer: str, copies: int, darkness: int | None) -> None:
    cmd = [
        "lp", "-d", printer,
        "-o", f"{option_name(printer, 'pagesize') or 'PageSize'}={page_size_option(printer)}",
        "-o", f"copies={copies}",
    ]
    # Only send options the queue actually understands (a Brother laser and a
    # thermal label printer have very different PPDs), and only values the PPD
    # lists - otherwise CUPS rejects the whole job.
    resolution = _dpi_choice(printer)
    if resolution and (name := option_name(printer, "resolution")):
        cmd += ["-o", f"{name}={resolution}"]
    if darkness is not None and (name := option_name(printer, "darkness")):
        if str(darkness) in option_choices(printer, "darkness"):
            cmd += ["-o", f"{name}={darkness}"]
        else:
            print(f"warning: printer {printer} has no darkness {darkness}; ignoring", file=sys.stderr)
    cmd.append(str(pdf))
    subprocess.run(cmd, check=True, timeout=120)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_listing(source: Path, infos: list[PageInfo], page_count: int) -> None:
    print(f"\n{source.name}  ({page_count} page(s))")
    for info in infos:
        cw, ch = info.crop_size
        print(
            f"  page {info.number}: score={info.score:+.0f} "
            f"rotation={info.rotation}deg "
            f"crop={cw:.0f}x{ch:.0f}pt frame={'yes' if info.frame else 'no'}"
        )
        for reason in info.reasons:
            print(f"      {reason}")


def select_pages(
    infos: list[PageInfo], args: argparse.Namespace, page_count: int, source: Path
) -> list[PageInfo]:
    """Pages to label, honouring --page/--all-labels or auto-detection."""
    if args.page:
        return [infos[n - 1] for n in parse_pages(args.page, page_count)]
    if args.all_labels:
        labels = [info for info in infos if info.score > 0]
        if not labels:
            raise ValueError(f"no label pages detected in {source.name}")
        return labels
    ranked = sorted(infos, key=lambda info: info.score, reverse=True)
    if ranked and ranked[0].score > 0:
        return [ranked[0]]
    print(
        f"warning: no strong label match in {source.name}; using page {infos[0].number}",
        file=sys.stderr,
    )
    return [infos[0]]


def output_path(
    source: Path, chosen: list[PageInfo], args: argparse.Namespace, multiple_sources: bool
) -> Path:
    if args.output and not multiple_sources and len(chosen) == 1:
        return args.output
    if args.output:
        print("warning: --output ignored for multiple labels; using default names", file=sys.stderr)
    name = f"{source.stem}_4x6"
    if len(chosen) > 1:
        name += "_p" + "_".join(str(info.number) for info in chosen)
    return source.with_name(name + ".pdf")


def process_pdf(
    source: Path,
    args: argparse.Namespace,
    multiple_sources: bool,
    manual_crop: pymupdf.Rect | None,
) -> bool:
    """Crop (and unless asked not to, print) one source PDF.  False on error."""
    try:
        doc = pymupdf.open(str(source))
    except Exception as exc:
        print(f"error: cannot open {source}: {exc}", file=sys.stderr)
        return False

    with doc:
        infos = [analyze_page(doc.load_page(i), i + 1) for i in range(doc.page_count)]
        if args.list:
            print_listing(source, infos, doc.page_count)
            return True

        try:
            chosen = select_pages(infos, args, doc.page_count, source)
            if manual_crop is not None:
                for info in chosen:
                    clipped = manual_crop & doc.load_page(info.number - 1).rect
                    if clipped.is_empty:
                        raise ValueError(f"--crop does not overlap page {info.number}")
                    info.crop = clipped
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return False

        output = output_path(source, chosen, args, multiple_sources)
        if output.resolve() == source.resolve():
            print(f"error: refusing to overwrite the source PDF {source}", file=sys.stderr)
            return False
        output.parent.mkdir(parents=True, exist_ok=True)

        rotation_override = int(args.rotate) if args.rotate is not None else None
        selections = [(info.number, info, rotation_override) for info in chosen]
        try:
            build_pdf(doc, selections, output, margin_in=args.margin, fit=args.fit)
        except Exception as exc:
            print(f"error: could not build label PDF: {exc}", file=sys.stderr)
            return False

        description = ", ".join(
            f"p{info.number} {info.crop.width:.0f}x{info.crop.height:.0f}pt @ "
            f"{rotation_override if rotation_override is not None else info.rotation}deg"
            for info in chosen
        )
        print(f"{source.name}: {description} -> {output}")

        preview = args.preview or (output.with_suffix(".png") if args.show else None)
        if preview:
            written = write_previews(output, preview)
            for path in written:
                print(f"preview: {path}")
            if args.show and written:
                # Opening the preview is a convenience; failing to spawn is fine.
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                try:
                    subprocess.Popen([opener, str(written[0])])
                except OSError:
                    pass

    if args.no_print or args.show:
        return True
    try:
        printer = choose_printer(args.printer)
        print_pdf(output, printer, args.copies, args.darkness)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f"error: printing failed: {exc}", file=sys.stderr)
        return False
    print(f"printed on {printer} ({args.copies} copy/copies)")
    return True


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    parser = argparse.ArgumentParser(
        description="Crop/rotate a shipping label PDF to 4x6 and print it on a thermal label printer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("pdf", nargs="?", type=Path, help="source PDF (or a directory of PDFs)")
    parser.add_argument("--list", action="store_true", help="show detected label pages/crops and exit")
    parser.add_argument("--list-printers", action="store_true", help="show CUPS printers and exit")
    parser.add_argument("--page", help="page number(s) to use: 1, 3, or 1-2 (default: auto-detect)")
    parser.add_argument("--all-labels", action="store_true", help="print every detected label page")
    parser.add_argument("--crop", help="manual crop in source-page points: x0,y0,x1,y1 (overrides auto-detection)")
    parser.add_argument("--rotate", choices=("0", "90", "180", "270"), help="override detected rotation (degrees clockwise)")
    parser.add_argument("--fit", choices=FIT_CHOICES, default=cfg.get("fit", "contain"),
                        help="how to fit content to 4x6 (default: contain, no distortion)")
    parser.add_argument("--margin", type=float, default=cfg.get("margin", 0.05),
                        help="blank margin in inches (default: 0.05)")
    parser.add_argument("--printer", default=cfg.get("printer"), help="CUPS printer name (default: auto-detect 4x6 queue)")
    parser.add_argument("--copies", type=int, default=1, help="number of copies (default: 1)")
    parser.add_argument("--darkness", type=int, help="printer darkness override, e.g. 10")
    parser.add_argument("--output", type=Path, help="where to save the generated 4x6 PDF")
    parser.add_argument("--preview", type=Path, help="also write a PNG preview of the 4x6 PDF")
    parser.add_argument("--show", action="store_true", help="generate a preview, open it with xdg-open, and do not print")
    parser.add_argument("--no-print", "--dry-run", action="store_true", help="generate files but do not print")
    parser.add_argument("--save-defaults", action="store_true", help="save printer/fit/margin as defaults")
    args = parser.parse_args(argv)

    if args.list_printers:
        for p in cups_printers():
            print(f"{p}{'  [4x6 capable]' if printer_page_size(p) else ''}")
        return 0
    if not args.pdf:
        parser.error("a PDF file is required (or use --list-printers)")
    if args.copies < 1:
        parser.error("--copies must be at least 1")
    if args.page and args.all_labels:
        print("warning: --all-labels ignored because --page was given", file=sys.stderr)

    manual_crop = None
    if args.crop:
        try:
            values = [float(v.strip()) for v in args.crop.split(",")]
            if len(values) != 4:
                raise ValueError("expected four values")
            manual_crop = pymupdf.Rect(*values)
        except ValueError as exc:
            parser.error(f"invalid --crop {args.crop!r}: {exc}")

    if args.save_defaults:
        cfg.update({k: v for k, v in vars(args).items() if k in ("printer", "fit", "margin") and v is not None})
        save_config(cfg)
        print(f"Saved defaults to {CONFIG_PATH}")

    if args.pdf.is_dir():
        files = sorted(
            p for p in args.pdf.iterdir()
            if p.suffix.lower() == ".pdf" and not GENERATED_RE.search(p.name)
        )
    else:
        files = [args.pdf]
    if not files:
        parser.error(f"no PDFs found in {args.pdf}")

    exit_code = 0
    for source in files:
        if not source.exists():
            print(f"error: no such file: {source}", file=sys.stderr)
            exit_code = 1
            continue
        if not process_pdf(source, args, len(files) > 1, manual_crop):
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
