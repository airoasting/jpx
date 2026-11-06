"""일본 상장사 공시 클라이언트. 키가 없다.

소스와 순서
- 회사 등록부: 번들 jp_companies.csv(EDINET 코드 목록에서 상장사만 뽑은 것) + jp_aliases.csv(한국어 별칭).
  30일보다 오래되면 EDINET 코드 목록 zip(키 불필요)을 새로 받아 ~/.claude/skills/jpx/cache 에 둔다.
- 회사별 XBRL 공시 색인: webapi.yanoshin.jp(비공식). 멈추면 캐시 → 금고 순서로 대신한다(degraded 표시).
- 문서: JPX 사본 www2.jpx.co.jp/disc/{code5}/{file} → TDnet www.release.tdnet.info/inbs/{file}.
- 받은 zip은 금고(vault)에 추가만 한다. 파싱 결과는 parser_version과 함께 옆에 둔다.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from http_compat import requests  # noqa: E402
import ixbrl  # noqa: E402

PARSER_VERSION = "0.2.0"
UA = "Mozilla/5.0 (compatible; jpx-skill/0.1; on-demand single-document fetch)"
JPX_DISC = "https://www2.jpx.co.jp/disc"
TDNET = "https://www.release.tdnet.info/inbs"
INDEX = "https://webapi.yanoshin.jp/webapi/tdnet/list"
CODELIST = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart"
MIN_GAP = {"www2.jpx.co.jp": 0.5, "www.release.tdnet.info": 1.0, "webapi.yanoshin.jp": 0.5,
           "query1.finance.yahoo.com": 0.3, "disclosure2dl.edinet-fsa.go.jp": 1.0}
INDEX_TTL = 6 * 3600
CODELIST_TTL = 30 * 24 * 3600
FILE_RE = re.compile(r"^\d{18}\.(zip|pdf)$")


def home() -> Path:
    return Path(os.environ.get("JPX_HOME") or Path.home() / ".claude" / "skills" / "jpx")


class SourceError(ConnectionError):
    """원격 소스 실패(재시도 후)."""


# ── 접속 ───────────────────────────────────────────────────────────────────

_last: dict[str, float] = {}


def get(url: str, retries: int = 2, timeout: int = 30, params: dict | None = None):
    host = urlparse(url).netloc
    err = ""
    for i in range(retries + 1):
        gap = MIN_GAP.get(host, 0.5) - (time.monotonic() - _last.get(host, 0.0))
        if gap > 0:
            time.sleep(gap)
        _last[host] = time.monotonic()
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA}, timeout=timeout)
        except requests.RequestException as e:
            err = f"{type(e).__name__}: {e}"
        else:
            if r.status_code in (403, 404, 410):
                return None
            if r.status_code < 400:
                return r
            err = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break
        if i < retries:
            time.sleep(1.5 * (i + 1))
    raise SourceError(f"{host} 접속 실패 ({err})")


# ── 회사 등록부 ────────────────────────────────────────────────────────────

def code5(code: str) -> str:
    """'7203' → '72030', '130A' → '130A0'. 이미 5자리면 그대로."""
    c = unicodedata.normalize("NFKC", str(code)).strip().upper()
    return c if len(c) == 5 else c + "0"


def code4(c5: str) -> str:
    return c5[:4] if len(c5) == 5 and c5.endswith("0") else c5


def norm(s: str) -> str:
    """비교용 이름. 전각→반각, 소문자, 법인 표기와 공백·기호 제거."""
    s = unicodedata.normalize("NFKC", str(s)).lower()
    s = re.sub(r"株式会社|\(株\)|㈱|有限会社|ホールディングス|holdings?|corporation|corp\.?|co\.,?\s*ltd\.?|"
               r"limited|ltd\.?|inc\.?|incorporated|company|group|グループ|\bthe\b|주식회사|홀딩스|그룹", " ", s)
    return re.sub(r"[\s・.,'&()\-/]+", "", s)


COMPANY_FIELDS = ("code", "name", "name_en", "name_kana", "industry", "fiscal_year_end", "edinet_code")


def parse_codelist(raw: bytes) -> list[dict]:
    """EDINET 코드 목록 zip 또는 CSV → 상장사 행. 열: [0]EDINET코드 [2]상장구분 [5]결산일 [6]이름 [7]영문 [8]요미 [10]업종 [11]증권코드."""
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            raw = z.read(next(n for n in z.namelist() if n.lower().endswith(".csv")))
    text = raw.decode("cp932", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    out = []
    for r in rows[2:]:
        if len(r) < 12 or not r[11].strip() or r[2].strip() != "上場":
            continue
        out.append({"code": r[11].strip(), "name": r[6].strip(), "name_en": r[7].strip(), "name_kana": r[8].strip(),
                    "industry": r[10].strip(), "fiscal_year_end": r[5].strip(), "edinet_code": r[0].strip()})
    return out


def _read_csv(p: Path) -> list[dict]:
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_companies(rows: list[dict], p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COMPANY_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COMPANY_FIELDS})


_companies: list[dict] | None = None


def companies(refresh: bool = True) -> list[dict]:
    """상장사 목록. 캐시가 30일 넘으면 새로 받고, 실패하면 번들을 쓴다."""
    global _companies
    if _companies is not None:
        return _companies
    cache = home() / "cache" / "jp_companies.csv"
    bundled = HERE / "jp_companies.csv"
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < CODELIST_TTL
    if refresh and not fresh and os.environ.get("JPX_OFFLINE") != "1":
        try:
            r = get(CODELIST, retries=1, timeout=60)
            if r is not None:
                rows = parse_codelist(r.content)
                if len(rows) > 1000:
                    write_companies(rows, cache)
                    fresh = True
        except (SourceError, ValueError, zipfile.BadZipFile, StopIteration):
            pass
    src = cache if cache.exists() else bundled
    _companies = _read_csv(src)
    return _companies


def aliases() -> list[dict]:
    p = HERE / "jp_aliases.csv"
    return _read_csv(p) if p.exists() else []


# ── 회사별 XBRL 공시 색인 ──────────────────────────────────────────────────

def _file_from_url(u: str | None) -> str | None:
    """색인이 준 링크 → TDnet 파일 이름. 호스트가 TDnet이 아니면 버린다(색인 링크를 그대로 믿지 않는다)."""
    if not u:
        return None
    if "rd.php?" in u:
        u = u.split("rd.php?", 1)[1]
    p = urlparse(u)
    if p.netloc != "www.release.tdnet.info" or not p.path.startswith("/inbs/"):
        return None
    name = p.path.rsplit("/", 1)[-1]
    return name if FILE_RE.match(name) else None


def classify(title: str) -> str:
    t = unicodedata.normalize("NFKC", title)
    if "訂正" in t:
        base = "correction"
    else:
        base = ""
    if "決算短信" in t:
        kind = "tanshin"
    elif "業績予想" in t or "業績見通し" in t or "予想値と" in t or "予想と実績" in t:
        kind = "earnings_forecast_revision"
    elif "配当" in t:
        kind = "dividend"
    elif "自己株式" in t or "自社株" in t:
        kind = "buyback"
    elif "公開買付" in t or "TOB" in t or "MBO" in t:
        kind = "tender_offer"
    elif "株式分割" in t:
        kind = "stock_split"
    elif "減損" in t or "特別損失" in t or "特別利益" in t:
        kind = "extraordinary"
    elif "中期経営計画" in t or "中期計画" in t:
        kind = "midterm_plan"
    elif "資本コスト" in t or "株価を意識" in t:
        kind = "capital_cost_policy"
    else:
        kind = "other"
    return f"{kind}_correction" if base and kind != "other" else ("correction" if base else kind)


def pdf_for(xbrl_file: str | None) -> str | None:
    """XBRL zip 이름 → 같은 공시의 PDF 이름. 0812…(단신)·0912…(예상 수정) ↔ 1401… (실측 규칙)."""
    if not xbrl_file or not FILE_RE.match(xbrl_file):
        return None
    return "1401" + xbrl_file[4:-4] + ".pdf"


def _item(t: dict) -> dict:
    xbrl = _file_from_url(t.get("url_xbrl"))
    pdf = _file_from_url(t.get("document_url"))
    if not pdf or not pdf.endswith(".pdf"):
        pdf = pdf_for(xbrl)      # XBRL만 거른 색인은 document_url에 zip을 넣는다
    return {"id": str(t.get("id")), "published": t.get("pubdate"), "title": t.get("title", "").strip(),
            "kind": classify(t.get("title", "")), "code": t.get("company_code"), "xbrl_file": xbrl, "pdf_file": pdf}


def index(c5: str, xbrl_only: bool = True, limit: int = 300) -> tuple[list[dict], list[str]]:
    """회사의 공시 목록(최신순)과 degraded 표시. 색인 실패 → 캐시(오래돼도) → 금고에 있는 파일 이름."""
    tag = "xbrl" if xbrl_only else "all"
    cache = home() / "cache" / "index" / f"{c5}_{tag}.json"
    degraded: list[str] = []
    if cache.exists() and time.time() - cache.stat().st_mtime < INDEX_TTL:
        return json.loads(cache.read_text(encoding="utf-8")), degraded
    try:
        params = {"limit": limit, **({"hasXBRL": 1} if xbrl_only else {})}
        r = get(f"{INDEX}/{code4(c5)}.json", params=params, timeout=30)
        if r is None:
            raise SourceError("색인 응답 없음")
        items = [_item(x["Tdnet"]) for x in r.json().get("items", []) if "Tdnet" in x]
        items = [i for i in items if code5(i["code"] or "") == c5]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return items, degraded
    except (SourceError, ValueError, KeyError):
        degraded.append("index_unavailable")
    if cache.exists():
        degraded.append("index_stale_cache")
        return json.loads(cache.read_text(encoding="utf-8")), degraded
    degraded.append("vault_only")
    return vault_items(c5), degraded


# ── 금고 ───────────────────────────────────────────────────────────────────

def vault_dir(c5: str) -> Path:
    return home() / "vault" / c5


def vault_items(c5: str) -> list[dict]:
    """금고에 있는 문서를 색인 모양으로. 색인이 멈췄을 때 쓴다."""
    out = []
    for meta in sorted(vault_dir(c5).glob("*.meta.json"), reverse=True):
        m = json.loads(meta.read_text(encoding="utf-8"))
        out.append({"id": None, "published": m.get("published"), "title": m.get("title") or "",
                    "kind": classify(m.get("title") or ""), "code": c5, "xbrl_file": m["file"], "pdf_file": None})
    return out


def fetch_zip(c5: str, fname: str, title: str = "", published: str | None = None) -> tuple[bytes, dict]:
    """XBRL zip을 금고 → JPX 사본 → TDnet 순서로. (zip 바이트, 출처 메타)."""
    if not FILE_RE.match(fname):
        raise ValueError(f"파일 이름 형식이 아닙니다: {fname}")
    d = vault_dir(c5)
    zp, mp = d / fname, d / f"{fname}.meta.json"
    if zp.exists() and mp.exists():
        return zp.read_bytes(), {**json.loads(mp.read_text(encoding="utf-8")), "from_vault": True}
    for url in (f"{JPX_DISC}/{c5}/{fname}", f"{TDNET}/{fname}"):
        r = get(url)
        if r is not None and r.content[:2] == b"PK":
            data = r.content
            meta = {"file": fname, "source_url": url, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"), "sha256": hashlib.sha256(data).hexdigest(), "title": title,
                "published": published, "bytes": len(data)}
            d.mkdir(parents=True, exist_ok=True)
            zp.write_bytes(data)
            mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            return data, {**meta, "from_vault": False}
    raise SourceError(f"{fname}을 JPX 사본과 TDnet 어디에서도 받지 못했습니다(삭제되었거나 아직 올라오지 않음).")


def parsed(c5: str, fname: str, title: str = "", published: str | None = None) -> dict:
    """문서를 읽어 정리한 결과. 금고에 parser_version이 같은 결과가 있으면 그것을 쓴다."""
    cp = vault_dir(c5) / f"{fname}.parsed.json"
    if cp.exists():
        got = json.loads(cp.read_text(encoding="utf-8"))
        if got.get("parser_version") == PARSER_VERSION:
            return got
    data, meta = fetch_zip(c5, fname, title, published)
    facts = ixbrl.parse_zip(data)
    kind = ixbrl.kind_of_zip(facts)
    body: dict[str, Any] = {"kind": kind}
    if kind == "tanshin":
        body["summary"] = ixbrl.tanshin_summary(facts)
        body["attachment"] = ixbrl.attachment_items(facts)
        body["explanation"] = ixbrl.qualitative_text(data)
    elif kind == "revision":
        body["revision"] = ixbrl.revision_summary(facts)
    src = {k: meta.get(k) for k in ("file", "source_url", "fetched_at", "sha256", "title", "published")}
    out = {"parser_version": PARSER_VERSION, "source": src, **body}
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def source_links(c5: str, item: dict) -> dict:
    """사용자에게 보여 줄 원문 링크. JPX 사본(오래 남는다)을 먼저 둔다."""
    out = {}
    if item.get("pdf_file"):
        out["pdf"] = f"{JPX_DISC}/{c5}/{item['pdf_file']}"
    if item.get("xbrl_file"):
        out["xbrl"] = f"{JPX_DISC}/{c5}/{item['xbrl_file']}"
    return out


# ── 주가 (비공식, 참고) ────────────────────────────────────────────────────

def price(c4: str) -> dict | None:
    """직전 거래일 종가와 최근 1년 분할 이벤트. 오늘(도쿄) 행은 장중일 수 있어 쓰지 않는다."""
    r = get(f"{YAHOO}/{c4}.T", params={"range": "1y", "interval": "1d", "events": "split"}, timeout=20)
    if r is None:
        return None
    try:
        res = r.json()["chart"]["result"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    ts = res.get("timestamp") or []
    closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
    jst = dt.timezone(dt.timedelta(hours=9))
    today = dt.datetime.now(jst).date()
    rows = [(dt.datetime.fromtimestamp(t, jst).date(), c) for t, c in zip(ts, closes) if c is not None]
    rows = [x for x in rows if x[0] < today]
    if not rows:
        return None
    d, c = rows[-1]
    splits = [{"date": dt.datetime.fromtimestamp(int(v["date"]), jst).date().isoformat(),
               "ratio": f"{v.get('numerator')}:{v.get('denominator')}"}
              for v in ((res.get("events") or {}).get("splits") or {}).values()]
    return {"close": round(float(c), 2), "date": d.isoformat(), "currency": res.get("meta", {}).get("currency"),
            "splits_1y": sorted(splits, key=lambda x: x["date"]), "source": f"{YAHOO}/{c4}.T"}
