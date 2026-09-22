from intothedarkness.notify.defang import defang


def test_defang_breaks_scheme_and_tld_and_nothing_else():
    src = "leak: http://rhysidafc6lm7.onion/archive.php website: https://www.fitzgibbon.org/"
    out = defang(src)
    assert out == "leak: hxxp://rhysidafc6lm7[.]onion/archive.php website: hxxp://www.fitzgibbon[.]org/"
    assert "http://" not in out and "https://" not in out


def test_defang_leaves_prose_alone():
    assert defang("Fitzgibbon Hospital USA, published 2026-09-12") == (
        "Fitzgibbon Hospital USA, published 2026-09-12"
    )
