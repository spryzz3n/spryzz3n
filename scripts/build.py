"""Builds every image in assets/ for the profile README.

Run it with:  python scripts/build.py
Needs:        pip install fonttools
Optional:     GITHUB_TOKEN in the environment for higher GitHub API limits.

Text, links and repos come from scripts/config.json. Live data (repos and
GitHub stats) is fetched on every run. If a
source fails, its image is left as it was, so the README never shows a broken
card, just the last good one.
"""
import base64, datetime as dt, io, json, os, re, sys, traceback
import urllib.request
from collections import Counter
from xml.sax.saxutils import escape

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, 'assets')
CFG = json.load(open(os.path.join(HERE, 'config.json'), encoding='utf8'))
UA = f'{CFG["github"]}-profile-readme'

NAVY, SKY, SKY2, MUTED, FAINT = '#0f2a3d', '#1a7cc4', '#2b9be0', '#52677a', '#7b8fa0'
LINE, PILL_LINE, BAND, BAND_LINE, RULE = '#d6e6f3', '#bcd8ee', '#f1f7fd', '#e1edf7', '#cfe4f4'
HEAT = ['#eef4f9', '#cfe6f7', '#94c9ee', '#4fa9e4', '#1a7cc4']
LANG = ['#1a7cc4', '#2b9be0', '#4fa9e4', '#94c9ee', '#cfe6f7', '#e4eef6']
W, P = 840, 26          # full-width image size and card padding
HALF = 412              # card width for 2-across rows

# ------------------------------------------------------------------ fonts & drawing
FONTS = {
    's4': ('KSans', 400, 'ibm-plex-sans-400.woff'),
    's5': ('KSans', 500, 'ibm-plex-sans-500.woff'),
    'm4': ('KMono', 400, 'ibm-plex-mono-400.woff'),
    'm5': ('KMono', 500, 'ibm-plex-mono-500.woff'),
}
_tt = {k: TTFont(os.path.join(HERE, 'fonts', f)) for k, (_, _, f) in FONTS.items()}


def width(text, font, size, ls=0.0):
    f = _tt[font]
    cmap, hmtx = f.getBestCmap(), f['hmtx']
    w = sum(hmtx[cmap.get(ord(c), cmap[ord('?')])][0] for c in text)
    return w * size / f['head'].unitsPerEm + ls * size * len(text)


def fit(text, font, size, max_w):
    """Trims text with an ellipsis so it fits in max_w pixels."""
    if width(text, font, size) <= max_w:
        return text
    while text and width(text + '…', font, size) > max_w:
        text = text[:-1]
    return text.rstrip() + '…'


def icon(name, x, y, size, color, sw=2):
    src = open(os.path.join(HERE, 'icons', name + '.svg'), encoding='utf8').read()
    body = src[src.index('>', src.index('<svg')) + 1:src.rindex('</svg>')]
    body = re.sub(r'<path stroke="none"[^>]*/>', '', body).strip()
    return (f'<g transform="translate({x:.2f} {y:.2f}) scale({size / 24:.4f})" fill="none" '
            f'stroke="{color}" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round">{body}</g>')


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.title, self.parts, self.used, self.css = w, h, title, [], {}, ''

    def text(self, x, y, s, font='s4', size=14, fill=NAVY, ls=0.0, anchor=None, extra=''):
        s = str(s)
        self.used.setdefault(font, set()).update(s)
        fam, wt, _ = FONTS[font]
        attrs = f' letter-spacing="{ls * size:.2f}"' if ls else ''
        attrs += f' text-anchor="{anchor}"' if anchor else ''
        self.parts.append(f'<text x="{x:.2f}" y="{y:.2f}" font-family="{fam}" font-weight="{wt}" '
                          f'font-size="{size}" fill="{fill}"{attrs}{extra}>{escape(s)}</text>')

    def add(self, s):
        self.parts.append(s)

    def _fonts(self):
        out = []
        for key, chars in self.used.items():
            fam, wt, file = FONTS[key]
            opts = subset.Options()
            opts.flavor = 'woff'
            opts.layout_features = ['kern', 'liga']
            font = TTFont(os.path.join(HERE, 'fonts', file), recalcTimestamp=False)  # keep output stable
            sub = subset.Subsetter(opts)
            sub.populate(text=''.join(sorted(chars)) + ' ')
            sub.subset(font)
            buf = io.BytesIO()
            font.flavor = 'woff'
            font.save(buf)
            b64 = base64.b64encode(buf.getvalue()).decode()
            out.append(f"@font-face{{font-family:'{fam}';font-weight:{wt};"
                       f"src:url(data:font/woff;base64,{b64}) format('woff')}}")
        return ''.join(out)

    def save(self, name):
        doc = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
               f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{escape(self.title)}">'
               f'<title>{escape(self.title)}</title><style>{self._fonts()}{self.css}</style>'
               + ''.join(self.parts) + '</svg>')
        with open(os.path.join(OUT, name), 'w', encoding='utf8', newline='\n') as fh:
            fh.write(doc)
        print(f'  wrote {name} ({len(doc) // 1024} KB)')


def card(svg, w, h, r=12):
    svg.add(f'<rect x=".25" y=".25" width="{w - .5}" height="{h - .5}" rx="{r}" fill="#fff" '
            f'stroke="{LINE}" stroke-width="0.5"/>')


def label(svg, x, y, s, anchor=None):
    svg.text(x, y, s, 'm4', 11, SKY, ls=0.14, anchor=anchor)


def stat_row(svg, x, y, items, gap=16):
    """Big number with a small caption under it, laid out left to right."""
    for value, caption in items:
        svg.text(x, y, value, 's5', 19)
        svg.text(x, y + 18, caption, 's4', 11, MUTED)
        x += max(width(value, 's5', 19), width(caption, 's4', 11)) + gap


def fmt(n):
    return f'{n:,}'


# ------------------------------------------------------------------ fetching
def get(url, token=False, raw=False):
    headers = {'User-Agent': UA}
    if token and os.environ.get('GITHUB_TOKEN'):
        headers['Authorization'] = f'Bearer {os.environ["GITHUB_TOKEN"]}'
    if 'api.github.com' in url:
        headers['Accept'] = 'application/vnd.github+json'
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        data = r.read()
    return data if raw else data.decode('utf8')


def gh(path):
    return json.loads(get('https://api.github.com' + path, token=True))


def contributions(user):
    """Daily contribution counts for the last year, from the public calendar."""
    page = get(f'https://github.com/users/{user}/contributions')
    days = {}
    for m in re.finditer(r'<td[^>]*?data-date="([\d-]+)"[^>]*?id="([^"]+)"[^>]*?data-level="(\d)"', page):
        days[m.group(2)] = [m.group(1), 0, int(m.group(3))]
    for m in re.finditer(r'<tool-tip[^>]*?for="([^"]+)"[^>]*>(\d[\d,]*) contributions? on', page):
        if m.group(1) in days:
            days[m.group(1)][1] = int(m.group(2).replace(',', ''))
    if not days:
        raise RuntimeError('contribution calendar not found')
    return sorted((dt.date.fromisoformat(d), c, lv) for d, c, lv in days.values())


# ------------------------------------------------------------------ static pieces
def build_hero():
    pills, px, py = [], P, 0
    for ic, name in CFG['stack']:
        pw = 12 + 15 + 6 + width(name, 's4', 13) + 12
        if px + pw > W - P:
            px, py = P, py + 32
        pills.append((px, py, pw, ic, name))
        px += pw + 6

    band_h = 132
    by = band_h + 33                      # FOCUS / STUDYING labels
    stack_y = by + 54                     # CORE STACK label
    pill_top = stack_y + 12
    H = pill_top + py + 26 + 26

    h = Svg(W, H, f'{CFG["name"]}, {CFG["role"]}')
    h.add(f'<defs><clipPath id="c"><rect width="{W}" height="{H}" rx="14"/></clipPath></defs>'
          f'<g clip-path="url(#c)"><rect width="{W}" height="{H}" fill="#fff"/>'
          f'<rect width="{W}" height="{band_h}" fill="{BAND}"/>'
          f'<rect y="{band_h - .5}" width="{W}" height="0.5" fill="{BAND_LINE}"/></g>'
          f'<rect x=".25" y=".25" width="{W - .5}" height="{H - .5}" rx="14" fill="none" '
          f'stroke="{LINE}" stroke-width="0.5"/>')
    h.text(P, 58, CFG['name'], 's5', 30, NAVY, ls=-0.01)
    h.text(P, 86, CFG['role'], 's4', 16, SKY)

    # Typing line: a clip rect per phrase, stepped through per-character widths.
    TYPE, DEL, HOLD, GAP = 0.07, 0.03, 1.6, 0.3
    lines, cw, frames, t = CFG['typing'], width('a', 'm4', 13), [], 0.0
    for i, s in enumerate(lines):
        for c in range(1, len(s) + 1):
            frames.append((t, i, c)); t += TYPE
        t += HOLD - TYPE
        for c in range(len(s) - 1, -1, -1):
            frames.append((t, i, c)); t += DEL
        t += GAP
    total, ty, tx = t, 116, P
    kt = ';'.join(f'{f[0] / total:.5f}' for f in frames)
    anim = f'dur="{total:.2f}s" repeatCount="indefinite" calcMode="discrete" keyTimes="{kt}"'
    clips = ''.join(
        f'<clipPath id="t{i}"><rect x="{tx}" y="{ty - 14}" height="20" width="0"><animate attributeName="width" '
        f'{anim} values="{";".join(f"{(f[2] * cw if f[1] == i else 0):.2f}" for f in frames)}"/></rect></clipPath>'
        for i in range(len(lines)))
    h.add(f'<defs>{clips}</defs>')
    for i, s in enumerate(lines):
        h.text(tx, ty, s, 'm4', 13, SKY2, extra=f' clip-path="url(#t{i})"')
    h.add(f'<rect x="{tx + 2}" y="{ty - 12}" width="2" height="15" fill="{SKY2}">'
          f'<animate attributeName="x" {anim} values="{";".join(f"{tx + f[2] * cw + 2:.2f}" for f in frames)}"/>'
          f'<animate attributeName="opacity" dur="1s" repeatCount="indefinite" calcMode="discrete" '
          f'keyTimes="0;0.5" values="1;0"/></rect>')

    col2 = P + (W - 2 * P - 18) / 2 + 18
    label(h, P, by, 'FOCUS')
    h.text(P, by + 26, CFG['focus'], 's4', 14)
    label(h, col2, by, 'STUDYING')
    h.text(col2, by + 26, CFG['studying'], 's4', 14)

    label(h, P, stack_y, 'CORE STACK')
    for x, y, pw, ic, name in pills:
        y += pill_top
        h.add(f'<rect x="{x + .25:.2f}" y="{y + .25}" width="{pw - .5:.2f}" height="25.5" rx="12.75" '
              f'fill="#fff" stroke="{PILL_LINE}" stroke-width="0.5"/>')
        h.add(icon(ic, x + 12, y + 5.5, 15, SKY))
        h.text(x + 33, y + 17.5, name, 's4', 13)
    h.save('hero.svg')


def build_button(name, ic, text, filled):
    fill, stroke, color, font = ('#fff', SKY2, SKY, 's5') if not filled else (SKY2, SKY2, '#fff', 's5')
    bw = round(14 + 15 + 6 + width(text, font, 13) + 14)
    b = Svg(bw, 34, text)
    b.add(f'<rect x=".25" y=".25" width="{bw - .5}" height="33.5" rx="7" fill="{fill}" '
          f'stroke="{stroke}" stroke-width="0.5"/>')
    b.add(icon(ic, 14, 9.5, 15, color))
    b.text(35, 21.5, text, font, 13, color)
    b.save(name)


def build_section(name, title):
    s = Svg(W, 34, title)
    # No card behind these, so use colors that read on GitHub's light and dark themes.
    s.text(0, 22, title, 's5', 18, SKY2)
    x = width(title, 's5', 18) + 10
    s.add(f'<rect x="{x:.2f}" y="16" width="{W - x:.2f}" height="1" fill="{SKY2}" fill-opacity="0.35"/>')
    s.save(name)


def build_footer():
    f = Svg(W, 20, CFG['footer'])
    f.text(0, 14, CFG['footer'], 'm4', 11, FAINT)
    f.save('footer.svg')


# ------------------------------------------------------------------ live: repos
def build_repos():
    user = CFG['github']
    ok = True
    for i, name in enumerate(CFG['repos'], 1):
        full = name if '/' in name else f'{user}/{name}'   # "owner/repo" for repos you contribute to
        own = full.split('/')[0].lower() == user.lower()
        try:
            r = gh(f'/repos/{full}')
        except Exception as e:
            ok = False
            print(f'  ! repo {name}: {e}', file=sys.stderr)
            if os.path.exists(os.path.join(OUT, f'repo-{i}.svg')) or getattr(e, 'code', None) != 404:
                continue  # keep the last good card; only a missing repo gets a placeholder
            # Not public yet: show a placeholder until it is.
            r = {'name': full.split('/')[1], 'description': 'Coming soon', 'language': None,
                 'stargazers_count': 0, 'pushed_at': None}
        c = Svg(HALF, 100, f'{r["name"]}: {r["description"] or "repository"}')
        card(c, HALF, 100)
        c.add(icon('book-2', 16, 14, 16, SKY))
        tag_w = 0
        if not own:
            tag = 'contributor'
            tag_w = width(tag, 'm4', 10) + 16
            c.add(f'<rect x="{HALF - 16 - tag_w:.2f}" y="12.25" width="{tag_w:.2f}" height="19.5" rx="9.75" '
                  f'fill="{BAND}" stroke="{PILL_LINE}" stroke-width="0.5"/>')
            c.text(HALF - 16 - tag_w / 2, 25.5, tag, 'm4', 10, SKY, anchor='middle')
        title = r['name'] if own else f"{full.split('/')[0]}/{r['name']}"
        c.text(38, 27.5, fit(title, 's5', 14, HALF - 54 - tag_w - 8), 's5', 14)
        desc = CFG.get('repo_descriptions', {}).get(name) or CFG.get('repo_descriptions', {}).get(full) or r['description'] or 'No description yet'
        c.text(16, 52, fit(desc, 's4', 12, HALF - 32), 's4', 12, MUTED)
        x = 16
        if r['language']:
            c.add(f'<circle cx="{x + 4}" cy="78" r="4" fill="{SKY2}"/>')
            c.text(x + 13, 82, r['language'], 'm4', 11, SKY)
            x += 13 + width(r['language'], 'm4', 11) + 16
        c.add(icon('star', x, 72, 13, FAINT, 2))
        c.text(x + 17, 82, fmt(r['stargazers_count']), 'm4', 11, MUTED)
        if r['pushed_at']:
            updated = dt.datetime.fromisoformat(r['pushed_at'].replace('Z', '+00:00'))
            c.text(HALF - 16, 82, f'updated {updated:%b %Y}', 'm4', 11, FAINT, anchor='end')
        c.save(f'repo-{i}.svg')
    if not ok:
        raise RuntimeError('some repos could not be fetched')


# ------------------------------------------------------------------ live: GitHub stats
def streaks(days):
    longest = cur = 0
    for _, n, _ in days:
        cur = cur + 1 if n else 0
        longest = max(longest, cur)
    return longest


def build_stats(days):
    user = CFG['github']
    repos = [r for r in gh(f'/users/{user}/repos?per_page=100&type=owner')
             if not r['fork'] and r['name'].lower() != user.lower()]
    langs = Counter()
    for r in repos:
        langs.update(gh(f'/repos/{user}/{r["name"]}/languages'))
    commits = gh(f'/search/commits?q=author:{user}&per_page=1')['total_count']

    metrics = [
        ('activity', fmt(sum(n for _, n, _ in days)), 'contributions, last year'),
        ('git-commit', fmt(commits), 'public commits'),
        ('book-2', fmt(len(repos)), 'public repos'),
        ('calendar-event', fmt(sum(1 for _, n, _ in days if n)), 'active days'),
        ('flame', fmt(streaks(days)), 'longest streak (days)'),
    ]
    H = 196
    s = Svg(W, H, 'GitHub stats: ' + ', '.join(f'{v} {l}' for _, v, l in metrics))
    card(s, W, H, 14)
    col = (W - 2 * P) / len(metrics)
    for i, (ic, value, cap) in enumerate(metrics):
        x = P + i * col
        s.add(icon(ic, x, 24, 16, SKY))
        s.text(x, 66, value, 's5', 24)
        s.text(x, 86, cap, 's4', 11, MUTED)
        if i:
            s.add(f'<rect x="{x - 14:.2f}" y="26" width="0.5" height="60" fill="{RULE}"/>')

    label(s, P, 124, 'TOP LANGUAGES')
    total = sum(langs.values()) or 1
    top = langs.most_common(5)
    rest = total - sum(b for _, b in top)
    if rest > 0:
        top.append(('Other', rest))
    bar_w, x = W - 2 * P, P
    s.add(f'<defs><clipPath id="bar"><rect x="{P}" y="136" width="{bar_w}" height="10" rx="5"/></clipPath></defs>')
    s.css = ('@keyframes grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}'
             f'.bar{{transform-origin:{P}px 0;animation:grow 1.2s cubic-bezier(.2,.7,.2,1) both}}')
    segs = ''
    for i, (_, b) in enumerate(top):
        w = bar_w * b / total
        segs += f'<rect x="{x:.2f}" y="136" width="{w + .5:.2f}" height="10" fill="{LANG[i]}"/>'
        x += w
    s.add(f'<g clip-path="url(#bar)"><rect x="{P}" y="136" width="{bar_w}" height="10" fill="{HEAT[0]}"/>'
          f'<g class="bar">{segs}</g></g>')
    x = P
    for i, (name, b) in enumerate(top):
        pct = f'{100 * b / total:.1f}%'
        s.add(f'<circle cx="{x + 4}" cy="170" r="4" fill="{LANG[i]}"' +
              (f' stroke="{PILL_LINE}" stroke-width="0.5"' if i >= 4 else '') + '/>')
        s.text(x + 13, 174, name, 's4', 12)
        nx = x + 13 + width(name, 's4', 12) + 5
        s.text(nx, 174, pct, 'm4', 11, FAINT)
        x = nx + width(pct, 'm4', 11) + 20
    s.save('stats.svg')


def smooth(points, floor):
    """Catmull-Rom spline through the points, as an SVG path. Control points never go below floor."""
    d = f'M{points[0][0]:.2f},{points[0][1]:.2f}'
    for i in range(len(points) - 1):
        p0, p1, p2 = points[max(i - 1, 0)], points[i], points[i + 1]
        p3 = points[min(i + 2, len(points) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, min(floor, p1[1] + (p2[1] - p0[1]) / 6))
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, min(floor, p2[1] - (p3[1] - p1[1]) / 6))
        d += f' C{c1[0]:.2f},{c1[1]:.2f} {c2[0]:.2f},{c2[1]:.2f} {p2[0]:.2f},{p2[1]:.2f}'
    return d


def build_activity(days):
    """Weekly contribution graph. The daily heatmap is left to GitHub's own profile."""
    weeks = []
    for d, n, _ in days:
        if not weeks or d.weekday() == 6:   # weeks start on Sunday, like GitHub's
            weeks.append([])
        weeks[-1].append((d, n))

    chart_top, chart_h = 58, 96
    base = chart_top + chart_h
    H = base + 38
    plot_w = W - 2 * P
    weekly = [sum(n for _, n in wk) for wk in weeks]
    peak = max(weekly) or 1

    a = Svg(W, H, f'Weekly contributions over the last 12 months, peaking at {peak}')
    card(a, W, H, 14)
    a.css = ('@keyframes in{from{opacity:0}to{opacity:1}}'
             '@keyframes draw{to{stroke-dashoffset:0}}'
             '.ln{stroke-dasharray:1;stroke-dashoffset:1;animation:draw 2.4s ease-out .3s both}'
             '.ar{animation:in 1.2s ease-out 1.3s both}')
    label(a, P, 30, 'WEEKLY CONTRIBUTIONS')

    zero = base - 6
    pts = [(P + i * plot_w / (len(weekly) - 1), zero - (chart_h - 12) * v / peak) for i, v in enumerate(weekly)]
    line = smooth(pts, zero)
    a.add(f'<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
          f'<stop offset="0" stop-color="{SKY2}" stop-opacity=".28"/>'
          f'<stop offset="1" stop-color="{SKY2}" stop-opacity="0"/></linearGradient></defs>')
    for k in (0.5, 1):
        y = zero - (chart_h - 12) * k
        a.add(f'<rect x="{P}" y="{y:.2f}" width="{plot_w}" height="0.5" fill="{BAND_LINE}"/>')
    a.add(f'<rect x="{P}" y="{base}" width="{plot_w}" height="0.5" fill="{RULE}"/>')
    a.add(f'<path class="ar" d="{line} L{pts[-1][0]:.2f},{base} L{pts[0][0]:.2f},{base} Z" fill="url(#g)"/>')
    a.add(f'<path class="ln" pathLength="1" d="{line}" fill="none" stroke="{SKY2}" stroke-width="2" '
          f'stroke-linecap="round"/>')
    if max(weekly):
        px, py = pts[weekly.index(max(weekly))]
        a.add(f'<circle class="ar" cx="{px:.2f}" cy="{py:.2f}" r="3.5" fill="#fff" stroke="{SKY}" stroke-width="2"/>')
        tx = min(max(px, P + 30), W - P - 30)   # keep the label inside the card
        a.text(tx, py - 9, f'{max(weekly)} peak', 'm4', 10, SKY, anchor='middle')
    a.text(P, base + 20, f'{weeks[0][0][0]:%b %d, %Y}', 's4', 10, FAINT)
    a.text(W - P, base + 20, f'{weeks[-1][0][0]:%b %d, %Y}', 's4', 10, FAINT, anchor='end')
    a.save('activity.svg')


# ------------------------------------------------------------------ main
def step(name, fn, *args):
    try:
        fn(*args)
        return True
    except Exception:
        print(f'  ! {name} failed, keeping the previous image', file=sys.stderr)
        traceback.print_exc()
        return False


def main():
    os.makedirs(OUT, exist_ok=True)
    print('static')
    build_hero()
    build_button('btn-thm.svg', 'flag', 'TryHackMe', True)
    build_button('btn-ptd.svg', 'swords', 'PwnTillDawn', False)
    build_button('btn-cylabs.svg', 'school', 'CyLab Academy', False)
    for name, title in (('projects', 'Projects'), ('github', 'GitHub stats')):
        build_section(f'sec-{name}.svg', title)
    build_footer()

    print('live')
    ok = step('repos', build_repos)
    try:
        days = contributions(CFG['github'])
        ok &= step('stats', build_stats, days)
        ok &= step('activity', build_activity, days)
    except Exception:
        traceback.print_exc()
        ok = False
    print('done' if ok else 'done, with some sources skipped')


if __name__ == '__main__':
    main()
