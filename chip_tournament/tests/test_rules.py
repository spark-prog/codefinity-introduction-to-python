from chips.rules import render


def test_render_structure():
    html = render("# Fouls\n## Ball in hand\n- one\n- two\n\n1. first\n2) second\nPlain **bold** line\n1.1 Numbered rule text")
    assert '<h3 class="rule-block rule-head" id="rules--fouls" data-section="Fouls">Fouls<a class="rule-link" href="#rules--fouls"' in html
    assert '<h4 class="rule-block rule-head" id="rules--ball-in-hand" data-section="Fouls › Ball in hand">Ball in hand<a' in html
    assert html.count("<ul>") == 1 and html.count('<li class="rule-block"') == 4 and html.count("<ol>") == 1
    assert '<p class="rule-block" data-section="Fouls › Ball in hand">Plain <b>bold</b> line</p>' in html
    assert '<p class="rule-block" data-section="Fouls › Ball in hand">1.1 Numbered rule text</p>' in html  # numbers stay as written


def test_render_escapes_html():
    assert "<img" not in render('<img src=x onerror="alert(1)">')
    html = render('# Fouls "<b>" & more')
    assert "<b>" not in html and 'data-section="Fouls &quot;&lt;b&gt;&quot; &amp; more"' in html


def test_section_links_are_unique_and_scoped():
    html = render("# Fouls\ntext\n# Fouls\n# **Bold** heading\nunder it", prefix="9-ball-rules")
    assert 'id="9-ball-rules--fouls"' in html and 'id="9-ball-rules--fouls-2"' in html
    assert 'id="9-ball-rules--bold-heading"' in html and 'data-section="Bold heading">under it' in html
    assert '<p class="rule-block" data-section="Fouls">text</p>' in html


def test_new_heading_resets_subheading():
    html = render("# A\n## B\n# C\nline")
    assert 'data-section="C">line' in html
