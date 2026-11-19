#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Atlas Omnia
"""Build the three demo documents in docs/fixtures/ deterministically with stdlib zipfile.

The documents hold only made-up demo data (four sales regions, one sentence, one
slide). Running this script twice produces byte-identical files.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "fixtures"
STAMP = (2026, 1, 1, 0, 0, 0)

SALES = [("North", 120), ("South", 95), ("East", 143), ("West", 88)]
NOTE = "Hermes Writer original text."
SLIDE_TITLE = "Hermes Impress Proof"
SLIDE_BODY = "Original slide content"

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:presentation="urn:oasis:names:tc:opendocument:xmlns:presentation:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
    'office:version="1.3"'
)
HEAD = '<?xml version="1.0" encoding="UTF-8"?>\n'


def text_cell(value: str) -> str:
    return f'<table:table-cell office:value-type="string"><text:p>{escape(value)}</text:p></table:table-cell>'


def number_cell(value: int) -> str:
    return (f'<table:table-cell office:value-type="float" office:value="{value}">'
            f'<text:p>{value}</text:p></table:table-cell>')


def sales_content() -> str:
    rows = [text_cell("Region") + text_cell("Units")]
    rows += [text_cell(region) + number_cell(units) for region, units in SALES]
    body = "".join(f"<table:table-row>{row}</table:table-row>" for row in rows)
    return (HEAD + f'<office:document-content {NS}><office:body><office:spreadsheet>'
            '<table:table table:name="Sheet1"><table:table-column table:number-columns-repeated="2"/>'
            f'{body}</table:table></office:spreadsheet></office:body></office:document-content>')


def notes_content() -> str:
    return (HEAD + f'<office:document-content {NS}><office:body><office:text>'
            f'<text:p>{escape(NOTE)}</text:p></office:text></office:body></office:document-content>')


def deck_styles() -> str:
    return (HEAD + f'<office:document-styles {NS}><office:styles>'
            '<style:style style:name="Default-title" style:family="presentation"/>'
            '<style:style style:name="Default-outline1" style:family="presentation"/>'
            '</office:styles><office:automatic-styles>'
            '<style:page-layout style:name="PM1"><style:page-layout-properties fo:page-width="28cm" '
            'fo:page-height="15.75cm" style:print-orientation="landscape"/></style:page-layout>'
            '</office:automatic-styles><office:master-styles>'
            '<style:master-page style:name="Default" style:page-layout-name="PM1"/>'
            '</office:master-styles></office:document-styles>')


def frame(kind: str, style: str, y: str, height: str, value: str) -> str:
    # LibreOffice imports a frame as a title/outline placeholder only when it has a presentation style.
    return (f'<draw:frame presentation:style-name="{style}" presentation:class="{kind}" svg:x="1.4cm" svg:y="{y}" svg:width="25.2cm" '
            f'svg:height="{height}"><draw:text-box><text:p>{escape(value)}</text:p></draw:text-box></draw:frame>')


def deck_content() -> str:
    return (HEAD + f'<office:document-content {NS}><office:body><office:presentation>'
            '<draw:page draw:name="Slide1" draw:master-page-name="Default">'
            + frame("title", "Default-title", "0.6cm", "2.6cm", SLIDE_TITLE)
            + frame("outline", "Default-outline1", "3.7cm", "10.4cm", SLIDE_BODY)
            + '</draw:page></office:presentation></office:body></office:document-content>')


def manifest(media_type: str, with_styles: bool) -> str:
    entries = [("/", media_type), ("content.xml", "text/xml")]
    if with_styles:
        entries.append(("styles.xml", "text/xml"))
    lines = "".join(f'<manifest:file-entry manifest:full-path="{path}" manifest:media-type="{kind}"'
                    + (' manifest:version="1.3"' if path == "/" else "") + "/>" for path, kind in entries)
    return (HEAD + '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
            f'manifest:version="1.3">{lines}</manifest:manifest>')


DOCUMENTS = {
    "demo-sales.ods": ("application/vnd.oasis.opendocument.spreadsheet", sales_content, None),
    "demo-notes.odt": ("application/vnd.oasis.opendocument.text", notes_content, None),
    "demo-deck.odp": ("application/vnd.oasis.opendocument.presentation", deck_content, deck_styles),
}


def entry(name: str) -> zipfile.ZipInfo:
    # Every entry is stored uncompressed, so the bytes do not depend on the zlib version.
    info = zipfile.ZipInfo(name, date_time=STAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = 0o100644 << 16
    info.create_system = 3
    return info


def write_document(path: Path, media_type: str, content, styles) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        # ODF requires an uncompressed "mimetype" as the first entry.
        archive.writestr(entry("mimetype"), media_type)
        archive.writestr(entry("content.xml"), content())
        if styles is not None:
            archive.writestr(entry("styles.xml"), styles())
        archive.writestr(entry("META-INF/manifest.xml"), manifest(media_type, styles is not None))


def build(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (media_type, content, styles) in DOCUMENTS.items():
        path = out_dir / name
        write_document(path, media_type, content, styles)
        written.append(path)
    return written


# ---- Workflow-acceptance scenario fixtures (synthetic data only; never written to docs/fixtures) ----

CATEGORIES = ("Travel", "Supplies", "Software", "Training")
UNICODE_LINE = "Ünïcödé check — Zürich, 東京, naïve café ✓"
KEY_FIGURE = "Key figure: 4,321 widgets"
TYPOS = ("recieve", "seperate", "occured", "definately", "untill")


def budget_rows() -> list[tuple[str, str, int]]:
    """60 deterministic expense rows: category, item, amount."""
    return [(CATEGORIES[i % 4], f"Item {i + 1:02d}", 10 + (i * 37) % 190) for i in range(60)]


def budget_totals() -> dict[str, int]:
    totals = {name: 0 for name in CATEGORIES}
    for category, _item, amount in budget_rows():
        totals[category] += amount
    return totals


def messy_rows() -> list[tuple[str, str, str]]:
    """200 rows of deliberately untidy text: names with spaces, numeric text, N/A, ISO dates."""
    rows = []
    for i in range(200):
        name = f"  Client {i % 23:02d}  " if i % 5 == 0 else f"Client {i % 23:02d}"
        amount = "N/A" if i % 17 == 0 else (f"{1000 + i * 13:,}" if i % 3 == 0 else str(100 + i))
        rows.append((name, amount, f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}"))
    return rows


def table_xml(name: str, rows: list[list[str]]) -> str:
    width = max(len(row) for row in rows)
    body = "".join("<table:table-row>" + "".join(text_cell(cell) for cell in row) + "</table:table-row>" for row in rows)
    return (f'<table:table table:name="{escape(name)}"><table:table-column table:number-columns-repeated="{width}"/>'
            f"{body}</table:table>")


def spreadsheet(sheets: list) -> str:
    """Sheets as ``(name, rows)``; an ``int`` cell is numeric, anything else is text."""
    tables = []
    for name, rows in sheets:
        width = max(len(row) for row in rows)
        body = "".join("<table:table-row>" + "".join(
            number_cell(cell) if isinstance(cell, int) else text_cell(str(cell)) for cell in row) + "</table:table-row>"
            for row in rows)
        tables.append(f'<table:table table:name="{escape(name)}"><table:table-column '
                      f'table:number-columns-repeated="{width}"/>{body}</table:table>')
    return (HEAD + f'<office:document-content {NS}><office:body><office:spreadsheet>{"".join(tables)}'
            '</office:spreadsheet></office:body></office:document-content>')


def text_document(blocks: list[str]) -> str:
    return (HEAD + f'<office:document-content {NS}><office:body><office:text>{"".join(blocks)}'
            '</office:text></office:body></office:document-content>')


def para(value: str) -> str:
    return f"<text:p>{escape(value)}</text:p>"


def heading(value: str, level: int = 1) -> str:
    return (f'<text:h text:style-name="Heading_20_{level}" text:outline-level="{level}">'
            f"{escape(value)}</text:h>")


def long_paragraphs() -> list[str]:
    return [f"Paragraph {n:03d}. " + (UNICODE_LINE if n % 50 == 0 else "Routine filler sentence for paging.")
            for n in range(1, 601)]


def long_blocks() -> list[str]:
    blocks = [para(text) for text in long_paragraphs()]
    # The key figure lives only in a table cell, after paragraph 300.
    blocks.insert(300, table_xml("KeyFigures", [["Metric", "Value"], ["Widgets", KEY_FIGURE]]))
    return blocks


def typo_paragraphs() -> list[str]:
    return [
        f"Please {TYPOS[0]} the parcel and keep the {TYPOS[1]} invoices apart.",
        f"The outage {TYPOS[2]} twice; it {TYPOS[2]} again on Friday.",
        f"We will {TYPOS[3]} wait {TYPOS[4]} noon.",
        "This sentence is already correct.",
    ]


OUTLINE = [(f"Section {n}: {topic}", f"Notes about {topic.lower()}.") for n, topic in
           enumerate(("Goals", "Scope", "Timeline", "Budget", "Risks", "Next steps"), start=1)]


REVIEW_SENTENCES = ("The launch moved to May.", "Costs rose by 4 percent.", "Hiring stays flat this quarter.")


def review_content() -> str:
    """Three sentences; the second holds a bold span; the third already carries a comment."""
    styles = ('<office:automatic-styles><style:style style:name="T1" style:family="text">'
              '<style:text-properties fo:font-weight="bold"/></style:style></office:automatic-styles>')
    note = ('<office:annotation><dc:creator>Existing Reviewer</dc:creator><dc:date>2026-01-01T00:00:00</dc:date>'
            '<text:p>Existing note</text:p></office:annotation>')
    body = (para(REVIEW_SENTENCES[0])
            + '<text:p>Costs rose by <text:span text:style-name="T1">4 percent</text:span>.</text:p>'
            + f"<text:p>{note}{escape(REVIEW_SENTENCES[2])}</text:p>")
    ns = NS.replace('office:version="1.3"', 'xmlns:dc="http://purl.org/dc/elements/1.1/" office:version="1.3"')
    return (HEAD + f'<office:document-content {ns}>{styles}<office:body><office:text>{body}'
            '</office:text></office:body></office:document-content>')


def deck(slides: list[tuple[str, str]], bold_word: str = "") -> str:
    """Slides as (title, body); ``bold_word`` is set in bold wherever it ends a body (mixed formatting)."""
    def body_frame(body: str) -> str:
        xml = frame("outline", "Default-outline1", "3.7cm", "10.4cm", body)
        if bold_word and body.endswith(bold_word):
            plain = escape(body[:-len(bold_word)])
            xml = xml.replace(f"<text:p>{escape(body)}</text:p>",
                              f'<text:p>{plain}<text:span text:style-name="T1">{escape(bold_word)}</text:span></text:p>')
        return xml
    styles = ('<office:automatic-styles><style:style style:name="T1" style:family="text">'
              '<style:text-properties fo:font-weight="bold"/></style:style></office:automatic-styles>') if bold_word else ""
    pages = "".join(
        f'<draw:page draw:name="Slide{n}" draw:master-page-name="Default">'
        + frame("title", "Default-title", "0.6cm", "2.6cm", title)
        + body_frame(body) + "</draw:page>"
        for n, (title, body) in enumerate(slides, start=1))
    return (HEAD + f'<office:document-content {NS}>{styles}<office:body><office:presentation>{pages}'
            '</office:presentation></office:body></office:document-content>')


DECK_SLIDES = [("Acme Widget launch", "Acme Widget overview"), ("Market", "Acme Widget buyers"),
               ("Plan", "Ship Acme Widget in May"), ("Close", "Questions")]

# Actual against budget per item; rows 2, 5 and 6 (Rent, Training, Travel) are over budget.
PLAN_ROWS = [("Rent", 1200, 1000), ("Software", 800, 800), ("Supplies", 90, 120), ("Training", 650, 500),
             ("Travel", 460, 450), ("Hosting", 300, 340)]

# ---- tool-coverage fixtures: the tools no model run had called yet ----

# tracker.ods, sheet Tasks: every Status is one of the dropdown items, every Percent is 0..100.
TRACKER_STATUSES = ("Open", "In progress", "Done")
TRACKER_ROWS = [("Draft brief", "Ana", "Done", 100), ("Book venue", "Ben", "In progress", 40), ("Order badges", "Cai", "Open", 0),
                ("Send invites", "Ana", "In progress", 60), ("Plan catering", "Dee", "Open", 10), ("Print signs", "Ben", "Done", 100)]
# sales.ods: Q3 has its line-total formula in D2 only; Q4 is complete with a grand total in D7.
SALES_Q3 = [(f"Product {n:02d}", 10 + 3 * n, 4 + 2 * (n % 4)) for n in range(1, 13)]
SALES_Q4 = [("North", 25, 6), ("South", 18, 8), ("East", 30, 5), ("West", 12, 9), ("Online", 40, 4)]
SALES_Q4_CORRECTED_UNITS = 33  # what the user types into Q4!B2 with automatic calculation off (scenario C11)
# ledger.ods, sheet Costs: vendors, comments and a scratch area; sheets Jan, Feb and an empty Old.
LEDGER_COSTS = [("Hosting", "Acme Ltd", 300), ("Licences", "Acme Ltd Services", 120), ("Travel", "Northwind", 460),
                ("Catering", "Acme Ltd", 90), ("Training", "Contoso", 650)]
LEDGER_COMMENTS = {"A3": "Supplier changed in March", "B4": "Check the vendor name", "C2": "Resolved: approved by Dana",
                   "C5": "Resolved: paid"}
LEDGER_SCRATCH = [["scratch", "temp"], [1, 2], ["delete me", 3]]   # Costs!E1:F3
LEDGER_MONTHS = {"Jan": [["Week", "Spend"], ["W1", 120], ["W2", 95], ["W3", 143]],
                 "Feb": [["Week", "Spend"], ["W1", 88], ["W2", 101], ["W3", 77]]}


def formula_cell(formula: str, value: int) -> str:
    return (f'<table:table-cell table:formula="{escape(formula)}" office:value-type="float" office:value="{value}">'
            f"<text:p>{value}</text:p></table:table-cell>")


def annotated(cell_xml: str, comment: str) -> str:
    note = (f"<office:annotation><dc:creator>Existing Reviewer</dc:creator><dc:date>2026-01-01T00:00:00</dc:date>"
            f"<text:p>{escape(comment)}</text:p></office:annotation>")
    return cell_xml.replace(">", ">" + note, 1)


def _cell(value) -> str:
    if value is None:
        return "<table:table-cell/>"
    return number_cell(value) if isinstance(value, int) else text_cell(str(value))


def sheet_xml(name: str, rows: list, width: int) -> str:
    """Rows of pre-rendered cell XML (strings starting with '<') or plain values."""
    body = "".join("<table:table-row>" + "".join(c if isinstance(c, str) and c.startswith("<") else _cell(c) for c in row)
                   + "</table:table-row>" for row in rows)
    return (f'<table:table table:name="{escape(name)}"><table:table-column table:number-columns-repeated="{width}"/>'
            f"{body}</table:table>")


def spreadsheet_xml(tables: list[str]) -> str:
    ns = NS.replace('office:version="1.3"', 'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:of='
                    '"urn:oasis:names:tc:opendocument:xmlns:of:1.2" office:version="1.3"')
    return (HEAD + f'<office:document-content {ns}><office:body><office:spreadsheet>{"".join(tables)}'
            '</office:spreadsheet></office:body></office:document-content>')


def sales_content_tool_coverage() -> str:
    q3 = [["Product", "Units", "Price", "Line total"]]
    for n, (product, units, price) in enumerate(SALES_Q3, start=2):
        q3.append([product, units, price, formula_cell(f"of:=[.B{n}]*[.C{n}]", units * price) if n == 2 else None])
    q4 = [["Region", "Units", "Price", "Line total"]]
    for n, (region, units, price) in enumerate(SALES_Q4, start=2):
        q4.append([region, units, price, formula_cell(f"of:=[.B{n}]*[.C{n}]", units * price)])
    q4.append(["Total", None, None, formula_cell("of:=SUM([.D2:.D6])", sum(u * p for _r, u, p in SALES_Q4))])
    return spreadsheet_xml([sheet_xml("Q3", q3, 4), sheet_xml("Q4", q4, 4)])


def ledger_content() -> str:
    rows = [["Item", "Vendor", "Amount", None, *LEDGER_SCRATCH[0]]]
    for n, record in enumerate(LEDGER_COSTS, start=2):
        rows.append(list(record) + [None] + (list(LEDGER_SCRATCH[n - 1]) if n <= 3 else [None, None]))
    for address, comment in LEDGER_COMMENTS.items():
        col, row = ord(address[0]) - 65, int(address[1:]) - 1
        rows[row][col] = annotated(_cell(rows[row][col]), comment)
    tables = [sheet_xml("Costs", rows, 6)] + [sheet_xml(name, data, 2) for name, data in LEDGER_MONTHS.items()]
    tables.append('<table:table table:name="Old"><table:table-column/><table:table-row><table:table-cell/>'
                  "</table:table-row></table:table>")
    return spreadsheet_xml(tables)


SCENARIO_DOCUMENTS = {
    "budget.ods": ("application/vnd.oasis.opendocument.spreadsheet", lambda: spreadsheet(
        [("Inputs", [["Category", "Item", "Amount"]] + [list(row) for row in budget_rows()])]), None),
    "messy.ods": ("application/vnd.oasis.opendocument.spreadsheet", lambda: spreadsheet(
        [("Raw", [["Name", "Amount", "Date"]] + [list(row) for row in messy_rows()])]), None),
    "witness.ods": ("application/vnd.oasis.opendocument.spreadsheet", lambda: spreadsheet(
        [("Sheet1", [["Witness", "Do not change"], ["Value", 7]])]), None),
    "plan.ods": ("application/vnd.oasis.opendocument.spreadsheet", lambda: spreadsheet(
        [("Plan", [["Item", "Actual", "Budget"]] + [list(row) for row in PLAN_ROWS])]), None),
    "long.odt": ("application/vnd.oasis.opendocument.text", lambda: text_document(long_blocks()), None),
    "typos.odt": ("application/vnd.oasis.opendocument.text",
                  lambda: text_document([para(text) for text in typo_paragraphs()]), None),
    "outline.odt": ("application/vnd.oasis.opendocument.text", lambda: text_document(
        [block for title, body in OUTLINE for block in (heading(title), para(body))]), None),
    "empty.odt": ("application/vnd.oasis.opendocument.text", lambda: text_document([para("")]), None),
    "tables-only.odt": ("application/vnd.oasis.opendocument.text", lambda: text_document(
        [table_xml("OnlyTable", [["A1 text", "B1 text"], ["A2 ✓", "B2"]])]), None),
    "review.odt": ("application/vnd.oasis.opendocument.text", review_content, None),
    "witness.odt": ("application/vnd.oasis.opendocument.text",
                    lambda: text_document([para("Witness document. Do not change.")]), None),
    "deck.odp": ("application/vnd.oasis.opendocument.presentation", lambda: deck(DECK_SLIDES, bold_word="buyers"), deck_styles),
    "witness.odp": ("application/vnd.oasis.opendocument.presentation",
                    lambda: deck([("Witness deck", "Do not change")]), deck_styles),
    "tracker.ods": ("application/vnd.oasis.opendocument.spreadsheet", lambda: spreadsheet(
        [("Tasks", [["Task", "Owner", "Status", "Percent"]] + [list(row) for row in TRACKER_ROWS])]), None),
    "sales.ods": ("application/vnd.oasis.opendocument.spreadsheet", sales_content_tool_coverage, None),
    "ledger.ods": ("application/vnd.oasis.opendocument.spreadsheet", ledger_content, None),
}


def build_scenarios(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (media_type, content, styles) in SCENARIO_DOCUMENTS.items():
        path = out_dir / name
        write_document(path, media_type, content, styles)
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the demo documents in docs/fixtures/.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scenarios", action="store_true",
                        help="build the workflow-acceptance fixtures into --out-dir instead (never docs/fixtures)")
    options = parser.parse_args()
    if options.scenarios:
        if options.out_dir.resolve() == DEFAULT_OUT.resolve():
            parser.error("--scenarios needs an --out-dir outside docs/fixtures")
        paths = build_scenarios(options.out_dir)
    else:
        paths = build(options.out_dir)
    for path in paths:
        print(f"built {path}")


if __name__ == "__main__":
    main()
