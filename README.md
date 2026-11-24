# label-print

Turn awkward shipping-label PDFs (multi-page eBay/UPS/FedEx returns, letter-size
sheets with the label in one corner) into clean 4x6 prints on a thermal label
printer. Prints through CUPS with `lp`.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
source .venv/bin/activate   # so ./print_label.py finds pymupdf
```

## Examples

```sh
# Detect what's on each page without printing
./print_label.py ~/Downloads/ReturnLabel.pdf --list

# Write a 4x6 PDF plus a PNG preview, don't print
./print_label.py ~/Downloads/ReturnLabel.pdf --preview

# Print it (auto-detects the 4x6 label page and the 4x6-capable printer)
./print_label.py ~/Downloads/ReturnLabel.pdf

# Print 2 copies, darker
./print_label.py label.pdf --copies 2 --darkness 10

# Generate and open a preview instead of printing
./print_label.py label.pdf --show

# Convert every PDF in a directory
./print_label.py ~/Downloads/labels/

# Keep every detected label page in one multi-page 4x6 PDF
./print_label.py label.pdf --all-labels

# Pick pages yourself, override the detection
./print_label.py label.pdf --page 2 --rotate 90 --crop 20,20,400,600

# What printers are available?
./print_label.py --list-printers

# Remember printer/fit/margin in ~/.config/label-print.json
./print_label.py label.pdf --printer PL70e-BT --margin 0.1 --save-defaults --no-print
```

Output naming: `label_4x6.pdf`, or `label_4x6_p1_2.pdf` for multiple label pages.
Directory mode skips files it generated itself.

## How it works (high level)

1. **Look at every page.** For each page the script collects the text, the text
   and image rectangles, and the vector drawings (one extraction pass).
2. **Score the page.** A page scores points for carrier/label wording ("UPS",
   "shipping label", "tracking number"), for a large drawn box around the
   content (the usual label outline) and for a barcode-shaped image; it loses
   points for packing-slip/invoice wording. The best-scoring page wins.
3. **Guess the crop and the rotation.** The crop is the label frame if one was
   found, otherwise the bounding box of all text and images. The rotation comes
   from a vote on the writing direction of the text lines (with a barcode
   aspect-ratio fallback), so sideways-scanned labels still come out upright.
4. **Rebuild the page.** The crop is placed on a fresh 288x432 pt (4x6 in) page
   with `contain` / `fill` / `stretch` scaling and an optional margin; the
   source page is never modified. Multi-page input becomes one 4x6 page per
   label.
5. **Optional preview**, then print: the script asks CUPS for the printer's PPD
   options and only sends ones the queue actually has (4x6 `PageSize`,
   `Resolution`, `Darkness`), so a laser printer and a label printer can coexist.

All heuristics can be overridden with `--page`, `--crop`, `--rotate`, `--fit`,
`--margin`, `--printer`; `--list` shows the score and the reasons behind it.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

## License

MIT — see [LICENSE](LICENSE).
