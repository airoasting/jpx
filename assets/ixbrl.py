"""TDnet 인라인 XBRL(iXBRL) 읽기. 표준 라이브러리만 쓴다.

결산단신(tse-ed-t 요약), 업적 예상 수정(tse-rvfc), 첨부 재무제표(jppfs_cor, jpigp_cor)를 같은 방식으로 읽는다.
도쿄증권거래소는 컨텍스트 ID를 표준 이름으로 쓴다(예: CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember).
그래서 컨텍스트 정의를 따라가지 않고 ID를 토큰으로 쪼개 읽는다.

    facts = parse_zip(zip_bytes)          # [Fact, ...]
    s = tanshin_summary(facts)            # 실적·예상·배당·주식 수를 정리한 dict
"""
from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass, field

# ── 사실(fact) 하나 ─────────────────────────────────────────────────────────

GAAP_SUFFIX = re.compile(r"(IFRS|US|JMIS)$")


@dataclass
class Fact:
    name: str                 # 접두어 없는 요소 이름. 예: NetSales, OperatingIncomeIFRS
    prefix: str               # tse-ed-t, tse-rvfc, jppfs_cor, jpigp_cor ...
    context: str
    value: float | str | None
    numeric: bool
    unit: str | None = None
    file: str = ""
    tokens: list[str] = field(default_factory=list)

    @property
    def period(self) -> str:
        return self.tokens[0] if self.tokens else ""

    def has(self, member: str) -> bool:
        return member in self.tokens


_TAG = re.compile(r"<(/?)ix:(nonFraction|nonNumeric)\b([^>]*?)(/?)>", re.S)
_ATTR = re.compile(r"([\w:.-]+)\s*=\s*\"([^\"]*)\"")
_DASHES = {"", "-", "－", "―", "—", "‐", "△", "▲", "nil"}


def _text(fragment: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", fragment)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"[ \t　]+", " ", html.unescape(s)).strip()


def _number(raw: str, attrs: dict) -> float | None:
    if attrs.get("xsi:nil") == "true":
        return None
    fmt = attrs.get("format", "")
    s = raw.replace(",", "").replace("，", "").replace(" ", "").strip()
    if s in _DASHES:
        return 0.0 if "zero" in fmt else None
    s = s.translate(str.maketrans("０１２３４５６７８９．", "0123456789."))
    try:
        v = float(s)
    except ValueError:
        return None
    scale = attrs.get("scale")
    if scale not in (None, "", "0"):
        v *= 10 ** int(scale)
    if attrs.get("sign") == "-":
        v = -v
    return v


def parse_html(doc: str, file: str = "") -> list[Fact]:
    """iXBRL 문서 하나에서 nonFraction·nonNumeric 사실을 모두 꺼낸다. 자기 닫힘 태그(xsi:nil)와 중첩을 처리한다."""
    out: list[Fact] = []
    stack: list[tuple[str, dict, int]] = []
    for m in _TAG.finditer(doc):
        closing, kind, attr_s, selfclose = m.group(1), m.group(2), m.group(3), m.group(4)
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == kind:
                    k, attrs, start = stack.pop(i)
                    out.append(_fact(k, attrs, doc[start:m.start()], file))
                    break
            continue
        attrs = dict(_ATTR.findall(attr_s))
        if selfclose:
            out.append(_fact(kind, attrs, "", file))
        else:
            stack.append((kind, attrs, m.end()))
    return [f for f in out if f.name]


def _fact(kind: str, attrs: dict, inner: str, file: str) -> Fact:
    prefix, _, name = attrs.get("name", "").rpartition(":")
    ctx = attrs.get("contextRef", "")
    numeric = kind == "nonFraction"
    raw = _text(inner)
    value: float | str | None = _number(raw, attrs) if numeric else (None if attrs.get("xsi:nil") == "true" else raw)
    return Fact(name=name, prefix=prefix, context=ctx, value=value, numeric=numeric,
                unit=attrs.get("unitRef"), file=file, tokens=ctx.split("_") if ctx else [])


def parse_zip(data: bytes) -> list[Fact]:
    """TDnet XBRL zip 전체. 요약(Summary)과 첨부(Attachment), 예상 수정 문서를 모두 읽는다."""
    facts: list[Fact] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for n in z.namelist():
            if n.lower().endswith(("ixbrl.htm", "ixbrl.html")):
                facts += parse_html(z.read(n).decode("utf-8", errors="replace"), n)
    return facts


def kind_of_zip(facts: list[Fact]) -> str:
    """tanshin(결산단신) / revision(업적·배당 예상 수정) / other."""
    files = {f.file.rsplit("/", 1)[-1] for f in facts}
    names = {f.name for f in facts}
    if any("-rvfc-" in n for n in files) or "ReportingDateOfFinancialForecastCorrection" in names:
        return "revision"
    if any("-rvdf-" in n for n in files) or "ReportingDateOfDividendForecastCorrection" in names:
        return "dividend_revision"
    if "tse-ed-t" in {f.prefix for f in facts} and any("FiscalYearEnd" == n for n in names):
        return "tanshin"
    return "other"


# ── 결산단신 요약 정리 ──────────────────────────────────────────────────────

# 표준 지표 → (한국어 이름, 요약 요소의 기본 이름 후보). GAAP 접미어(IFRS, US, JMIS)는 떼고 비교한다.
FLOW = [
    ("revenue", "매출", ("NetSales", "OperatingRevenues", "Sales", "Revenue", "Revenues", "NetRevenues",
                        "TotalRevenues", "OperatingRevenue", "SalesAndOperatingRevenues", "GrossOperatingRevenues")),
    ("ordinary_revenue", "경상수익(은행·보험)", ("OrdinaryRevenues", "OrdinaryRevenuesBK", "OrdinaryRevenuesIN",
                                         "OrdinaryIncomeBK")),
    ("operating_income", "영업이익", ("OperatingIncome", "OperatingProfit", "OperatingIncomeLoss")),
    ("ordinary_income", "경상이익", ("OrdinaryIncome", "OrdinaryProfit", "OrdinaryProfitBK", "OrdinaryProfitIN")),
    ("pretax_income", "세전이익", ("ProfitBeforeTax", "IncomeBeforeIncomeTaxes", "ProfitBeforeIncomeTaxes",
                                  "IncomeFromContinuingOperationsBeforeIncomeTaxes")),
    ("net_income", "순이익(지배주주)", ("ProfitAttributableToOwnersOfParent", "NetIncomeAttributableToOwnersOfParent",
                                    "NetIncomeAttributableToToyotaMotorCorporation", "NetIncome", "Profit")),
    ("eps", "주당순이익(엔)", ("NetIncomePerShare", "BasicEarningsPerShare", "EarningsPerShare")),
    ("eps_diluted", "희석 주당순이익(엔)", ("DilutedNetIncomePerShare", "DilutedEarningsPerShare")),
    ("operating_cf", "영업현금흐름", ("CashFlowsFromOperatingActivities",)),
    ("investing_cf", "투자현금흐름", ("CashFlowsFromInvestingActivities",)),
    ("financing_cf", "재무현금흐름", ("CashFlowsFromFinancingActivities",)),
    ("cash_end", "기말 현금성자산", ("CashAndEquivalentsEndOfPeriod", "CashAndCashEquivalentsAtEndOfPeriod")),
    ("roe", "ROE(%)", ("NetIncomeToShareholdersEquityRatio", "RateOfReturnOnEquity",
                      "ProfitToEquityAttributableToOwnersOfParentRatio")),
    ("payout_ratio", "배당성향(%)", ("PayoutRatio",)),
]
STOCK = [
    ("total_assets", "자산총계", ("TotalAssets",)),
    ("net_assets", "순자산(J-GAAP)", ("NetAssets",)),
    ("total_equity", "자본총계", ("TotalEquity", "Equity")),
    ("owners_equity", "자기자본(지배주주)", ("OwnersEquity", "EquityAttributableToOwnersOfParent",
                                         "ShareholdersEquity", "TotalShareholdersEquity")),
    ("equity_ratio", "자기자본비율(%)", ("CapitalAdequacyRatio", "EquityAttributableToOwnersOfParentToTotalAssetsRatio",
                                     "EquityToAssetRatio", "ShareholdersEquityToTotalAssetsRatio")),
    ("bps", "주당순자산 BPS(엔)", ("NetAssetsPerShare", "EquityAttributableToOwnersOfParentPerShare",
                                  "ShareholdersEquityPerShare")),
]
PCT_NAMES = {"roe", "payout_ratio", "equity_ratio"}


def base_name(name: str) -> str:
    return GAAP_SUFFIX.sub("", name)


def gaap_of(facts: list[Fact]) -> str:
    """요약 요소의 접미어로 회계기준을 판정한다. JP(일본 기준), IFRS, US(미국 기준), JMIS(수정 국제기준)."""
    count = {"IFRS": 0, "US": 0, "JMIS": 0, "JP": 0}
    for f in facts:
        if f.prefix != "tse-ed-t" or not f.numeric or f.value is None:
            continue
        m = GAAP_SUFFIX.search(f.name)
        count[m.group(1) if m else "JP"] += 1
    # 배당·주식 수 요소는 접미어가 없으므로 JP 표가 늘 몇 개 섞인다. 접미어 붙은 요소가 있으면 그쪽이다.
    for g in ("IFRS", "US", "JMIS"):
        if count[g] >= 3:
            return g
    return "JP"


GAAP_LABEL = {"JP": "일본 기준(J-GAAP)", "IFRS": "IFRS", "US": "미국 기준(US GAAP)", "JMIS": "수정 국제기준(JMIS)"}


def _lookup(facts: list[Fact], candidates: tuple[str, ...], pred) -> Fact | None:
    """후보 이름 순서대로, 조건(pred)에 맞고 값이 있는 첫 사실."""
    for c in candidates:
        for f in facts:
            if f.numeric and f.value is not None and base_name(f.name) == c and pred(f):
                return f
    return None


def _change(facts: list[Fact], hit: Fact, pred) -> float | None:
    """같은 컨텍스트의 증감률(ChangeIn…). 회사가 공시한 값 그대로(%)."""
    for prefix in ("ChangeIn", "ChangesIn"):
        want = prefix + hit.name
        for f in facts:
            if f.numeric and f.name == want and f.context == hit.context and f.value is not None:
                return round(f.value * 100, 2)
    return None


def _scope_pred(scope: str):
    return lambda f: f.has(scope)


def _pick_scope(facts: list[Fact]) -> str:
    for scope in ("ConsolidatedMember", "NonConsolidatedMember"):
        if any(f.numeric and f.value is not None and f.has(scope) and f.has("ResultMember") and
               base_name(f.name) in ("NetSales", "OperatingRevenues", "Revenue", "Sales", "OperatingIncome",
                                     "OrdinaryIncome", "ProfitAttributableToOwnersOfParent", "NetIncome",
                                     "OrdinaryRevenues", "TotalAssets")
               for f in facts):
            return scope
    return "ConsolidatedMember"


def _text_of(facts: list[Fact], name: str) -> str | None:
    for f in facts:
        if f.name == name and not f.numeric and f.value:
            return str(f.value)
    return None


def _yen(v: float | None) -> float | None:
    """엔 → 백만 엔. 공시 표의 숫자와 같은 단위로 돌려준다(반올림 없이 원문 그대로 나오도록)."""
    if v is None:
        return None
    m = v / 1_000_000
    return int(m) if abs(m - round(m)) < 1e-6 else round(m, 3)


def _val(metric: str, f: Fact | None) -> float | None:
    if f is None or f.value is None:
        return None
    if metric in PCT_NAMES:
        return round(float(f.value) * 100, 2)
    if f.unit and "PerShare" in f.unit:
        return round(float(f.value), 2)
    return _yen(float(f.value))


def period_tokens(facts: list[Fact]) -> dict:
    """요약에 쓰인 기간 토큰으로 이 단신이 몇 분기 누적인지 판정한다."""
    q = None
    for f in facts:
        if f.name == "QuarterlyPeriod" and f.numeric and f.value is not None:
            q = int(f.value)
    periods = {f.period for f in facts if f.prefix == "tse-ed-t" and f.has("ResultMember")}
    if q is None:
        m = [re.match(r"CurrentAccumulatedQ(\d)Duration", p) for p in periods]
        qs = [int(x.group(1)) for x in m if x]
        q = max(qs) if qs else 4
    if q in (1, 2, 3):
        return {"quarter": q, "cur": f"CurrentAccumulatedQ{q}Duration", "prior": f"PriorAccumulatedQ{q}Duration",
                "cur_i": f"CurrentAccumulatedQ{q}Instant", "prior_i": "PriorYearInstant",
                "fc_period": "CurrentYearDuration", "fc_same_year": True}
    return {"quarter": 4, "cur": "CurrentYearDuration", "prior": "PriorYearDuration",
            "cur_i": "CurrentYearInstant", "prior_i": "PriorYearInstant",
            "fc_period": "NextYearDuration", "fc_same_year": False}


def _metrics(facts: list[Fact], table, scope: str, cur: str, prior: str) -> dict:
    out: dict[str, dict] = {}
    for key, label, names in table:
        def p_cur(f, per=cur):
            return f.prefix == "tse-ed-t" and f.period == per and f.has(scope) and f.has("ResultMember")

        def p_pri(f, per=prior):
            return f.prefix == "tse-ed-t" and f.period == per and f.has(scope) and f.has("ResultMember")
        c, p = _lookup(facts, names, p_cur), _lookup(facts, names, p_pri)
        if c is None and p is None:
            continue
        row = {"label": label, "current": _val(key, c), "prior": _val(key, p),
               "element": (c or p).name}
        if c is not None and table is FLOW and key not in PCT_NAMES:
            ch = _change(facts, c, p_cur)
            if ch is not None:
                row["yoy_pct_reported"] = ch
        out[key] = row
    return out


def _forecast(facts: list[Fact], scope: str, period: str) -> dict:
    out: dict[str, dict] = {}
    for key, label, names in FLOW:
        if key in ("operating_cf", "investing_cf", "financing_cf", "cash_end", "roe", "payout_ratio"):
            continue
        row: dict = {}
        for member, slot in (("ForecastMember", "value"), ("LowerMember", "lower"), ("UpperMember", "upper")):
            f = _lookup(facts, names, lambda x, m=member: x.prefix == "tse-ed-t" and x.period == period
                        and x.has(scope) and x.has(m))
            if f is not None:
                row[slot] = _val(key, f)
                if member == "ForecastMember":
                    ch = _change(facts, f, None)
                    if ch is not None:
                        row["yoy_pct_reported"] = ch
                    row["element"] = f.name
        if row:
            out[key] = {"label": label, **row}
    return out


def _dividends(facts: list[Fact]) -> dict:
    slots = {"FirstQuarterMember": "q1", "SecondQuarterMember": "q2", "ThirdQuarterMember": "q3",
             "YearEndMember": "year_end", "AnnualMember": "annual"}
    out: dict[str, dict] = {"prior_year": {}, "current_year": {}, "next_year": {}}
    for f in facts:
        if f.name != "DividendPerShare" or not f.numeric or f.value is None:
            continue
        year = {"PriorYearDuration": "prior_year", "CurrentYearDuration": "current_year",
                "NextYearDuration": "next_year"}.get(f.period)
        slot = next((v for k, v in slots.items() if f.has(k)), None)
        if not year or not slot:
            continue
        kind = "actual" if f.has("ResultMember") else "forecast" if f.has("ForecastMember") else \
            "lower" if f.has("LowerMember") else "upper" if f.has("UpperMember") else None
        if kind:
            out[year].setdefault(slot, {})[kind] = round(float(f.value), 2)
    return {k: v for k, v in out.items() if v}


def _shares(facts: list[Fact], pt: dict) -> dict:
    def get(name: str, period: str) -> int | None:
        for f in facts:
            if f.name == name and f.period == period and f.numeric and f.value is not None:
                return int(f.value)
        return None
    issued = get("NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock", pt["cur_i"])
    treasury = get("NumberOfTreasuryStockAtTheEndOfFiscalYear", pt["cur_i"])
    out = {"issued_incl_treasury": issued, "treasury": treasury,
           "issued_prior_year_end": get("NumberOfIssuedAndOutstandingSharesAtTheEndOfFiscalYearIncludingTreasuryStock",
                                        "PriorYearInstant"),
           "treasury_prior_year_end": get("NumberOfTreasuryStockAtTheEndOfFiscalYear", "PriorYearInstant"),
           "average_shares": get("AverageNumberOfShares", pt["cur"])}
    if issued is not None and treasury is not None:
        out["outstanding_ex_treasury"] = issued - treasury
    return {k: v for k, v in out.items() if v is not None}


FLAGS = {
    "CorrectionOfConsolidatedFinancialForecastInThisQuarter": "forecast_revised_this_quarter",
    "CorrectionOfNonConsolidatedFinancialForecastInThisQuarter": "forecast_revised_this_quarter",
    "CorrectionOfFinancialForecastInThisQuarter": "forecast_revised_this_quarter",
    "CorrectionOfDividendForecastInThisQuarter": "dividend_forecast_revised_this_quarter",
}
DOC_TEXT = {
    "DocumentName": "document_name", "CompanyName": "company_name", "FilingDate": "filing_date_text",
    "FiscalYearEnd": "fiscal_year_end", "TitleForForecasts": "forecast_title",
    "NoteToFractionProcessingMethod": "rounding", "SemiAnnualStatementFilingDateAsPlanned": "semiannual_report_planned",
    "AnnualSecuritiesReportFilingDateAsPlanned": "annual_report_planned",
    "DividendPayableDateAsPlanned": "dividend_payable_planned",
    "NoteToForecasts": "forecast_note", "NoteToSignificantChangesInTheScopeOfConsolidationDuringThePeriod":
        "consolidation_change_note", "NoteToDividends": "dividend_note", "PreambleToForecasts": "forecast_preamble",
}


def jdate(s: str | None) -> str | None:
    """'2026年11月５日' → '2026-11-05'. 실패하면 None."""
    if not s:
        return None
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s) or re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return None
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def tanshin_summary(facts: list[Fact]) -> dict:
    """결산단신 요약을 표준 모양으로. 금액은 백만 엔, 주당 금액은 엔, 비율은 %."""
    summ = [f for f in facts if f.prefix == "tse-ed-t"]
    if not summ:
        raise ValueError("결산단신 요약(tse-ed-t) 사실이 없습니다.")
    scope = _pick_scope(summ)
    pt = period_tokens(summ)
    doc = {v: _text_of(summ, k) for k, v in DOC_TEXT.items()}
    doc = {k: v for k, v in doc.items() if v}
    for k in ("semiannual_report_planned", "annual_report_planned", "dividend_payable_planned"):
        if k in doc:
            doc[k] = jdate(doc[k]) or doc[k]
    doc["filing_date"] = jdate(doc.pop("filing_date_text", None))
    flags = {}
    for k, v in FLAGS.items():
        t = _text_of(summ, k)
        if t:
            flags[v] = t.strip() in ("有", "true", "あり")
    gaap = gaap_of(summ)
    code = _text_of(summ, "SecuritiesCode")
    results = _metrics(summ, FLOW, scope, pt["cur"], pt["prior"])
    balance = _metrics(summ, STOCK, scope, pt["cur_i"], pt["prior_i"])
    fc = _forecast(summ, scope, pt["fc_period"])
    h1_fc = _forecast(summ, scope, "NextAccumulatedQ2Duration") if pt["quarter"] == 4 else \
        _forecast(summ, scope, "CurrentAccumulatedQ2Duration") if pt["quarter"] == 1 else {}
    return {
        "securities_code": code,
        "document": doc,
        "gaap": gaap, "gaap_label": GAAP_LABEL[gaap],
        "scope": "연결" if scope == "ConsolidatedMember" else "비연결(단독)",
        "quarter": pt["quarter"],
        "cumulative_months": pt["quarter"] * 3,
        "results": results,
        "balance": balance,
        "forecast": {"target": "this_fiscal_year" if pt["fc_same_year"] else "next_fiscal_year",
                     "metrics": fc, **({"first_half": h1_fc} if h1_fc else {})},
        "flags": flags,
        "dividends": _dividends(summ),
        "shares": _shares(summ, pt),
        "unmapped": _unmapped(summ, scope, pt),
    }


def _unmapped(summ: list[Fact], scope: str, pt: dict) -> list[dict]:
    """표준 지표에 안 잡힌 실적 숫자(업종 고유 항목 등). 버리지 않고 이름째 넘긴다."""
    known = {n for _k, _l, names in FLOW + STOCK for n in names}
    out = []
    for f in summ:
        if not f.numeric or f.value is None or not f.has("ResultMember") or f.period != pt["cur"]:
            continue
        b = base_name(f.name)
        if b in known or b.startswith(("ChangeIn", "ChangesIn")) or b in ("DividendPerShare", "QuarterlyPeriod") \
                or "NumberOf" in b or "AverageNumber" in b:
            continue
        out.append({"element": f.name, "value": _val("x", f), "context": f.context})
    return out[:30]


# ── 업적 예상 수정 (tse-rvfc) ──────────────────────────────────────────────

def revision_summary(facts: list[Fact]) -> dict:
    """업적 예상 수정 공시. 이전 예상(A, PreviousMember)과 새 예상(B, CurrentMember), 전기 실적(ResultMember)."""
    rv = [f for f in facts if f.prefix in ("tse-rvfc", "tse-ed-t")]
    doc = {"document_name": _text_of(rv, "DocumentName"), "company_name": _text_of(rv, "CompanyName"),
           "reporting_date": jdate(_text_of(rv, "ReportingDateOfFinancialForecastCorrection")),
           "fiscal_year_end": _text_of(rv, "FiscalYearEnd"), "reason": _text_of(rv, "NoticeOfForecastCorrection")}
    if doc["reason"] and len(doc["reason"]) > 600:
        doc["reason"] = doc["reason"][:600] + "…"
    blocks: dict[str, dict] = {}
    for f in rv:
        if not f.numeric or f.value is None:
            continue
        b = base_name(f.name)
        if b.startswith(("AmountChange", "ChangeIn", "ChangesIn")):
            continue
        key = next((k for k, _l, names in FLOW if b in names), None)
        if not key:
            continue
        scope = "연결" if f.has("ConsolidatedMember") else "비연결" if f.has("NonConsolidatedMember") else "?"
        horizon = {"CurrentYearDuration": "연간", "CurrentAccumulatedQ2Duration": "상반기(중간기)",
                   "PriorYearDuration": "전기 연간", "PriorAccumulatedQ2Duration": "전기 상반기"}.get(f.period, f.period)
        slot = ("previous" if f.has("PreviousMember") else "new" if f.has("CurrentMember") and
                not f.has("ResultMember") else "actual" if f.has("ResultMember") else None)
        if f.has("LowerMember") or f.has("UpperMember"):
            slot = (slot or "") + ("_lower" if f.has("LowerMember") else "_upper")
        if not slot:
            continue
        g = blocks.setdefault(f"{scope}·{horizon}", {})
        g.setdefault(key, {"label": dict((k, l) for k, l, _n in FLOW)[key]})[slot] = _val(key, f)
    for g in blocks.values():
        for row in g.values():
            if row.get("previous") not in (None, 0) and row.get("new") is not None and isinstance(row["previous"], (int, float)):
                row["change_pct"] = round((row["new"] / row["previous"] - 1) * 100, 1)
    return {"document": {k: v for k, v in doc.items() if v}, "blocks": blocks,
            "gaap": "IFRS" if any(f.name.endswith("IFRS") for f in rv) else
            "US" if any(f.name.endswith("US") for f in rv) else "JP"}


# ── 첨부 재무제표에서 몇 개만 ───────────────────────────────────────────────

ATTACH = {
    "cash": ("CashAndDeposits", "CashAndCashEquivalentsIFRS", "CashAndCashEquivalents"),
    "short_term_borrowings": ("ShortTermLoansPayable", "ShortTermBorrowings", "BorrowingsCLIFRS", "ShortTermBorrowingsIFRS"),
    "current_long_term_debt": ("CurrentPortionOfLongTermLoansPayable", "CurrentPortionOfBonds", "CurrentPortionOfBondsPayable"),
    "bonds": ("BondsPayable", "BondsIFRS", "BondsPayableNCLIFRS"),
    "long_term_borrowings": ("LongTermLoansPayable", "BorrowingsNCLIFRS", "LongTermBorrowingsIFRS"),
    "commercial_paper": ("CommercialPapersLiabilities", "CommercialPaper"),
    "bonds_and_borrowings_cl": ("BondsAndBorrowingsCLIFRS",),
    "bonds_and_borrowings_ncl": ("BondsAndBorrowingsNCLIFRS",),
    "investment_securities": ("InvestmentSecurities",),
    "equity_method_income": ("ShareOfProfitOfEntitiesAccountedForUsingEquityMethod", "EquityInEarningsOfAffiliatesNOI",
                             "ShareOfProfitLossOfInvestmentsAccountedForUsingEquityMethodIFRS", "EquityInEarningsOfAffiliates"),
    "fx_gain": ("ForeignExchangeGainsNOI", "ForeignExchangeGainLossNetIFRS", "ForeignExchangeGains"),
    "fx_loss": ("ForeignExchangeLossesNOE", "ForeignExchangeLosses"),
    "extraordinary_income": ("ExtraordinaryIncome",),
    "extraordinary_loss": ("ExtraordinaryLoss",),
    "impairment_loss": ("ImpairmentLossEL", "ImpairmentLosses", "ImpairmentLossIFRS"),
    "purchase_of_treasury_stock": ("PurchaseOfTreasuryStockFinCF", "PurchaseOfTreasurySharesFinCF", "PurchaseOfTreasuryStock",
                                   "PurchaseOfTreasurySharesFinCFIFRS", "PaymentsForPurchaseOfTreasurySharesFinCFIFRS",
                                   "NetDecreaseIncreaseInTreasurySharesFinCFIFRS", "NetDecreaseIncreaseInTreasuryStockFinCF"),
    "dividends_paid": ("CashDividendsPaidFinCF", "DividendsPaidFinCF", "DividendsPaidFinCFIFRS",
                       "DividendsPaidToOwnersOfParentFinCFIFRS"),
    "capex": ("PurchaseOfPropertyPlantAndEquipmentInvCF", "PurchaseOfPropertyPlantAndEquipmentInvCFIFRS",
              "PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCFIFRS"),
    "operating_cf": ("NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesIFRS"),
}


def _is_current(ctx: str) -> bool:
    return not ctx.startswith(("Prior", "FilingDate", "Next")) and ctx.endswith(("Duration", "Instant"))


# 당기 컨텍스트 우선순위. 누적(Interim·YTD·Year)을 먼저, 3개월(CurrentQuarterDuration)은 마지막에 고른다.
# 소니 실측: 지분법손익이 3개월(-228억 엔)과 누적(-208억 엔) 두 컨텍스트에 있었다.
_CUR_RANK = ("InterimDuration", "CurrentYTDDuration", "CurrentYearDuration", "InterimInstant", "CurrentQuarterInstant",
             "CurrentYearInstant", "CurrentYTDInstant")


def _rank(ctx: str) -> int:
    if ctx == "CurrentQuarterDuration":
        return 99
    return _CUR_RANK.index(ctx) if ctx in _CUR_RANK else 50


def _prior_ctx(ctx: str) -> str:
    if ctx.endswith("Instant"):
        return "Prior1YearInstant"
    return "Prior1" + ctx.removeprefix("Current")


def attachment_items(facts: list[Fact]) -> dict:
    """첨부 재무제표의 몇 가지 항목(현금, 차입, 지분법, 환차손익, 특별손익, 자사주 매입, 배당 지급).

    컨텍스트 이름은 EDINET식이다(당기 InterimDuration·CurrentYTDDuration·CurrentYearInstant, 전기 Prior1…).
    멤버가 붙지 않고 Prior로 시작하지 않는 컨텍스트를 당기로 본다. 회사별 확장 요소(tse-…-72030:…)도 이름이 같으면 받는다.
    """
    att = [f for f in facts if f.prefix not in ("tse-ed-t", "tse-rvfc") and f.numeric and f.value is not None
           and len(f.tokens) == 1]
    out: dict[str, dict] = {}
    for key, names in ATTACH.items():
        for n in names:
            cands = sorted((f for f in att if f.name == n and _is_current(f.context)), key=lambda f: _rank(f.context))
            cur = cands[0] if cands else None
            if cur is None:
                continue
            pri = next((f for f in att if f.name == n and f.context == _prior_ctx(cur.context)), None)
            out[key] = {"value": _yen(float(cur.value)), "prior": _yen(float(pri.value)) if pri else None,
                        "element": f"{cur.prefix}:{cur.name}", "context": cur.context,
                        "period": "three_months" if cur.context == "CurrentQuarterDuration" else
                        "point_in_time" if cur.context.endswith("Instant") else "cumulative"}
            break
    # 환차손익은 이익(営業外収益)과 손실(営業外費用)에 나뉘어 실린다. 해마다 칸이 바뀌므로 순액으로 합친다.
    gain_names, loss_names = ATTACH["fx_gain"], ATTACH["fx_loss"]

    def net(pred) -> float | None:
        g = next((f.value for f in att if f.name in gain_names and pred(f.context)), None)
        lo = next((f.value for f in att if f.name in loss_names and pred(f.context)), None)
        return None if g is None and lo is None else _yen((g or 0) - (lo or 0))
    fx_ctx = sorted({f.context for f in att if (f.name in gain_names or f.name in loss_names) and _is_current(f.context)},
                    key=_rank)
    cur_ctx = fx_ctx[0] if fx_ctx else None
    if cur_ctx:
        out["fx_net"] = {"value": net(lambda c: c == cur_ctx), "prior": net(lambda c: c == _prior_ctx(cur_ctx)),
                         "element": "환차익 − 환차손", "context": cur_ctx,
                         "period": "three_months" if cur_ctx == "CurrentQuarterDuration" else "cumulative"}
    out.pop("fx_gain", None)
    out.pop("fx_loss", None)
    return out


# ── 회사가 쓴 설명 (qualitative.htm) ──────────────────────────────────────

def qualitative_text(data: bytes, limit: int = 4000) -> dict | None:
    """단신 zip의 qualitative.htm에서 본문 설명(업적 개황, 재무 상태, 예상 수정 사유)만 꺼낸다.

    목차(…… 점선 줄)는 버리고, 재무제표 표가 시작되는 제목 앞에서 멈춘다. 회사가 쓴 일본어 그대로다.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith("qualitative.htm")), None)
            if not name:
                return None
            raw = z.read(name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return None
    raw = re.sub(r"<(style|script)\b.*?</\1>", "", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</(p|div|tr|h\d)>", "\n", raw, flags=re.I)
    raw = re.sub(r"</t[dh]>", " ", raw, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    lines = [re.sub(r"[ \t\u3000\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and "……" not in ln and "…" * 3 not in ln and ln != "qualitative.htm"]
    stop = re.compile(r"^[0-9０-９]+\s*[.．]\s*.*(財務諸表|計算書)")
    out, started = [], False
    for ln in lines:
        if not started:
            if stop.match(ln):
                return None          # 본문 설명 없이 재무제표부터 시작하는 회사(소니 실측). 설명은 별도 자료에 있다
            if re.match(r"^[0-9０-９]+\s*[.．]", ln) or re.match(r"^[(（][0-9０-９]+[)）]", ln):
                started = True
            else:
                continue
        if out and stop.match(ln):
            break
        out.append(ln)
    body = "\n".join(out).strip()
    if not body:
        return None
    return {"text": body[:limit], "truncated": len(body) > limit, "chars": len(body)}
