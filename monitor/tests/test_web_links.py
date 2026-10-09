from intothedarkness.notify.web import open_links_in_new_tab


def test_external_links_open_in_new_tab_internal_ones_do_not():
    html = (
        '<a href="https://x.com/FalconFeedsio/status/1" style="color:#b91c1c">read on X</a>'
        '<a href="http://news.example/a">article</a>'
        '<a href="#diagnostics">issues</a>'
        '<a href="reports/2026-10-09.html">archive</a>'
        '<a href="hxxp://abc[.]onion">leak</a>'
    )
    out = open_links_in_new_tab(html)
    assert out.count('target="_blank" rel="noopener noreferrer"') == 2   # the two http(s) links
    assert 'target="_blank" rel="noopener noreferrer" href="https://x.com' in out
    assert 'target="_blank" rel="noopener noreferrer" href="http://news.example' in out
    assert '<a href="#diagnostics">' in out            # in-page anchor untouched
    assert '<a href="reports/2026-10-09.html">' in out  # relative archive link untouched
    assert '<a href="hxxp://abc[.]onion">' in out       # de-fanged onion untouched
