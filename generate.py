#!/usr/bin/env python3
"""
Generate the three output artifacts from bookmarks.json:

  - index.html                 styled, self-contained dashboard (search + collapsible sections)
  - tor_bookmarks.html         Chrome tab launcher: one "open these in tabs" button per section
  - tor_bookmarks_import.html  Netscape bookmark file, importable into Tor Browser / Firefox

Usage:  python3 generate.py

The outputs are committed, so this must reproduce them byte-for-byte from
the same bookmarks.json. Set GENERATE_STAMP="2026-09-03 15:55 UTC" to pin the
timestamp and check that with `git diff` -- the launcher and the badge removal
were once hand-edited into the HTML while this file was left behind, and the
next person to run it undid both.
"""
import json
import html
import datetime
import os
import pathlib

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "bookmarks.json"

CSS = """
  :root{
    --bg:#0b0d10; --panel:#14181d; --panel-2:#191e25; --border:#262d36;
    --text:#d7dde5; --muted:#8b96a5; --accent:#6ea8fe; --onion:#b48ef0;
    --clear:#5bd6a0; --danger:#ff7a7a;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:var(--bg); color:var(--text);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  header{
    position:sticky; top:0; z-index:5; background:rgba(11,13,16,.92);
    backdrop-filter:blur(8px); border-bottom:1px solid var(--border);
    padding:18px 20px 14px;
  }
  h1{margin:0 0 4px; font-size:19px; letter-spacing:.2px}
  .sub{color:var(--muted); font-size:13px; margin:0 0 12px; max-width:820px}
  .toolbar{display:flex; gap:10px; flex-wrap:wrap; align-items:center}
  #search{
    flex:1 1 260px; min-width:200px; background:var(--panel-2);
    border:1px solid var(--border); color:var(--text); border-radius:8px;
    padding:9px 12px; font-size:14px; outline:none;
  }
  #search:focus{border-color:var(--accent)}
  .meta{color:var(--muted); font-size:12px; white-space:nowrap}
  .ctrl{
    background:var(--panel-2); border:1px solid var(--border); color:var(--muted);
    border-radius:8px; padding:9px 12px; font-size:13px; cursor:pointer;
  }
  .ctrl:hover{color:var(--text); border-color:var(--accent)}
  main{padding:16px 20px 60px; max-width:960px; margin:0 auto}
  .cat{
    background:var(--panel); border:1px solid var(--border); border-radius:12px;
    margin:14px 0; overflow:hidden;
  }
  .cat.collapsed .entries,.cat.collapsed .cat-note{display:none}
  .cat-title{
    display:flex; align-items:center; gap:10px; margin:0; cursor:pointer;
    padding:14px 16px; font-size:15px; user-select:none;
  }
  .cat-title:hover{background:var(--panel-2)}
  .chev{display:inline-block; transition:transform .15s ease; color:var(--muted); font-size:12px}
  .cat.collapsed .chev{transform:rotate(-90deg)}
  .count{
    margin-left:auto; background:var(--panel-2); color:var(--muted);
    border:1px solid var(--border); border-radius:20px; padding:1px 9px; font-size:12px;
  }
  .cat-note{margin:0 16px 8px; color:var(--muted); font-size:12.5px}
  .entries{list-style:none; margin:0; padding:4px 8px 10px}
  .entry{
    display:flex; align-items:center; gap:10px; padding:9px 8px;
    border-radius:8px; border-bottom:1px solid transparent;
  }
  .entry:hover{background:var(--panel-2)}
  .entry-main{display:flex; align-items:center; gap:8px; flex:0 0 auto; min-width:0}
  .entry-link{color:var(--accent); text-decoration:none; font-weight:600; white-space:nowrap}
  .entry-link:hover{text-decoration:underline}
  .badge{font-size:10.5px; padding:1px 7px; border-radius:20px; border:1px solid var(--border); text-transform:uppercase; letter-spacing:.4px}
  .badge.onion{color:var(--onion)}
  .badge.clear{color:var(--clear)}
  .entry-url{
    flex:1 1 auto; min-width:0; color:var(--muted); font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
    font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  }
  .copy{
    flex:0 0 auto; background:transparent; border:1px solid var(--border); color:var(--muted);
    border-radius:6px; padding:3px 9px; font-size:11px; cursor:pointer;
  }
  .copy:hover{color:var(--text); border-color:var(--accent)}
  .copy.done{color:var(--clear); border-color:var(--clear)}
  .empty{display:none; color:var(--muted); text-align:center; padding:40px}
  footer{color:var(--muted); font-size:12px; text-align:center; padding:24px 20px 40px}
  .opsec{
    max-width:960px; margin:0 auto; background:rgba(255,122,122,.06);
    border:1px solid rgba(255,122,122,.25); border-radius:10px; padding:10px 14px;
    color:#e7b8b8; font-size:12.5px;
  }
  @media (max-width:620px){
    .entry{flex-wrap:wrap}
    .entry-url{flex-basis:100%; order:3}
  }
"""

JS = """
  const search = document.getElementById('search');
  const cats = [...document.querySelectorAll('[data-cat]')];
  const empty = document.getElementById('empty');

  function applyFilter(q){
    q = q.trim().toLowerCase();
    let anyVisible = false;
    cats.forEach(cat=>{
      let visibleInCat = 0;
      cat.querySelectorAll('.entry').forEach(e=>{
        const hit = !q || e.dataset.search.includes(q);
        e.style.display = hit ? '' : 'none';
        if(hit) visibleInCat++;
      });
      const show = visibleInCat > 0;
      cat.style.display = show ? '' : 'none';
      if(show){ anyVisible = true; if(q) cat.classList.remove('collapsed'); }
    });
    empty.style.display = anyVisible ? 'none' : 'block';
  }
  search.addEventListener('input', ()=>applyFilter(search.value));

  document.querySelectorAll('.cat-title').forEach(t=>{
    t.addEventListener('click', ()=> t.parentElement.classList.toggle('collapsed'));
  });
  document.getElementById('expand').addEventListener('click', ()=>{
    cats.forEach(c=>c.classList.remove('collapsed'));
  });
  document.getElementById('collapse').addEventListener('click', ()=>{
    cats.forEach(c=>c.classList.add('collapsed'));
  });
  document.querySelectorAll('.copy').forEach(b=>{
    b.addEventListener('click', async (ev)=>{
      ev.stopPropagation();
      try{
        await navigator.clipboard.writeText(b.dataset.url);
        const old = b.textContent; b.textContent='copied'; b.classList.add('done');
        setTimeout(()=>{b.textContent=old; b.classList.remove('done');},1200);
      }catch(e){
        const ta=document.createElement('textarea'); ta.value=b.dataset.url;
        document.body.appendChild(ta); ta.select();
        try{document.execCommand('copy');}catch(_){}
        ta.remove();
        const old=b.textContent; b.textContent='copied'; b.classList.add('done');
        setTimeout(()=>{b.textContent=old; b.classList.remove('done');},1200);
      }
    });
  });
"""

LAUNCHER_CSS = """
  .bar{position:sticky;top:0;z-index:5;background:rgba(11,13,16,.95);backdrop-filter:blur(8px);
       border-bottom:1px solid var(--border);padding:16px 20px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
  .bar-hint{color:var(--muted);font-size:13px}
  .open-sec{background:var(--panel-2);border:1px solid var(--border);color:var(--text);border-radius:7px;
       padding:6px 12px;font-size:12.5px;cursor:pointer;white-space:nowrap}
  .open-sec:hover{border-color:var(--accent)}
  .msg{font-size:13px;margin:0}
  .msg.ok{color:var(--clear)} .msg.warn{color:#ffcf7a}
  .cat-head{display:flex;align-items:center;gap:12px;justify-content:space-between;margin:0 16px}
  .cat-head h2{font-size:15px;margin:14px 0 8px}
  .entry{display:flex;align-items:center;gap:10px;padding:7px 8px;border-radius:8px;flex-wrap:wrap}
  .entry:hover{background:var(--panel-2)}
  .lnk{color:var(--accent);text-decoration:none;font-weight:600}
  .lnk:hover{text-decoration:underline}
  .u{color:var(--muted);font-family:ui-monospace,Menlo,monospace;font-size:11.5px;
     overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 300px;min-width:0}
"""

LAUNCHER_JS = """
  function openTabs(urls){
    // Open one background/foreground tab per URL. Chrome blocks all but the
    // first until pop-ups are allowed for this page — see the on-page note.
    var blocked = 0;
    urls.forEach(function(u){
      var w = window.open(u, '_blank');
      if(!w) blocked++;
    });
    var msg = document.getElementById('msg');
    if(blocked > 0){
      msg.textContent = 'Chrome blocked ' + blocked + ' of ' + urls.length +
        ' tabs. Click the blocked-pop-ups icon in the address bar, choose ' +
        '"Always allow pop-ups from this site", then click again.';
      msg.className = 'msg warn';
    } else {
      msg.textContent = 'Opened ' + urls.length + ' tab' + (urls.length===1?'':'s') + '.';
      msg.className = 'msg ok';
    }
  }
  function urlsFrom(scope){
    return Array.prototype.map.call(scope.querySelectorAll('a.lnk'), function(a){ return a.href; });
  }
  document.querySelectorAll('.open-sec').forEach(function(b){
    b.addEventListener('click', function(){
      openTabs(urlsFrom(b.closest('section')));
    });
  });
"""


def stamp() -> str:
    """Now, unless pinned -- pinning is how the outputs are proven reproducible."""
    return os.environ.get("GENERATE_STAMP") or datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


def load():
    with open(DATA, "r", encoding="utf-8") as f:
        return json.load(f)


def is_onion(url: str) -> bool:
    return ".onion" in url


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def build_dashboard(data: dict) -> str:
    gen = stamp()
    total = sum(len(c["links"]) for c in data["categories"])
    ncat = len(data["categories"])

    sections = []
    for cat in data["categories"]:
        rows = []
        for link in cat["links"]:
            url = esc(link["url"])
            raw = link["url"]
            # No onion/clearnet badge: the address itself says which it is.
            search_attr = esc((link["title"] + " " + raw).lower())
            rows.append(
                '        <li class="entry" data-search="' + search_attr + '">\n'
                '          <div class="entry-main">\n'
                '            <a class="entry-link" href="' + url + '" target="_blank" rel="noopener noreferrer">'
                + esc(link["title"]) + '</a>\n'
                '          </div>\n'
                '          <div class="entry-url" title="' + url + '">' + url + '</div>\n'
                '          <button class="copy" data-url="' + url + '">copy</button>\n'
                '        </li>'
            )
        rows_html = "\n".join(rows)
        sections.append(
            '    <section class="cat" data-cat>\n'
            '      <h2 class="cat-title"><span class="chev">&#9656;</span>'
            + esc(cat["name"]) + '<span class="count">' + str(len(cat["links"])) + '</span></h2>\n'
            '      <p class="cat-note">' + esc(cat.get("note", "")) + '</p>\n'
            '      <ul class="entries">\n' + rows_html + '\n      </ul>\n'
            '    </section>'
        )
    sections_html = "\n".join(sections)

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>' + esc(data["title"]) + '</title>\n'
        '<style>' + CSS + '</style>\n'
        '</head>\n<body>\n'
        '<header>\n'
        '  <h1>' + esc(data["title"]) + '</h1>\n'
        '  <p class="sub">' + esc(data["description"]) + '</p>\n'
        '  <div class="toolbar">\n'
        '    <input id="search" type="search" placeholder="Filter by name or address…" autocomplete="off" spellcheck="false">\n'
        '    <button class="ctrl" id="expand">Expand all</button>\n'
        '    <button class="ctrl" id="collapse">Collapse all</button>\n'
        '    <span class="meta">' + str(total) + ' links · ' + str(ncat) + ' sections · updated ' + gen + '</span>\n'
        '  </div>\n'
        '</header>\n'
        '<main>\n'
        '  <p class="opsec">Investigation resource. Open .onion links only in Tor Browser, on an isolated/VM environment appropriate to your operational security. Addresses are volatile and may be offline, seized, or rotated.</p>\n'
        + sections_html + '\n'
        '  <p class="empty" id="empty">No links match your filter.</p>\n'
        '</main>\n'
        '<footer>IntoTheDarkness · generated from bookmarks.json · ' + gen + '</footer>\n'
        '<script>' + JS + '</script>\n'
        '</body>\n</html>\n'
    )


def build_netscape(data: dict) -> str:
    out = [
        '<!DOCTYPE NETSCAPE-Bookmark-file-1>',
        '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
        '<TITLE>Bookmarks</TITLE>',
        '<H1>Bookmarks</H1>',
        '<DL><p>',
        '    <DT><H3>IntoTheDarkness</H3>',
        '    <DL><p>',
    ]
    for cat in data["categories"]:
        out.append('        <DT><H3>' + esc(cat["name"]) + '</H3>')
        out.append('        <DL><p>')
        for link in cat["links"]:
            out.append('            <DT><A HREF="' + esc(link["url"]) + '">' + esc(link["title"]) + '</A>')
        out.append('        </DL><p>')
    out.append('    </DL><p>')
    out.append('</DL><p>')
    return "\n".join(out) + "\n"


def build_launcher(data: dict) -> str:
    """Chrome tab launcher: every link is a plain anchor, one button per section."""
    gen = stamp()
    total = sum(len(c["links"]) for c in data["categories"])

    sections = []
    for cat in data["categories"]:
        rows = []
        for link in cat["links"]:
            url = esc(link["url"])
            rows.append(
                '        <li class="entry"><a class="lnk" href="' + url
                + '" target="_blank" rel="noopener noreferrer">' + esc(link["title"])
                + '</a> <span class="u">' + url + '</span></li>'
            )
        sections.append(
            '    <section class="cat">\n'
            '      <div class="cat-head"><h2>' + esc(cat["name"]) + '</h2>'
            '<button class="open-sec">Open these ' + str(len(cat["links"])) + ' in tabs</button></div>\n'
            '      <ul class="entries">\n' + "\n".join(rows) + '\n      </ul>\n'
            '    </section>'
        )

    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        '<title>' + esc(data["title"]) + ' — Tab Launcher</title>\n'
        '<style>' + CSS + LAUNCHER_CSS + '</style>\n'
        '</head>\n<body>\n'
        '<div class="bar">\n'
        '  <span class="bar-hint">Open a category in its own tabs with the section buttons below.</span>\n'
        '  <p class="msg" id="msg"></p>\n'
        '</div>\n'
        '<main>\n'
        '  <p class="opsec"><strong>Two things to know:</strong> the first time you click, Chrome blocks the extra tabs '
        '— click the blocked-pop-ups icon in the address bar, choose &ldquo;Always allow pop-ups from this site&rdquo;, '
        'then click again. And <strong>.onion links only load if Chrome is routed through Tor</strong> '
        '(a SOCKS proxy/extension); otherwise those tabs will fail to connect. Use Tor Browser for onion sites, '
        'or the styled dashboard (index.html) for browsing.</p>\n'
        + "\n".join(sections) + '\n'
        '</main>\n'
        '<footer>IntoTheDarkness tab launcher · ' + str(total) + ' links · generated ' + gen + '</footer>\n'
        '<script>' + LAUNCHER_JS + '</script>\n'
        '</body>\n</html>\n'
    )


def main():
    data = load()
    (ROOT / "index.html").write_text(build_dashboard(data), encoding="utf-8")
    (ROOT / "tor_bookmarks.html").write_text(build_launcher(data), encoding="utf-8")
    (ROOT / "tor_bookmarks_import.html").write_text(build_netscape(data), encoding="utf-8")
    total = sum(len(c["links"]) for c in data["categories"])
    print(
        f"Wrote index.html, tor_bookmarks.html and tor_bookmarks_import.html "
        f"({total} links, {len(data['categories'])} sections)"
    )


if __name__ == "__main__":
    main()
