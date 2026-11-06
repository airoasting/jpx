#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jpx 스킬 회귀 테스트. 네트워크 없이 돈다.

    python3 -m unittest discover -s tests -v

픽스처는 2026-11-05 실제 TDnet XBRL이다: 토요타 2분기(IFRS), 닌텐도 2분기(J-GAAP), 도레이 상반기 예상 수정.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "assets"))
os.environ["JPX_HOME"] = tempfile.mkdtemp()
os.environ["JPX_OFFLINE"] = "1"

import analysis as an  # noqa: E402
import ixbrl  # noqa: E402
import jpx_client as jc  # noqa: E402
import mcp_server as ms  # noqa: E402


def facts(name: str):
    return ixbrl.parse_zip((FIX / name).read_bytes())


def doc_from(name: str, title: str = "", published: str = "2026-11-05 15:00:00") -> dict:
    f = facts(name)
    return {"kind": "tanshin", "summary": ixbrl.tanshin_summary(f), "attachment": ixbrl.attachment_items(f),
            "source": {"file": name, "title": title, "published": published, "sha256": "x"}}


class ParseTest(unittest.TestCase):
    def test_ifrs_toyota_summary_matches_filing(self):
        s = ixbrl.tanshin_summary(facts("toyota_2027q2_ifrs.zip"))
        self.assertEqual(s["gaap"], "IFRS")
        self.assertEqual(s["quarter"], 2)
        self.assertEqual(s["scope"], "연결")
        self.assertEqual(s["results"]["revenue"]["current"], 26962462)        # 百万円, 원문 표와 같다
        self.assertEqual(s["results"]["operating_income"]["yoy_pct_reported"], 9.2)
        self.assertNotIn("ordinary_income", s["results"])                       # IFRS에는 경상이익이 없다
        self.assertEqual(s["forecast"]["metrics"]["operating_income"]["value"], 3900000)
        self.assertTrue(s["flags"]["forecast_revised_this_quarter"])
        self.assertEqual(s["document"]["semiannual_report_planned"], "2026-11-12")
        self.assertEqual(s["shares"]["outstanding_ex_treasury"], 14594987460 - 2816740147)

    def test_jgaap_nintendo_has_ordinary_income_and_negative_change(self):
        s = ixbrl.tanshin_summary(facts("nintendo_2027q2_jgaap.zip"))
        self.assertEqual(s["gaap"], "JP")
        self.assertEqual(s["results"]["ordinary_income"]["current"], 300676)
        self.assertEqual(s["results"]["revenue"]["yoy_pct_reported"], -12.1)   # sign="-" 처리
        self.assertEqual(s["forecast"]["metrics"]["net_income"]["yoy_pct_reported"], -26.9)
        self.assertFalse(s["flags"]["forecast_revised_this_quarter"])
        self.assertTrue(s["flags"]["dividend_forecast_revised_this_quarter"])
        self.assertEqual(s["dividends"]["current_year"]["annual"]["forecast"], 162.0)
        self.assertIn("切捨て", s["document"]["rounding"])

    def test_attachment_fx_is_netted_across_gain_and_loss_lines(self):
        a = ixbrl.attachment_items(facts("nintendo_2027q2_jgaap.zip"))
        self.assertEqual(a["fx_net"]["value"], -7258)      # 올해는 환차손 칸
        self.assertEqual(a["fx_net"]["prior"], 18495)      # 전년은 환차익 칸
        self.assertNotIn("fx_gain", a)

    def test_revision_previous_and_new(self):
        f = facts("toray_2027h1_revision.zip")
        self.assertEqual(ixbrl.kind_of_zip(f), "revision")
        r = ixbrl.revision_summary(f)
        h1 = r["blocks"]["연결·상반기(중간기)"]
        self.assertEqual(h1["net_income"]["previous"], 45000)
        self.assertEqual(h1["net_income"]["new"], 69100)
        self.assertEqual(h1["net_income"]["change_pct"], 53.6)
        self.assertEqual(r["document"]["fiscal_year_end"], "2027-03-31")

    def test_self_closing_nil_does_not_swallow_next_fact(self):
        html = ('<ix:nonFraction name="tse-ed-t:SalesIFRS" contextRef="A" xsi:nil="true"/>'
                '<ix:nonFraction name="tse-ed-t:SalesIFRS" contextRef="B" scale="6" sign="-">1,234</ix:nonFraction>')
        got = ixbrl.parse_html(html)
        self.assertEqual([(f.context, f.value) for f in got], [("A", None), ("B", -1234e6)])

    def test_japanese_date(self):
        self.assertEqual(ixbrl.jdate("2026年11月５日"), "2026-11-05")
        self.assertIsNone(ixbrl.jdate("未定"))


class PeriodTest(unittest.TestCase):
    def test_title_period(self):
        self.assertEqual(an.title_period("2027年３月期 第２四半期（中間期）決算短信〔日本基準〕"), (2027, 3, 2))
        self.assertEqual(an.title_period("2026年12月期第3四半期決算短信〔米国基準〕(連結)"), (2026, 12, 3))
        self.assertEqual(an.title_period("令和９年３月期 第２四半期（中間期）決算短信"), (2027, 3, 2))
        self.assertEqual(an.title_period("平成31年3月期 第1四半期決算短信"), (2019, 3, 1))
        self.assertEqual(an.title_period("2026年9月期 決算短信〔日本基準〕"), (2026, 9, 4))
        self.assertIsNone(an.title_period("組織改正のお知らせ"))

    def test_fiscal_period_plain_label(self):
        p = an.fiscal_period("2027-03-31", 2)
        self.assertEqual(p["cumulative_end"], "2026-09-30")
        self.assertIn("2026.4~2027.3", p["plain"])
        self.assertEqual(an.fiscal_period("2026-12-31", 3)["cumulative_start"], "2026-01-01")
        self.assertEqual(an.fye_of(2027, 2), "2027-02-28")

    def test_ko_yen(self):
        self.assertEqual(an.ko_yen(966041), "9,660억 엔")
        self.assertEqual(an.ko_yen(26962462), "26조 9,625억 엔")
        self.assertEqual(an.ko_yen(2050000), "2조 500억 엔")
        self.assertEqual(an.ko_yen(-145384), "-1,454억 엔")
        self.assertEqual(an.ko_yen(168), "2억 엔")
        self.assertEqual(an.ko_yen(42), "42백만 엔")
        self.assertIsNone(an.ko_yen(None))


class SlotTest(unittest.TestCase):
    def item(self, title, pub, file, kind=None):
        return {"title": title, "published": pub, "xbrl_file": file, "kind": kind or jc.classify(title)}

    def test_review_reissue_does_not_replace_original(self):
        # 캐논 실측: 감사인 검토 완료 판은 같은 숫자에 '이번 분기 수정 무'로 다시 나온다
        items = [self.item("2026年12月期第3四半期決算短信〔米国基準〕（連結）（監査法人による期中レビューの完了）",
                           "2026-10-30 15:30:00", "081220261030561244.zip"),
                 self.item("2026年12月期 第3四半期決算短信〔米国基準〕(連結)", "2026-10-27 15:30:00", "081220261027556569.zip")]
        s = an.tanshin_slots(items)
        self.assertEqual(s[("2026-12-31", 3)]["xbrl_file"], "081220261027556569.zip")
        self.assertEqual(s[("2026-12-31", 3)]["other_versions"], ["081220261030561244.zip"])

    def test_correction_wins(self):
        items = [self.item("2027年３月期 第２四半期（中間期）決算短信〔日本基準〕（連結）", "2026-11-01 15:00:00", "081220261101000001.zip"),
                 self.item("（訂正・数値データ訂正）「2027年３月期 第２四半期（中間期）決算短信〔日本基準〕（連結）」の一部訂正について",
                           "2026-11-05 19:10:00", "081220261105000002.zip")]
        s = an.tanshin_slots(items)
        best = s[("2027-03-31", 2)]
        self.assertEqual(best["xbrl_file"], "081220261105000002.zip")
        self.assertEqual(best["supersedes"], ["081220261101000001.zip"])
        self.assertTrue(best["kind"].endswith("_correction"))
        self.assertEqual(best["fallbacks"][0]["xbrl_file"], "081220261101000001.zip")

    def test_loader_falls_back_when_correction_has_no_summary(self):
        # 미쓰비시UFJ 실측: 수치 데이터 정정 zip에 첨부 재무제표만 있고 요약이 없다
        items = [self.item("2026年3月期 第3四半期決算短信〔日本基準〕（連結）", "2026-02-04 15:00:00", "081220260203000001.zip"),
                 self.item("（訂正・数値データ訂正）「2026年3月期 第3四半期決算短信〔日本基準〕（連結）」の一部訂正について",
                           "2026-02-05 15:00:00", "081220260205000002.zip")]
        it = an.tanshin_slots(items)[("2026-03-31", 3)]
        docs = {"081220260205000002.zip": {"kind": "other", "source": {}},
                "081220260203000001.zip": {"kind": "tanshin", "source": {"file": "081220260203000001.zip"}}}
        with unittest.mock.patch.object(jc, "parsed", side_effect=lambda c5, f, *a: docs[f]):
            got = ms._loader("83060")(it)
        self.assertEqual(got["kind"], "tanshin")
        self.assertEqual(got["source"]["correction_attachment_only"], "081220260205000002.zip")


class CalcTest(unittest.TestCase):
    def test_discrete_q1_is_reported_and_mismatch_refused(self):
        toyota, nintendo = doc_from("toyota_2027q2_ifrs.zip"), doc_from("nintendo_2027q2_jgaap.zip")
        out, ref = an.discrete(nintendo, None, 1)
        self.assertEqual(out["revenue"]["kind"], "reported")
        out, ref = an.discrete(toyota, nintendo, 2)
        self.assertEqual(out, {})
        self.assertIn("회계기준", ref[0]["reason"])
        out, ref = an.discrete(toyota, None, 2)
        self.assertIn("1분기", ref[0]["reason"])

    def test_progress_range_and_missing(self):
        self.assertEqual(an.progress_row(50, {"value": 100}), {"pct": 50.0})
        self.assertEqual(an.progress_row(50, {"lower": 80, "upper": 125})["pct_range"], [40.0, 62.5])
        self.assertIsNone(an.progress_row(50, None))
        self.assertIsNone(an.progress_row(50, {"value": 0})["pct"])

    def test_guidance_from_fake_documents(self):
        def doc(q, cum, fc, nxt=False, flag=False, pub="2026", prior=None):
            res = {"revenue": {"current": cum, "prior": prior}}
            return {"kind": "tanshin", "source": {"published": pub, "file": f"{q}{pub}"},
                    "summary": {"gaap": "JP", "gaap_label": "J", "scope": "연결", "quarter": q, "results": res,
                                "forecast": {"target": "next_fiscal_year" if nxt else "this_fiscal_year",
                                             "metrics": {"revenue": {"value": fc}}},
                                "flags": {"forecast_revised_this_quarter": flag}}}
        docs = {("2026-03-31", 4): doc(4, 1000, 1100, nxt=True, pub="2026-05-10"),
                ("2027-03-31", 1): doc(1, 300, 1100, pub="2026-08-05"),
                ("2027-03-31", 2): doc(2, 600, 1200, flag=True, pub="2026-11-05", prior=450),
                ("2026-03-31", 2): doc(2, 450, 1000, pub="2025-11-05"),
                ("2025-03-31", 4): doc(4, 900, 950, nxt=True, pub="2025-05-10")}
        slots = {k: {"xbrl_file": f"{k}", "kind": "tanshin"} for k in docs}
        load = lambda it: docs[next(k for k in docs if f"{k}" == it["xbrl_file"])]  # noqa: E731
        g = an.guidance("2027-03-31", slots, [], load, 1, lambda it: {})
        events = [r["event"] for r in g["timeline"]]
        self.assertEqual(events, ["기초 예상(期初予想)", "1분기 결산단신", "2분기 결산단신"])
        self.assertEqual(g["timeline"][2]["direction_vs_previous"]["revenue"], "상향")
        self.assertEqual(g["progress"]["revenue"]["pct"], 50.0)                   # 600 / 1200
        self.assertEqual(g["progress"]["revenue"]["past_same_quarter"][0]["pct"], 45.0)   # 450 / 1000
        imp = g["implied_remaining"]["revenue"]
        self.assertEqual(imp["implied_remaining_value"], 600)                    # 1200 − 600
        self.assertEqual(imp["prior_year_same_months_value"], 550)               # 1000 − 450
        self.assertEqual(g["initial_forecast_track_record"][0]["revenue"]["achievement_pct"], round(1000 / 950 * 100, 1))   # 기초 예상은 전전기 연간 단신의 950


class JapanSpecificTest(unittest.TestCase):
    def test_target_from_bank_text(self):
        t = "2027年3月期の「親会社株主に帰属する当期純利益」は27,000億円を目標としております。"
        self.assertEqual(an.target_from_text(t), {"net_income": 2700000})
        self.assertEqual(an.target_from_text("業績予想は未定です"), {})
        self.assertEqual(an.target_from_text("当期純利益は1兆2,000億円"), {})       # 목표·예상 문구가 없다

    def test_basis_change_detected(self):
        def d(cur, prior):
            return {"summary": {"results": {"net_income": {"current": cur, "prior": prior}}}}
        self.assertIsNone(an.basis_changed(d(120, 100), d(100.5, 90), "net_income"))
        self.assertIn("다르다", an.basis_changed(d(120, 80), d(100, 90), "net_income"))   # 전년 열이 작년 값과 20% 다르다
        self.assertIn("싣지 않았다", an.basis_changed(d(120, None), d(100, 90), "net_income"))

    def test_attachment_prefers_cumulative_over_three_months(self):
        html = ('<ix:nonFraction name="jpigp_cor:ShareOfProfitLossOfInvestmentsAccountedForUsingEquityMethodIFRS" '
                'contextRef="CurrentQuarterDuration" scale="6" sign="-">22,800</ix:nonFraction>'
                '<ix:nonFraction name="jpigp_cor:ShareOfProfitLossOfInvestmentsAccountedForUsingEquityMethodIFRS" '
                'contextRef="InterimDuration" scale="6" sign="-">20,781</ix:nonFraction>')
        a = ixbrl.attachment_items(ixbrl.parse_html(html))
        self.assertEqual(a["equity_method_income"]["value"], -20781)       # 소니 실측: 3개월치를 고르면 안 된다
        self.assertEqual(a["equity_method_income"]["period"], "cumulative")

    def test_qualitative_text_from_fixture(self):
        q = ixbrl.qualitative_text((FIX / "nintendo_2027q2_jgaap.zip").read_bytes())
        self.assertIsNotNone(q)
        self.assertNotIn("……", q["text"])


class ClientTest(unittest.TestCase):
    def test_index_links_must_point_to_tdnet(self):
        good = "https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/081220261030561115.zip"
        self.assertEqual(jc._file_from_url(good), "081220261030561115.zip")
        self.assertIsNone(jc._file_from_url("https://evil.example.com/inbs/081220261030561115.zip"))
        self.assertIsNone(jc._file_from_url("https://www.release.tdnet.info/inbs/../../x.zip"))
        self.assertEqual(jc.pdf_for("091220261104564372.zip"), "140120261104564372.pdf")

    def test_classify(self):
        self.assertEqual(jc.classify("業績予想の修正に関するお知らせ"), "earnings_forecast_revision")
        self.assertEqual(jc.classify("自己株式取得に係る事項の決定に関するお知らせ"), "buyback")
        self.assertEqual(jc.classify("（訂正）「2027年３月期 決算短信」の一部訂正について"), "tanshin_correction")

    def test_index_degrades_to_vault_when_source_fails(self):
        d = jc.vault_dir("99990")
        d.mkdir(parents=True, exist_ok=True)
        (d / "081220260101000001.zip.meta.json").write_text(json.dumps(
            {"file": "081220260101000001.zip", "title": "2026年3月期 決算短信", "published": "2026-05-01"}), encoding="utf-8")
        with unittest.mock.patch.object(jc, "get", side_effect=jc.SourceError("down")):
            items, degraded = jc.index("99990")
        self.assertIn("vault_only", degraded)
        self.assertEqual(items[0]["xbrl_file"], "081220260101000001.zip")

    def test_vault_is_used_before_network(self):
        data = (FIX / "nintendo_2027q2_jgaap.zip").read_bytes()
        d = jc.vault_dir("79740")
        d.mkdir(parents=True, exist_ok=True)
        (d / "081220261027557139.zip").write_bytes(data)
        (d / "081220261027557139.zip.meta.json").write_text(json.dumps({"file": "081220261027557139.zip",
                                                                        "sha256": "abc"}), encoding="utf-8")
        with unittest.mock.patch.object(jc, "get", side_effect=AssertionError("network")):
            p = jc.parsed("79740", "081220261027557139.zip")
        self.assertEqual(p["kind"], "tanshin")
        self.assertEqual(p["parser_version"], jc.PARSER_VERSION)


class ResolveTest(unittest.TestCase):
    def test_aliases_and_codes(self):
        self.assertEqual(ms.resolve_company("토요타")["code"], "7203")
        self.assertEqual(ms.resolve_company("도요타자동차")["code"], "7203")
        self.assertEqual(ms.resolve_company("７９７４")["name_en"], "Nintendo Co., Ltd.")
        self.assertEqual(ms.resolve_company("285A")["code"], "285A")
        u = ms.resolve_company("유니클로")
        self.assertEqual(u["code"], "9983")
        self.assertIn("유니클로", u["alias_note"])
        self.assertEqual(ms.resolve_company("Nintendo")["code"], "7974")

    def test_ambiguous_korean_prefix(self):
        with self.assertRaises(ms.ToolError) as cm:
            ms.resolve_company("미쓰비시")
        self.assertEqual(cm.exception.data["status"], "ambiguous")
        self.assertGreaterEqual(len(cm.exception.data["candidates"]), 5)

    def test_every_alias_points_to_a_listed_company(self):
        codes = {r["code"] for r in jc.companies(refresh=False)}
        missing = [a["code"] for a in jc.aliases() if jc.code5(a["code"]) not in codes]
        self.assertEqual(missing, [])


class ProtocolTest(unittest.TestCase):
    def test_six_tools(self):
        names = [t["name"] for t in ms.tool_specs()]
        self.assertEqual(names, ["company_resolve", "filing_search", "fin_tanshin", "fin_guidance", "fin_quarters",
                                 "market_valuation"])

    def test_jsonrpc_and_errors(self):
        r = ms.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["serverInfo"]["name"], "jpx")
        body, err = ms.run_tool("fin_tanshin", {"company": "토요타", "bogus": 1})
        self.assertTrue(err)
        self.assertEqual(body["error_code"], "BAD_INPUT")
        body, err = ms.run_tool("fin_tanshin", {})
        self.assertTrue(err)
        self.assertIsNone(ms.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


if __name__ == "__main__":
    unittest.main()
