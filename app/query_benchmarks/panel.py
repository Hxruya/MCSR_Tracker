"""The panel's HTML: a launcher button and a drawer, with inline CSS and JS so it needs no static files.

Theming: colours use light-dark(), so the panel follows the host page's `color-scheme`. A host can set
--qb-accent, --qb-sql and --qb-mongo on :root to match its palette. Everything is scoped under #query_benchmarks.
"""
from markupsafe import escape

CSS = """
#query_benchmarks{--qb-bg:light-dark(#ffffff,#1b1820);--qb-bg-2:light-dark(#f4f2f6,#25212b);--qb-ink:light-dark(#1d1a21,#f1eef4);
--qb-ink-2:light-dark(#5d5666,#bdb5c6);--qb-line:light-dark(#e3dfe8,#36303e);
--qb-accent-c:var(--qb-accent,light-dark(#5b4bc4,#a99cf2));--qb-sql-c:var(--qb-sql,light-dark(#2f6fb5,#7fb0ea));
--qb-mongo-c:var(--qb-mongo,light-dark(#2e8a57,#6fcf98));font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
#query_benchmarks *{box-sizing:border-box}
#query_benchmarks .qb-open{position:fixed;right:16px;bottom:16px;z-index:2147483000;display:flex;gap:8px;align-items:center;
padding:8px 14px;border-radius:999px;border:1px solid var(--qb-line);background:var(--qb-bg);color:var(--qb-ink);
font:inherit;font-weight:600;cursor:pointer;box-shadow:0 2px 10px light-dark(rgba(30,20,40,.12),rgba(0,0,0,.5))}
#query_benchmarks .qb-open:hover{border-color:var(--qb-accent-c)}
#query_benchmarks .qb-dot{width:8px;height:8px;border-radius:50%;background:var(--qb-accent-c)}
#query_benchmarks .qb-drawer{position:fixed;top:0;right:0;bottom:0;z-index:2147483001;width:min(600px,100vw);display:flex;
flex-direction:column;background:var(--qb-bg);color:var(--qb-ink);border-left:1px solid var(--qb-line);
box-shadow:-8px 0 30px light-dark(rgba(30,20,40,.14),rgba(0,0,0,.55));transform:translateX(0);transition:transform .18s ease}
#query_benchmarks .qb-drawer[hidden]{display:flex;transform:translateX(105%);visibility:hidden}
#query_benchmarks header{padding:16px 18px 12px;border-bottom:1px solid var(--qb-line)}
#query_benchmarks h2{margin:0;font-size:17px;outline:none}#query_benchmarks h2:focus-visible{outline:none}
#query_benchmarks .qb-sub{margin:4px 0 0;color:var(--qb-ink-2)}
#query_benchmarks .qb-close{position:absolute;top:12px;right:12px;width:32px;height:32px;border-radius:8px;border:1px solid var(--qb-line);
background:var(--qb-bg-2);color:var(--qb-ink);font:inherit;font-size:18px;line-height:1;cursor:pointer}
#query_benchmarks .qb-engines{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
#query_benchmarks ol{list-style:none;margin:0;padding:8px 10px 24px;overflow-y:auto;flex:1}
#query_benchmarks li{border:1px solid var(--qb-line);border-radius:10px;margin:8px 0;background:var(--qb-bg)}
#query_benchmarks details>summary{list-style:none;cursor:pointer;padding:10px 12px;display:grid;gap:4px}
#query_benchmarks details>summary::-webkit-details-marker{display:none}
#query_benchmarks details[open]>summary{border-bottom:1px solid var(--qb-line)}
#query_benchmarks .qb-row{display:flex;gap:8px;align-items:baseline}
#query_benchmarks .qb-what{flex:1;min-width:0;font-weight:600;overflow-wrap:anywhere}
#query_benchmarks .qb-num{color:var(--qb-ink-2);font-variant-numeric:tabular-nums;white-space:nowrap;font-size:13px}
#query_benchmarks .qb-tag{display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px;font-weight:600;white-space:nowrap;
color:var(--qb-bg);background:var(--qb-ink-2)}
#query_benchmarks .qb-tag.sql{background:var(--qb-sql-c)}#query_benchmarks .qb-tag.mongo{background:var(--qb-mongo-c)}
#query_benchmarks .qb-auto{color:var(--qb-ink-2);font-size:13px;overflow-wrap:anywhere}
#query_benchmarks .qb-chips{display:flex;flex-wrap:wrap;gap:4px}
#query_benchmarks .qb-chip{padding:0 7px;border-radius:999px;border:1px solid var(--qb-line);color:var(--qb-ink-2);font-size:12px}
#query_benchmarks pre{margin:0;padding:10px 12px;max-height:340px;overflow:auto;background:var(--qb-bg-2);border-radius:0 0 10px 10px;
font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--qb-ink)}
#query_benchmarks .qb-note{padding:8px 12px;color:var(--qb-ink-2);font-size:13px}
#query_benchmarks :focus-visible{outline:2px solid var(--qb-accent-c);outline-offset:2px}
@media (prefers-reduced-motion:reduce){#query_benchmarks .qb-drawer{transition:none}}
@media print{#query_benchmarks{display:none}}
"""

JS = """
(function(){var r=document.getElementById("query_benchmarks");if(!r)return;
var o=r.querySelector(".qb-open"),d=r.querySelector(".qb-drawer"),h=d.querySelector("h2");
function set(open){d.hidden=!open;o.setAttribute("aria-expanded",open);if(open){h.focus()}else{o.focus()}}
o.addEventListener("click",function(){set(d.hidden)});
r.querySelector(".qb-close").addEventListener("click",function(){set(false)});
document.addEventListener("keydown",function(e){if(e.key==="Escape"&&!d.hidden)set(false)});
if(location.hash==="#show-queries"){set(true);var f=d.querySelector("li.qb-labelled details")||d.querySelector("details");if(f)f.open=true}})();
"""


def _ms(value):
    return f"{value:.1f} ms" if value < 100 else f"{value:.0f} ms"


def _plural(n, word):
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word[:-1]}ies" if word.endswith("y") else f"{n} {word}s"


def _engine_class(engine):
    return "mongo" if engine.lower().startswith("mongo") else "sql"


def _entry(e):
    tag = f'<span class="qb-tag {_engine_class(e.engine)}">{escape(e.engine)}</span>'
    nums = _ms(e.ms) + ("" if e.rows is None else f", {_plural(e.rows, 'row')}")
    if e.hidden:
        return (f'<li><div class="qb-note"><span class="qb-row">{tag}<span class="qb-what">Hidden by configuration'
                f'</span><span class="qb-num">{nums}</span></span></div></li>')
    what = escape(e.label or e.summary or e.kind)
    auto = f'<span class="qb-auto">{escape(e.summary)}</span>' if e.label and e.summary else ""
    chips = "".join(f'<span class="qb-chip">{escape(f)}</span>' for f in e.features)
    chips = f'<span class="qb-chips">{chips}</span>' if chips else ""
    cls = ' class="qb-labelled"' if e.label else ""
    return (f'<li{cls}><details><summary><span class="qb-row">{tag}<span class="qb-what">{what}</span>'
            f'<span class="qb-num">{nums}</span></span>{auto}{chips}</summary>'
            f'<pre>{escape(e.statement)}</pre></details></li>')


def render(log, page_ms):
    engines = "".join(f'<span class="qb-tag {_engine_class(k)}">{escape(k)}: {v}</span>'
                      for k, v in sorted(log.by_engine.items()))
    more = log.count - len(log.entries)
    items = "".join(_entry(e) for e in log.entries)
    if more > 0:
        items += f'<li><div class="qb-note">{more} more not listed (limit reached).</div></li>'
    if not log.count:
        items = '<li><div class="qb-note">This page ran no database queries.</div></li>'
    button = f"{_plural(log.count, 'query')}, {_ms(log.ms)}"
    sub = (f"{_plural(log.count, 'query')} took {_ms(log.ms)} "
           f"of the {_ms(page_ms)} it took to build this page. Open one to see the statement.")
    return (f'<div id="query_benchmarks"><style>{CSS}</style>'
            f'<button class="qb-open" type="button" aria-expanded="false" aria-controls="qb-drawer">'
            f'<span class="qb-dot" aria-hidden="true"></span>{button}</button>'
            f'<aside class="qb-drawer" id="qb-drawer" hidden aria-labelledby="qb-title">'
            f'<header><h2 id="qb-title" tabindex="-1">Behind this page</h2><p class="qb-sub">{sub}</p>'
            f'<div class="qb-engines">{engines}</div>'
            f'<button class="qb-close" type="button" aria-label="Close">×</button></header>'
            f'<ol>{items}</ol></aside><script>{JS}</script></div>')
