"""EDGAR route: vendor -> filer mapping, Item 1.05 exactness, text-search filtering."""

from datetime import UTC, datetime
from pathlib import Path

from intothedarkness.scrapers.sec import Edgar, collect, map_vendors
from intothedarkness.watchlist import parse

COMPANIES = [
    {"cik_str": 1800, "ticker": "ABT", "title": "ABBOTT LABORATORIES"},
    {"cik_str": 885725, "ticker": "BSX", "title": "BOSTON SCIENTIFIC CORP"},
    {"cik_str": 1, "ticker": "ARRY", "title": "Array Technologies, Inc."},
    {"cik_str": 2, "ticker": "GEHC", "title": "GE HEALTHCARE TECHNOLOGIES INC"},
    {"cik_str": 3, "ticker": "GE", "title": "GE AEROSPACE"},
]


def test_vendors_map_to_filers_conservatively():
    vendors = parse("Abbott\nBoston Scientific\nArray\nGE Healthcare\n")
    m = map_vendors(vendors, COMPANIES)
    assert m[1800] == ("Abbott", "ABBOTT LABORATORIES", "ABT")          # word + generic word
    assert m[885725][0] == "Boston Scientific"                          # multi-word prefix
    assert m[2][0] == "GE Healthcare"
    assert 1 not in m and 3 not in m      # "Array" is not Array Technologies; "GE" alone unmapped
    dell = [{"cik_str": 9, "ticker": "DELL", "title": "Dell Technologies Inc."}]
    assert map_vendors(parse("Dell\n"), dell) == {}              # needs its full name on the list
    assert 9 in map_vendors(parse("Dell Technologies\n"), dell)


class FakeEdgar(Edgar):
    """Canned EDGAR answers, no network, fixed clock."""

    def __init__(self, tmp_path: Path, subs: dict, fts: list):
        fixed = datetime(2026, 9, 23, tzinfo=UTC)
        super().__init__(lambda u, p: "{}", tmp_path, 240, now=lambda: fixed)
        self._subs, self._fts = subs, fts

    def companies(self):
        return COMPANIES

    def submissions(self, cik):
        cols = ("form", "filingDate", "items", "accessionNumber", "primaryDocument")
        empty = {c: [] for c in cols}
        return self._subs.get(cik, {"filings": {"recent": empty}})

    def fulltext(self, query, start, end):
        return self._fts


def recent(*rows):
    cols = ["form", "filingDate", "items", "accessionNumber", "primaryDocument"]
    return {"filings": {"recent": {c: [r[i] for r in rows] for i, c in enumerate(cols)}}}


def test_item_105_is_exact_and_old_filings_are_ignored(tmp_path):
    subs = {1800: recent(("8-K", "2026-09-01", "1.05,9.01", "0001-26-000001", "abt.htm"),
                         ("8-K", "2026-09-10", "2.02,9.01", "0001-26-000002", "earn.htm"),
                         ("8-K", "2026-01-05", "1.05", "0001-26-000003", "old.htm"))}
    items = collect(FakeEdgar(tmp_path, subs, []), parse("Abbott\n"), 90, "sec-8k")
    assert [i.key for i in items] == ["0001-26-000001"]
    it = items[0]
    assert it.fields["watchlist"] == "Abbott" and it.fields["status"] == "Item 1.05"
    assert it.title == "ABBOTT LABORATORIES" and it.fields["published"] == "2026-09-01"
    assert it.url == "https://www.sec.gov/Archives/edgar/data/1800/000126000001/abt.htm"
    assert "Item 1.05 material cybersecurity incident" in it.text


def test_text_search_hits_are_kept_only_for_vendor_filers(tmp_path):
    fts = [
        {"ciks": ["0000885725"], "file_date": "2026-08-26", "form": "8-K", "items": ["8.01"],
         "_id": "0000885725-26-000056:bsx-20260826.htm"},
        {"ciks": ["0001083446"], "file_date": "2026-09-23", "form": "8-K", "items": ["1.05"],
         "_id": "0001104659-26-109813:asth.htm"},                       # not a vendor
    ]
    items = collect(FakeEdgar(tmp_path, {}, fts), parse("Boston Scientific\n"), 90, "sec-8k")
    assert [i.title for i in items] == ["BOSTON SCIENTIFIC CORP"]
    assert items[0].fields["status"] == "cyber wording" and items[0].fields["items"] == "8.01"
    assert items[0].url.endswith("/000088572526000056/bsx-20260826.htm")


def test_the_same_filing_from_both_routes_is_one_item(tmp_path):
    subs = {885725: recent(("8-K", "2026-08-26", "1.05,8.01", "0000885725-26-000056", "bsx.htm"))}
    fts = [{"ciks": ["885725"], "file_date": "2026-08-26", "form": "8-K", "items": ["1.05", "8.01"],
            "_id": "0000885725-26-000056:bsx.htm"}]
    items = collect(FakeEdgar(tmp_path, subs, fts), parse("Boston Scientific\n"), 90, "sec-8k")
    assert len(items) == 1 and items[0].fields["status"] == "Item 1.05"


def test_incident_wording_in_an_earnings_release_is_not_an_incident(tmp_path):
    # Measured: three of six text-search hits were 2.02 earnings releases that
    # mention cybersecurity as a risk factor. Only 1.05 and 8.01 count.
    fts = [
        {"ciks": ["885725"], "file_date": "2026-07-30", "form": "8-K", "items": ["2.02", "9.01"],
         "_id": "0000885725-26-000040:earnings.htm"},
        {"ciks": ["885725"], "file_date": "2026-08-26", "form": "8-K", "items": ["8.01"],
         "_id": "0000885725-26-000056:bsx-20260826.htm"},
    ]
    items = collect(FakeEdgar(tmp_path, {}, fts), parse("Boston Scientific\n"), 90, "sec-8k")
    assert [i.key for i in items] == ["0000885725-26-000056"]
