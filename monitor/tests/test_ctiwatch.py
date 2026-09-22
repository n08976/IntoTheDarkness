"""CTIWatch's Telegram preview, parsed from posts copied verbatim."""

from intothedarkness.scrapers.ctiwatch import _flag_to_iso, parse_preview


def wrap(post_id: str, when: str, lines: list[str], links: list[str]) -> str:
    body = "\n".join(f"<div>{ln}</div>" for ln in lines)
    anchors = "".join(f'<a href="{h}">x</a>' for h in links)
    return (f'<div class="tgme_widget_message_wrap"><div class="tgme_widget_message" data-post="{post_id}">'
            f'<div class="tgme_widget_message_text">{body}{anchors}</div><time datetime="{when}"></time></div></div>')


CARD = ["🚨", "RANSOMWARE ATTACK CONFIRMED", "🏢", "Columbus Informatica", "🇮🇹", "IT  ·", "💻", "Technology",
        "🌐", "www.columbusinformatica.it", "🦠", "Threat Group:", "Qilin", "🎯", "Motivation:", "financial",
        "📋", "N/A", "💀", "Victim listed on group's dark web leak site", "📅", "Date:", "Sep 22, 2026",
        "🔗", "Full Intel → CTIWATCH.COM", "#Ransomware"]
CARD_LINKS = ["https://ctiwatch.com/threats/Qilin",
              "https://ctiwatch.com/victims/09e95d8b-bc14-4fe7-8d5d-7f1b0c9f02fb", "?q=%23Ransomware"]

BATCH = ["🚨", "RANSOMWARE — 4 new victims listed", "🦠", "AuditTeam", "(2)", "•", "vit.ac.in", "🇮🇳", "Education",
         "•", "Pr***IT", "🇮🇹", "Professional Services", "🦠", "Akira", "(1)", "•", "DI.C.S.EL. S.R.L.", "🇮🇹",
         "Manufacturing", "🦠", "Qilin", "(1)", "•", "Textile City", "🇨🇦", "Manufacturing", "🔗",
         "All Victims → CTIWATCH.COM", "#Ransomware"]
BATCH_LINKS = ["https://ctiwatch.com/threats/AuditTeam",
               "https://ctiwatch.com/victims/67bdc613-532e-4c9f-b8e5-b801a2772a13",
               "https://ctiwatch.com/victims/1ffa5a4f-2407-4c42-be56-6af29eed3c3f",
               "https://ctiwatch.com/threats/win.akira",
               "https://ctiwatch.com/victims/aaaaaaaa-0000-0000-0000-000000000003",
               "https://ctiwatch.com/victims/aaaaaaaa-0000-0000-0000-000000000004"]


def test_flag_emoji_becomes_a_country_code():
    assert _flag_to_iso("🇺🇸") == "US" and _flag_to_iso("🇮🇹") == "IT" and _flag_to_iso("💻") == ""


def test_confirmed_attack_card_yields_one_fully_described_victim():
    items = parse_preview(wrap("ctiwatch/12578", "2026-09-22T11:21:00+00:00", CARD, CARD_LINKS), "cw")
    assert len(items) == 1
    it = items[0]
    assert it.title == "Columbus Informatica"
    assert it.key == "09e95d8b-bc14-4fe7-8d5d-7f1b0c9f02fb"          # CTIWatch's own id
    assert it.url.endswith("/victims/09e95d8b-bc14-4fe7-8d5d-7f1b0c9f02fb")
    assert it.fields["group"] == "Qilin"
    assert it.fields["sector"] == "Technology"
    assert it.fields["country"] == "IT"
    assert it.fields["website"] == "www.columbusinformatica.it"
    assert it.fields["published"] == "2026-09-22T11:21:00+00:00"


def test_batch_post_yields_every_victim_with_its_group_and_sector():
    items = parse_preview(wrap("ctiwatch/12580", "2026-09-22T13:21:00+00:00", BATCH, BATCH_LINKS), "cw")
    got = {i.title: (i.fields.get("group"), i.fields.get("country"), i.fields.get("sector")) for i in items}
    assert got == {
        "vit.ac.in": ("AuditTeam", "IN", "Education"),
        "Pr***IT": ("AuditTeam", "IT", "Professional Services"),
        "DI.C.S.EL. S.R.L.": ("Akira", "IT", "Manufacturing"),
        "Textile City": ("Qilin", "CA", "Manufacturing"),
    }
    assert [i.key for i in items][:2] == ["67bdc613-532e-4c9f-b8e5-b801a2772a13",
                                          "1ffa5a4f-2407-4c42-be56-6af29eed3c3f"]


def test_a_victim_in_both_shapes_is_one_item_and_the_card_wins():
    batch = ["🚨", "RANSOMWARE — 1 new victims listed", "🦠", "Qilin", "(1)", "•", "Columbus Informatica", "🇮🇹",
             "Technology", "🔗", "All Victims → CTIWATCH.COM"]
    html = wrap("ctiwatch/1", "2026-09-22T10:00:00+00:00", batch, CARD_LINKS[1:2]) + \
           wrap("ctiwatch/2", "2026-09-22T11:21:00+00:00", CARD, CARD_LINKS)
    items = parse_preview(html, "cw")
    assert len(items) == 1 and items[0].fields["website"] == "www.columbusinformatica.it"


def test_non_victim_posts_are_ignored():
    cve = ["🚨", "CTI ALERT —", "🔴", "CRITICAL", "Active exploitation detected: CVE-2026-27960"]
    assert parse_preview(wrap("ctiwatch/3", "2026-09-22T04:11:06+00:00", cve, []), "cw") == []


def test_batch_sector_survives_a_truncation_marker():
    # A long batch post ends "… +1 more"; the sector before it must still be read.
    lines = ["🚨", "RANSOMWARE — 5 new victims listed", "🦠", "Metaencryptor", "(1)", "•",
             "HyVision System. Inc", "🇰🇷", "Technology", "… +1 more", "🔗", "All Victims → CTIWATCH.COM"]
    items = parse_preview(wrap("ctiwatch/12552", "2026-09-21T13:06:00+00:00", lines,
                               ["https://ctiwatch.com/victims/aaaaaaaa-0000-0000-0000-000000000009"]), "cw")
    assert items[0].fields["sector"] == "Technology" and items[0].fields["country"] == "KR"


def test_leak_reposts_keep_the_victims_real_name():
    card = ["🚨", "RANSOMWARE ATTACK CONFIRMED", "🏢", "Leak: NAI Earle Furman", "🦠", "Threat Group:", "Secp0",
            "🎯", "Motivation:", "financial", "🔗", "Full Intel → CTIWATCH.COM"]
    items = parse_preview(wrap("ctiwatch/12569", "2026-09-22T03:11:00+00:00", card,
                               ["https://ctiwatch.com/victims/a240a66f-3055-43ec-b2a8-c4f8619561da"]), "cw")
    assert items[0].title == "NAI Earle Furman" and items[0].fields["group"] == "Secp0"
    assert "sector" not in items[0].fields                  # the post gave none; none invented
