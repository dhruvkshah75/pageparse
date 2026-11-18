from markdown_it import MarkdownIt

from pageparse.preview import render_tables


def test_table_preview_keeps_prose_and_literal_examples():
    table = (
        "<table><caption>Timing</caption><tr><th>Case</th><th>Result</th></tr>"
        '<tr><td rowspan="2">Reads | writes</td><td>Fast &amp; steady</td></tr>'
        "<tr><td>*literal*<br>second line</td></tr></table>"
    )
    fenced = f"```html\n{table}\n```"
    preview = render_tables(f"### Results\n\n{table}\nA note after the table.\n\n{fenced}")
    tokens = MarkdownIt("gfm-like").parse(preview)
    assert sum(token.type == "table_open" for token in tokens) == 1
    assert fenced in preview
    assert "A note after the table." in preview
    assert "Timing" in preview
    texts = [child.content for token in tokens if token.type == "inline" for child in token.children]
    assert texts.count("Reads | writes") == 2
    assert "Fast & steady" in texts
    assert "*literal* second line" in texts
