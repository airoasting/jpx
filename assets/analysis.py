"""결산단신 여러 장을 이어서 계산한다. 가이던스 이력, 진척률 기준선, 3개월 분리, 밸류에이션.

규칙 (docs/DESIGN.md 7절)
- 문서 하나에서 나온 숫자는 kind="reported". 문서 두 개 이상이나 주가를 쓴 숫자는 kind="derived"이고 inputs를 단다.
- 계산할 수 없으면 숫자를 지어내지 않고 refused에 이유를 남긴다.
- 금액 단위는 백만 엔(공시 표와 같다). 한국어 표기는 ko_yen.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Any, Callable

FLOW_KEYS = ("revenue", "ordinary_revenue", "operating_income", "ordinary_income", "pretax_income", "net_income")
LABEL = {"revenue": "매출", "ordinary_revenue": "경상수익", "operating_income": "영업이익",
         "ordinary_income": "경상이익", "pretax_income": "세전이익", "net_income": "순이익(지배주주)"}


# ── 표기 ───────────────────────────────────────────────────────────────────

def ko_yen(million: float | int | None) -> str | None:
    """백만 엔 → '9,660억 엔', '26조 9,625억 엔', '-1,453억 엔', '168백만 엔'."""
    if million is None:
        return None
    sign = "-" if million < 0 else ""
    m = abs(float(million))
    if m < 100:
        return f"{sign}{m:,.0f}백만 엔"
    oku = round(m / 100)
    cho, rest = divmod(oku, 10000)
    if cho and rest:
        return f"{sign}{cho:,}조 {rest:,}억 엔"
    if cho:
        return f"{sign}{cho:,}조 엔"
    return f"{sign}{oku:,}억 엔"


def pct(a: float | None, b: float | None, digits: int = 1) -> float | None:
    """a ÷ b × 100. b가 0 이하이거나 없으면 None."""
    if a is None or b is None or b <= 0:
        return None
    return round(a / b * 100, digits)


def growth(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b <= 0:
        return None
    return round((a / b - 1) * 100, 1)


# ── 기간 ───────────────────────────────────────────────────────────────────

def _add_months(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return dt.date(y, m, min(d.day, last))


def fiscal_period(fye: str, quarter: int) -> dict:
    """결산일과 분기 → 회계연도 기간과 누적 기간. 예: 2027-03-31, 2 → 2026-04-01~2027-03-31, 누적 2026-04-01~2026-09-30."""
    end = dt.date.fromisoformat(fye)
    start = _add_months(end + dt.timedelta(days=1), -12)
    months = 12 if quarter == 4 else quarter * 3
    cum_end = _add_months(start, months) - dt.timedelta(days=1)
    q_label = "연간" if quarter == 4 else ("상반기(中間期)" if quarter == 2 else f"{quarter}분기")
    return {"fiscal_year_label": f"{end.year}년 {end.month}월기", "fiscal_year_ja": f"{end.year}年{end.month}月期",
            "fiscal_year_start": start.isoformat(), "fiscal_year_end": end.isoformat(),
            "cumulative_start": start.isoformat(), "cumulative_end": cum_end.isoformat(),
            "cumulative_months": months, "quarter": quarter, "quarter_label": q_label,
            "plain": f"{end.year}년 {end.month}월기({start.year}.{start.month}~{end.year}.{end.month})"
                     f" {q_label} 누적 {months}개월({start.year}.{start.month}~{cum_end.year}.{cum_end.month})"}


_ERA = {"令和": 2018, "平成": 1988}


def title_period(title: str) -> tuple[int, int, int] | None:
    """공시 제목 → (결산 연도, 결산 월, 분기 1~4). 예: '2027年3月期 第2四半期（中間期）決算短信' → (2027, 3, 2)."""
    t = unicodedata.normalize("NFKC", title)
    m = re.search(r"(令和|平成)?\s*(\d{1,4})\s*年\s*(\d{1,2})\s*月期", t)
    if not m:
        return None
    year = int(m.group(2)) + _ERA.get(m.group(1) or "", 0)
    month = int(m.group(3))
    q = re.search(r"第\s*([1-3])\s*四半期", t)
    quarter = int(q.group(1)) if q else (2 if "中間" in t else 4)
    return year, month, quarter


def fye_of(year: int, month: int) -> str:
    """결산 연월 → 그 달 말일."""
    return (_add_months(dt.date(year, month, 1), 1) - dt.timedelta(days=1)).isoformat()


# ── 문서 고르기 ────────────────────────────────────────────────────────────

def tanshin_slots(items: list[dict]) -> dict[tuple[str, int], dict]:
    """색인 → {(결산일, 분기): 색인 항목}.

    같은 기간에 단신이 여럿이면
    - 정정(訂正) 공시가 있으면 가장 늦은 정정본을 정본으로 하고 나머지를 supersedes에 남긴다.
    - 정정이 없으면 처음 공시한 판을 쓴다. 감사인 검토 완료 판(期中レビューの完了)처럼 같은 숫자를 다시 낸 판은
      '이번 분기 예상 수정' 표시가 무(無)로 바뀌어 수정 사실을 놓치게 된다(캐논 2026년 3분기 실측).
    """
    slots: dict[tuple[str, int], list[dict]] = {}
    for it in items:
        if not it.get("xbrl_file") or not it["kind"].startswith("tanshin"):
            continue
        tp = title_period(it["title"])
        if not tp:
            continue
        slots.setdefault((fye_of(tp[0], tp[1]), tp[2]), []).append(it)
    out = {}
    for k, group in slots.items():
        group = sorted(group, key=lambda x: (x.get("published") or "", x.get("id") or ""))
        fixes = [g for g in group if g["kind"].endswith("_correction")]
        originals = [g for g in group if not g["kind"].endswith("_correction")]
        best = dict(fixes[-1] if fixes else group[0])
        if len(group) > 1:
            best["supersedes" if fixes else "other_versions"] = [g["xbrl_file"] for g in group
                                                                 if g["xbrl_file"] != best["xbrl_file"]]
        if fixes:
            # 정정 zip에는 고친 첨부만 있고 요약이 없을 수 있다(미쓰비시UFJ 2026년 3분기 실측). 그때 쓸 원본.
            best["fallbacks"] = [dict(g) for g in fixes[-2::-1] + originals]
        out[k] = best
    return out


def revision_items(items: list[dict]) -> list[dict]:
    return [it for it in items if it.get("xbrl_file") and it["kind"].startswith("earnings_forecast_revision")]


# ── 결과 행 ────────────────────────────────────────────────────────────────

def money_row(key: str, value: float | None, **extra: Any) -> dict:
    row = {"label": LABEL.get(key, key), "value": value, "display": ko_yen(value)}
    row.update({k: v for k, v in extra.items() if v is not None})
    return row


def cum_of(doc: dict, key: str) -> float | None:
    r = doc["summary"]["results"].get(key)
    return r.get("current") if r else None


def prior_cum_of(doc: dict, key: str) -> float | None:
    r = doc["summary"]["results"].get(key)
    return r.get("prior") if r else None


def forecast_of(doc: dict, key: str) -> dict | None:
    return doc["summary"]["forecast"]["metrics"].get(key)


def comparable(a: dict, b: dict) -> str | None:
    """두 단신을 이어 계산해도 되는지. 안 되면 이유."""
    sa, sb = a["summary"], b["summary"]
    if sa["gaap"] != sb["gaap"]:
        return f"회계기준이 다릅니다({sa['gaap_label']} ↔ {sb['gaap_label']})"
    if sa["scope"] != sb["scope"]:
        return f"연결 범위가 다릅니다({sa['scope']} ↔ {sb['scope']})"
    return None


def src(doc: dict, links: dict | None = None) -> dict:
    s = doc["source"]
    out = {"title": s.get("title"), "published": s.get("published"), "file": s.get("file"),
           "sha256": s.get("sha256"), "fetched_at": s.get("fetched_at")}
    if links:
        out.update(links)
    return out


def metric_keys(doc: dict) -> list[str]:
    res = doc["summary"]["results"]
    return [k for k in FLOW_KEYS if k in res]


# ── 회사 목표 문구 ─────────────────────────────────────────────────────────

_TARGET_METRIC = (("net_income", r"(親会社株主に帰属する|当社株主に帰属する|親会社の所有者に帰属する)?(当期|中間)?純利益"),
                  ("ordinary_income", r"経常利益"), ("operating_income", r"営業利益"))
_AMOUNT = re.compile(r"([0-9０-９][0-9０-９,，.．]*)\s*(兆|億|百万)\s*円")


def target_from_text(text: str | None) -> dict:
    """예상 대신 목표를 글로 적는 회사(은행 등)의 문구에서 지표별 금액(백만 엔)을 꺼낸다.

    '「親会社株主に帰属する当期純利益」は27,000億円を目標' → {"net_income": 2700000}.
    '목표'·'予想' 문구 안에서 지표 이름 바로 뒤 40자 안의 금액 하나만 받는다. 애매하면 비운다.
    """
    if not text:
        return {}
    t = unicodedata.normalize("NFKC", text)
    if not re.search(r"目標|予想|見通し", t):
        return {}
    out = {}
    for key, pat in _TARGET_METRIC:
        m = re.search(pat, t)
        if not m:
            continue
        hits = _AMOUNT.findall(t[m.end(): m.end() + 40])
        if len(hits) != 1:
            continue
        num, unit = hits[0]
        v = float(num.replace(",", ""))
        out[key] = round(v * {"兆": 1_000_000, "億": 100, "百万": 1}[unit], 3)
    return out


def basis_changed(cur: dict, last_year_same_q: dict | None, key: str, tol_pct: float = 1.0) -> str | None:
    """이번 단신의 전년 열과 작년 같은 분기 단신의 당기 값이 다르면 기준이 바뀐 것(사업 분리, 소급 수정)."""
    if last_year_same_q is None:
        return None
    p, old = prior_cum_of(cur, key), cum_of(last_year_same_q, key)
    if old is None:
        return None
    if p is None:
        return "이번 단신이 전년 값을 싣지 않았다(기준 변경 가능)"
    g = growth(p, old) if old > 0 else None
    if g is not None and abs(g) > tol_pct:
        return f"이번 단신의 전년 값이 작년 단신과 {g:+.1f}% 다르다(사업 분리·소급 수정)"
    return None


# ── 3개월 분리 ─────────────────────────────────────────────────────────────

def discrete(cur: dict, prev: dict | None, quarter: int) -> tuple[dict, list[dict]]:
    """이번 누적 − 직전 누적 = 이번 분기 3개월치. 1분기는 누적이 곧 3개월치다."""
    out, refused = {}, []
    if quarter == 1:
        for k in metric_keys(cur):
            out[k] = money_row(k, cum_of(cur, k), kind="reported", note="1분기는 누적이 곧 3개월치")
        return out, refused
    if prev is None:
        return {}, [{"metric": "3개월치", "reason": f"같은 회계연도의 {quarter - 1}분기 누적 단신이 없습니다"}]
    why = comparable(cur, prev)
    if why:
        return {}, [{"metric": "3개월치", "reason": why}]
    for k in metric_keys(cur):
        a, b = cum_of(cur, k), cum_of(prev, k)
        if a is None or b is None:
            refused.append({"metric": LABEL.get(k, k), "reason": "직전 누적 값이 없습니다"})
            continue
        out[k] = money_row(k, round(a - b, 3), kind="derived",
                           note="누적 − 직전 누적. 반올림된 두 값의 차라서 ±1백만 엔 오차가 있을 수 있다")
    return out, refused


# ── 가이던스 이력 ──────────────────────────────────────────────────────────

def forecast_snapshot(doc: dict, keys: list[str], next_year: bool = False) -> dict:
    fc = doc["summary"]["forecast"]
    if next_year != (fc["target"] == "next_fiscal_year"):
        return {}
    out = {}
    for k in keys:
        f = fc["metrics"].get(k)
        if f and (f.get("value") is not None or f.get("lower") is not None):
            out[k] = {"value": f.get("value"), "lower": f.get("lower"), "upper": f.get("upper"),
                      "display": ko_yen(f.get("value"))}
            out[k] = {a: b for a, b in out[k].items() if b is not None}
    return out


def revision_snapshot(rv: dict, keys: list[str], scope: str) -> dict:
    """예상 수정 공시의 연간 블록 → {지표: {previous, new, change_pct}}."""
    blocks = rv["revision"]["blocks"]
    want = f"{'연결' if scope == '연결' else '비연결'}·연간"
    blk = blocks.get(want) or {}
    out = {}
    for k in keys:
        r = blk.get(k)
        if r and r.get("new") is not None:
            out[k] = {"previous": r.get("previous"), "value": r.get("new"), "change_pct": r.get("change_pct"),
                      "display": ko_yen(r.get("new"))}
    return out


def direction(prev: float | None, new: float | None) -> str | None:
    if prev is None or new is None:
        return None
    if new > prev:
        return "상향"
    if new < prev:
        return "하향"
    return "유지"


def progress_row(cum: float | None, fc: dict | None) -> dict | None:
    """진척률 = 누적 ÷ 연간 예상. 범위 예상이면 하한·상한 두 값."""
    if cum is None or not fc:
        return None
    out = {}
    if fc.get("value") is not None:
        out["pct"] = pct(cum, fc["value"])
    if fc.get("lower") is not None and fc.get("upper") is not None:
        out["pct_range"] = [pct(cum, fc["upper"]), pct(cum, fc["lower"])]
    return out or None


def guidance(target_fye: str, slots: dict, revisions: list[dict], load: Callable[[dict], dict],
             years_back: int, links: Callable[[dict], dict]) -> dict:
    """한 회계연도의 예상 이력과 진척률 기준선.

    slots: tanshin_slots 결과. revisions: revision_items. load(item) → parsed 문서.
    """
    refused: list[dict] = []
    inputs: list[dict] = []
    tfy = dt.date.fromisoformat(target_fye)

    def fye_minus(n: int) -> str:
        return fye_of(tfy.year - n, tfy.month)

    def get(fye: str, q: int) -> dict | None:
        it = slots.get((fye, q))
        if not it:
            return None
        try:
            doc = load(it)
        except Exception as e:  # noqa: BLE001  (사본이 없어진 문서 하나 때문에 전체를 멈추지 않는다)
            refused.append({"metric": f"{fye} {q}분기 단신", "reason": f"받지 못함: {e}"})
            return None
        if doc.get("kind") != "tanshin":
            return None
        inputs.append(src(doc, links(it)))
        return doc

    latest_q = max((q for (f, q) in slots if f == target_fye), default=None)
    prior_annual = get(fye_minus(1), 4)
    keys = list(metric_keys(prior_annual)) if prior_annual else []
    timeline: list[dict] = []
    if prior_annual:
        snap = forecast_snapshot(prior_annual, keys, next_year=True)
        if snap:
            timeline.append({"date": prior_annual["source"].get("published"), "event": "기초 예상(期初予想)",
                             "where": "전년 연간 결산단신", "forecast": snap})
    else:
        refused.append({"metric": "기초 예상", "reason": "전년 연간 결산단신이 색인에 없습니다"})

    docs_q: dict[int, dict] = {}
    for q in (1, 2, 3):
        d = get(target_fye, q)
        if d:
            docs_q[q] = d
            if not keys:
                keys = list(metric_keys(d))
            snap = forecast_snapshot(d, keys)
            flag = d["summary"]["flags"].get("forecast_revised_this_quarter")
            timeline.append({"date": d["source"].get("published"), "event": f"{q}분기 결산단신",
                             "revised_flag": flag, "forecast": snap,
                             "cumulative_actual": {k: money_row(k, cum_of(d, k)) for k in keys if cum_of(d, k) is not None}})

    start = (prior_annual or {}).get("source", {}).get("published") or "0000"
    for it in revisions:
        if (it.get("published") or "") < start:
            continue
        try:
            rv = load(it)
        except Exception as e:  # noqa: BLE001
            refused.append({"metric": "예상 수정 공시", "reason": f"{it.get('title')}: {e}"})
            continue
        if rv.get("kind") != "revision" or rv["revision"]["document"].get("fiscal_year_end") != target_fye:
            continue
        scope = (prior_annual or next(iter(docs_q.values()), None) or {"summary": {"scope": "연결"}})["summary"]["scope"]
        snap = revision_snapshot(rv, keys or list(FLOW_KEYS), scope)
        h1 = rv["revision"]["blocks"].get(f"{'연결' if scope == '연결' else '비연결'}·상반기(중간기)")
        inputs.append(src(rv, links(it)))
        timeline.append({"date": it.get("published"), "event": "예상 수정 공시(業績予想の修正)",
                         "forecast": snap, **({"first_half_revision": h1} if h1 and not snap else {}),
                         "reason": rv["revision"]["document"].get("reason")})

    actual_doc = get(target_fye, 4)
    if actual_doc:
        timeline.append({"date": actual_doc["source"].get("published"), "event": "연간 실적",
                         "actual": {k: money_row(k, cum_of(actual_doc, k)) for k in keys if cum_of(actual_doc, k) is not None}})
    timeline.sort(key=lambda r: r.get("date") or "")

    # 단계마다 직전 예상 대비 방향과 변화율, 기초 예상 대비 누적 변화율
    last: dict[str, float] = {}
    first: dict[str, float] = {}
    for row in timeline:
        fc = row.get("forecast") or {}
        moves, chg, cum = {}, {}, {}
        for k, v in fc.items():
            if v.get("value") is None:
                continue
            d = direction(last.get(k), v["value"])
            if d:
                moves[k] = d
                chg[k] = growth(v["value"], last.get(k))
            first.setdefault(k, v["value"])
            if row is not timeline[0]:
                cum[k] = growth(v["value"], first[k])
            last[k] = v["value"]
        if moves:
            row["direction_vs_previous"] = moves
            row["change_pct_vs_previous"] = chg
        if cum:
            row["change_pct_vs_initial"] = cum

    # 진척률과 과거 같은 시점 기준선
    progress = {}
    basis_warning: dict[str, str] = {}
    targets: dict[str, float] = {}
    if latest_q and latest_q in docs_q:
        cur = docs_q[latest_q]
        base_rows = []
        broken: dict[str, str] = {}
        for n in range(1, years_back + 1):
            qd, ad = get(fye_minus(n), latest_q), get(fye_minus(n), 4)
            if not qd or not ad:
                base_rows.append({"fiscal_year_end": fye_minus(n), "missing": "단신이 색인에 없거나 받지 못함"})
                continue
            why = comparable(qd, ad) or comparable(qd, cur)
            if why:
                base_rows.append({"fiscal_year_end": fye_minus(n), "missing": why})
                continue
            newer = cur if n == 1 else get(fye_minus(n - 1), latest_q)
            row = {"fiscal_year_end": fye_minus(n)}
            for k in keys:
                w = basis_changed(newer, qd, k) if newer else None
                if not w and k in broken:
                    w = f"더 최근 해에서 기준이 끊겼다({broken[k]}). 이 해도 옛 기준"
                if w:                      # 이 해의 숫자는 그다음 해 단신이 고쳐 실은 값과 다르다(옛 기준)
                    broken.setdefault(k, fye_minus(n)[:7])
                    row[k] = None
                    row.setdefault("old_basis", {})[k] = w
                else:
                    row[k] = pct(cum_of(qd, k), cum_of(ad, k))
            base_rows.append(row)
        cdoc = cur["summary"].get("document", {})
        targets = target_from_text(cdoc.get("forecast_preamble") or cdoc.get("forecast_note"))
        last_same = get(fye_minus(1), latest_q)
        for k in keys:
            fc = forecast_of(cur, k)
            pr = progress_row(cum_of(cur, k), fc)
            if not pr and k in targets:
                pr = {"pct": pct(cum_of(cur, k), targets[k]), "basis": "회사 목표 문구(숫자 예상 아님). 계산값",
                      "company_target": ko_yen(targets[k])}
            if not pr:
                refused.append({"metric": f"{LABEL.get(k, k)} 진척률(이번 기)",
                                "reason": "숫자로 된 연간 예상이 없거나(未定) 0 이하. 과거 같은 분기 진척률은 아래에 있다"})
                pr = {"pct": None}
            warn = basis_changed(cur, last_same, k)
            if warn:
                basis_warning[k] = warn
            past = [r[k] for r in base_rows if r.get(k) is not None] if not warn else []
            progress[k] = {"label": LABEL.get(k, k), **pr, **({"basis_warning": warn} if warn else {}),
                           "past_same_quarter": [{"fiscal_year_end": r["fiscal_year_end"], "pct": r.get(k),
                                                  **({"old_basis": r["old_basis"][k]} if k in r.get("old_basis", {}) else {})}
                                                 for r in base_rows if "missing" not in r],
                           "past_average_pct": round(sum(past) / len(past), 1) if past else None,
                           "past_n_years": len(past)}
            if warn:
                progress[k]["past_note"] = "기준이 바뀌어 과거 같은 분기 값은 참고로만 두고 평균을 내지 않는다"
        progress["_definition"] = (f"이번 기: {latest_q}분기 누적 ÷ 최신 회사 예상. 과거: 그해 {latest_q}분기 누적 ÷ 그해 연간 실적."
                                   " 과거 값은 당시 문서 그대로이며 이후 소급 수정은 반영하지 않는다.")
        missing_years = [r for r in base_rows if "missing" in r]
        if missing_years:
            progress["_missing_years"] = missing_years

    # 남은 기간 암묵 예상 (연간 예상 − 누적) vs 전년 같은 기간 실적
    implied = {}
    if latest_q and latest_q in docs_q and prior_annual:
        cur = docs_q[latest_q]
        why = comparable(cur, prior_annual)
        if why:
            refused.append({"metric": "남은 기간 암묵 예상", "reason": why})
        else:
            for k in keys:
                fc = forecast_of(cur, k)
                if (not fc or fc.get("value") is None) and k in targets:
                    fc = {"value": targets[k]}          # 은행처럼 목표 문구만 있는 회사
                c, pc, pa = cum_of(cur, k), prior_cum_of(cur, k), cum_of(prior_annual, k)
                if k in basis_warning:
                    refused.append({"metric": f"{LABEL.get(k, k)} 남은 기간 암묵 예상", "reason": basis_warning[k]})
                    continue
                if not fc or fc.get("value") is None or c is None or pc is None or pa is None:
                    continue
                rest, prior_rest = fc["value"] - c, pa - pc
                implied[k] = {"label": LABEL.get(k, k), **({"basis": "회사 목표 문구 기준"} if not forecast_of(cur, k) else {}),
                              "implied_remaining": money_row(k, round(rest, 3))["display"],
                              "implied_remaining_value": round(rest, 3),
                              "prior_year_same_months": ko_yen(prior_rest), "prior_year_same_months_value": round(prior_rest, 3),
                              "implied_yoy_pct": growth(rest, prior_rest)}
            implied["_definition"] = (f"남은 {12 - latest_q * 3}개월 암묵 예상 = 연간 예상 − {latest_q}분기 누적. "
                                      "전년 같은 기간 = 전년 연간 실적(전년 연간 단신) − 전년 같은 누적(이번 단신의 전년 열).")

    # 과거 기초 예상 달성률 (보수성 이력)
    track = []
    for n in range(1, years_back + 1):
        fy = fye_minus(n)
        init_doc, act_doc = get(fye_minus(n + 1), 4), get(fy, 4)
        if not init_doc or not act_doc:
            track.append({"fiscal_year_end": fy, "missing": "기초 예상이나 실적 단신이 없음"})
            continue
        why = comparable(init_doc, act_doc)
        if why:
            track.append({"fiscal_year_end": fy, "missing": why})
            continue
        row: dict[str, Any] = {"fiscal_year_end": fy}
        for k in keys:
            f = forecast_of_next(init_doc, k)
            a = cum_of(act_doc, k)
            if f is not None and a is not None:
                row[k] = {"initial": f, "actual": a, "achievement_pct": pct(a, f)}
        track.append(row)

    for row in track:
        for k, w in basis_warning.items():
            if isinstance(row.get(k), dict):
                row[k]["basis_warning"] = "옛 기준 문서끼리의 달성률. " + w
    return {"target_fiscal_year_end": target_fye, "latest_quarter": latest_q, "metrics": keys,
            "basis_warning": basis_warning or None,
            "timeline": timeline, "progress": progress, "implied_remaining": implied,
            "initial_forecast_track_record": track,
            "kind": "derived", "inputs": inputs, "refused": refused}


def forecast_of_next(doc: dict, key: str) -> float | None:
    fc = doc["summary"]["forecast"]
    if fc["target"] != "next_fiscal_year":
        return None
    f = fc["metrics"].get(key)
    return f.get("value") if f else None


# ── 분기 시계열 ────────────────────────────────────────────────────────────

def quarter_series(fyes: list[str], slots: dict, load: Callable[[dict], dict], links: Callable[[dict], dict]) -> dict:
    rows, refused, inputs = [], [], []
    cache: dict[tuple[str, int], dict | None] = {}

    def get(fye: str, q: int) -> dict | None:
        if (fye, q) in cache:
            return cache[(fye, q)]
        it = slots.get((fye, q))
        doc = None
        if it:
            try:
                doc = load(it)
                if doc.get("kind") == "tanshin":
                    inputs.append(src(doc, links(it)))
                else:
                    doc = None
            except Exception as e:  # noqa: BLE001
                refused.append({"metric": f"{fye} {q}분기", "reason": f"받지 못함: {e}"})
        cache[(fye, q)] = doc
        return doc

    keys: list[str] = []
    for fye in fyes:
        for q in (1, 2, 3, 4):
            cur = get(fye, q)
            if not cur:
                continue
            if not keys:
                keys = metric_keys(cur)
            prev = get(fye, q - 1) if q > 1 else None
            vals, why = {}, None
            if q > 1 and prev is None:
                why = f"{q - 1}분기 누적 단신이 없어 뺄 수 없습니다"
            elif prev is not None:
                why = comparable(cur, prev)
            if why:
                refused.append({"metric": f"{fye} {q}Q", "reason": why})
                continue
            for k in keys:
                a = cum_of(cur, k)
                b = cum_of(prev, k) if prev else 0
                if a is None or b is None:
                    continue
                vals[k] = round(a - b, 3)
            fp = fiscal_period(fye, q)
            q_start = _add_months(dt.date.fromisoformat(fp["fiscal_year_start"]), (q - 1) * 3)
            q_end = _add_months(q_start, 3) - dt.timedelta(days=1)
            rows.append({"fiscal_year_end": fye, "quarter": q, "months": f"{q_start.isoformat()}~{q_end.isoformat()}",
                         "values": vals, "display": {k: ko_yen(v) for k, v in vals.items()},
                         "operating_margin_pct": pct(vals.get("operating_income"), vals.get("revenue")),
                         "kind": "reported" if q == 1 else "derived"})
    # 전년 같은 분기 대비
    by = {(r["fiscal_year_end"], r["quarter"]): r for r in rows}
    for r in rows:
        prev_fye = fye_of(dt.date.fromisoformat(r["fiscal_year_end"]).year - 1,
                          dt.date.fromisoformat(r["fiscal_year_end"]).month)
        p = by.get((prev_fye, r["quarter"]))
        if p:
            cur_doc, old_doc = cache.get((r["fiscal_year_end"], r["quarter"])), cache.get((prev_fye, r["quarter"]))
            yoy, warn = {}, {}
            for k, v in r["values"].items():
                w = basis_changed(cur_doc, old_doc, k) if cur_doc else None
                if w:
                    warn[k] = w
                else:
                    yoy[k] = growth(v, p["values"].get(k))
            r["yoy_pct"] = yoy
            if warn:
                r["yoy_refused"] = warn
    return {"metrics": keys, "rows": rows, "kind": "derived", "inputs": inputs, "refused": refused,
            "note": "일본 단신은 누적만 공시한다. 2~4분기 3개월치는 같은 해 누적끼리 뺀 값이다(±1백만 엔 반올림 오차)."}
