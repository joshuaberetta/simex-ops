from ops import richtext


def test_clean_keeps_formatting_and_drops_the_rest():
    out = richtext.clean('<p onclick="x()">Hi <b>bold</b> <i>it</i> <u>u</u> <s>s</s></p>'
                         '<script>alert(1)</script><style>p{}</style><iframe src="x"></iframe>'
                         '<ul><li>one</li><li>two</li></ul><unknown>kept text</unknown>')
    assert out == ("<p>Hi <b>bold</b> <i>it</i> <u>u</u> <s>s</s></p><ul><li>one</li><li>two</li></ul>kept text")


def test_clean_links_styles_and_images():
    out = richtext.clean('<a href="javascript:alert(1)">x</a><a href=" JaVa\tscript:1">y</a>'
                         '<a href="https://example.org/a?b=1&amp;c=2">ok</a>'
                         '<span style="color: red; background-image: url(x); font-weight: bold; position: fixed">c</span>'
                         '<img src="https://evil/x.png"><img src="data:image/png;base64,AAAA">'
                         '<img src="cid:img-1" alt="map" width="300" onerror="x">'
                         '<img src="/inbox/file/3/?inline=1" data-cid="img-2">')
    assert '<a target="_blank" rel="noopener noreferrer">x</a>' in out
    assert 'href="https://example.org/a?b=1&amp;c=2"' in out
    assert 'style="color: red; font-weight: bold"' in out
    assert "evil" not in out and "data:" not in out and "onerror" not in out
    assert '<img src="cid:img-1" alt="map" width="300">' in out
    assert '<img src="cid:img-2">' in out
    assert richtext.cids(out) == ["img-1", "img-2"]


def test_attribute_values_are_escaped():
    out = richtext.clean('<img src="cid:a" alt="&quot; onload=&quot;x"><p title="x">"<b>&lt;i&gt;</b></p>')
    assert 'alt="&quot; onload=&quot;x"' in out
    assert "<p>&quot;" not in out and '<p>"<b>&lt;i&gt;</b></p>' in out


def test_unclosed_and_stray_tags_are_balanced():
    assert richtext.clean("<b>a<i>b</b>c</i></p>") == "<b>a<i>b</i></b>c"
    assert richtext.clean("<script>x<script>y</script>z</script>after") == "zafter"  # as a browser parses it: z is text, never script


def test_render_points_images_at_files():
    class F:
        def __init__(self, id, cid):
            self.id, self.cid = id, cid
    out = richtext.render('<p>x<img src="cid:a"><img src="cid:gone"></p>', [F(7, "a")], lambda f: f"/inbox/file/{f.id}/")
    assert out == '<p>x<img src="/inbox/file/7/" data-cid="a"></p>'


def test_rich_or_plain_and_text():
    assert not richtext.is_rich("<div>Hello</div><div><br></div><p>there</p>")
    assert richtext.is_rich("<p>Hello <b>there</b></p>")
    assert richtext.is_rich('<span style="color: red">x</span>')
    text = richtext.to_text('<p>Dear team,</p><p>See <b>this</b>:<br>line two</p><ul><li>one</li><li>two</li></ul>'
                            '<ol><li>first</li></ol><img src="cid:a" alt="map"><a href="https://x.org">site</a>')
    assert text == "Dear team,\n\nSee this:\nline two\n\n- one\n- two\n1. first\n[image: map]site (https://x.org)"
    assert richtext.to_text("<div>a</div><div>b</div>") == "a\nb"
