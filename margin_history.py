"""
建立台股「大盤融資維持率」歷史資料庫（每日累積版）

重要說明：
  台灣證交所(TWSE)的 MI_MARGN API 只提供「最新交易日」的資料，
  並沒有提供歷史查詢功能（傳入過去日期通常查不到資料）。
  所以想要累積歷史趨勢，唯一可靠的做法是：

      每個交易日收盤後執行一次本程式 → 把當天結果 append 進 CSV/資料庫

  做法上有兩個選項：
  A) 排程執行（推薦）：用 cron / Windows 工作排程器，每天下午 5~6 點跑一次
     （TWSE 資料通常在下午 4~5 點後才會更新當日資料）
  B) 補歷史資料：若想要「過去」的歷史維持率，TWSE 官網沒有，
     可考慮：
       - MacroMicro 網站的圖表（無公開 API，需付費或人工查看）
       - 自己长期排程，從『今天』開始累積（最務實的方式）

本程式做的事：
  1. 呼叫 margin_today.py 裡的函式抓「今天」的大盤融資維持率
  2. 把結果 append 寫入本地的 margin_ratio_history.csv
  3. 若該日期已經存在於 CSV，則略過（避免重複寫入）
  4. 提供一個畫圖/查詢用的函式，讀取整份歷史 CSV
"""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd

from margin_today import get_market_margin_maintenance_ratio

HISTORY_CSV = "margin_ratio_history.csv"


def append_today_to_history(date: str | None = None, csv_path: str = HISTORY_CSV) -> None:
    """
    抓「今天」的大盤融資維持率，append 進歷史 CSV。
    如果該日期已經存在，就跳過，不會重複寫入。
    """
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    # 讀取既有歷史（若檔案不存在就建立空的 DataFrame）
    if os.path.exists(csv_path):
        history = pd.read_csv(csv_path, dtype={"日期": str})
    else:
        history = pd.DataFrame(columns=["日期", "全市場融資市值", "融資金額餘額", "大盤融資維持率"])

    if date in history["日期"].astype(str).values:
        print(f"{date} 已存在於歷史紀錄，略過。")
        return

    try:
        result = get_market_margin_maintenance_ratio(date)
    except ValueError as e:
        print(f"{date} 抓取失敗（可能非交易日）：{e}")
        return

    new_row = {
        "日期": result["日期"],
        "全市場融資市值": result["全市場融資市值(元)"],
        "融資金額餘額": result["融資金額餘額(元)"],
        "大盤融資維持率": result["大盤融資維持率(%)"],
    }

    history = pd.concat([history, pd.DataFrame([new_row])], ignore_index=True)
    history = history.sort_values("日期").reset_index(drop=True)
    history.to_csv(csv_path, index=False, encoding="utf-8-sig")

    print(f"已寫入 {date} 的大盤融資維持率：{new_row['大盤融資維持率']}%")
    print(f"歷史紀錄已儲存於：{os.path.abspath(csv_path)}")


def load_history(csv_path: str = HISTORY_CSV) -> pd.DataFrame:
    """讀取目前累積的歷史資料，回傳 DataFrame（依日期排序）。"""
    if not os.path.exists(csv_path):
        print(f"找不到歷史檔案 {csv_path}，請先執行 append_today_to_history() 累積資料。")
        return pd.DataFrame(columns=["日期", "全市場融資市值", "融資金額餘額", "大盤融資維持率"])

    df = pd.read_csv(csv_path, dtype={"日期": str})
    df["日期_datetime"] = pd.to_datetime(df["日期"], format="%Y%m%d")
    return df.sort_values("日期_datetime").reset_index(drop=True)


def plot_history(csv_path: str = HISTORY_CSV) -> None:
    """簡單畫出大盤融資維持率歷史趨勢圖（需要 matplotlib）。"""
    import matplotlib.pyplot as plt

    df = load_history(csv_path)
    if df.empty:
        print("沒有資料可畫圖。")
        return

    plt.figure(figsize=(10, 5))
    plt.plot(df["日期_datetime"], df["大盤融資維持率"], marker="o")
    plt.axhline(166, color="orange", linestyle="--", label="一般警戒線 166%")
    plt.axhline(130, color="red", linestyle="--", label="追繳線 130%")
    plt.title("台股大盤融資維持率歷史趨勢")
    plt.xlabel("日期")
    plt.ylabel("維持率 (%)")
    plt.legend()
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.savefig("margin_ratio_history.png", dpi=150)
    print("圖表已儲存為 margin_ratio_history.png")


if __name__ == "__main__":
    # 每次執行：抓今天的資料、寫入歷史 CSV
    append_today_to_history()

    # 印出目前累積的歷史資料
    df = load_history()
    print("\n目前累積的歷史資料：")
    print(df[["日期", "大盤融資維持率"]].to_string(index=False))
