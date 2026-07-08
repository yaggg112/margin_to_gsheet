"""
台股大盤融資維持率（每日）- 完整版（上市 + 上櫃）

資料來源：
  上市融資彙總：https://www.twse.com.tw/exchangeReport/MI_MARGN?selectType=MS
  上市個股融資：https://www.twse.com.tw/exchangeReport/MI_MARGN?selectType=ALL
  上市個股收盤：https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL (CSV)
  上櫃融資彙總：https://www.tpex.org.tw/www/zh-tw/margin/balance
  上櫃個股收盤：https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php

大盤融資維持率 = (上市融資市值 + 上櫃融資市值) / (上市融資金額 + 上櫃融資金額) × 100%
"""

from __future__ import annotations

import io
import requests
import pandas as pd
from datetime import datetime, timedelta

TWSE_MARGIN_URL  = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
STOCK_DAY_URL    = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
TPEX_BALANCE_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
TPEX_CLOSE_URL   = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarginFetcher/1.0)"}


def _n(s) -> float:
    """'1,234,567' → 1234567.0"""
    if s is None: return 0.0
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "X", "N/A", "-"): return 0.0
    try: return float(s)
    except ValueError: return 0.0


def _tw_date(date_str: str) -> str:
    """'20260630' → '115/06/30'（西元轉民國）"""
    dt = datetime.strptime(date_str, "%Y%m%d")
    return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"


# ── 上市：融資金額彙總（分母之一）───────────────────────────────────────────

def fetch_twse_credit_summary(date: str) -> dict:
    params = {"response": "json", "date": date, "selectType": "MS"}
    r = requests.get(TWSE_MARGIN_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        raise ValueError(f"{date} 上市彙總查無資料")
    table = next((t for t in data["tables"] if t.get("fields")), None)
    if not table:
        raise ValueError(f"{date} 上市彙總找不到 table")
    fields    = table["fields"]
    today_idx = fields.index("今日餘額")
    rows      = {row[0]: _n(row[today_idx]) for row in table["data"]}
    return {
        "上市融資金元_今日": rows.get("融資金額(仟元)", 0.0) * 1000,
    }


# ── 上市：個股融資張數（分子之一）───────────────────────────────────────────

def fetch_twse_margin_by_stock(date: str) -> pd.DataFrame:
    """tables[1]，16欄，index[0]=代號，index[6]=融資今日餘額。只取純4碼。"""
    params = {"response": "json", "date": date, "selectType": "ALL"}
    r = requests.get(TWSE_MARGIN_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("stat") != "OK":
        raise ValueError(f"{date} 上市個股融資查無資料")
    stock_table = next(
        (t for t in data["tables"] if t.get("data") and len(t["fields"]) == 16), None
    )
    if not stock_table:
        raise ValueError(f"{date} 找不到個股融資明細 table")
    records = [{"證券代號": row[0], "融資張數": _n(row[6])} for row in stock_table["data"]]
    df = pd.DataFrame(records)
    return df[df["證券代號"].str.match(r"^\d{4}$")]


# ── 上市：個股收盤價 ──────────────────────────────────────────────────────────

def fetch_twse_close_prices() -> pd.DataFrame:
    """STOCK_DAY_ALL CSV，只取純4碼。"""
    r = requests.get(STOCK_DAY_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.content.decode("utf-8")), dtype={"證券代號": str})
    df = df[df["證券代號"].str.match(r"^\d{4}$")]
    df["收盤價"] = pd.to_numeric(df["收盤價"], errors="coerce").fillna(0.0)
    return df[["證券代號", "收盤價"]]


# ── 上櫃：融資金額彙總 + 個股融資張數（分母之二 + 分子之二）────────────────

def fetch_tpex_balance() -> tuple[float, pd.DataFrame]:
    """
    從上櫃融資餘額 API 同時取得：
      1. 上櫃融資金額今日餘額（元）→ 分母
      2. 上櫃個股融資張數 DataFrame → 分子（需再乘收盤價）

    已驗證格式（2026-07-01）：
      tables[0].fields = ["代號","名稱","前資餘額(張)","資買","資賣","現償","資餘額",
                          "資屬證金","資使用率(%)","資限額", ...]
      tables[0].data[i][0] = 代號，[i][6] = 資餘額（融資張數）
      tables[0].summary[1] = ["","融資金(仟元)", 買進, 賣出, 現償, 前日, 今日, ...]
        → index[6] = 上櫃融資金今日餘額(仟元)
    """
    r = requests.get(TPEX_BALANCE_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    table = next((t for t in data["tables"] if t.get("summary") and t.get("data")), None)
    if not table:
        raise ValueError("上櫃找不到含 summary 和 data 的 table")

    # ── 分母：融資金額 ──
    margin_money_row = next(
        (row for row in table["summary"] if "融資金" in str(row[1])), None
    )
    if margin_money_row is None:
        raise ValueError(f"找不到融資金列，summary={table['summary']}")
    tpex_margin_amount = _n(margin_money_row[6]) * 1000  # 仟元 → 元

    # ── 分子：個股融資張數 ──
    # 欄位：[0]=代號, [1]=名稱, [6]=資餘額（融資張數）
    # 排除 ETF/ETN（代號含字母或非純4碼）
    records = [
        {"證券代號": row[0], "融資張數": _n(row[6])}
        for row in table["data"]
        if row[0] and str(row[0]).strip()
    ]
    tpex_margin_df = pd.DataFrame(records)
    tpex_margin_df = tpex_margin_df[
        tpex_margin_df["證券代號"].str.match(r"^\d{4}$")
    ]

    return tpex_margin_amount, tpex_margin_df


# ── 上櫃：個股收盤價 ──────────────────────────────────────────────────────────

def fetch_tpex_close_prices(date: str) -> pd.DataFrame:
    """
    抓上櫃個股收盤價。
    日期格式需轉民國年，例如 '20260630' → '115/06/30'。

    已驗證格式（2026-07-02）：
      回傳 JSON 的 tables[0].data，欄位順序：
      [0]=代號, [1]=名稱, [2]=收盤, [3]=漲跌, [4]=開盤,
      [5]=最高, [6]=最低, [7]=均價, [8]=成交股數, ...
    只保留純4碼數字代號（排除 ETF/ETN，如 006201、00679B）。
    """
    tw_date = _tw_date(date)
    params = {"l": "zh-tw", "d": tw_date, "s": "0,asc,0"}
    r = requests.get(TPEX_CLOSE_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()

    # 資料在 tables[0].data（不是 aaData）
    table = next((t for t in data.get("tables", []) if t.get("data")), None)
    if table is None:
        raise ValueError(f"{date} 上櫃收盤價找不到 table，實際 keys：{list(data.keys())}")

    rows = table["data"]
    if not rows:
        raise ValueError(f"{date} 上櫃收盤價查無資料")

    # [0]=代號, [2]=收盤（字串含千分位，用 _n() 轉換）
    records = [
        {"證券代號": str(row[0]).strip(), "收盤價": _n(row[2])}
        for row in rows
        if row[0]
    ]
    df = pd.DataFrame(records)
    df = df[df["證券代號"].str.match(r"^\d{4}$")]  # 只取純4碼一般股票
    return df[["證券代號", "收盤價"]]


# ── 計算大盤融資維持率 ────────────────────────────────────────────────────────

def get_market_margin_ratio(date: str) -> dict:
    """
    計算指定日期大盤融資維持率。

    分子 = 上市融資市值 + 上櫃融資市值
    分母 = 上市融資金額 + 上櫃融資金額
    """
    # 上市
    twse_summary   = fetch_twse_credit_summary(date)
    twse_margin_df = fetch_twse_margin_by_stock(date)
    twse_price_df  = fetch_twse_close_prices()

    twse_merged = twse_margin_df.merge(twse_price_df, on="證券代號", how="left")
    twse_merged["收盤價"]  = twse_merged["收盤價"].fillna(0.0)
    twse_merged["融資市值"] = twse_merged["融資張數"] * 1000 * twse_merged["收盤價"]
    twse_value = twse_merged["融資市值"].sum()

    # 上櫃
    tpex_margin_amount, tpex_margin_df = fetch_tpex_balance()
    tpex_price_df = fetch_tpex_close_prices(date)

    tpex_merged = tpex_margin_df.merge(tpex_price_df, on="證券代號", how="left")
    tpex_merged["收盤價"]  = tpex_merged["收盤價"].fillna(0.0)
    tpex_merged["融資市值"] = tpex_merged["融資張數"] * 1000 * tpex_merged["收盤價"]
    tpex_value = tpex_merged["融資市值"].sum()

    # 合計
    total_value  = twse_value + tpex_value
    total_margin = twse_summary["上市融資金元_今日"] + tpex_margin_amount
    ratio        = (total_value / total_margin * 100) if total_margin else None

    return {
        "日期":               date,
        "上市融資市值(億元)":  round(twse_value        / 1e8, 2),
        "上櫃融資市值(億元)":  round(tpex_value        / 1e8, 2),
        "融資市值合計(億元)":  round(total_value        / 1e8, 2),
        "上市融資金額(億元)":  round(twse_summary["上市融資金元_今日"] / 1e8, 2),
        "上櫃融資金額(億元)":  round(tpex_margin_amount / 1e8, 2),
        "融資金額合計(億元)":  round(total_margin        / 1e8, 2),
        "大盤融資維持率(%)":   round(ratio, 2) if ratio else None,
    }


# ── 自動往前找最近交易日 ─────────────────────────────────────────────────────

def get_market_margin_ratio_auto(
    start_date: str | None = None,
    max_lookback: int = 10,
) -> dict:
    start_dt = (
        datetime.now() if start_date is None
        else datetime.strptime(start_date, "%Y%m%d")
    )
    last_err = None
    for i in range(max_lookback):
        try_date = (start_dt - timedelta(days=i)).strftime("%Y%m%d")
        try:
            result = get_market_margin_ratio(try_date)
            if i > 0:
                print(f"提示：{start_dt.strftime('%Y%m%d')} 查無資料，已改抓 {try_date}。")
            return result
        except (ValueError, KeyError) as e:
            last_err = e
            continue
    raise ValueError(f"往前找了 {max_lookback} 天都查不到資料。最後錯誤：{last_err}")


# ── 主程式 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today = datetime.now().strftime("%Y%m%d")
    print(f"嘗試抓取日期：{today}\n")

    try:
        r = get_market_margin_ratio_auto(today)
        print(f"實際資料日期　　：{r['日期']}")
        print(f"上市融資市值　　：{r['上市融資市值(億元)']:>10,.2f} 億元")
        print(f"上櫃融資市值　　：{r['上櫃融資市值(億元)']:>10,.2f} 億元")
        print(f"融資市值合計　　：{r['融資市值合計(億元)']:>10,.2f} 億元")
        print(f"上市融資金額　　：{r['上市融資金額(億元)']:>10,.2f} 億元")
        print(f"上櫃融資金額　　：{r['上櫃融資金額(億元)']:>10,.2f} 億元")
        print(f"融資金額合計　　：{r['融資金額合計(億元)']:>10,.2f} 億元")
        print(f"大盤融資維持率　：{r['大盤融資維持率(%)']:>10.2f} %")
    except (ValueError, KeyError) as e:
        print(f"取得資料失敗：{e}")