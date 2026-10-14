#!/usr/bin/env python3
"""매일 아침 경제·사회 뉴스 브리핑 생성기.

feeds.json 에 적힌 언론사 RSS 를 모아 최근 기사만 골라 중복을 지우고,
한눈에 볼 수 있는 HTML 페이지(news/index.html)와 날짜별 보관본을 만든다.
표준 라이브러리만 사용하므로 별도 설치 없이 `python3 news/fetch_news.py` 로 실행된다.
"""

import argparse
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

KST = timezone(timedelta(hours=9))
HERE = Path(__file__).resolve().parent
USER_AGENT = "Mozilla/5.0 (compatible; morning-news-brief/1.0)"
ATOM = "{http://www.w3.org/2005/Atom}"

# 제목에서 키워드를 뽑을 때 빼는 단어와 조사
STOPWORDS = {
    "속보", "종합", "단독", "포토", "사진", "영상", "오늘", "내일", "올해", "지난해",
    "이번", "관련", "위해", "대한", "통해", "따라", "있는", "없는", "하는", "했다",
    "한다", "된다", "까지", "부터", "기자", "뉴스", "가장", "다시", "모든", "만에",
}
PARTICLES = ("에서는", "으로는", "에서", "으로", "에게", "까지", "부터", "보다",
             "은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "와", "과", "로")


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def text_of(el, *tags):
    for tag in tags:
        child = el.find(tag)
        if child is not None:
            if child.text and child.text.strip():
                return child.text.strip()
            if child.get("href"):
                return child.get("href").strip()
    return ""


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def clean_summary(raw, limit=140):
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def parse_feed(data, source, category):
    root = ET.fromstring(data)
    items = root.iter("item")
    entries = list(items) or list(root.iter(ATOM + "entry"))
    articles = []
    for it in entries:
        title = html.unescape(text_of(it, "title", ATOM + "title"))
        link = text_of(it, "link", ATOM + "link")
        if not title or not link:
            continue
        articles.append({
            "category": category,
            "source": source,
            "title": re.sub(r"\s+", " ", title),
            "link": link,
            "summary": clean_summary(text_of(it, "description", ATOM + "summary", ATOM + "content")),
            "published": parse_date(text_of(it, "pubDate", "{http://purl.org/dc/elements/1.1/}date",
                                            ATOM + "published", ATOM + "updated")),
        })
    return articles


def title_key(title):
    """[속보], (종합) 같은 머리말과 기호를 지워 같은 기사인지 비교할 때 쓴다."""
    t = re.sub(r"^\s*([\[\(【<][^\]\)】>]*[\]\)】>]\s*)+", "", title)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t).lower()


def dedupe(articles):
    seen, result = set(), []
    for a in articles:
        key = title_key(a["title"])
        if key and key not in seen:
            seen.add(key)
            result.append(a)
    return result


def keywords(articles, top=12):
    counter = Counter()
    for a in articles:
        words = set()
        for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", re.sub(r"[\[\(][^\]\)]*[\]\)]", " ", a["title"])):
            for p in PARTICLES:
                if w.endswith(p) and len(w) - len(p) >= 2:
                    w = w[: -len(p)]
                    break
            if w not in STOPWORDS and not w.isdigit():
                words.add(w)
        counter.update(words)
    return [(w, n) for w, n in counter.most_common(top) if n >= 2]


def collect(feeds, hours):
    jobs = [(cat, f["source"], f["url"]) for cat, fs in feeds.items() for f in fs]
    errors = []

    def run(job):
        cat, source, url = job
        try:
            return parse_feed(fetch(url), source, cat)
        except Exception as e:  # 한 언론사가 실패해도 나머지는 계속 만든다
            errors.append(f"{source}({cat}): {e}")
            return []

    with ThreadPoolExecutor(max_workers=8) as pool:
        articles = [a for batch in pool.map(run, jobs) for a in batch]

    cutoff = datetime.now(KST) - timedelta(hours=hours)
    articles = [a for a in articles if a["published"] is None or a["published"] >= cutoff]
    articles.sort(key=lambda a: a["published"] or cutoff, reverse=True)
    return dedupe(articles), errors


# ---------------------------------------------------------------- HTML 출력

PAGE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>아침 뉴스 브리핑</title>
<style>
:root {{
  --bg: #f6f7f9; --card: #ffffff; --text: #1d2129; --muted: #6b7280;
  --line: #e5e7eb; --accent: #2563eb; --chip: #eef2ff;
  --econ: #0f766e; --soc: #b45309;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0f1115; --card: #181b21; --text: #e5e7eb; --muted: #9ca3af;
    --line: #2a2f38; --accent: #60a5fa; --chip: #1e2533;
    --econ: #2dd4bf; --soc: #fbbf24;
  }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", "Malgun Gothic", sans-serif;
  line-height: 1.5; }}
.wrap {{ max-width: 1100px; margin: 0 auto; padding: 24px 16px 48px; }}
header h1 {{ margin: 0; font-size: 1.6rem; }}
header p {{ margin: 4px 0 0; color: var(--muted); font-size: .9rem; }}
.stats {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 20px 0; }}
.stat {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; min-width: 110px; }}
.stat b {{ display: block; font-size: 1.4rem; }}
.stat span {{ color: var(--muted); font-size: .8rem; }}
.kw {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 0 0 24px; padding: 0; list-style: none; }}
.kw li {{ background: var(--chip); border-radius: 999px; padding: 4px 12px; font-size: .9rem; }}
.kw li small {{ color: var(--muted); margin-left: 4px; }}
h2.sec {{ font-size: 1rem; color: var(--muted); margin: 0 0 8px; font-weight: 600; }}
.tabs {{ display: flex; gap: 8px; margin-bottom: 12px; position: sticky; top: 0; background: var(--bg); padding: 8px 0;
  overflow-x: auto; white-space: nowrap; }}
.tabs button {{ border: 1px solid var(--line); background: var(--card); color: var(--text);
  border-radius: 999px; padding: 6px 14px; font-size: .9rem; cursor: pointer; }}
.tabs button.on {{ background: var(--accent); border-color: var(--accent); color: #fff; }}
.grid {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; }}
@media (max-width: 760px) {{ .grid {{ grid-template-columns: minmax(0, 1fr); }} }}
article, header p {{ word-break: keep-all; overflow-wrap: anywhere; }}
.col h3 {{ margin: 0 0 8px; font-size: 1.1rem; }}
.col[data-cat="경제"] h3 {{ color: var(--econ); }}
.col[data-cat="사회"] h3 {{ color: var(--soc); }}
article {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin-bottom: 10px; }}
article a {{ color: var(--text); text-decoration: none; font-weight: 600; }}
article a:hover {{ color: var(--accent); text-decoration: underline; }}
article p {{ margin: 6px 0 0; color: var(--muted); font-size: .88rem; }}
.meta {{ font-size: .78rem; color: var(--muted); margin-top: 6px; }}
.meta .src {{ font-weight: 600; }}
.hidden {{ display: none; }}
footer {{ margin-top: 32px; color: var(--muted); font-size: .8rem; }}
footer a {{ color: var(--accent); }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>☀️ 아침 뉴스 브리핑</h1>
  <p>{date_label} · 최근 {hours}시간 경제·사회 뉴스 · {generated} 업데이트</p>
</header>

<div class="stats">{stats}</div>

<h2 class="sec">오늘의 키워드</h2>
<ul class="kw">{keywords}</ul>

<div class="tabs">{tabs}</div>
<div class="grid">{columns}</div>

<footer>
  {errors}
  <p>지난 브리핑: {archive}</p>
  <p>기사 제목과 요약은 각 언론사 RSS 에서 가져왔으며, 저작권은 해당 언론사에 있습니다.</p>
</footer>
</div>
<script>
document.querySelectorAll('.tabs button').forEach(function (btn) {{
  btn.addEventListener('click', function () {{
    document.querySelectorAll('.tabs button').forEach(function (b) {{ b.classList.toggle('on', b === btn); }});
    var src = btn.dataset.src;
    document.querySelectorAll('article').forEach(function (a) {{
      a.classList.toggle('hidden', src !== '전체' && a.dataset.src !== src);
    }});
  }});
}});
</script>
</body>
</html>
"""


def render(articles, errors, hours, now, archive_dates):
    e = html.escape
    cats = list(dict.fromkeys(a["category"] for a in articles)) or ["경제", "사회"]
    sources = list(dict.fromkeys(a["source"] for a in articles))

    stats = [f'<div class="stat"><b>{len(articles)}</b><span>전체 기사</span></div>']
    stats += [f'<div class="stat"><b>{sum(a["category"] == c for a in articles)}</b><span>{e(c)}</span></div>'
              for c in cats]
    stats.append(f'<div class="stat"><b>{len(sources)}</b><span>언론사</span></div>')

    kws = keywords(articles)
    kw_html = "".join(f"<li>{e(w)}<small>{n}</small></li>" for w, n in kws) or "<li>키워드 없음</li>"

    tabs = ['<button class="on" data-src="전체">전체</button>']
    tabs += [f'<button data-src="{e(s)}">{e(s)}</button>' for s in sources]

    columns = []
    for c in cats:
        items = []
        for a in (x for x in articles if x["category"] == c):
            when = a["published"].strftime("%m/%d %H:%M") if a["published"] else ""
            summary = f'<p>{e(a["summary"])}</p>' if a["summary"] else ""
            items.append(
                f'<article data-src="{e(a["source"])}">'
                f'<a href="{e(a["link"])}" target="_blank" rel="noopener">{e(a["title"])}</a>{summary}'
                f'<div class="meta"><span class="src">{e(a["source"])}</span> · {when}</div></article>'
            )
        columns.append(f'<section class="col" data-cat="{e(c)}"><h3>{e(c)}</h3>'
                       + ("".join(items) or "<p>기사가 없습니다.</p>") + "</section>")

    err_html = ""
    if errors:
        err_html = "<p>불러오지 못한 피드: " + e(", ".join(err.split(":")[0] for err in errors)) + "</p>"

    archive = " · ".join(f'<a href="archive/{d}.html">{d}</a>' for d in archive_dates) or "없음"

    weekday = "월화수목금토일"[now.weekday()]
    return PAGE.format(
        date_label=now.strftime(f"%Y년 %m월 %d일 ({weekday})"),
        hours=hours,
        generated=now.strftime("%H:%M"),
        stats="".join(stats),
        keywords=kw_html,
        tabs="".join(tabs),
        columns="".join(columns),
        errors=err_html,
        archive=archive,
    )


def main():
    ap = argparse.ArgumentParser(description="경제·사회 아침 뉴스 브리핑 생성")
    ap.add_argument("--hours", type=int, default=24, help="최근 몇 시간 기사까지 모을지 (기본 24)")
    ap.add_argument("--out", default=str(HERE), help="결과를 저장할 폴더")
    ap.add_argument("--feeds", default=str(HERE / "feeds.json"), help="RSS 목록 파일")
    ap.add_argument("--keep", type=int, default=14, help="보관할 지난 브리핑 일수")
    args = ap.parse_args()

    feeds = json.loads(Path(args.feeds).read_text(encoding="utf-8"))
    articles, errors = collect(feeds, args.hours)
    now = datetime.now(KST)

    out = Path(args.out)
    archive_dir = out / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 오래된 보관본 정리
    today = now.strftime("%Y-%m-%d")
    dates = sorted({p.stem for p in archive_dir.glob("*.html")} | {today}, reverse=True)
    for old in dates[args.keep:]:
        (archive_dir / f"{old}.html").unlink(missing_ok=True)
    past = [d for d in dates[: args.keep] if d != today]

    page = render(articles, errors, args.hours, now, past)
    (out / "index.html").write_text(page, encoding="utf-8")
    (archive_dir / f"{today}.html").write_text(page.replace('href="archive/', 'href="'), encoding="utf-8")
    (out / "latest.json").write_text(json.dumps(
        {"generated": now.isoformat(), "errors": errors,
         "articles": [{**a, "published": a["published"].isoformat() if a["published"] else None}
                      for a in articles]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"기사 {len(articles)}건 수집, 실패한 피드 {len(errors)}개")
    for err in errors:
        print("  -", err, file=sys.stderr)
    if not articles:
        sys.exit("기사를 하나도 가져오지 못했습니다.")


if __name__ == "__main__":
    main()
