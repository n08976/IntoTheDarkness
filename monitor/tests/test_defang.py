from intothedarkness.notify.defang import defang, needs_defang, split_recipients


def test_defang_breaks_scheme_and_tld_and_nothing_else():
    src = "leak: http://rhysidafc6lm7.onion/archive.php website: https://www.fitzgibbon.org/"
    out = defang(src)
    assert out == "leak: hxxp://rhysidafc6lm7[.]onion/archive.php website: hxxp://www.fitzgibbon[.]org/"
    assert "http://" not in out and "https://" not in out


def test_defang_leaves_prose_alone():
    assert defang("Fitzgibbon Hospital USA, published 2026-09-12") == (
        "Fitzgibbon Hospital USA, published 2026-09-12"
    )


def test_recipient_rules_match_addresses_and_bare_domains():
    rules = ["hospital.example", "exact@example.com"]
    assert needs_defang("someone@hospital.example", rules)
    assert needs_defang("EXACT@example.com", rules)
    assert not needs_defang("reader@example.net", rules)
    assert needs_defang("anyone@anywhere.org", ["*"])


def test_split_keeps_order_and_sends_nobody_twice():
    people = ["a@home.example", "b@hospital.example", "c@x.org"]
    raw, fanged = split_recipients(people, ["hospital.example"])
    assert (raw, fanged) == (["a@home.example", "c@x.org"], ["b@hospital.example"])
