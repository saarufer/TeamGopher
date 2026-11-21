"""生成 README 用的地鼠 logo 与 Agent 头像（纯 SVG，无外部依赖）。

运行：uv run python docs/assets/make_art.py
"""

from pathlib import Path

OUT = Path(__file__).parent

FUR, FUR_DARK, BELLY = "#c98a4b", "#9a6332", "#f3d6ab"
EAR_IN, NOSE, BLUSH = "#f2a7a0", "#6b3b2a", "#f4a1a1"
INK, WHITE = "#2b1d14", "#ffffff"


def head(cx: float, cy: float, s: float = 1.0) -> str:
    """一个圆滚滚的地鼠头，中心 (cx, cy)，s=1 时宽约 120。"""
    def p(x, y):
        return f"{cx + x * s:.1f},{cy + y * s:.1f}"
    r = lambda v: f"{v * s:.1f}"
    X = lambda v: f"{cx + v * s:.1f}"
    Y = lambda v: f"{cy + v * s:.1f}"
    return f"""
  <g>
    <circle cx="{X(-44)}" cy="{Y(-42)}" r="{r(12)}" fill="{FUR}" stroke="{FUR_DARK}" stroke-width="{r(3)}"/>
    <circle cx="{X(44)}" cy="{Y(-42)}" r="{r(12)}" fill="{FUR}" stroke="{FUR_DARK}" stroke-width="{r(3)}"/>
    <circle cx="{X(-44)}" cy="{Y(-42)}" r="{r(6)}" fill="{EAR_IN}"/>
    <circle cx="{X(44)}" cy="{Y(-42)}" r="{r(6)}" fill="{EAR_IN}"/>
    <ellipse cx="{X(0)}" cy="{Y(0)}" rx="{r(60)}" ry="{r(54)}" fill="{FUR}" stroke="{FUR_DARK}" stroke-width="{r(3)}"/>
    <ellipse cx="{X(0)}" cy="{Y(20)}" rx="{r(36)}" ry="{r(28)}" fill="{BELLY}"/>
    <ellipse cx="{X(-22)}" cy="{Y(-8)}" rx="{r(9)}" ry="{r(11)}" fill="{INK}"/>
    <ellipse cx="{X(22)}" cy="{Y(-8)}" rx="{r(9)}" ry="{r(11)}" fill="{INK}"/>
    <circle cx="{X(-19)}" cy="{Y(-12)}" r="{r(3.5)}" fill="{WHITE}"/>
    <circle cx="{X(25)}" cy="{Y(-12)}" r="{r(3.5)}" fill="{WHITE}"/>
    <circle cx="{X(-24)}" cy="{Y(-4)}" r="{r(1.6)}" fill="{WHITE}"/>
    <circle cx="{X(20)}" cy="{Y(-4)}" r="{r(1.6)}" fill="{WHITE}"/>
    <ellipse cx="{X(-36)}" cy="{Y(12)}" rx="{r(9)}" ry="{r(6)}" fill="{BLUSH}" opacity="0.75"/>
    <ellipse cx="{X(36)}" cy="{Y(12)}" rx="{r(9)}" ry="{r(6)}" fill="{BLUSH}" opacity="0.75"/>
    <path d="M{p(-7, 5)} Q{p(0, 1)} {p(7, 5)} Q{p(0, 13)} {p(-7, 5)} Z" fill="{NOSE}"/>
    <path d="M{p(0, 11)} L{p(0, 16)} M{p(-9, 16)} Q{p(-4, 21)} {p(0, 16)} Q{p(4, 21)} {p(9, 16)}"
          fill="none" stroke="{INK}" stroke-width="{r(2.2)}" stroke-linecap="round"/>
    <rect x="{X(-6.5)}" y="{Y(18)}" width="{r(6)}" height="{r(12)}" rx="{r(1.5)}" fill="{WHITE}" stroke="{INK}" stroke-width="{r(1.5)}"/>
    <rect x="{X(0.5)}" y="{Y(18)}" width="{r(6)}" height="{r(12)}" rx="{r(1.5)}" fill="{WHITE}" stroke="{INK}" stroke-width="{r(1.5)}"/>
    <path d="M{p(-16, 10)} L{p(-40, 6)} M{p(-16, 14)} L{p(-40, 17)} M{p(16, 10)} L{p(40, 6)} M{p(16, 14)} L{p(40, 17)}"
          stroke="{FUR_DARK}" stroke-width="{r(1.4)}" stroke-linecap="round" opacity="0.7"/>
  </g>"""


def logo() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" width="240" height="240" role="img" aria-label="地鼠团队 Team Gopher logo">
  <title>Team Gopher 地鼠团队</title>
  <circle cx="120" cy="120" r="116" fill="#fff4e0" stroke="#f0c98f" stroke-width="4"/>
  <!-- 洞口与土堆 -->
  <ellipse cx="120" cy="196" rx="86" ry="22" fill="#a8743f"/>
  <ellipse cx="120" cy="190" rx="62" ry="14" fill="#4a2d17"/>
  <!-- 身体从洞里探出来 -->
  <path d="M68 190 Q66 140 120 132 Q174 140 172 190 Z" fill="{FUR}" stroke="{FUR_DARK}" stroke-width="3"/>
  <path d="M92 190 Q92 156 120 152 Q148 156 148 190 Z" fill="{BELLY}"/>
  {head(120, 104, 1.0)}
  <!-- 小爪子举着一张上涨的图表卡片 -->
  <g transform="rotate(-8 186 128)">
    <rect x="160" y="104" width="54" height="40" rx="7" fill="#ffffff" stroke="#2b1d14" stroke-width="2.5"/>
    <polyline points="167,134 179,126 189,130 205,113" fill="none" stroke="#1baf7a" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M199 112 L206 112 L206 119" fill="none" stroke="#1baf7a" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  </g>
  <ellipse cx="164" cy="150" rx="10" ry="8" fill="{FUR}" stroke="{FUR_DARK}" stroke-width="2.5"/>
  <ellipse cx="78" cy="160" rx="10" ry="8" fill="{FUR}" stroke="{FUR_DARK}" stroke-width="2.5"/>
  <!-- 土块 -->
  <circle cx="44" cy="196" r="6" fill="#8b5a2b"/><circle cx="196" cy="200" r="5" fill="#8b5a2b"/><circle cx="58" cy="208" r="4" fill="#8b5a2b"/>
</svg>
"""


# ---------- Agent 头像 ----------

OLD, NEW = "#2a78d6", "#eb6834"   # 与架构图一致：蓝 = 原有，橙 = 新增


def icon(kind: str, x: float, y: float) -> str:
    """徽章里的小道具，中心 (x, y)，约 24×24。"""
    k = 'stroke="#2b1d14" stroke-linecap="round" stroke-linejoin="round"'
    return {
        "magnifier": f'<circle cx="{x-2}" cy="{y-2}" r="6.5" fill="#d9f0ff" {k} stroke-width="2"/><path d="M{x+3} {y+3} L{x+9} {y+9}" {k} stroke-width="3"/>',
        "bars": f'<rect x="{x-9}" y="{y+1}" width="5" height="8" fill="#1baf7a" {k} stroke-width="2"/><rect x="{x-2.5}" y="{y-4}" width="5" height="13" fill="#eda100" {k} stroke-width="2"/><rect x="{x+4}" y="{y-9}" width="5" height="18" fill="#2a78d6" {k} stroke-width="2"/>',
        "news": f'<rect x="{x-10}" y="{y-8}" width="20" height="16" rx="2" fill="#ffffff" {k} stroke-width="2"/><path d="M{x-6} {y-3} H{x+6} M{x-6} {y+1} H{x+6} M{x-6} {y+5} H{x+2}" {k} stroke-width="1.5"/>',
        "thermo": f'<rect x="{x-3}" y="{y-11}" width="6" height="15" rx="3" fill="#ffffff" {k} stroke-width="2"/><circle cx="{x}" cy="{y+6}" r="5" fill="#e34948" {k} stroke-width="2"/><path d="M{x} {y-4} V{y+4}" stroke="#e34948" stroke-width="3" stroke-linecap="round"/>',
        "pie": f'<circle cx="{x}" cy="{y}" r="10" fill="#ffe7a8" {k} stroke-width="2"/><path d="M{x} {y} L{x} {y-10} A10 10 0 0 1 {x+9.5} {y+3} Z" fill="#1baf7a" {k} stroke-width="2"/>',
        "calendar": f'<rect x="{x-10}" y="{y-8}" width="20" height="18" rx="2.5" fill="#ffffff" {k} stroke-width="2"/><rect x="{x-10}" y="{y-8}" width="20" height="6" rx="2.5" fill="#e34948" {k} stroke-width="2"/><circle cx="{x-4}" cy="{y+3}" r="1.6" fill="#2b1d14"/><circle cx="{x+2}" cy="{y+3}" r="1.6" fill="#2b1d14"/><circle cx="{x+4}" cy="{y+7}" r="1.6" fill="#eb6834"/>',
        "check": f'<rect x="{x-8}" y="{y-10}" width="16" height="20" rx="2.5" fill="#ffffff" {k} stroke-width="2"/><rect x="{x-4}" y="{y-12}" width="8" height="4" rx="1.5" fill="#c98a4b" {k} stroke-width="1.5"/><path d="M{x-4} {y+1} L{x-1} {y+4} L{x+5} {y-3}" fill="none" stroke="#1baf7a" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>',
        "target": f'<circle cx="{x}" cy="{y}" r="10" fill="#ffffff" {k} stroke-width="2"/><circle cx="{x}" cy="{y}" r="6" fill="#e34948" {k} stroke-width="1.5"/><circle cx="{x}" cy="{y}" r="2.2" fill="#ffffff"/>',
        "flag": f'<path d="M{x-7} {y+11} V{y-11}" {k} stroke-width="2.5"/><path d="M{x-6} {y-10} L{x+10} {y-5} L{x-6} {y+1} Z" fill="#e34948" {k} stroke-width="2"/>',
        "shield": f'<path d="M{x} {y-11} L{x+9} {y-7} V{y} Q{x+9} {y+8} {x} {y+11} Q{x-9} {y+8} {x-9} {y} V{y-7} Z" fill="#2a78d6" {k} stroke-width="2"/><path d="M{x-4} {y} L{x-1} {y+3} L{x+5} {y-4}" fill="none" stroke="#ffffff" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>',
        "bolt": f'<path d="M{x+2} {y-12} L{x-7} {y+2} L{x-1} {y+2} L{x-3} {y+12} L{x+7} {y-3} L{x+1} {y-3} Z" fill="#fab219" {k} stroke-width="2"/>',
        "pencil": f'<g transform="rotate(45 {x} {y})"><rect x="{x-3.5}" y="{y-11}" width="7" height="16" fill="#eda100" {k} stroke-width="2"/><path d="M{x-3.5} {y+5} L{x} {y+11} L{x+3.5} {y+5} Z" fill="#f3d6ab" {k} stroke-width="2"/><rect x="{x-3.5}" y="{y-13}" width="7" height="3" fill="#e87ba4" {k} stroke-width="1.5"/></g>',
    }[kind]


AGENTS = [
    ("auditor", "magnifier", OLD), ("rotation", "bars", OLD), ("news", "news", OLD),
    ("regime", "thermo", NEW), ("exposure", "pie", NEW), ("expectations", "calendar", NEW),
    ("postmortem", "check", NEW), ("strategist", "target", OLD), ("red_team", "flag", OLD),
    ("risk", "shield", OLD), ("execution", "bolt", OLD), ("reporter", "pencil", OLD),
]


def avatar(kind: str, ring: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120" width="120" height="120">
  <circle cx="60" cy="60" r="55" fill="#fff4e0" stroke="{ring}" stroke-width="5"/>
  {head(56, 66, 0.62)}
  <circle cx="92" cy="92" r="19" fill="#ffffff" stroke="{ring}" stroke-width="3.5"/>
  {icon(kind, 92, 92)}
</svg>
"""


TEAM = [  # (文件名, 昵称, 正式名)
    ("auditor", "审计鼠", "Auditor"), ("rotation", "轮动鼠", "行业轮动"), ("news", "新闻鼠", "新闻核验"),
    ("regime", "天气鼠", "市场状态"), ("exposure", "仓位鼠", "仓位管理"), ("expectations", "日历鼠", "事件预期"),
    ("postmortem", "复盘鼠", "复盘校准"), ("strategist", "军师鼠", "Strategist"), ("red_team", "杠精鼠", "Red Team"),
    ("risk", "保安鼠", "Risk"), ("execution", "跑腿鼠", "Execution"), ("reporter", "记者鼠", "Reporter"),
]


def team() -> str:
    """12 只地鼠排成两行的“团队合影”，名字直接画在图里。"""
    kinds = {n: (k, r) for n, k, r in AGENTS}
    W, cw, ch = 780, 130, 168
    font = 'font-family="system-ui,-apple-system,\'PingFang SC\',\'Microsoft YaHei\',\'Noto Sans CJK SC\',sans-serif"'
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {2 * ch + 16}" width="{W}" height="{2 * ch + 16}" '
             f'role="img" aria-label="地鼠团队成员">',
             f'<rect width="{W}" height="{2 * ch + 16}" rx="16" fill="#fffaf2" stroke="#f0c98f" stroke-width="2"/>']
    for i, (n, nick, formal) in enumerate(TEAM):
        kind, ring = kinds[n]
        x, y = (i % 6) * cw + 5, (i // 6) * ch + 8
        inner = avatar(kind, ring).split("\n", 1)[1].rsplit("</svg>", 1)[0]
        parts.append(f'<svg x="{x + 10}" y="{y + 6}" width="110" height="110" viewBox="0 0 120 120">{inner}</svg>')
        new = ring == NEW
        parts.append(f'<text x="{x + 65}" y="{y + 136}" text-anchor="middle" {font} font-size="15" font-weight="700" '
                     f'fill="#2b1d14">{nick}{" 🆕" if new else ""}</text>')
        parts.append(f'<text x="{x + 65}" y="{y + 155}" text-anchor="middle" {font} font-size="12" '
                     f'fill="{ring}">{formal}</text>')
    parts.append("</svg>\n")
    return "\n".join(parts)


if __name__ == "__main__":
    (OUT / "gopher_logo.svg").write_text(logo(), encoding="utf-8")
    av = OUT / "avatars"
    av.mkdir(exist_ok=True)
    for name, kind, ring in AGENTS:
        (av / f"{name}.svg").write_text(avatar(kind, ring), encoding="utf-8")
    (OUT / "gopher_team.svg").write_text(team(), encoding="utf-8")
    print("written", len(AGENTS), "avatars + team")
