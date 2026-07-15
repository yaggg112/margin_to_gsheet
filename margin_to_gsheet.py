"""
台股大盤融資維持率 → Google Sheets

前置作業：
  1. 到 Google Cloud Console 建立專案，啟用 Google Sheets API 和 Google Drive API
  2. 建立「服務帳戶」，下載 JSON 金鑰檔（例如 credentials.json）
  3. 在 Google Sheets 共用給服務帳戶的 email（在試算表右上角「共用」）
  4. pip install gspread google-auth

執行：
  python margin_to_gsheet.py

Sheet 結構：
  Sheet「每日維持率」（每天執行 append 一列，最新在最上方）：
    日期 | 上市融資市值(億) | 上櫃融資市值(億) | 融資市值合計(億) |
    上市融資金額(億) | 上櫃融資金額(億) | 融資金額合計(億) |
    上市融資維持率(%) | 上櫃融資維持率(%) | 大盤融資維持率(%)

  Sheet「ETF清單」（只寫一次，之後每次執行會更新）：
    代號 | 名稱 | 市場 | 備註
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import gspread
import requests
import pandas as pd
import io
from google.oauth2.service_account import Credentials

# ── 設定區（請依實際狀況修改）────────────────────────────────────────────────
CREDENTIALS_FILE  = "service_account.json"
SPREADSHEET_ID    = "1bWB2dmwiGXp9NJ9GCTHsRja6wkHz_TJFCE-rWftLdD8"
SHEET_DAILY       = "每日維持率"
SHEET_ETF         = "ETF清單"

TWSE_MARGIN_URL  = "https://www.twse.com.tw/exchangeReport/MI_MARGN"
STOCK_DAY_URL    = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL"
TPEX_BALANCE_URL = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
TPEX_CLOSE_URL   = "https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_quote_result.php"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarginFetcher/1.0)"}
# ─────────────────────────────────────────────────────────────────────────────


def _n(s) -> float:
    if s is None: return 0.0
    s = str(s).replace(",", "").strip()
    if s in ("", "--", "X", "N/A", "-"): return 0.0
    try: return float(s)
    except ValueError: return 0.0


def _tw_date(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y%m%d")
    return f"{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}"


def _is_stock(code: str) -> bool:
    return bool(re.match(r"^\d{4}$", str(code).strip()))


def _ratio(value: float, amount: float) -> float | None:
    return round(value / amount * 100, 2) if amount else None


# ── 資料抓取 ──────────────────────────────────────────────────────────────────

def fetch_twse_data(date: str) -> tuple[float, float, list[dict]]:
    """回傳：(上市融資金額元, 上市融資市值元, ETF清單)"""
    # 融資金額彙總
    r = requests.get(TWSE_MARGIN_URL,
                     params={"response": "json", "date": date, "selectType": "MS"},
                     headers=HEADERS, timeout=20)
    data = r.json()
    if data.get("stat") != "OK":
        raise ValueError(f"{date} 上市彙總查無資料")
    table = next((t for t in data["tables"] if t.get("fields")), None)
    fields = table["fields"]
    today_idx = fields.index("今日餘額")
    rows = {row[0]: _n(row[today_idx]) for row in table["data"]}
    twse_margin_amount = rows.get("融資金額(仟元)", 0.0) * 1000

    # 個股融資張數
    r = requests.get(TWSE_MARGIN_URL,
                     params={"response": "json", "date": date, "selectType": "ALL"},
                     headers=HEADERS, timeout=20)
    data = r.json()
    if data.get("stat") != "OK":
        raise ValueError(f"{date} 上市個股融資查無資料")
    stock_table = next(
        (t for t in data["tables"] if t.get("data") and len(t["fields"]) == 16), None
    )
    all_records = [{"證券代號": row[0], "名稱": row[1], "融資張數": _n(row[6])}
                   for row in stock_table["data"]]

    etf_twse = [{"代號": d["證券代號"], "名稱": d["名稱"], "市場": "上市", "備註": "非純4碼或含字母"}
                for d in all_records if not _is_stock(d["證券代號"])]

    margin_df = pd.DataFrame([d for d in all_records if _is_stock(d["證券代號"])])

    # 個股收盤價
    r = requests.get(STOCK_DAY_URL, headers=HEADERS, timeout=30)
    price_df = pd.read_csv(io.StringIO(r.content.decode("utf-8")), dtype={"證券代號": str})
    price_df = price_df[price_df["證券代號"].str.match(r"^\d{4}$")]
    price_df["收盤價"] = pd.to_numeric(price_df["收盤價"], errors="coerce").fillna(0.0)

    merged = margin_df.merge(price_df[["證券代號", "收盤價"]], on="證券代號", how="left")
    merged["收盤價"] = merged["收盤價"].fillna(0.0)
    merged["融資市值"] = merged["融資張數"] * 1000 * merged["收盤價"]
    twse_value = merged["融資市值"].sum()

    return twse_margin_amount, twse_value, etf_twse


def fetch_tpex_data(date: str) -> tuple[float, float, list[dict]]:
    """回傳：(上櫃融資金額元, 上櫃融資市值元, ETF清單)"""
    r = requests.get(TPEX_BALANCE_URL, headers=HEADERS, timeout=20)
    data = r.json()
    table = next((t for t in data["tables"] if t.get("summary") and t.get("data")), None)

    margin_money_row = next(
        (row for row in table["summary"] if "融資金" in str(row[1])), None
    )
    tpex_margin_amount = _n(margin_money_row[6]) * 1000

    all_records = [{"證券代號": row[0], "名稱": row[1], "融資張數": _n(row[6])}
                   for row in table["data"] if row[0]]

    etf_tpex = [{"代號": d["證券代號"], "名稱": d["名稱"], "市場": "上櫃", "備註": "非純4碼或含字母"}
                for d in all_records if not _is_stock(d["證券代號"])]

    margin_df = pd.DataFrame([d for d in all_records if _is_stock(d["證券代號"])])

    r = requests.get(TPEX_CLOSE_URL,
                     params={"l": "zh-tw", "d": _tw_date(date), "s": "0,asc,0"},
                     headers=HEADERS, timeout=20)
    data2 = r.json()
    table2 = next((t for t in data2.get("tables", []) if t.get("data")), None)
    price_records = [{"證券代號": str(row[0]).strip(), "收盤價": _n(row[2])}
                     for row in table2["data"] if row[0]]
    price_df = pd.DataFrame(price_records)
    price_df = price_df[price_df["證券代號"].str.match(r"^\d{4}$")]

    merged = margin_df.merge(price_df, on="證券代號", how="left")
    merged["收盤價"] = merged["收盤價"].fillna(0.0)
    merged["融資市值"] = merged["融資張數"] * 1000 * merged["收盤價"]
    tpex_value = merged["融資市值"].sum()

    return tpex_margin_amount, tpex_value, etf_tpex


def get_data_auto(max_lookback: int = 10) -> dict:
    last_err = None
    for i in range(max_lookback):
        date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            twse_amount, twse_value, etf_twse = fetch_twse_data(date)
            tpex_amount, tpex_value, etf_tpex = fetch_tpex_data(date)
            if i > 0:
                print(f"提示：今天查無資料，已改抓 {date}。")

            total_value  = twse_value  + tpex_value
            total_amount = twse_amount + tpex_amount

            return {
                "date":          date,
                "twse_value":    round(twse_value   / 1e8, 2),
                "tpex_value":    round(tpex_value   / 1e8, 2),
                "total_value":   round(total_value  / 1e8, 2),
                "twse_amount":   round(twse_amount  / 1e8, 2),
                "tpex_amount":   round(tpex_amount  / 1e8, 2),
                "total_amount":  round(total_amount / 1e8, 2),
                "twse_ratio":    _ratio(twse_value,  twse_amount),   # 上市維持率
                "tpex_ratio":    _ratio(tpex_value,  tpex_amount),   # 上櫃維持率
                "total_ratio":   _ratio(total_value, total_amount),  # 大盤維持率
                "etf_list":      etf_twse + etf_tpex,
            }
        except (ValueError, KeyError) as e:
            last_err = e
            continue
    raise ValueError(f"往前找了 {max_lookback} 天都查不到資料：{last_err}")


# ── Google Sheets 寫入 ────────────────────────────────────────────────────────

def get_sheet(spreadsheet, name: str):
    try:
        return spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=name, rows=1000, cols=20)


def update_daily_sheet(ws, data: dict) -> None:
    HEADERS_ROW = [
        "日期",
        "上市融資市值(億)", "上櫃融資市值(億)", "融資市值合計(億)",
        "上市融資金額(億)", "上櫃融資金額(億)", "融資金額合計(億)",
        "上市融資維持率(%)", "上櫃融資維持率(%)", "大盤融資維持率(%)",
    ]
    NEW_ROW = [
        data["date"],
        data["twse_value"],  data["tpex_value"],  data["total_value"],
        data["twse_amount"], data["tpex_amount"], data["total_amount"],
        data["twse_ratio"],  data["tpex_ratio"],  data["total_ratio"],
    ]

    all_values = ws.get_all_values()

    if not all_values or all_values[0] == []:
        ws.append_row(HEADERS_ROW)
        all_values = [HEADERS_ROW]

    if all_values[0] != HEADERS_ROW:
        ws.insert_row(HEADERS_ROW, index=1)
        all_values.insert(0, HEADERS_ROW)

    existing_dates = [row[0] for row in all_values[1:] if row]
    if data["date"] in existing_dates:
        print(f"{data['date']} 已存在於「{ws.title}」，跳過。")
        return

    ws.insert_row(NEW_ROW, index=2)
    print(f"已寫入 {data['date']} 的維持率資料到「{ws.title}」。")


def update_etf_sheet(ws, etf_list: list[dict]) -> None:
    HEADERS_ROW = ["代號", "名稱", "市場", "備註"]
    rows = [HEADERS_ROW] + [
        [e["代號"], e["名稱"], e["市場"], e["備註"]]
        for e in sorted(etf_list, key=lambda x: (x["市場"], x["代號"]))
    ]
    ws.clear()
    ws.update(rows, "A1")
    print(f"已更新 ETF 清單（{len(etf_list)} 筆）到「{ws.title}」。")


# ── 主程式 ───────────────────────────────────────────────────────────────────

def main():
    import os, json
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credential_json = os.environ.get("GCP_ACCOUNT_CREDENTIAL")
    if credential_json:
        creds = Credentials.from_service_account_info(
            json.loads(credential_json), scopes=scopes
        )
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)

    try:
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    except gspread.SpreadsheetNotFound:
        raise FileNotFoundError(
            f"找不到試算表（ID：{SPREADSHEET_ID}），請確認 ID 正確且已共用給服務帳戶。"
        )

    print("抓取融資資料中...")
    data = get_data_auto()

    print(f"\n日期：{data['date']}")
    print(f"上市融資市值：{data['twse_value']:,.2f} 億  |  上市融資金額：{data['twse_amount']:,.2f} 億  |  上市維持率：{data['twse_ratio']} %")
    print(f"上櫃融資市值：{data['tpex_value']:,.2f} 億  |  上櫃融資金額：{data['tpex_amount']:,.2f} 億  |  上櫃維持率：{data['tpex_ratio']} %")
    print(f"合計市值：{data['total_value']:,.2f} 億  |  合計金額：{data['total_amount']:,.2f} 億  |  大盤維持率：{data['total_ratio']} %\n")

    ws_daily = get_sheet(spreadsheet, SHEET_DAILY)
    ws_etf   = get_sheet(spreadsheet, SHEET_ETF)

    update_daily_sheet(ws_daily, data)
    update_etf_sheet(ws_etf, data["etf_list"])
    print("\n完成！")


if __name__ == "__main__":
    main()