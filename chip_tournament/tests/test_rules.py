from chips.rules import render


def test_render_structure():
    html = render("# Fouls\n## Ball in hand\n- one\n- two\n\n1. first\n2) second\nPlain **bold** line\n1.1 Numbered rule text")
    assert '<h3 class="rule-block">Fouls</h3>' in html
    assert '<h4 class="rule-block">Ball in hand</h4>' in html
    assert html.count("<ul>") == 1 and html.count('<li class="rule-block">') == 4 and html.count("<ol>") == 1
    assert '<p class="rule-block">Plain <b>bold</b> line</p>' in html
    assert '<p class="rule-block">1.1 Numbered rule text</p>' in html  # section numbers stay as written


def test_render_escapes_html():
    assert "<img" not in render('<img src=x onerror="alert(1)">')
