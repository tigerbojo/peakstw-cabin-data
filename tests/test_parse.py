"""解析器測試：fixtures/ 是 2026-10-21 從官網存下來的真實 HTML（python main.py --once --dump-html）。

官網改版時這些測試不會壞（素材是舊的），要靠 /api/health 的 lastError 發現；
發現後重新 dump 一份、換掉 fixtures、看哪個測試掛掉就知道哪裡改了。
"""

from datetime import datetime
from pathlib import Path

import pytest

import scraper as main

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return main.parse_cabin_page((FIX / f"{name}.html").read_text(encoding="utf-8"))


def test_cabin_list_is_read_from_page_not_hardcoded():
    options = main.parse_cabin_options((FIX / "list.html").read_text(encoding="utf-8"))
    assert len(options) == 27
    assert options[0] == ("3", "排雲山莊")
    assert ("241", "新仙山前營地") in options


def test_paiyun_basic_fields():
    r = load("paiyun-2026-10")
    assert (r["id"], r["name"], r["month"]) == ("3", "排雲山莊", "2026-10")
    assert r["elevation_m"] == 3402
    assert r["capacity"] == {"weekday_beds": 136, "holiday_beds": 136, "weekday_tents": 0, "holiday_tents": 0}
    assert r["signals"] == ["GSM", "無線電", "衛星"]
    # 官網：東經120°56’58.83，北緯23°27’59.77
    assert r["lng"] == pytest.approx(120.949675, abs=1e-6)
    assert r["lat"] == pytest.approx(23.466603, abs=1e-6)


def test_day_with_beds_left_total_is_derived():
    days = {d.date: d for d in load("paiyun-2026-10")["days"]}
    d = days["2026-10-26"]  # 餘額 (10,0) 排隊預約 4 審核中 17 核准入園 105 (126,0)
    assert (d.full, d.beds_left, d.beds_used, d.queue, d.reviewing, d.approved) == (False, 10, 126, 4, 17, 105)
    assert d.beds_total == 136  # 10 + 126，不是寫死的


def test_full_day():
    days = {d.date: d for d in load("paiyun-2026-10")["days"]}
    d = days["2026-10-31"]  # 額滿 排隊預約 244 … (136,0)
    assert d.full and d.beds_left == 0 and d.queue == 244 and d.beds_total == 136


def test_totals_match_published_capacity_every_day():
    r = load("paiyun-2026-11")
    assert len(r["days"]) == 28
    for d in r["days"]:
        assert d.beds_total in (r["capacity"]["weekday_beds"], r["capacity"]["holiday_beds"]), d


def test_campsite_uses_tents():
    r = load("xinxian-camp-2026-10")
    d = r["days"][0]
    assert (d.beds_total, d.tents_left, d.tents_total) == (0, 21, 24)


def test_missing_coordinates_stay_none_not_guessed():
    assert load("xinxian-camp-2026-10")["lat"] is None


def test_closed_campsite_has_no_days():
    r = load("dujuan-closed-2026-10")
    assert r["days"] == [] and r["lat"] is not None


@pytest.mark.parametrize(
    "fixture,level",
    [
        ("paiyun-2026-10", "seasonal"),  # 供水平時無虞，冬季…缺水
        ("xinxian-camp-2026-10", "none"),  # 水源缺乏，窪地水源不穩常乾沽
        ("duomeili-camp-2026-10", "none"),  # 水源 : 無
        ("dujuan-closed-2026-10", "ok"),  # 水源：位於東側小溝約5分鐘步程
    ],
)
def test_water_level(fixture, level):
    assert load(fixture)["water_level"] == level


def test_water_notes_skip_field_echo_and_hot_water():
    notes = load("paiyun-2026-10")["water_notes"]
    assert notes == ["供水平時無虞，冬季則因水源短缺或管線結冰有缺水情形"]


def test_tm2_conversion_matches_published_latlng():
    # 排雲山莊官網同時給了 TM2（X 244859, Y 2595954）和經緯度，兩者應在 ~30 m 內
    lng, lat = main.tm2_to_wgs84(244859, 2595954)
    assert lng == pytest.approx(120.949675, abs=3e-4)
    assert lat == pytest.approx(23.466603, abs=3e-4)


def test_tm2_fallback_when_no_dms():
    assert main.parse_coordinates("◎TWD97TM2：X座標244859，Y座標2595954") == pytest.approx(
        (120.9497, 23.4666), abs=3e-4
    )


@pytest.mark.parametrize(
    "hhmm,expected",
    [("22:59", False), ("23:00", True), ("03:00", True), ("06:59", True), ("07:00", False), ("12:00", False)],
)
def test_quiet_hours_cross_midnight(hhmm, expected):
    h, m = map(int, hhmm.split(":"))
    now = datetime(2026, 10, 21, h, m, tzinfo=main.TZ)
    assert main.in_quiet_hours(now, "23:00", "07:00") is expected


def test_quiet_hours_disabled_when_start_equals_end():
    assert main.in_quiet_hours(datetime(2026, 10, 21, 3, 0, tzinfo=main.TZ), "00:00", "00:00") is False


# ─────────────────────────────────────────────────────────────────────────────
# 失真路徑（2026-10-22 Codex 審查 P1）：官網格式出狀況時，寧可「讀取失敗、保留舊資料」，
# 也不能默默算出錯的數字或當成「沒開放」。
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
from datetime import timedelta


def raw(name: str) -> str:
    return (FIX / f"{name}.html").read_text(encoding="utf-8")


def test_missing_occupied_pair_is_parse_failure_not_wrong_total():
    # 把 10/26 那格尾端的「已佔用 (126,0)」拿掉：以前會把餘額 (10,0) 當成已佔用 → 總量 20
    html = raw("paiyun-2026-10")
    cell_start = html.index("sdate=2026-10-26")
    cell_end = html.index("</a>", cell_start)
    cell = html[cell_start:cell_end]
    tail = cell.rindex('<span class="text-primary">(')
    broken = html[:cell_start] + cell[:tail] + html[cell_end:]
    r = main.parse_cabin_page(broken)
    assert "2026-10-26" not in {d.date for d in r["days"]}
    assert r["cells_parsed"] == r["cells_expected"] - 1
    with pytest.raises(main.ParseError):
        main.check_month_page(r, "3", "2026-10")


def test_renamed_date_links_are_detected():
    # 官網把 sdate= 改名：一格都讀不到，但頁面上明明有資料 → 不能當成「沒開放」
    r = main.parse_cabin_page(raw("paiyun-2026-10").replace("sdate=", "date="))
    assert r["cells_parsed"] == 0 and r["cells_expected"] > 0
    with pytest.raises(main.ParseError, match="只讀懂 0 格"):
        main.check_month_page(r, "3", "2026-10")


def test_month_did_not_flip_is_detected():
    # 按了「下個月」但頁面還停在 10 月
    r = main.parse_cabin_page(raw("paiyun-2026-10"))
    with pytest.raises(main.ParseError, match="月份不是 2026-11"):
        main.check_month_page(r, "3", "2026-11")


def test_wrong_cabin_is_detected():
    r = main.parse_cabin_page(raw("paiyun-2026-10"))
    with pytest.raises(main.ParseError, match="宿營地不是 4"):
        main.check_month_page(r, "4", "2026-10")


def test_good_pages_pass_validation_and_closed_campsite_is_not_an_error():
    main.check_month_page(main.parse_cabin_page(raw("paiyun-2026-10")), "3", "2026-10")
    main.check_month_page(main.parse_cabin_page(raw("paiyun-2026-11")), "3", "2026-11")
    closed = main.parse_cabin_page(raw("dujuan-closed-2026-10"))
    assert closed["cells_expected"] == 0
    main.check_month_page(closed, "226", "2026-10")  # 真的沒開放：不是錯誤


def test_month_after_crosses_year():
    assert main.month_after("2026-10") == "2026-11"
    assert main.month_after("2026-12") == "2027-01"


@pytest.mark.parametrize("status,exc", [(429, main.SourceUnavailable), (503, main.SourceUnavailable), (404, RuntimeError)])
def test_http_errors_stop_scraping(status, exc):
    with pytest.raises(exc):
        main.check_status(status)


def test_http_ok_passes():
    main.check_status(200)
    main.check_status(None)


def _cabin(cid: str, scraped_at: str) -> "main.Cabin":
    return main.Cabin(id=cid, name=cid, scraped_at=scraped_at)


def test_cache_keeps_untouched_and_failed_cabins(tmp_path):
    c = main.CabinCache(tmp_path / "cache.json")
    asyncio.run(c.replace({"a": _cabin("a", "2026-10-20T08:00:00+08:00"), "b": _cabin("b", "2026-10-20T08:00:00+08:00")}, {}))
    # 下一輪：a 成功、b 沒輪到（中途停手）、c 失敗且從沒成功過
    asyncio.run(c.replace({"a": _cabin("a", "2026-10-22T08:00:00+08:00")}, {"c": "boom"}))
    assert set(c.cabins) == {"a", "b"}
    assert c.cabins["b"].scraped_at == "2026-10-20T08:00:00+08:00"


def test_snapshot_marks_each_old_cabin_stale(tmp_path):
    now = datetime(2026, 10, 22, 9, 0, tzinfo=main.TZ)
    c = main.CabinCache(tmp_path / "cache.json")
    fresh_at = (now - timedelta(minutes=5)).isoformat()
    old_at = (now - timedelta(days=3)).isoformat()
    c.cabins = {"a": _cabin("a", fresh_at), "b": _cabin("b", old_at)}
    c.updated_at = fresh_at  # 有一處剛成功，整批時間很新……
    snap = c.snapshot(now)
    by_id = {x["id"]: x for x in snap["cabins"]}
    assert snap["stale"] is False
    assert by_id["a"]["stale"] is False
    assert by_id["b"]["stale"] is True  # ……但三天沒更新的那處必須自己標出來
    assert snap["partial"] is True and snap["staleCount"] == 1


def test_run_once_merges_and_writes_public_json(tmp_path, monkeypatch):
    """整條流程（不碰官網）：上一份有 a、b；這輪 a 成功、b 失敗 → 兩處都在，b 保留舊資料並帶錯誤。"""
    import json as _json

    state, out = tmp_path / "state.json", tmp_path / "yushan.json"
    old = main.CabinCache(state)
    asyncio.run(old.replace({"a": _cabin("a", "2026-10-21T08:00:00+08:00"), "b": _cabin("b", "2026-10-21T08:00:00+08:00")}, {}))

    async def fake_scrape_all(dump_dir=None):
        return {"a": _cabin("a", datetime.now(main.TZ).isoformat(timespec="seconds"))}, {"b": "ParseError: 讀不懂"}

    monkeypatch.setattr(main, "scrape_all", fake_scrape_all)
    code = asyncio.run(main.run_once(state, out, force=True))
    assert code == 0
    snap = _json.loads(out.read_text(encoding="utf-8"))
    by_id = {c["id"]: c for c in snap["cabins"]}
    assert set(by_id) == {"a", "b"}
    assert by_id["a"]["stale"] is False and by_id["a"]["error"] is None
    assert by_id["b"]["error"] == "ParseError: 讀不懂"
    assert by_id["b"]["scrapedAt"] == "2026-10-21T08:00:00+08:00"
    assert snap["lastError"] == "1 處失敗"


def test_run_once_all_failed_exits_nonzero_but_keeps_old_data(tmp_path, monkeypatch):
    import json as _json

    state, out = tmp_path / "state.json", tmp_path / "yushan.json"
    asyncio.run(main.CabinCache(state).replace({"a": _cabin("a", "2026-10-21T08:00:00+08:00")}, {}))

    async def boom(dump_dir=None):
        raise main.SourceUnavailable("HTTP 503")

    monkeypatch.setattr(main, "scrape_all", boom)
    assert asyncio.run(main.run_once(state, out, force=True)) == 1
    snap = _json.loads(out.read_text(encoding="utf-8"))
    assert [c["id"] for c in snap["cabins"]] == ["a"]
    assert "503" in snap["lastError"]
