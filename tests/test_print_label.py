"""Tests for print_label.py.

Run with:  ./.venv/bin/python -m unittest discover -s tests -v
No extra dependencies beyond pymupdf.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pymupdf  # noqa: E402

import print_label as pl  # noqa: E402

PT = pl.PT_PER_IN


def make_pdf(path: Path, *, rotate_text: int = 0, page_rotation: int = 0) -> Path:
    """A fake label: framed box with text, placed in the top-left of a letter page."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    box = pymupdf.Rect(30, 30, 318, 462)  # 4x6 inches
    page.draw_rect(box)
    page.insert_textbox(
        box + (10, 10, -10, -10),
        "UPS SHIPPING LABEL\nTRACKING NUMBER: 1Z999AA10123456784\nSHIP TO: TEST RECEIVER",
        fontsize=14,
        rotate=rotate_text,
    )
    if page_rotation:
        page.set_rotation(page_rotation)
    doc.save(str(path))
    return path


class ParsePagesTest(unittest.TestCase):
    def test_specs(self):
        self.assertEqual(pl.parse_pages(None, 3), [])
        self.assertEqual(pl.parse_pages("2", 3), [2])
        self.assertEqual(pl.parse_pages("1, 3", 3), [1, 3])
        self.assertEqual(pl.parse_pages("1-3", 3), [1, 2, 3])
        self.assertEqual(pl.parse_pages("1-3,2", 3), [1, 2, 3])  # de-duplicated

    def test_bad_specs(self):
        for spec in ("0", "4", "1-4", "abc", "2-1", "1,,x"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                pl.parse_pages(spec, 3)


class FitRectTest(unittest.TestCase):
    def test_contain_keeps_aspect_and_centers(self):
        target = pl._fit_rect(pymupdf.Rect(0, 0, 400, 200), 0, "contain", 0)
        self.assertAlmostEqual(target.width / target.height, 2.0)
        self.assertAlmostEqual(target.width, 4 * PT)
        self.assertAlmostEqual(target.y0, (6 * PT - 2 * PT) / 2)  # vertically centered

    def test_rotation_swaps_dimensions(self):
        portrait = pl._fit_rect(pymupdf.Rect(0, 0, 100, 300), 0, "contain", 0)
        rotated = pl._fit_rect(pymupdf.Rect(0, 0, 100, 300), 90, "contain", 0)
        self.assertAlmostEqual(portrait.height, 6 * PT)
        self.assertAlmostEqual(rotated.width, 4 * PT)

    def test_stretch_fills_available_area(self):
        target = pl._fit_rect(pymupdf.Rect(0, 0, 100, 100), 0, "stretch", 0)
        self.assertAlmostEqual(target.width, 4 * PT)
        self.assertAlmostEqual(target.height, 6 * PT)


class AnalyzeAndBuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_detects_framed_label(self):
        src = make_pdf(self.tmp / "label.pdf")
        with pymupdf.open(str(src)) as doc:
            info = pl.analyze_page(doc.load_page(0), 1)
        self.assertTrue(info.frame)
        self.assertGreater(info.score, 0)
        self.assertEqual(info.rotation, 0)
        self.assertLessEqual(info.crop.width, 300)  # cropped to the frame, not the sheet

    def test_detects_rotated_content(self):
        src = make_pdf(self.tmp / "rot.pdf", rotate_text=90)
        with pymupdf.open(str(src)) as doc:
            self.assertEqual(pl.analyze_page(doc.load_page(0), 1).rotation, 270)

    def test_page_rotation_is_not_double_applied(self):
        src = make_pdf(self.tmp / "pagerot.pdf", page_rotation=90)
        with pymupdf.open(str(src)) as doc:
            self.assertEqual(pl.analyze_page(doc.load_page(0), 1).rotation, 0)

    def test_build_pdf_writes_six_by_four(self):
        src = make_pdf(self.tmp / "label.pdf")
        out = self.tmp / "label_4x6.pdf"
        with pymupdf.open(str(src)) as doc:
            info = pl.analyze_page(doc.load_page(0), 1)
            pl.build_pdf(doc, [(1, info, None)], out, margin_in=0.05, fit="contain")
        with pymupdf.open(str(out)) as doc:
            self.assertEqual(doc.page_count, 1)
            rect = doc.load_page(0).rect
            self.assertAlmostEqual(rect.width, 4 * PT)
            self.assertAlmostEqual(rect.height, 6 * PT)
        self.assertTrue(pl.write_previews(out, self.tmp / "label.png")[0].exists())

    def test_build_pdf_rejects_bad_margin(self):
        src = make_pdf(self.tmp / "label.pdf")
        with pymupdf.open(str(src)) as doc:
            info = pl.analyze_page(doc.load_page(0), 1)
            for margin in (-0.1, 10):
                with self.subTest(margin=margin), self.assertRaises(ValueError):
                    pl.build_pdf(doc, [(1, info, None)], self.tmp / "x.pdf",
                                 margin_in=margin, fit="contain")


class ConfigTest(unittest.TestCase):
    def test_bad_config_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "label-print.json"
            original = pl.CONFIG_PATH
            pl.CONFIG_PATH = path
            try:
                path.write_text('{"fit": "bogus", "margin": "oops", "printer": "  "}')
                self.assertEqual(pl.load_config(), {})
                path.write_text('{"fit": "fill", "margin": "0.1", "printer": "PL70e"}')
                self.assertEqual(pl.load_config(), {"fit": "fill", "margin": 0.1, "printer": "PL70e"})
                path.write_text("not json")
                self.assertEqual(pl.load_config(), {})
            finally:
                pl.CONFIG_PATH = original


class PrinterOptionTest(unittest.TestCase):
    def test_option_parsing(self):
        fake = (
            "PageSize/Media Size: *Letter w288h432 4x6\n"
            "Resolution/Output Resolution: 300dpi *203x203dpi\n"
            "Darkness/Darkness: *0 5 10\n"
        )
        original = pl.lpoptions
        pl.lpoptions = lambda printer: fake
        try:
            self.assertTrue(pl.printer_page_size("p"))
            self.assertEqual(pl.page_size_option("p"), "w288h432")
            self.assertEqual(pl.option_name("p", "resolution"), "Resolution")
            self.assertEqual(pl._dpi_choice("p"), "203x203dpi")
            self.assertEqual(pl.option_choices("p", "darkness"), ["0", "5", "10"])
            self.assertEqual(pl.option_name("p", "nope"), "")
        finally:
            pl.lpoptions = original


if __name__ == "__main__":
    unittest.main()
