#!/usr/bin/env python3
"""
2014年12月末 現物ポートフォリオ 株式分割情報収集スクリプト

■ 使い方
  1. pip install yfinance
  2. python split_collector.py
  3. 同じフォルダに split_history.tsv が出力される

■ 出力
  split_history.tsv（BOM付きUTF-8 / Excel対応）
    - 証券コード
    - 銘柄名
    - 株式分割履歴（半角スペース区切り、例: "2018/10 1:2 2024/04 1:10"）
    - 2026年6月19日終値

■ 注意
  - 対象期間: 2014-12-31 ～ 2026-06-19
  - 6月20日(土)のため6月19日(金)を終値採用
  - 上場廃止銘柄はデータ取得失敗として記録
  - 実行に約3〜5分（105銘柄 × 1.5秒インターバル）
"""

import yfinance as yf
import time
import sys
from datetime import datetime, timedelta
from pandas import Timestamp

# ============================================================
# 2014年12月末 現物105銘柄
# ============================================================
STOCKS = [
    7611, 7575, 7309, 7412, 7936, 9201, 8113, 2801, 7846, 2751,
    2269, 4507, 6741, 6645, 7272, 2412, 2331, 9974, 7951, 8332,
    7458, 4901, 2193, 6118, 2229, 3030, 6501, 6257, 9735, 4837,
    3097, 2165, 8830, 7013, 5332, 3407, 3193, 8001, 5943, 6849,
    6952, 8214, 5110, 4503, 9436, 7701, 4967, 6503, 9644, 9006,
    8022, 7564, 5949, 2288, 9989, 7453, 5233, 6758, 7751, 2764,
    2362, 1801, 4327, 2587, 4793, 1802, 9381, 6425, 8306, 9020,
    4452, 9795, 8086, 8031, 8316, 8802, 4666, 8002, 7752, 9843,
    2371, 4326, 3197, 4555, 9412, 9437, 7739, 3079, 9766, 2670,
    9984, 4568, 4578, 6098, 8136, 9672, 9432, 4293, 4911, 5015,
    9468, 3765, 3662, 6460, 2678,
]
# ※末尾の6460(セガサミーHD), 2678(アスクル)はスクショ3枚目に含まれる銘柄

# 分割情報の取得対象期間
SPLIT_START = Timestamp("2014-12-31")
SPLIT_END = Timestamp("2026-06-19")


# ============================================================
# ヘルパー関数
# ============================================================

def get_ticker(code):
    """証券コードからyfinanceのTickerオブジェクトを取得"""
    return yf.Ticker(f"{code}.T")


def safe_get_close(hist, target_date_str, window_days=5):
    """
    指定日の終値を取得。休場日の場合は前後window_days日以内で最も近い日を探す。
    """
    if hist is None or hist.empty:
        return None

    target = Timestamp(target_date_str)
    hist_idx = hist.index.tz_localize(None) if hist.index.tz is not None else hist.index

    # 完全一致
    if target in hist_idx:
        idx_pos = hist_idx.get_loc(target)
        return float(hist.iloc[idx_pos]["Close"])

    # 前後で最も近い日を探す
    best_date = None
    best_diff = timedelta(days=window_days + 1)
    for dt in hist_idx:
        diff = abs(dt - target)
        if diff < best_diff:
            best_diff = diff
            best_date = dt

    if best_date is not None and best_diff <= timedelta(days=window_days):
        idx_pos = hist_idx.get_loc(best_date)
        return float(hist.iloc[idx_pos]["Close"])

    return None


def get_splits_in_range(ticker_obj):
    """
    2014-12-31 ～ 2026-06-19 の株式分割履歴を取得。
    半角スペース区切りで返す。例: "2018/10 1:2 2024/04 1:10"
    """
    try:
        splits = ticker_obj.splits
        if splits is None or splits.empty:
            return "なし"

        split_list = []
        for date, ratio in splits.items():
            dt = date.tz_localize(None) if date.tzinfo else date
            if SPLIT_START <= dt <= SPLIT_END:
                # ratio = 分割後株数（例: 2.0→1:2分割、10.0→1:10分割）
                split_list.append(f"{dt.strftime('%Y/%m')} 1:{ratio:g}")

        return " ".join(split_list) if split_list else "なし"
    except Exception:
        return "取得失敗"


def progress(current, total, code, name=""):
    """進捗バー表示"""
    pct = current / total * 100
    bar_len = 30
    filled = int(bar_len * current / total)
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"\r  [{bar}] {current}/{total} ({pct:.0f}%) - {code} {name}    ",
          end="", flush=True)


# ============================================================
# メイン処理
# ============================================================

def main():
    print("=" * 60)
    print("  株式分割情報 収集スクリプト")
    print("  対象: 2014年12月末 現物105銘柄")
    print("  期間: 2014-12-31 ～ 2026-06-19")
    print("=" * 60)
    print()
    print(f"  対象銘柄数: {len(STOCKS)}")
    print("  （約3〜5分かかります）")
    print()

    results = []
    errors = []

    for i, code in enumerate(STOCKS, 1):
        try:
            ticker = get_ticker(code)

            # 銘柄名取得
            try:
                ticker_info = ticker.info
                name = (ticker_info.get("longName")
                        or ticker_info.get("shortName")
                        or f"不明({code})")
            except Exception:
                name = f"不明({code})"

            progress(i, len(STOCKS), code, name)

            # 分割履歴
            splits = get_splits_in_range(ticker)

            # 2026年6月19日の終値（調整後）
            hist = ticker.history(start="2026-06-15", end="2026-06-21",
                                  auto_adjust=True)
            close_2026 = safe_get_close(hist, "2026-06-19")

            results.append({
                "code": code,
                "name": name,
                "splits": splits,
                "close_2026": round(close_2026, 1) if close_2026 else "N/A",
            })

        except Exception as e:
            errors.append((code, str(e)))
            results.append({
                "code": code,
                "name": f"取得失敗({code})",
                "splits": "取得失敗",
                "close_2026": "N/A",
            })

        # Yahoo側の負荷軽減
        time.sleep(1.5)

    print()
    print()
    print(f"  完了！ 成功: {len(STOCKS) - len(errors)}件 / エラー: {len(errors)}件")
    print()

    # ----------------------------------------------------------
    # TSV出力
    # ----------------------------------------------------------
    output_file = "split_history.tsv"

    with open(output_file, "w", encoding="utf-8-sig") as f:
        headers = ["証券コード", "銘柄名", "株式分割履歴", "2026年6月19日終値"]
        f.write("\t".join(headers) + "\n")

        for r in results:
            row = [
                str(r["code"]),
                r["name"],
                r["splits"],
                str(r["close_2026"]),
            ]
            f.write("\t".join(row) + "\n")

    print(f"  → {output_file} 出力完了")
    print()

    # エラーサマリー
    if errors:
        print(f"  ⚠ エラー {len(errors)}件（上場廃止の可能性）:")
        for code, msg in errors:
            print(f"    {code}: {msg}")
        print()

    # 分割あり銘柄のサマリー表示
    split_found = [r for r in results if r["splits"] not in ("なし", "取得失敗")]
    print(f"  📊 分割あり銘柄: {len(split_found)}件")
    for r in split_found:
        print(f"    {r['code']} {r['name']}: {r['splits']}")
    print()
    print("  このTSVをクロちゃん（Claude）に渡してください！")


if __name__ == "__main__":
    main()
