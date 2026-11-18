"""Convert model HTML tables to Markdown for previews and export."""
import re

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

_TABLE = re.compile(r"<table(?=\s|>)[^>]*>.*?</table\s*>", re.IGNORECASE | re.DOTALL)
_MARKDOWN_PUNCTUATION = str.maketrans({char: f"&#{ord(char)};" for char in "\\|`*_[]<>&~"})
_MAX_COLUMNS = 64
_MAX_CELLS = 20_000


def render_preview(pages):
    return "\n\n---\n\n".join(
        f"### Page {page['page_number']}\n\n{render_tables(page['markdown'])}" for page in pages
    )


def render_tables(markdown):
    if "<table" not in markdown.lower():
        return markdown
    lines = markdown.splitlines(keepends=True)
    # Parse the block boundaries so a table shown in a code example stays literal.
    for token in reversed(MarkdownIt("commonmark").parse(markdown)):
        if token.type == "html_block":
            start, end = token.map
            lines[start:end] = [_TABLE.sub(html_table_to_markdown, "".join(lines[start:end]))]
    return "".join(lines)


def span(cell, attribute):
    try:
        return max(1, int(cell.get(attribute, "1")))
    except (ValueError, TypeError):
        return 1


def html_table_to_markdown(match):
    table = BeautifulSoup(match[0], "html.parser").find("table")
    rows = [row for row in table.find_all("tr") if row.find_parent("table") is table]
    fallback = "\n\n*Open JSON to inspect the original table.*\n\n"
    grid = {}
    width = 0
    header_rows = 0
    for row_index, row in enumerate(rows):
        cells = row.find_all(["th", "td"], recursive=False)
        if row_index == header_rows and (
            row.parent.name == "thead" or (cells and all(cell.name == "th" for cell in cells))
        ):
            header_rows += 1
        column = 0
        for cell in cells:
            across = span(cell, "colspan")
            down = min(span(cell, "rowspan"), len(rows) - row_index)
            while column < _MAX_COLUMNS and any(
                (row_index, existing) in grid for existing in range(column, min(column + across, _MAX_COLUMNS))
            ):
                column += 1
            end = column + across
            if end > _MAX_COLUMNS or len(grid) + down * across > _MAX_CELLS:
                return fallback
            text = " ".join(cell.get_text(" ", strip=True).split()).translate(_MARKDOWN_PUNCTUATION)
            for next_row in range(row_index, row_index + down):
                for next_column in range(column, end):
                    grid[next_row, next_column] = text
            width = max(width, end)
            column = end

    if not width or len(rows) * width > _MAX_CELLS:
        return fallback

    def format_row(values):
        return "| " + " | ".join(values) + " |"

    headers = []
    for column in range(width):
        labels = (grid.get((row, column), "") for row in range(header_rows))
        headers.append(" / ".join(dict.fromkeys(label for label in labels if label)))
    output = [format_row(headers), format_row(["---"] * width)]
    for row in range(header_rows, len(rows)):
        output.append(format_row(grid.get((row, column), "") for column in range(width)))
    caption = table.find("caption", recursive=False)
    if caption:
        label = " ".join(caption.get_text(" ", strip=True).split()).translate(_MARKDOWN_PUNCTUATION)
        output = [label, "", *output]
    return "\n\n" + "\n".join(output) + "\n\n"
