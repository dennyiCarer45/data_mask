"""Reading order and structure for extracted text: columns, headings, tables, pages, lists.

Only text is produced. Markup added here ("## ", "| ", "--- page 2 ---", "Page footer: ") is not
printed in the document, so it has no position and the masked review copy never covers it.
Every printed character keeps its page box, so the review copy covers exactly what was masked.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

# A side of a vertical gap is read as a text column only when it looks like running text: at
# least this many lines, this many characters per line on average. Label columns, invoice
# headers, and forms are shorter and keep the row-by-row order (label beside its value).
PROSE_LINES, PROSE_CHARS = 3, 20
# A horizontal gap across the whole region at least this many line heights tall splits it into
# bands that are read one after another (header block, then table, then body).
BAND_GAP = 1.0
# Top and bottom share of the page searched for running headers and footers.
MARGIN = 0.1
# Gaps between pieces of one row, in estimated character widths. A wide gap is written as at
# least three spaces, which ends a labeled value (see detection.CONTEXT capture).
NARROW_GAP, MIN_WIDE, MAX_WIDE = 1.5, 3, 12


class TextBuilder:
    """Accumulates text and, for every printed character, its (start, end, page, box)."""

    def __init__(self):
        self.parts, self.size, self.boxes = [], 0, []

    def add(self, text: str):
        self.parts.append(text)
        self.size += len(text)

    def add_chars(self, chars, page):
        for char, box in chars:
            if not char.isspace():
                self.boxes.append((self.size, self.size + len(char), page, box))
            self.add(char)

    def text(self) -> str:
        return "".join(self.parts)

    def ends_with(self, suffix: str) -> bool:
        return self.text()[-len(suffix) :] == suffix if self.size >= len(suffix) else False

    def newline(self):
        if self.size and not self.ends_with("\n"):
            self.add("\n")

    def blank_line(self):
        self.newline()
        if self.size and not self.ends_with("\n\n"):
            self.add("\n")


# --- PDF -------------------------------------------------------------------------------------


class Line:
    """Characters printed on one line, each with its (x0, y0, x1, y1) box."""

    def __init__(self, chars, size, bold):
        self.chars, self.size, self.bold = chars, size, bold
        self.bbox = union(box for char, box in chars if not char.isspace())
        self.text = "".join(char for char, _ in chars)


@dataclass(eq=False)
class Unit:
    """A text block or a table, placed on the page as one piece."""

    bbox: tuple
    lines: list = field(default_factory=list)
    rows: list | None = None  # table: rows of cells, each cell a list of Line fragments
    role: str = ""  # "Page header" or "Page footer" when repeated across pages
    heading: int = 0

    @property
    def text(self):
        return " ".join(line.text.strip() for line in self.lines)


def union(boxes):
    boxes = list(boxes)
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def inside(point, box):
    return box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]


def page_units(page, pymupdf) -> list[Unit]:
    """Text blocks and tables of one page, with every character's box."""
    flags = pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    blocks = []
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        if block.get("type") != 0:
            continue
        lines = []
        for line in block["lines"]:
            chars, sizes, bold = [], Counter(), True
            for span in line["spans"]:
                visible = [c for c in span["chars"] if not c["c"].isspace()]
                chars.extend((c["c"], tuple(c["bbox"])) for c in span["chars"])
                if visible:
                    sizes[round(span["size"], 1)] += len(visible)
                    bold = bold and bool(span["flags"] & 16)
            if sizes:
                lines.append(Line(chars, sizes.most_common(1)[0][0], bold))
        if lines:
            blocks.append(lines)
    tables = []
    for table in page.find_tables().tables:
        grid = [list(row.cells) for row in table.rows]
        # A frame around a paragraph is found as a one-column table, and a shaded row as a
        # one-row table; leave both as text.
        if len(grid) >= 2 and max(len(row) for row in grid) >= 2:
            tables.append((tuple(table.bbox), grid))
    units = []
    cells = {}  # (table, row, column) -> list of Line fragments
    for lines in blocks:
        rest = []
        for line in lines:
            loose = []
            for char, box in line.chars:
                center = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
                key = cell_of(center, tables)
                if key is None:
                    loose.append((char, box))
                else:
                    fragments = cells.setdefault(key, [])
                    if not fragments or fragments[-1][0] is not line:
                        fragments.append((line, []))
                    fragments[-1][1].append((char, box))
            if any(not c.isspace() for c, _ in loose):
                rest.append(Line(loose, line.size, line.bold))
        if rest:
            units.append(Unit(union(line.bbox for line in rest), rest))
    for index, (bbox, grid) in enumerate(tables):
        rows = [
            [
                [
                    Line(chars, line.size, line.bold)
                    for line, chars in cells.get((index, r, c), [])
                    if any(not ch.isspace() for ch, _ in chars)
                ]
                for c in range(len(row))
            ]
            for r, row in enumerate(grid)
        ]
        if any(cell for row in rows for cell in row):
            units.append(Unit(bbox, rows=rows))
    printed = sum(not c.isspace() for lines in blocks for line in lines for c, _ in line.chars)
    if printed != visible_chars(units):
        raise ValueError("layout_lost_text")
    return units


def visible_chars(units) -> int:
    """Non-space characters held by the units; writing a page must emit exactly this many."""
    return sum(
        not char.isspace()
        for unit in units
        for line in unit.lines or table_lines(unit)
        for char, _ in line.chars
    )


def cell_of(center, tables):
    for index, (bbox, grid) in enumerate(tables):
        if not inside(center, bbox):
            continue
        for r, row in enumerate(grid):
            for c, cell in enumerate(row):
                if cell is not None and inside(center, cell):
                    return index, r, c
    return None


def mark_page_furniture(pages, heights):
    """Label running headers and footers: margin text repeated on two or more pages."""
    if len(pages) < 2:
        return
    seen = Counter()
    candidates = []
    for units, height in zip(pages, heights, strict=True):
        keys = set()
        for unit in units:
            if unit.rows is not None:
                continue
            role = (
                "Page header"
                if unit.bbox[3] <= height * MARGIN
                else "Page footer"
                if unit.bbox[1] >= height * (1 - MARGIN)
                else ""
            )
            if role:
                # Page numbers differ from page to page; compare the rest.
                key = (role, re.sub(r"\d+", "#", unit.text.casefold()))
                candidates.append((unit, role, key))
                keys.add(key)
        seen.update(keys)
    for unit, role, key in candidates:
        if seen[key] >= 2:
            unit.role = role


def body_size(pages) -> float:
    sizes = Counter()
    for units in pages:
        for unit in units:
            for line in unit.lines or table_lines(unit):
                sizes[line.size] += len(line.text.strip())
    return sizes.most_common(1)[0][0] if sizes else 0.0


def mark_headings(units, body):
    """A short single-line block in a larger font, or entirely bold, is a heading."""
    for unit in units:
        # One line only: a label and its value printed on one row are two lines.
        if unit.rows is not None or unit.role or len(unit.lines) != 1:
            continue
        text = unit.text
        if not re.search(r"[^\W\d_]", text) or len(text) > 100 or re.search(r":(\s|$)", text):
            continue
        size = max(line.size for line in unit.lines)
        bold = all(line.bold for line in unit.lines)
        if body and size >= body * 1.5:
            unit.heading = 1
        elif body and size >= body * 1.15:
            unit.heading = 2
        elif bold and size >= body and len(text) <= 60 and not text.endswith("."):
            unit.heading = 3


def rows_of(lines):
    """Group lines printed on the same baseline, top to bottom, each row left to right."""
    rows = []
    for line in sorted(lines, key=lambda line: (line.bbox[1] + line.bbox[3]) / 2):
        x0, y0, x1, y1 = line.bbox
        if rows:
            top, bottom = rows[-1][0]
            overlap = min(bottom, y1) - max(top, y0)
            if overlap >= 0.5 * min(bottom - top, y1 - y0):
                rows[-1][0] = (min(top, y0), max(bottom, y1))
                rows[-1][1].append(line)
                continue
        rows.append([(y0, y1), [line]])
    return [sorted(row, key=lambda line: line.bbox[0]) for _, row in rows]


def arrange(units):
    """Reading order: bands separated by wide horizontal gaps, then text columns, then rows."""
    if len(units) <= 1:
        return [units]
    bands = split_bands(units)
    if len(bands) > 1:
        return [group for band in bands for group in arrange(band)]
    columns = split_columns(units)
    if columns:
        return [group for column in columns for group in arrange(column)]
    return [units]


def line_height(units):
    heights = sorted(
        line.bbox[3] - line.bbox[1] for unit in units for line in unit.lines or table_lines(unit)
    )
    return heights[len(heights) // 2] if heights else 10.0


def table_lines(unit):
    return [line for row in unit.rows or [] for cell in row for line in cell]


def split_bands(units):
    gap = BAND_GAP * line_height(units)
    bands, bottom = [], None
    for unit in sorted(units, key=lambda unit: unit.bbox[1]):
        if bands and unit.bbox[1] - bottom < gap:
            bands[-1].append(unit)
            bottom = max(bottom, unit.bbox[3])
        else:
            bands.append([unit])
            bottom = unit.bbox[3]
    return bands


def split_columns(units):
    """Two text columns separated by a vertical gap, or None. Wide units (titles, figures,
    tables across both columns) are placed in order between the column segments."""
    left_edge = min(unit.bbox[0] for unit in units)
    right_edge = max(unit.bbox[2] for unit in units)
    width = right_edge - left_edge
    narrow = sorted(
        (unit.bbox[0], unit.bbox[2]) for unit in units if unit.bbox[2] - unit.bbox[0] < 0.6 * width
    )
    best = None
    reach = None
    for x0, x1 in narrow:
        if reach is not None and x0 - reach >= 4:
            center = (reach + x0) / 2
            if left_edge + 0.2 * width <= center <= left_edge + 0.8 * width:
                if best is None or x0 - reach > best[1] - best[0]:
                    best = (reach, x0)
        reach = x1 if reach is None else max(reach, x1)
    if best is None:
        return None
    left = [unit for unit in units if unit.bbox[2] <= best[0] + 1]
    right = [unit for unit in units if unit.bbox[0] >= best[1] - 1]
    if not (prose(left) and prose(right)):
        return None
    across = sorted(
        (unit for unit in units if unit not in left and unit not in right),
        key=lambda unit: unit.bbox[1],
    )
    ordered, remaining_left, remaining_right = [], list(left), list(right)
    for wide in [*across, None]:
        limit = wide.bbox[1] if wide is not None else float("inf")
        above_left = [unit for unit in remaining_left if unit.bbox[1] < limit]
        above_right = [unit for unit in remaining_right if unit.bbox[1] < limit]
        ordered += [group for group in (above_left, above_right) if group]
        remaining_left = [unit for unit in remaining_left if unit not in above_left]
        remaining_right = [unit for unit in remaining_right if unit not in above_right]
        if wide is not None:
            ordered.append([wide])
    return ordered


def prose(units):
    lines = [line.text.strip() for unit in units for line in unit.lines]
    return len(lines) >= PROSE_LINES and sum(map(len, lines)) / len(lines) >= PROSE_CHARS


def write_gap(builder, gap, size):
    width = max(size * 0.5, 1.0)
    if gap <= NARROW_GAP * width:
        builder.add(" ")
    else:
        builder.add(" " * max(MIN_WIDE, min(MAX_WIDE, round(gap / width))))


def write_row(builder, row, page):
    for position, line in enumerate(row):
        if position:
            write_gap(builder, line.bbox[0] - row[position - 1].bbox[2], line.size)
        chars = line.chars
        # Leading/trailing spaces of a line say nothing once lines are joined.
        while chars and chars[0][0].isspace():
            chars = chars[1:]
        while chars and chars[-1][0].isspace():
            chars = chars[:-1]
        builder.add_chars(chars, page)


def write_rows(builder, lines, page, separator="\n"):
    for index, row in enumerate(rows_of(lines)):
        if index:
            builder.add(separator)
        write_row(builder, row, page)


def write_table(builder, unit, page):
    builder.blank_line()
    width = max(len(row) for row in unit.rows)
    for index, row in enumerate(unit.rows):
        builder.add("|")
        for column in range(width):
            builder.add(" ")
            if column < len(row):
                write_rows(builder, row[column], page, separator=" ")
            builder.add(" |")
        builder.add("\n")
        if index == 0:
            builder.add("|" + " --- |" * width + "\n")
    builder.add("\n")


def write_page(builder, units, page, body):
    mark_headings(units, body)
    for group in arrange(units):
        # Top to bottom: a table, or a run of text blocks written row by row between tables.
        pieces = []
        for unit in sorted(group, key=lambda unit: unit.bbox[1]):
            if unit.rows is not None:
                pieces.append(unit)
            elif pieces and isinstance(pieces[-1], list):
                pieces[-1].append(unit)
            else:
                pieces.append([unit])
        for piece in pieces:
            if isinstance(piece, Unit):
                write_table(builder, piece, page)
                continue
            owner = {id(line): unit for unit in piece for line in unit.lines}
            for row in rows_of([line for unit in piece for line in unit.lines]):
                units_in_row = {id(owner[id(line)]): owner[id(line)] for line in row}.values()
                builder.newline()
                only = next(iter(units_in_row)) if len(units_in_row) == 1 else None
                if only is not None and only.role:
                    builder.add(only.role + ": ")
                elif only is not None and only.heading:
                    builder.add("#" * only.heading + " ")
                write_row(builder, row, page)
                builder.add("\n")


# --- DOCX ------------------------------------------------------------------------------------

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
WRAPPERS = {W + "sdt", W + "sdtContent", W + "customXml", W + "smartTag"}
RUN_MARKS = {W + "tab": "\t", W + "br": "\n", W + "cr": "\n", W + "noBreakHyphen": "-"}
NOTE_TYPES = {"separator", "continuationSeparator", "continuationNotice"}


def attribute(element, tag):
    child = element.find(W + tag) if element is not None else None
    return None if child is None else child.get(W + "val")


def paragraph_styles(root):
    """styleId -> (heading level, numbering) from style names ("heading 2", "Title"), outline
    levels, and the list numbering a style applies ("List Number")."""
    styles = {}
    if root is None:
        return styles
    for style in root.findall(W + "style"):
        name = (attribute(style, "name") or "").casefold()
        match = re.fullmatch(r"heading ([1-9])", name)
        level = int(match[1]) if match else 1 if name == "title" else 0
        properties = style.find(W + "pPr")
        outline = attribute(properties, "outlineLvl")
        if not level and outline is not None and outline.isdigit() and int(outline) < 9:
            level = int(outline) + 1
        numbering = properties.find(W + "numPr") if properties is not None else None
        if level or numbering is not None:
            styles[style.get(W + "styleId")] = (min(level, 6), numbering)
    return styles


def numbering_formats(root):
    """numId -> {level: (format, level text, start)} from numbering.xml."""
    if root is None:
        return {}
    abstract = {}
    for definition in root.findall(W + "abstractNum"):
        levels = {}
        for level in definition.findall(W + "lvl"):
            ilvl = int(level.get(W + "ilvl", "0"))
            text = attribute(level, "lvlText")
            levels[ilvl] = (
                attribute(level, "numFmt") or "decimal",
                f"%{ilvl + 1}." if text is None else text,
                int(attribute(level, "start") or 1),
            )
        abstract[definition.get(W + "abstractNumId")] = levels
    return {
        number.get(W + "numId"): abstract.get(attribute(number, "abstractNumId"), {})
        for number in root.findall(W + "num")
    }


def roman(number):
    values = [
        (1000, "m"),
        (900, "cm"),
        (500, "d"),
        (400, "cd"),
        (100, "c"),
        (90, "xc"),
        (50, "l"),
        (40, "xl"),
        (10, "x"),
        (9, "ix"),
        (5, "v"),
        (4, "iv"),
        (1, "i"),
    ]
    out = ""
    for value, letters in values:
        while number >= value:
            out, number = out + letters, number - value
    return out


def letters(number):
    out = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(97 + rest) + out
    return out


def format_number(number, kind):
    if kind == "lowerLetter":
        return letters(number)
    if kind == "upperLetter":
        return letters(number).upper()
    if kind == "lowerRoman":
        return roman(number)
    if kind == "upperRoman":
        return roman(number).upper()
    if kind == "decimalZero":
        return f"{number:02d}"
    return str(number)


class DocxWriter:
    """Word XML to structured text: headings, list numbers, tables as "| a | b |" rows."""

    def __init__(self, styles, numbering):
        self.styles, self.numbering = styles, numbering
        self.counters = {}
        self.visited = set()

    def paragraph(self, p) -> str:
        self.visited.add(p)
        pieces = []
        for node in p.iter():
            if node.tag == W + "t":
                pieces.append(node.text or "")
            elif node.tag in RUN_MARKS and node.getparent().tag == W + "r":
                pieces.append(RUN_MARKS[node.tag])
            elif node.tag == W + "footnoteReference":
                pieces.append(f"[^{node.get(W + 'id')}]")
            elif node.tag == W + "endnoteReference":
                pieces.append(f"[^e{node.get(W + 'id')}]")
        text = "".join(pieces)
        if not text.strip():
            return text
        properties = p.find(W + "pPr")
        prefix = ""
        level, style_numbering = self.styles.get(attribute(properties, "pStyle"), (0, None))
        numbering = properties.find(W + "numPr") if properties is not None else None
        if numbering is not None or style_numbering is not None:
            prefix = self.list_marker(numbering, style_numbering)
        outline = attribute(properties, "outlineLvl")
        if not level and outline is not None and outline.isdigit() and int(outline) < 9:
            level = int(outline) + 1
        if level:
            prefix = "#" * min(level, 6) + " " + prefix
        return prefix + text

    def list_marker(self, numbering, style_numbering=None) -> str:
        # The paragraph's own numbering overrides its style's, field by field.
        number_id = attribute(numbering, "numId") or attribute(style_numbering, "numId")
        ilvl = int(attribute(numbering, "ilvl") or attribute(style_numbering, "ilvl") or 0)
        levels = self.numbering.get(number_id)
        if not levels or number_id == "0":
            return ""
        kind, text, start = levels.get(ilvl, ("decimal", f"%{ilvl + 1}.", 1))
        counters = self.counters.setdefault(number_id, {})
        counters[ilvl] = counters.get(ilvl, start - 1) + 1
        for deeper in [level for level in counters if level > ilvl]:
            del counters[deeper]
        if kind == "bullet":
            marker = "-"
        elif kind == "none":
            marker = ""
        else:

            def value(match):
                index = int(match[1]) - 1
                fmt, _, first = levels.get(index, ("decimal", "", 1))
                return format_number(counters.get(index, first), fmt)

            marker = re.sub(r"%([1-9])", value, text)
        return "  " * ilvl + (marker + " " if marker else "")

    def blocks(self, element, out: list):
        for child in element:
            if child.tag == W + "p":
                out.append(self.paragraph(child))
            elif child.tag == W + "tbl":
                out.extend(["", *self.table(child), ""])
            else:
                self.blocks(child, out)

    def table(self, table) -> list[str]:
        rows = []
        for row in children(table, W + "tr"):
            cells = []
            for cell in children(row, W + "tc"):
                # Nested tables are flattened into their cell's text.
                text = " ".join(
                    piece.replace("\n", " ").strip()
                    for piece in (self.paragraph(p) for p in cell.iter(W + "p"))
                    if piece.strip()
                )
                span = attribute(cell.find(W + "tcPr"), "gridSpan")
                cells.append(text)
                # A cell merged across columns keeps the later columns aligned.
                cells.extend([""] * (int(span) - 1 if span and span.isdigit() else 0))
            rows.append(cells)
        return markdown_table(rows)

    def check_complete(self, root):
        """Every paragraph of a part must have been written; otherwise text could be lost."""
        if any(p not in self.visited for p in root.iter(W + "p")):
            raise ValueError("unvisited_paragraph")


def children(element, tag):
    for child in element:
        if child.tag == tag:
            yield child
        elif child.tag in WRAPPERS:
            yield from children(child, tag)


def markdown_table(rows) -> list[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows) or 1
    lines = []
    for index, row in enumerate(rows):
        row = row + [""] * (width - len(row))
        lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
        if index == 0:
            lines.append("|" + " --- |" * width)
    return lines


def docx_text(parts: dict) -> str:
    """parts: package name -> parsed XML root, for the parts that carry text."""
    writer = DocxWriter(
        paragraph_styles(parts.get("word/styles.xml")),
        numbering_formats(parts.get("word/numbering.xml")),
    )
    names = sorted(parts)
    out = []

    def furniture(prefix, kind):
        for name in names:
            if name.rsplit("/", 1)[-1].startswith(kind):
                lines = []
                writer.blocks(parts[name], lines)
                out.extend(f"{prefix}: {line}" for line in lines if line.strip())
                writer.check_complete(parts[name])

    def notes(name, tag, marker, title):
        root = parts.get(name)
        if root is None:
            return
        entries = []
        for note in root.findall(W + tag):
            if note.get(W + "type") in NOTE_TYPES:
                writer.visited.update(note.iter(W + "p"))
                continue
            text = " ".join(
                piece.strip()
                for piece in map(writer.paragraph, note.iter(W + "p"))
                if piece.strip()
            )
            if text:
                entries.append(marker(note.get(W + "id")) + text)
        writer.check_complete(root)
        if entries:
            out.extend(["", f"--- {title} ---", *entries])

    furniture("Page header", "header")
    for name in ("word/document.xml", "word/glossary/document.xml"):
        if name in parts:
            if name != "word/document.xml":
                out.extend(["", "--- glossary ---"])
            writer.blocks(parts[name], out)
            writer.check_complete(parts[name])
    furniture("Page footer", "footer")
    notes("word/footnotes.xml", "footnote", lambda n: f"[^{n}]: ", "footnotes")
    notes("word/endnotes.xml", "endnote", lambda n: f"[^e{n}]: ", "endnotes")
    notes("word/comments.xml", "comment", lambda _: "Comment: ", "comments")
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
