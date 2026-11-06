from app.markdown_lite import render


def test_raw_html_is_escaped_in_plain_text():
    out = str(render("<script>alert(1)</script> and <img src=x onerror=alert(1)>"))
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "&lt;img" in out


def test_raw_html_escaped_in_headings_lists_tables():
    md = "# <script>x</script>\n\n- <img onerror=a>\n\n| <b>h</b> |\n|---|\n| <i>c</i> |\n"
    out = str(render(md))
    assert "<script" not in out and "<img" not in out and "<b>" not in out and "<i>" not in out
    assert "<h1>" in out and "<ul>" in out and "<table>" in out


def test_raw_html_escaped_inside_inline_marks():
    out = str(render("`<script>` **<img onerror=a>** *<svg onload=a>*"))
    assert "<script" not in out and "<img" not in out and "<svg" not in out
    assert "<code>" in out and "<strong>" in out and "<em>" in out


def test_fenced_code_is_escaped():
    out = str(render("```\n<script>alert(1)</script>\n```"))
    assert "<pre><code>" in out and "<script" not in out and "&lt;script&gt;" in out


def test_structures():
    out = str(render("## Title\n\n1. one\n2. two\n\n- a\n- b\n\ntext"))
    assert "<h2>Title</h2>" in out and "<ol>" in out and "<ul>" in out and "<p>text</p>" in out


def test_empty():
    assert str(render("")) == ""
