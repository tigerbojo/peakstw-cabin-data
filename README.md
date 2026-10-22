# peakstw-cabin-data

玉山國家公園 27 處山屋／營地的每日床位，每小時更新一次。給 [PEAKSTW 山屋 3D 觀測站](https://www.peakstw.com/cabins) 用。

## 資料在哪

最新一份永遠在 `data` 分支（只留最新，不累積歷史）：

```
https://raw.githubusercontent.com/tigerbojo/peakstw-cabin-data/data/yushan.json
```

每間山屋有：座標、海拔、承載量、水源說明、訊號，以及接下來約一個多月每天的
餘額、排隊預約（候補）、審核中、核准入園。每間各自帶 `scrapedAt`（最後一次成功抓到的時間）
與 `stale`（超過 2.5 小時沒更新）。

## 資料來源與授權

床位資料來源：[臺灣登山申請一站式服務網](https://hike.taiwan.gov.tw/bed_6.aspx)（內政部國家公園署），
依該站〈資料開放宣告〉再利用，須註明出處、不得竄改。**申請與最終名額一律以官方網站為準。**

## 怎麼運作

`.github/workflows/scrape.yml` 每小時（台北 07:17～22:17，避開官網夜間維護）：

1. 從 `data` 分支拿上一份 `state.json`
2. `scraper.py` 用 Playwright 開瀏覽器逐一查 27 處（間隔 1.5 秒），每頁月曆都要過驗收
   （宿營地對、月份對、每格都讀懂），不過關就保留那處上一輪的資料
3. 官網回 429／5xx 或連續 3 處失敗就整輪停手
4. 結果推回 `data` 分支

## 本機跑

```
python -m pip install -r requirements.txt
python -m playwright install chromium
python scraper.py --state data/state.json --out data/yushan.json --force
python -m pytest tests -q
```

`tests/fixtures/` 是 2026-10-21 存下的官網真實頁面。官網改版時用
`python scraper.py --force --dump-html dump` 重存一份，換掉素材，看哪個測試壞掉就知道哪裡變了。
