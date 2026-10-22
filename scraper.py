"""
臺灣山屋床位抄寫員（第一版：玉山國家公園 27 處宿營地）

由 GitHub Actions 每小時跑一次（.github/workflows/scrape.yml）：
  讀上一份 state.json → 抓官網 → 合併（失敗的山屋保留舊資料）→ 寫 state.json 與 yushan.json → 推到 data 分支。
PEAKSTW 官網（peakstw.com/cabins）讀 data 分支上的 yushan.json。

資料來源：臺灣登山申請一站式服務網 https://hike.taiwan.gov.tw/bed_6.aspx
  - 依〈資料開放宣告〉可再利用，條件是「註明出處、不得竄改」→ 輸出的 JSON 都帶 attribution。
  - 官網每個宿營地是一張「月曆」，每一天一格：
        餘額 (床位,營位) / 額滿
        排隊預約 N      ← 等候抽籤或遞補（前端說的「候補」）
        審核中 N
        核准入園 N
        (已佔床位,已佔營位)
    總量不寫死：總量 = 餘額 + 已佔（例：10 + 126 = 136），頁首「非假日／假日床位」只拿來交叉核對。
  - 切換宿營地、切換月份都是 ASP.NET postback（UpdatePanel 局部更新），所以用 Playwright 開真瀏覽器操作，
    拿到 HTML 後交給 selectolax 解析（比 BeautifulSoup 快很多，27 處 × 2 個月一輪不到 1 秒解析）。

設計重點：
  1. 每頁月曆都要過驗收（宿營地對、月份對、每格都讀懂），不過關就保留那處上一輪的資料，不當成「沒開放」。
  2. 一輪抓完才整批合併；沒輪到、失敗的山屋保留舊資料，每處各自記 scrapedAt，網站據此標「舊資料」。
  3. 官網維護時段（預設 23:00–07:00，台北時間）不抓。
  4. 對官網友善：單一瀏覽器、宿營地之間間隔 1.5 秒、每小時一輪；官網回 429／5xx 或連續 3 處失敗就整輪停手。

執行（本機也可以跑）：
  python scraper.py --state data/state.json --out data/yushan.json
  加 --force 忽略維護時段；加 --dump-html 資料夾 把官網原始 HTML 存下來（官網改版時更新測試素材用）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

# ─────────────────────────────────────────────────────────────────────────────
# 設定（全部可用環境變數覆寫）
# ─────────────────────────────────────────────────────────────────────────────

TZ = ZoneInfo("Asia/Taipei")
SOURCE_URL = os.getenv("CABIN_SOURCE_URL", "https://hike.taiwan.gov.tw/bed_6.aspx")
ATTRIBUTION = "資料來源：臺灣登山申請一站式服務網（內政部國家公園署）"

REFRESH_SECONDS = int(os.getenv("CABIN_REFRESH_SECONDS", "3600"))  # 一輪間隔，預設 1 小時
CABIN_DELAY_SECONDS = float(os.getenv("CABIN_DELAY_SECONDS", "1.5"))  # 宿營地之間的間隔
MAX_BACKOFF_SECONDS = int(os.getenv("CABIN_MAX_BACKOFF_SECONDS", "7200"))
NAV_TIMEOUT_MS = int(os.getenv("CABIN_NAV_TIMEOUT_MS", "30000"))
STALE_AFTER_SECONDS = REFRESH_SECONDS * 2.5  # 單處或整批多久沒成功更新就標「資料可能過期」（預設 2.5 小時）
MAX_FAIL_STREAK = 3  # 連續幾處失敗就中止本輪（官網多半是改版或出問題，硬打沒意義）

# 維護時段（台北時間）。需求書說官網 23:00–07:00 關閉維護；官網頁面上查不到公告，所以做成可調。
# 設成同一個值（例如都填 0）就等於關掉休眠。
QUIET_START = os.getenv("CABIN_QUIET_START", "23:00")
QUIET_END = os.getenv("CABIN_QUIET_END", "07:00")

# 本機已經有 Playwright 下載好的 Chromium 但版本對不上時，可直接指定執行檔
CHROMIUM_EXECUTABLE = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE") or None

log = logging.getLogger("cabin-scraper")

# ─────────────────────────────────────────────────────────────────────────────
# 資料結構
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DayStatus:
    """某宿營地某一天的狀態（官網月曆的一格）。"""

    date: str  # YYYY-MM-DD
    full: bool  # 官網顯示「額滿」
    beds_left: int
    tents_left: int
    beds_used: int
    tents_used: int
    beds_total: int  # = beds_left + beds_used（動態推算，不寫死）
    tents_total: int
    queue: int  # 排隊預約（含備取）
    reviewing: int  # 審核中
    approved: int  # 核准入園


@dataclass
class Cabin:
    id: str  # 官網下拉選單的 value（node_id）
    name: str
    lat: float | None = None
    lng: float | None = None
    elevation_m: int | None = None
    capacity: dict[str, int | None] = field(default_factory=dict)  # 頁首公告的承載量
    water: str | None = None  # 官網「水源」欄，例如「有」
    water_notes: list[str] = field(default_factory=list)  # 介紹文字中跟供水有關的句子（例如冬季缺水）
    water_level: str = "unknown"  # none / unstable / seasonal / ok / unknown，見 classify_water()
    signals: list[str] = field(default_factory=list)  # 官網「訊號」欄，例如 GSM、無線電
    days: list[DayStatus] = field(default_factory=list)
    scraped_at: str | None = None
    error: str | None = None  # 這一輪抓取失敗的原因（資料保留上一輪）


# ─────────────────────────────────────────────────────────────────────────────
# 解析器（純函式，不碰網路，tests/ 直接拿真實 HTML 測）
# ─────────────────────────────────────────────────────────────────────────────

_INT = re.compile(r"-?\d+")
_SDATE = re.compile(r"sdate=(\d{4}-\d{2}-\d{2})")
_NODE = re.compile(r"node_id=(\d+)")


def _ints(text: str) -> list[int]:
    return [int(x) for x in _INT.findall(text)]


def _label_number(text: str, label: str) -> int:
    """從「排隊預約 4」這類文字取出數字；找不到回 0。"""
    m = re.search(re.escape(label) + r"\s*(\d+)", text)
    return int(m.group(1)) if m else 0


def parse_day_cell(anchor: Node) -> DayStatus | None:
    """解析月曆中的一格（<a href="...sdate=2026-10-26">）。格式不認得就回 None，不要猜。"""
    href = anchor.attributes.get("href") or ""
    m = _SDATE.search(href)
    if not m:
        return None
    text = anchor.text(separator=" ").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()

    # 格尾的括號 = 已佔用 (床位,營位)；「餘額 (x,y)」是另一組，兩組要分開讀。
    # 格式不完整（例如少了已佔用那組）就回 None，絕不拿餘額那組來充當已佔用——
    # 否則排雲山莊的總量會從 136 算成 20，而且看起來完全正常。
    pairs = list(re.finditer(r"\(\s*(\d+)\s*,\s*(\d+)\s*\)", text))
    left_m = re.search(r"餘額\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", text)
    left_paren_at = left_m.start() + left_m.group(0).index("(") if left_m else -1
    used_pairs = [p for p in pairs if p.start() != left_paren_at]
    if len(used_pairs) != 1:
        return None
    beds_used, tents_used = int(used_pairs[0].group(1)), int(used_pairs[0].group(2))

    full = "額滿" in text
    if full:
        if left_m:  # 同一格同時寫「額滿」又寫「餘額」，看不懂就不要猜
            return None
        beds_left = tents_left = 0
    else:
        if not left_m:
            return None
        beds_left, tents_left = int(left_m.group(1)), int(left_m.group(2))

    return DayStatus(
        date=m.group(1),
        full=full,
        beds_left=beds_left,
        tents_left=tents_left,
        beds_used=beds_used,
        tents_used=tents_used,
        beds_total=beds_left + beds_used,
        tents_total=tents_left + tents_used,
        queue=_label_number(text, "排隊預約"),
        reviewing=_label_number(text, "審核中"),
        approved=_label_number(text, "核准入園"),
    )


def parse_cabin_options(html: str) -> list[tuple[str, str]]:
    """宿營地下拉選單 → [(id, 名稱)]。清單從官網動態讀，不寫死 27 處。"""
    tree = HTMLParser(html)
    out: list[tuple[str, str]] = []
    for opt in tree.css("#con_rooms option"):
        value = (opt.attributes.get("value") or "").strip()
        if value:
            out.append((value, opt.text(strip=True)))
    return out


def dms_to_decimal(deg: str, minute: str, sec: str) -> float:
    return int(deg) + int(minute) / 60 + float(sec) / 3600


def tm2_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """TWD97 二度分帶（TM2，中央經線 121°E）平面座標 → 經緯度。TWD97 與 WGS84 差距在公分級，直接當 WGS84 用。

    標準的橫麥卡托逆算（GRS80 橢球），免裝 pyproj。回傳 (lng, lat)。
    """
    a = 6378137.0
    f = 1 / 298.257222101
    k0 = 0.9999
    lon0 = math.radians(121)
    x0 = 250000.0

    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    xx = x - x0
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2**2 / 64 - 5 * e2**3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    c1 = ep2 * math.cos(phi1) ** 2
    t1 = math.tan(phi1) ** 2
    n1 = a / math.sqrt(1 - e2 * math.sin(phi1) ** 2)
    r1 = a * (1 - e2) / (1 - e2 * math.sin(phi1) ** 2) ** 1.5
    d = xx / (n1 * k0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * ep2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * ep2 - 3 * c1**2) * d**6 / 720
    )
    lon = lon0 + (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * ep2 + 24 * t1**2) * d**5 / 120
    ) / math.cos(phi1)
    return math.degrees(lon), math.degrees(lat)


_DMS_LNG = re.compile(r"東經\s*(\d+)\s*[°度]\s*(\d+)\s*[’'′分]\s*([\d.]+)")
_DMS_LAT = re.compile(r"北緯\s*(\d+)\s*[°度]\s*(\d+)\s*[’'′分]\s*([\d.]+)")
_TM2 = re.compile(r"X\s*座標\s*([\d.]+)\D{0,6}Y\s*座標\s*([\d.]+)")


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """介紹文字 → (lng, lat)。先找度分秒經緯度，沒有再用 TM2 座標換算；都沒有回 None。"""
    lng_m, lat_m = _DMS_LNG.search(text), _DMS_LAT.search(text)
    if lng_m and lat_m:
        lng, lat = dms_to_decimal(*lng_m.groups()), dms_to_decimal(*lat_m.groups())
        if 119 < lng < 123 and 21 < lat < 26:  # 台灣本島範圍外就當解析錯，改試 TM2
            return lng, lat
    tm = _TM2.search(text)
    if tm:
        lng, lat = tm2_to_wgs84(float(tm.group(1)), float(tm.group(2)))
        if 119 < lng < 123 and 21 < lat < 26:
            return lng, lat
    return None


def _span_text(tree: HTMLParser, css: str) -> str | None:
    node = tree.css_first(css)
    return node.text(strip=True) if node else None


def _split_list_field(raw: str | None) -> list[str]:
    """「訊號 : GSM、無線電、衛星、」→ ["GSM", "無線電", "衛星"]"""
    if not raw:
        return []
    body = raw.split(":", 1)[-1].split("：", 1)[-1]
    return [s.strip() for s in re.split(r"[、,，]", body) if s.strip()]


def classify_water(water: str | None, notes: list[str]) -> str:
    """把官網的「水源」欄＋介紹文字歸成四級，給地圖上的水滴圖示用。

    這是官網的「固定說明」，不是今天的即時水況（即時缺水公告在官網最新消息，第二版再接）。
      none     官網寫「無」或「水源缺乏」
      unstable 不穩定、常乾涸
      seasonal 平時有水、特定季節（冬季、枯水期）會缺
      ok       有水源
      unknown  官網沒寫
    """
    text = " ".join(notes)
    if water == "無" or re.search(r"水源缺乏|無水源|沒有水源|無水", text):
        return "none"
    if re.search(r"不穩|常乾|乾涸|乾沽|乾枯", text):
        return "unstable"
    if re.search(r"(冬季|枯水期|乾季).{0,12}(缺水|結冰|短缺)", text):
        return "seasonal"
    if water == "有" or re.search(r"水源(位於|穩定|充足)|活水源|水源:|水源：", text):
        return "ok"
    return "unknown"


def parse_cabin_page(html: str) -> dict[str, Any]:
    """宿營地查詢結果頁 → 這處宿營地的基本資料＋這個月的每日狀態。"""
    tree = HTMLParser(html)

    selected = tree.css_first("#con_rooms option[selected]")
    cabin_id = (selected.attributes.get("value") or "").strip() if selected else ""
    name = selected.text(strip=True) if selected else ""

    capacity: dict[str, int | None] = {}
    for key, css in (
        ("weekday_beds", "#con_generalnum"),
        ("holiday_beds", "#con_holidaynum"),
        ("weekday_tents", "#con_generalnumt"),
        ("holiday_tents", "#con_holidaynumt"),
    ):
        val = _span_text(tree, css)
        nums = _ints(val or "")
        capacity[key] = nums[0] if nums else None

    days: dict[str, DayStatus] = {}
    for a in tree.css('a[href*="sdate="]'):
        # 只收這處宿營地的格子（href 帶 node_id），避免頁面其他連結混進來
        node_m = _NODE.search(a.attributes.get("href") or "")
        if cabin_id and node_m and node_m.group(1) != cabin_id:
            continue
        day = parse_day_cell(a)
        if day:
            days[day.date] = day

    # 驗收門檻：另外從「整頁文字」數有幾格看起來有資料（不靠連結格式），
    # 跟真正解析成功的格數比對。官網改版（連結換名、少欄位）時兩個數字會對不上，
    # 呼叫端就知道這頁「讀不懂」，而不是把它當成「沒開放」蓋掉舊資料。
    body_text = tree.body.text(separator=" ").replace("\xa0", " ") if tree.body else ""
    cells_expected = len(re.findall(r"餘額\s*\(\s*\d+\s*,|額滿\s*排隊預約", body_text))

    # 介紹文字：座標、海拔、供水說明都在 con_waters 前面的那段 <p>
    desc = ""
    waters = tree.css_first("#con_waters")
    if waters is not None:
        container = waters.parent.parent if waters.parent is not None else None
        if container is not None:
            desc = container.text(separator="\n")
    if not desc:
        body = tree.body
        desc = body.text(separator="\n") if body else ""

    coords = parse_coordinates(desc)
    elev_m = re.search(r"海拔(?:高度)?\s*[:：]?\s*約?\s*(\d{3,4})\s*(?:m|公尺)", desc)

    water_raw = _split_list_field(_span_text(tree, "#con_waters"))
    water_notes = []
    for sentence in re.split(r"[\n。]", desc):
        s = re.sub(r"^\s*(?:◎|\d+\.)\s*", "", sentence).strip()
        if re.fullmatch(r"水源\s*[:：].{0,4}", s) or "熱水" in s:
            continue  # 「水源 : 有、」那一欄已經另外放在 water；熱水說明跟水源無關
        if s and re.search(r"缺水|水源|供水|取水|水池|水塔", s) and len(s) <= 80:
            water_notes.append(s)

    water_notes = water_notes[:3]
    year = _span_text(tree, "#con_ddlYear option[selected]")
    month = _span_text(tree, "#con_ddlMonth option[selected]")

    return {
        "id": cabin_id,
        "name": name,
        "lng": coords[0] if coords else None,
        "lat": coords[1] if coords else None,
        "elevation_m": int(elev_m.group(1)) if elev_m else None,
        "capacity": capacity,
        "water": "、".join(water_raw) if water_raw else None,
        "water_notes": water_notes,
        "water_level": classify_water("、".join(water_raw) if water_raw else None, water_notes),
        "signals": _split_list_field(_span_text(tree, "#con_signals")),
        "days": sorted(days.values(), key=lambda d: d.date),
        "month": f"{year}-{int(month):02d}" if year and month and month.isdigit() else None,
        "cells_expected": cells_expected,
        "cells_parsed": len(days),
    }


class ParseError(RuntimeError):
    """官網頁面讀不懂（改版、少欄位、翻月失敗）。這一處保留上一輪資料，不能當成「沒開放」。"""


def check_month_page(page: dict[str, Any], cabin_id: str, expected_month: str) -> None:
    """一頁月曆的驗收：宿營地對、月份對、每一格都解析成功。不過關就丟 ParseError。"""
    if page["id"] != cabin_id:
        raise ParseError(f"宿營地不是 {cabin_id}（拿到 {page['id'] or '空白'}）")
    if page["month"] != expected_month:
        raise ParseError(f"月份不是 {expected_month}（拿到 {page['month'] or '空白'}）")
    if page["cells_parsed"] != page["cells_expected"]:
        raise ParseError(f"{expected_month} 有 {page['cells_expected']} 格資料，只讀懂 {page['cells_parsed']} 格")


def month_after(month: str) -> str:
    """'2026-12' → '2027-01'"""
    y, m = (int(x) for x in month.split("-"))
    return f"{y + (m == 12)}-{m % 12 + 1:02d}"


# ─────────────────────────────────────────────────────────────────────────────
# 維護時段守衛
# ─────────────────────────────────────────────────────────────────────────────


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def in_quiet_hours(now: datetime | None = None, start: str = QUIET_START, end: str = QUIET_END) -> bool:
    """是否在官網維護時段內（可跨午夜，例如 23:00–07:00）。start == end 代表不休眠。"""
    now = (now or datetime.now(TZ)).astimezone(TZ)
    s, e, t = _hm(start), _hm(end), now.time()
    if s == e:
        return False
    if s < e:
        return s <= t < e
    return t >= s or t < e


def seconds_until_quiet_end(now: datetime | None = None, end: str = QUIET_END) -> float:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    e = _hm(end)
    target = now.replace(hour=e.hour, minute=e.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ─────────────────────────────────────────────────────────────────────────────
# 快取（記憶體 dict＋cache.json）
# ─────────────────────────────────────────────────────────────────────────────


class CabinCache:
    def __init__(self, path: Path):
        self.path = path
        self.cabins: dict[str, Cabin] = {}
        self.updated_at: str | None = None  # 最後一輪成功（至少一處成功）的時間
        self.last_attempt_at: str | None = None
        self.last_error: str | None = None
        self.consecutive_failures = 0
        self._lock = asyncio.Lock()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.updated_at = raw.get("updated_at")
            for c in raw.get("cabins", []):
                days = [DayStatus(**d) for d in c.pop("days", [])]
                self.cabins[c["id"]] = Cabin(**c, days=days)
            log.info("從 %s 載入 %d 處快取（%s）", self.path.name, len(self.cabins), self.updated_at)
        except Exception as exc:  # 檔案壞了就當沒快取，不要讓服務起不來
            log.warning("cache.json 讀取失敗，忽略：%s", exc)

    def _save(self) -> None:
        data = {"updated_at": self.updated_at, "cabins": [asdict(c) for c in self.cabins.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)  # 原子替換：寫到一半當機也不會留下半個檔

    async def replace(self, fresh: dict[str, Cabin], errors: dict[str, str]) -> None:
        async with self._lock:
            # 以上一輪為底：這輪沒輪到的（中途停手）、失敗的，都保留舊資料；
            # 每處自己的 scraped_at 不變，過期與否由 snapshot() 逐處判斷
            merged: dict[str, Cabin] = dict(self.cabins)
            merged.update(fresh)
            for cid, err in errors.items():
                if cid in merged:
                    merged[cid].error = err
            self.cabins = merged
            now = datetime.now(TZ).isoformat(timespec="seconds")
            self.last_attempt_at = now
            if fresh:
                self.updated_at = now
            self._save()

    def snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(TZ)
        cabins = [_cabin_json(c, is_stale(c.scraped_at, now)) for c in self.cabins.values()]
        stale_count = sum(1 for c in cabins if c["stale"])
        return {
            "updatedAt": self.updated_at,
            "lastAttemptAt": self.last_attempt_at,
            # 整批過期＝最近一次成功太久以前；partial＝有些山屋這輪失敗或資料過期
            "stale": is_stale(self.updated_at, now),
            "partial": stale_count > 0 or any(c["error"] for c in cabins),
            "staleCount": stale_count,
            "quietHours": in_quiet_hours(now),
            "quietWindow": f"{QUIET_START}–{QUIET_END}",
            "source": SOURCE_URL,
            "attribution": ATTRIBUTION,
            "cabins": cabins,
        }


def is_stale(iso: str | None, now: datetime) -> bool:
    """超過 2.5 輪沒成功更新就算過期（預設 2.5 小時）。沒有時間＝從沒成功過＝過期。"""
    if not iso:
        return True
    return (now - datetime.fromisoformat(iso)).total_seconds() > STALE_AFTER_SECONDS


def _cabin_json(c: Cabin, stale: bool) -> dict[str, Any]:
    """對外 JSON 用 camelCase，前端直接吃。"""
    return {
        "id": c.id,
        "stale": stale,
        "name": c.name,
        "lat": c.lat,
        "lng": c.lng,
        "elevationM": c.elevation_m,
        "capacity": {
            "weekdayBeds": c.capacity.get("weekday_beds"),
            "holidayBeds": c.capacity.get("holiday_beds"),
            "weekdayTents": c.capacity.get("weekday_tents"),
            "holidayTents": c.capacity.get("holiday_tents"),
        },
        "water": c.water,
        "waterNotes": c.water_notes,
        "waterLevel": c.water_level,
        "signals": c.signals,
        "scrapedAt": c.scraped_at,
        "error": c.error,
        "days": [
            {
                "date": d.date,
                "full": d.full,
                "bedsLeft": d.beds_left,
                "tentsLeft": d.tents_left,
                "bedsTotal": d.beds_total,
                "tentsTotal": d.tents_total,
                "queue": d.queue,
                "reviewing": d.reviewing,
                "approved": d.approved,
            }
            for d in c.days
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# 爬蟲（Playwright）
# ─────────────────────────────────────────────────────────────────────────────


class SourceUnavailable(RuntimeError):
    """官網回 429（太多請求）或 5xx（伺服器出錯）：整輪立刻停手。"""


def check_status(status: int | None) -> None:
    if status is not None and (status == 429 or status >= 500):
        raise SourceUnavailable(f"HTTP {status}")
    if status is not None and status >= 400:
        raise RuntimeError(f"HTTP {status}")


async def _goto_source(page) -> None:
    resp = await page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    check_status(resp.status if resp else None)


async def _postback(page, action) -> None:
    """觸發一次 ASP.NET postback 並等它真的做完（UpdatePanel 局部更新或整頁回傳都適用）。"""
    async with page.expect_response(
        lambda r: r.request.method == "POST" and "bed_6" in r.url, timeout=NAV_TIMEOUT_MS
    ) as resp_info:
        await action()
    check_status((await resp_info.value).status)
    await page.wait_for_load_state("domcontentloaded")
    await page.wait_for_function(
        """() => !(window.Sys && Sys.WebForms && Sys.WebForms.PageRequestManager
                 && Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack())""",
        timeout=NAV_TIMEOUT_MS,
    )


async def scrape_one(page, cabin_id: str, dump_dir: Path | None = None) -> Cabin:
    """抓一處宿營地：本月＋下個月（日期接近月底時才湊得滿「接下來 7 天」）。"""
    # 每處都重新 GET 一次，讓官網的月份狀態回到「本月」，不用追蹤上一處停在哪個月
    await _goto_source(page)
    await page.select_option("#con_rooms", cabin_id)
    await _postback(page, lambda: page.click("#con_btnsearch"))
    html_this = await page.content()
    await _postback(page, lambda: page.click("#con_lbDownMonth"))
    html_next = await page.content()

    if dump_dir:  # 先存再驗收：驗收失敗時才有原始頁面可以查
        dump_dir.mkdir(parents=True, exist_ok=True)
        (dump_dir / f"{cabin_id}-this.html").write_text(html_this, encoding="utf-8")
        (dump_dir / f"{cabin_id}-next.html").write_text(html_next, encoding="utf-8")

    # 兩頁都要過驗收（宿營地對、月份對、每格都讀懂），任何一頁不過就整處算失敗、保留舊資料
    current = datetime.now(TZ).strftime("%Y-%m")
    this_month = parse_cabin_page(html_this)
    next_month = parse_cabin_page(html_next)
    check_month_page(this_month, cabin_id, current)
    check_month_page(next_month, cabin_id, month_after(current))

    days = {d.date: d for d in this_month["days"]}
    days.update({d.date: d for d in next_month["days"]})

    return Cabin(
        id=cabin_id,
        name=this_month["name"],
        lat=this_month["lat"],
        lng=this_month["lng"],
        elevation_m=this_month["elevation_m"],
        capacity=this_month["capacity"],
        water=this_month["water"],
        water_notes=this_month["water_notes"],
        water_level=this_month["water_level"],
        signals=this_month["signals"],
        days=sorted(days.values(), key=lambda d: d.date),
        scraped_at=datetime.now(TZ).isoformat(timespec="seconds"),
    )


async def scrape_all(dump_dir: Path | None = None) -> tuple[dict[str, Cabin], dict[str, str]]:
    """抓全部宿營地。回傳 (成功的, {失敗的 id: 原因})。"""
    from playwright.async_api import async_playwright  # 延後載入：跑單元測試不需要瀏覽器

    fresh: dict[str, Cabin] = {}
    errors: dict[str, str] = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROMIUM_EXECUTABLE)
        try:
            context = await browser.new_context(locale="zh-TW", timezone_id="Asia/Taipei")
            page = await context.new_page()
            # 地圖資料用不到圖片／字型，擋掉省官網流量
            await page.route(
                re.compile(r"\.(png|jpe?g|gif|svg|webp|woff2?|ttf|ico)(\?|$)", re.I),
                lambda route: route.abort(),
            )
            await _goto_source(page)
            options = parse_cabin_options(await page.content())
            if not options:
                raise RuntimeError("官網宿營地清單是空的（頁面改版或維護中？）")
            if dump_dir:
                dump_dir.mkdir(parents=True, exist_ok=True)
                (dump_dir / "list.html").write_text(await page.content(), encoding="utf-8")

            streak = 0  # 連續失敗幾處
            for i, (cid, name) in enumerate(options):
                if i:
                    await asyncio.sleep(CABIN_DELAY_SECONDS)
                if in_quiet_hours():
                    log.info("進入維護時段，本輪停在第 %d 處", i)
                    break
                try:
                    fresh[cid] = await scrape_one(page, cid, dump_dir)
                    streak = 0
                    log.info("✓ %s：%d 天", name, len(fresh[cid].days))
                except SourceUnavailable as exc:
                    # 官網說「太忙／壞了」：馬上停手，不要把剩下的宿營地也打一遍
                    errors[cid] = f"{type(exc).__name__}: {exc}"[:200]
                    log.warning("官網回 %s，本輪中止", exc)
                    break
                except Exception as exc:
                    errors[cid] = f"{type(exc).__name__}: {exc}"[:200]
                    streak += 1
                    log.warning("✗ %s：%s", name, errors[cid])
                    if streak >= MAX_FAIL_STREAK:
                        log.warning("連續 %d 處失敗，本輪中止（官網可能改版或異常）", streak)
                        break
        finally:
            await browser.close()
    return fresh, errors


# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# 命令列：抓一輪 → 合併 → 寫檔
# ─────────────────────────────────────────────────────────────────────────────


def write_public(snapshot: dict[str, Any], out: Path) -> None:
    """網站讀的那份 JSON（camelCase）。原子寫入，寫到一半當掉不會留下半個檔。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)


async def run_once(state: Path, out: Path, *, force: bool = False, dump_dir: Path | None = None) -> int:
    """抓一輪。回傳結束碼：0＝至少一處成功（或維護時段跳過）、1＝全部失敗、2＝跳過且沒有舊資料。"""
    cache = CabinCache(state)
    cache.load()

    if in_quiet_hours() and not force:
        log.info("官網維護時段（%s–%s），這次不抓", QUIET_START, QUIET_END)
        return 0

    started = time.monotonic()
    try:
        fresh, errors = await scrape_all(dump_dir)
        cache.last_error = f"{len(errors)} 處失敗" if errors else None
    except Exception as exc:  # 連清單頁都打不開
        fresh, errors = {}, {}
        cache.last_error = f"{type(exc).__name__}: {exc}"[:300]
        log.error("本輪失敗：%s", cache.last_error)

    await cache.replace(fresh, errors)
    snapshot = cache.snapshot()
    snapshot["lastError"] = cache.last_error
    write_public(snapshot, out)
    log.info(
        "本輪完成：%d 成功、%d 失敗、%d 處資料過期，耗時 %.0f 秒",
        len(fresh), len(errors), snapshot["staleCount"], time.monotonic() - started,
    )
    for cid, err in errors.items():
        log.warning("  %s：%s", cid, err)
    return 0 if fresh else 1


def _cli() -> None:
    ap = argparse.ArgumentParser(description="抓一輪玉山宿營地床位，合併上一份資料後寫檔")
    ap.add_argument("--state", type=Path, default=Path("data/state.json"), help="內部狀態（合併用）")
    ap.add_argument("--out", type=Path, default=Path("data/yushan.json"), help="給網站讀的 JSON")
    ap.add_argument("--force", action="store_true", help="維護時段也照抓")
    ap.add_argument("--dump-html", type=Path, help="把官網原始 HTML 存到這個資料夾")
    args = ap.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    sys.exit(asyncio.run(run_once(args.state, args.out, force=args.force, dump_dir=args.dump_html)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 終端機印中文不亂碼
    _cli()
