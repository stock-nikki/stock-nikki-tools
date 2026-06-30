#!/usr/bin/env python3
"""
Japan Stock Radar（v3.3: yfinance専用版）
======================================================
銘柄リストTSVを読み込み、yfinanceから1銘柄ずつファンダメンタル指標を取得。
1セット完了ごとに1〜3秒のランダム間隔を空ける。

【最終的な抽出項目】
  - 銘柄リスト由来:
    コード、銘柄名、市場・商品区分、33業種コード、33業種区分、
    17業種コード、17業種区分、規模コード、規模区分
  - yfinance由来:
    株価、1株配当_raw、配当利回り_raw、PER、PBR、時価総額(億円)、
    52週高値、52週安値、セクター
  - 派生/検証:
    配当利回り(%)、1株配当(円)、配当利回り_自前計算、配当状態、
    配当フォールバック使用、注意フラグ、要確認理由、
    配当ランキング利用可否、記事掲載区分、yfinance状態

【v3.1 実データフィードバック反映】
  - 注意フラグ・要確認理由を上書きせずマージする方式に変更（split_flags）
  - yfinanceデータなし時の「yfinanceデータなし」フラグが再検証で消えない
  - フォールバック値を配当利回り_自前計算に入れない（配当フォールバック使用列で区別）
  - 配当未取得と無配推定を区別（dividendYield=0なら無配推定）
  - 配当フォールバック使用銘柄を配当ランキングで要確認に判定
  - 0判定をfloat完全一致(==0)からis_zero_like()に変更（浮動小数点誤差対策）
  - normalize_raw_dividend_yieldで0相当の値をNoneではなく0.0として返す

【v3.2 変更点】
  - 外部サイトのスクレイピング処理を削除し、yfinance取得に一本化
  - 旧出力の外部サイト由来列は、resume時にも再出力しない
  - 取得間隔を1〜3秒に短縮

【v3.3 変更点】
  - fetch_yfinanceからTickerオブジェクトとinfoを返し、detail取得で使い回す
  - fetch_fundamental_detail内のtime.sleep(1)を削除（メインループの1〜2秒に統一）
  - 1銘柄あたりのAPIコール数を削減（info二重取得・Ticker再生成を排除）
  - APIコール単位の経過時間をログファイルに出力（[TIME]プレフィックス）
  - 1銘柄あたりの処理時間をログファイル＋コンソールに出力

【セットアップ】
  pip install yfinance pandas

【コマンドオプション】
  --list FILE   銘柄リストTSVファイルを指定（デフォルト: stock_list.tsv）
  --s N         開始行番号（ヘッダー込み1-indexed、省略時は先頭から）
  --e N         終了行番号（ヘッダー込み1-indexed、省略時は末尾まで）
  --resume FILE 前回の出力TSVを指定して途中再開（未取得・エラー銘柄のみ再取得）

【使い方】
  # 基本（stock_list.tsv の全銘柄を取得）
  python japan-stock-radar.py

  # 銘柄リストを指定
  python japan-stock-radar.py --list standard225.tsv

  # 行範囲を指定して実行（ヘッダー込みの行番号）
  python japan-stock-radar.py --s 1 --e 500
  python japan-stock-radar.py --s 501 --e 1000

  # 片方だけの指定もOK（--s だけなら末尾まで、--e だけなら先頭から）
  python japan-stock-radar.py --s 501
  python japan-stock-radar.py --e 500

  # 前回の出力TSVから途中再開
  python japan-stock-radar.py --resume japan-stock-radar_20260625.tsv
"""

import yfinance as yf
import pandas as pd
import time
import sys
import random
import logging
import argparse
import os
from datetime import datetime

# ============================================================
# 銘柄リスト読み込み
# ============================================================
STOCK_LIST_FILE = "stock_list.tsv"

JPX_EXTRA_COLUMNS = [
    "市場・商品区分", "33業種コード", "33業種区分",
    "17業種コード", "17業種区分", "規模コード", "規模区分",
]


def load_stock_list(filepath: str, start: int = None, end: int = None) -> list:
    if not os.path.exists(filepath):
        print(f"  ❌ 銘柄リストが見つかりません: {filepath}")
        print(f"  → --list オプションでファイルを指定してください")
        sys.exit(1)
    df = pd.read_csv(filepath, sep="\t", dtype=str, encoding="utf-8-sig").fillna("")
    cols = list(df.columns)
    code_col = next((c for c in cols if "コード" in c), cols[0])
    name_col = next((c for c in cols if "銘柄" in c and "コード" not in c), cols[1] if len(cols) > 1 else None)
    stock_list = []
    for _, row in df.iterrows():
        entry = {"コード": str(row[code_col]).strip(), "銘柄名": str(row[name_col]).strip() if name_col else ""}
        for col in JPX_EXTRA_COLUMNS:
            if col in cols:
                entry[col] = str(row[col]).strip()
        stock_list.append(entry)
    if start is not None or end is not None:
        s = max(start - 2, 0) if start is not None else 0
        e = min(end - 1, len(stock_list)) if end is not None else len(stock_list)
        stock_list = stock_list[s:e]
    print(f"  📂 銘柄リスト読み込み: {filepath} ({len(stock_list)} 銘柄)")
    return stock_list


# ============================================================
# ロガー設定
# ============================================================
def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("japan_stock_radar")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("  ⚠ %(message)s"))
    logger.addHandler(ch)
    return logger


# ============================================================
# 安全な数値変換（v3 §2）
# ============================================================
def safe_float(value):
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_round(value, digits=2):
    num = safe_float(value)
    if num is None:
        return ""
    return round(num, digits)


def is_zero_like(value, eps=1e-9):
    """0相当の数値かどうかを判定する。floatの完全一致判定を避ける。"""
    num = safe_float(value)
    if num is None:
        return False
    return abs(num) < eps


# ============================================================
# raw利回り正規化（v3 §1）
# ============================================================
def normalize_raw_dividend_yield(raw_div_yield, calc_yield=None):
    """rawそのまま / raw*100 のうち、自前計算値に近い方を採用する。
    0相当の値は 0.0 として扱う。"""
    raw = safe_float(raw_div_yield)
    if raw is None:
        return None
    if raw < 0:
        return None
    if is_zero_like(raw):
        return 0.0
    if calc_yield is not None:
        candidates = [raw, raw * 100]
        return min(candidates, key=lambda x: abs(x - calc_yield))
    # 自前計算値がない場合は保守的にそのまま返す
    if raw > 100:
        return raw
    return raw


# ============================================================
# TSV 読み書き
# ============================================================
# 最終出力列。抽出元と派生項目の内訳はファイル冒頭コメントの
# 「最終的な抽出項目」を参照。
COLUMNS = [
    "コード", "銘柄名",
    "市場・商品区分", "33業種コード", "33業種区分",
    "17業種コード", "17業種区分", "規模コード", "規模区分",
    "株価", "配当利回り(%)", "1株配当(円)",
    "配当利回り_raw", "1株配当_raw", "配当利回り_自前計算", "配当状態", "配当フォールバック使用",
    "PER", "PBR", "時価総額(億円)", "52週高値", "52週安値", "セクター",
    "注意フラグ", "要確認理由",
    "配当ランキング利用可否",
    "記事掲載区分",
    "yfinance状態",
]

DETAIL_COLUMNS = [
    "売上高_直近期(百万円)",
    "営業利益率(%)",
    "売上成長率_YoY(%)",
    "自己資本比率(%)",
    "DEレシオ",
    "FCF(百万円)",
    "EPS(円)",
    "配当性向(%)",
    "ROE(%)",
    "連続増配(年)",
]

REVENUE_KEYS = ["Total Revenue", "TotalRevenue"]
OPERATING_INCOME_KEYS = ["Operating Income", "OperatingIncome"]
TOTAL_ASSETS_KEYS = ["Total Assets", "TotalAssets"]
STOCKHOLDERS_EQUITY_KEYS = ["Stockholders Equity", "StockholdersEquity", "Total Stockholder Equity"]
TOTAL_DEBT_KEYS = ["Total Debt", "TotalDebt"]
FCF_KEYS = ["Free Cash Flow", "FreeCashFlow"]


def output_columns(detail: bool = False) -> list:
    return COLUMNS + DETAIL_COLUMNS if detail else COLUMNS

def empty_row(entry: dict, columns: list = None) -> dict:
    columns = columns or COLUMNS
    row = {col: "" for col in columns}
    row["コード"] = entry["コード"]
    row["銘柄名"] = entry["銘柄名"]
    for col in JPX_EXTRA_COLUMNS:
        row[col] = entry.get(col, "")
    row["yfinance状態"] = "未取得"
    return row


def load_existing_tsv(filepath: str, columns: list = None) -> dict:
    columns = columns or COLUMNS
    df = pd.read_csv(filepath, sep="\t", dtype=str, encoding="utf-8-sig").fillna("")
    existing = {}
    for _, row in df.iterrows():
        source = row.to_dict()
        d = {col: source.get(col, "") for col in columns}
        existing[str(d["コード"])] = d
    return existing


def save_tsv(results: dict, filename: str, columns: list = None):
    columns = columns or COLUMNS
    rows = list(results.values())
    df = pd.DataFrame(rows)
    out_cols = [c for c in columns if c in df.columns]
    df = df[out_cols]
    df.to_csv(filename, index=False, sep="\t", encoding="utf-8-sig")


# ============================================================
# スキップ判定（yfinance状態がOK系なら再取得しない）
# ============================================================
def needs_fetch(row: dict) -> bool:
    yf_status = row.get("yfinance状態", "")
    return not yf_status.startswith("OK")


def needs_detail_fetch(row: dict) -> bool:
    return all(str(row.get(col, "")).strip() == "" for col in DETAIL_COLUMNS)


def detail_filename(filename: str) -> str:
    root, ext = os.path.splitext(filename)
    if root.endswith("_detail"):
        return filename
    return f"{root}_detail{ext or '.tsv'}"


def detail_float(value):
    if value is None:
        return None
    if isinstance(value, str) and value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    try:
        if pd.isna(num):
            return None
    except (TypeError, ValueError):
        pass
    return num


def normalize_statement_key(value) -> str:
    return "".join(str(value).lower().split())


def ordered_statement(statement):
    if statement is None or getattr(statement, "empty", True):
        return None
    try:
        return statement.reindex(sorted(statement.columns, reverse=True), axis=1)
    except Exception:
        return statement


def statement_values(statement, keys: list) -> list:
    statement = ordered_statement(statement)
    if statement is None:
        return []

    for key in keys:
        if key in statement.index:
            row = statement.loc[key]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            return [detail_float(v) for v in row.tolist()]

    normalized_index = {}
    for idx in statement.index:
        normalized_index.setdefault(normalize_statement_key(idx), idx)

    for key in keys:
        idx = normalized_index.get(normalize_statement_key(key))
        if idx is None:
            continue
        row = statement.loc[idx]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return [detail_float(v) for v in row.tolist()]

    return []


def first_available(values: list, offset: int = 0):
    values = [v for v in values if v is not None]
    if len(values) <= offset:
        return None
    return values[offset]


def calc_consecutive_dividend_growth(dividends):
    if dividends is None or getattr(dividends, "empty", True):
        return ""
    try:
        clean = dividends.dropna()
        if clean.empty:
            return ""
        clean = pd.Series(clean.values, index=pd.to_datetime(clean.index))
        current_year = datetime.now().year
        clean = clean[clean.index.year < current_year]
        if clean.empty:
            return ""
        yearly = clean.groupby(clean.index.year).sum().sort_index()
        years = list(yearly.index)
        if len(years) < 2:
            return ""

        count = 0
        for pos in range(len(years) - 1, 0, -1):
            year = years[pos]
            prev_year = years[pos - 1]
            if year != prev_year + 1:
                break
            if detail_float(yearly.loc[year]) > detail_float(yearly.loc[prev_year]):
                count += 1
            else:
                break
        return count
    except Exception:
        return ""


def fetch_fundamental_detail(
    ticker_obj,
    dividend_per_share,
    per=None,
    pbr=None,
    info=None,
    logger: logging.Logger = None,
    code: str = "",
    name: str = "",
) -> dict:
    data = {col: "" for col in DETAIL_COLUMNS}

    income_status = "EMPTY"
    try:
        t0 = time.perf_counter()
        income = ticker_obj.income_stmt
        elapsed = time.perf_counter() - t0
        if logger:
            logger.debug(f"[TIME] {code} income_stmt: {elapsed:.2f}s")
        if income is not None and not income.empty:
            revenues = statement_values(income, REVENUE_KEYS)
            operating_incomes = statement_values(income, OPERATING_INCOME_KEYS)
            revenue = first_available(revenues)
            prev_revenue = first_available(revenues, 1)
            operating_income = first_available(operating_incomes)

            if revenue is not None:
                data["売上高_直近期(百万円)"] = round(revenue / 1_000_000, 1)
            if revenue and operating_income is not None:
                data["営業利益率(%)"] = round(operating_income / revenue * 100, 2)
            if revenue and prev_revenue:
                data["売上成長率_YoY(%)"] = round((revenue / prev_revenue - 1) * 100, 2)
            income_status = "OK"
    except Exception as e:
        income_status = f"ERR:{str(e)[:40]}"
        if logger:
            logger.debug(f"[DETAIL] income_stmt error: {code} - {e}")

    balance_status = "EMPTY"
    try:
        t0 = time.perf_counter()
        balance = ticker_obj.balance_sheet
        elapsed = time.perf_counter() - t0
        if logger:
            logger.debug(f"[TIME] {code} balance_sheet: {elapsed:.2f}s")
        if balance is not None and not balance.empty:
            total_assets = first_available(statement_values(balance, TOTAL_ASSETS_KEYS))
            equity = first_available(statement_values(balance, STOCKHOLDERS_EQUITY_KEYS))
            total_debt = first_available(statement_values(balance, TOTAL_DEBT_KEYS))

            if total_assets and equity is not None:
                data["自己資本比率(%)"] = round(equity / total_assets * 100, 2)
            if equity is not None and equity > 0 and total_debt is not None:
                data["DEレシオ"] = round(total_debt / equity, 2)
            balance_status = "OK"
    except Exception as e:
        balance_status = f"ERR:{str(e)[:40]}"
        if logger:
            logger.debug(f"[DETAIL] balance_sheet error: {code} - {e}")

    cashflow_status = "EMPTY"
    try:
        t0 = time.perf_counter()
        cashflow = ticker_obj.cashflow
        elapsed = time.perf_counter() - t0
        if logger:
            logger.debug(f"[TIME] {code} cashflow: {elapsed:.2f}s")
        if cashflow is not None and not cashflow.empty:
            fcf = first_available(statement_values(cashflow, FCF_KEYS))
            if fcf is not None:
                data["FCF(百万円)"] = round(fcf / 1_000_000, 1)
            cashflow_status = "OK"
    except Exception as e:
        cashflow_status = f"ERR:{str(e)[:40]}"
        if logger:
            logger.debug(f"[DETAIL] cashflow error: {code} - {e}")

    try:
        if info is None:
            t0 = time.perf_counter()
            info = ticker_obj.info
            elapsed = time.perf_counter() - t0
            if logger:
                logger.debug(f"[TIME] {code} info(detail): {elapsed:.2f}s")
        eps = detail_float(info.get("trailingEps"))
        if eps is not None:
            data["EPS(円)"] = round(eps, 2)

        dividend = detail_float(dividend_per_share)
        if eps is not None and eps > 0 and dividend is not None:
            data["配当性向(%)"] = round(dividend / eps * 100, 2)

        roe = detail_float(info.get("returnOnEquity"))
        if roe is not None:
            data["ROE(%)"] = round(roe * 100, 2)
        else:
            fallback_per = detail_float(per)
            fallback_pbr = detail_float(pbr)
            if fallback_per is None:
                fallback_per = detail_float(info.get("trailingPE"))
            if fallback_pbr is None:
                fallback_pbr = detail_float(info.get("priceToBook"))
            if fallback_per and fallback_per > 0 and fallback_pbr is not None:
                data["ROE(%)"] = round(fallback_pbr / fallback_per * 100, 2)
    except Exception as e:
        if logger:
            logger.debug(f"[DETAIL] info error: {code} - {e}")

    dividends_status = "EMPTY"
    try:
        t0 = time.perf_counter()
        dividends = ticker_obj.dividends
        elapsed = time.perf_counter() - t0
        if logger:
            logger.debug(f"[TIME] {code} dividends: {elapsed:.2f}s")
        if dividends is not None and not dividends.empty:
            data["連続増配(年)"] = calc_consecutive_dividend_growth(dividends)
            dividends_status = "OK"
    except Exception as e:
        dividends_status = f"ERR:{str(e)[:40]}"
        if logger:
            logger.debug(f"[DETAIL] dividends error: {code} - {e}")

    message = (
        f"[DETAIL] {code} {name}: income_stmt={income_status}, "
        f"balance_sheet={balance_status}, cashflow={cashflow_status}, dividends={dividends_status}"
    )
    if logger:
        logger.info(message)
    sys.stdout.write(f"\n  {message}\n")
    sys.stdout.flush()

    return data


# ============================================================
# 配当データ検証（v3 §1,§2,§3,§4,§9,§10,§11 / v3.1 §1,§2 追加）
# ============================================================
def split_flags(value):
    """既存のフラグ文字列を分割してリスト化する"""
    if not value:
        return []
    return [v.strip() for v in str(value).split("/") if v.strip()]


def validate_dividend_data(data: dict) -> dict:
    # 既存フラグをマージ（v3.1 §2: 上書きではなく追記）
    warnings = split_flags(data.get("注意フラグ", ""))
    reasons = split_flags(data.get("要確認理由", ""))

    # yfinanceデータなしの場合はフラグを必ず残す（v3.1 §1）
    if data.get("yfinance状態") == "データなし":
        warnings.append("yfinanceデータなし")
        reasons.append("yfinanceで株価・配当データが取得できなかった")

    price = safe_float(data.get("株価"))
    div_rate = safe_float(data.get("1株配当_raw"))
    raw_div_yield = safe_float(data.get("配当利回り_raw"))

    # --- 配当状態（v3 §4, §9 / v3.1 §3: 無配推定を追加 / v3.1 §5: is_zero_like化） ---
    if div_rate is None:
        if is_zero_like(raw_div_yield):
            data["配当状態"] = "無配推定"
            warnings.append("無配推定")
            reasons.append("dividendRate未取得かつdividendYieldが0相当")
        else:
            data["配当状態"] = "配当未取得"
            warnings.append("配当未取得")
            reasons.append("1株配当が取得できていない")
    elif is_zero_like(div_rate):
        data["配当状態"] = "無配"
    else:
        data["配当状態"] = "配当あり"

    # --- 自前計算 ---
    calc_yield = None
    if price is not None and div_rate is not None and price > 0:
        calc_yield = round(div_rate / price * 100, 2)
        data["配当利回り_自前計算"] = calc_yield
        data["配当利回り(%)"] = calc_yield
        data["1株配当(円)"] = round(div_rate, 2)
        data["配当フォールバック使用"] = "いいえ"
    elif raw_div_yield is not None:
        # フォールバック（v3 §1: normalize使用 / v3.1 §4: 自前計算には入れない）
        fb = normalize_raw_dividend_yield(raw_div_yield, None)
        if fb is not None:
            data["配当利回り(%)"] = round(fb, 2)
        data["配当利回り_自前計算"] = ""  # v3.1 §4: フォールバック値は自前計算に入れない
        data["配当フォールバック使用"] = "はい"
        warnings.append("配当フォールバック")
        reasons.append("株価or1株配当が不足のためraw値を使用")

    # --- 異常値チェック ---
    check_yield = calc_yield or 0
    if check_yield >= 30:
        warnings.append("配当かなり異常値疑い")
        reasons.append("配当利回りが30%以上")
    elif check_yield >= 15:
        warnings.append("異常値疑い")
        reasons.append("配当利回りが15%以上")
    elif check_yield >= 8:
        warnings.append("高利回り要確認")
        reasons.append("配当利回りが8%以上")

    if price is not None and div_rate is not None:
        if div_rate > price * 0.30:
            warnings.append("配当かなり異常値疑い")
            reasons.append("1株配当が株価の30%以上")
        elif div_rate > price * 0.15:
            warnings.append("配当異常値疑い")
            reasons.append("1株配当が株価の15%以上")
        elif div_rate > price * 0.10:
            reasons.append("1株配当が株価の10%以上")

    if price is None:
        warnings.append("株価未取得")
        reasons.append("株価が取得できていない")

    # --- raw乖離チェック（v3 §1: normalize使用）---
    if raw_div_yield is not None and calc_yield is not None:
        raw_pct = normalize_raw_dividend_yield(raw_div_yield, calc_yield)
        if raw_pct is not None:
            diff = abs(raw_pct - calc_yield)
            if diff >= 2:
                warnings.append("利回り乖離")
                reasons.append(f"raw={raw_pct:.2f}% vs calc={calc_yield:.2f}% 乖離{diff:.1f}%")

    # --- フラグ格納 ---
    data["注意フラグ"] = " / ".join(sorted(set(warnings))) if warnings else ""
    data["要確認理由"] = " / ".join(sorted(set(reasons))) if reasons else ""

    # --- yfinance状態の細分化 ---
    base = data.get("yfinance状態", "")
    if base == "OK" or base.startswith("OK"):
        if "かなり異常値疑い" in data["注意フラグ"] or "異常値疑い" in data["注意フラグ"]:
            data["yfinance状態"] = "OK_異常値疑い"
        elif "高利回り要確認" in data["注意フラグ"]:
            data["yfinance状態"] = "OK_高利回り要確認"
        elif data["注意フラグ"]:
            data["yfinance状態"] = "OK_配当データ要確認"
        else:
            data["yfinance状態"] = "OK"

    # --- 配当ランキング利用可否（v3.1 §4: フォールバック考慮）---
    yf_ok = data.get("yfinance状態", "").startswith("OK")
    has_div = data.get("配当状態") == "配当あり"
    is_fallback = data.get("配当フォールバック使用") == "はい"
    if yf_ok and has_div:
        if "かなり異常値疑い" in data.get("注意フラグ", ""):
            data["配当ランキング利用可否"] = "除外推奨"
        elif "異常値疑い" in data.get("注意フラグ", "") or "高利回り要確認" in data.get("注意フラグ", ""):
            data["配当ランキング利用可否"] = "要確認"
        elif is_fallback:
            data["配当ランキング利用可否"] = "要確認"
        else:
            data["配当ランキング利用可否"] = "OK"
    else:
        data["配当ランキング利用可否"] = "除外推奨"

    # --- 記事掲載区分 ---
    if data["配当ランキング利用可否"] == "除外推奨":
        data["記事掲載区分"] = "掲載非推奨"
    elif data["配当ランキング利用可否"] == "要確認":
        data["記事掲載区分"] = "手動確認後に掲載"
    else:
        data["記事掲載区分"] = "通常掲載可"

    return data


# ============================================================
# yfinance 取得（v3 §2, §3 反映）
# ============================================================
def fetch_yfinance(code: str, logger: logging.Logger) -> tuple:
    """戻り値: (data_dict, ticker_obj_or_None, info_dict_or_None)"""
    ticker = f"{code}.T"
    data = {}
    try:
        stock = yf.Ticker(ticker)
        t0 = time.perf_counter()
        info = stock.info
        elapsed_info = time.perf_counter() - t0
        logger.debug(f"[TIME] {code} info: {elapsed_info:.2f}s")
        if not info or info.get("regularMarketPrice") is None:
            logger.warning(f"yfinance データなし: {code}")
            return ({
                "yfinance状態": "データなし",
                "注意フラグ": "yfinanceデータなし",
                "要確認理由": "yfinanceで株価・配当データが取得できなかった",
                "配当状態": "配当未取得",
                "配当ランキング利用可否": "除外推奨",
                "記事掲載区分": "掲載非推奨",
            }, None, None)

        price = info.get("regularMarketPrice") or info.get("currentPrice")
        div_rate = info.get("dividendRate")
        raw_div_yield = info.get("dividendYield")

        data["株価"] = safe_float(price) or ""
        data["1株配当_raw"] = div_rate if div_rate is not None else ""
        data["配当利回り_raw"] = raw_div_yield if raw_div_yield is not None else ""
        if div_rate is not None:
            data["1株配当(円)"] = safe_round(div_rate, 2)

        # PER/PBR等は safe_round で安全化（v3 §2）
        per = safe_round(info.get("trailingPE"), 2)
        if per != "":
            data["PER"] = per
        pbr = safe_round(info.get("priceToBook"), 2)
        if pbr != "":
            data["PBR"] = pbr
        mcap = safe_float(info.get("marketCap"))
        if mcap:
            data["時価総額(億円)"] = round(mcap / 1e8, 1)
        data["52週高値"] = safe_round(info.get("fiftyTwoWeekHigh"), 2)
        data["52週安値"] = safe_round(info.get("fiftyTwoWeekLow"), 2)
        data["セクター"] = info.get("sector", "")
        data["yfinance状態"] = "OK"
        logger.debug(f"yfinance OK: {code}")

    except Exception as e:
        stock, info = None, None
        msg = str(e)[:80]
        data["yfinance状態"] = f"エラー: {msg}"
        logger.error(f"yfinance エラー: {code} - {msg}")

    data = validate_dividend_data(data)
    return (data, stock, info)


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Japan Stock Radar v3.3")
    parser.add_argument("--resume", type=str, default=None, help="前回の出力TSVを指定して途中再開")
    parser.add_argument("--list", type=str, default=STOCK_LIST_FILE, help=f"銘柄リストTSV（デフォルト: {STOCK_LIST_FILE}）")
    parser.add_argument("--s", type=int, default=None, help="開始行番号")
    parser.add_argument("--e", type=int, default=None, help="終了行番号")
    parser.add_argument("--detail", action="store_true", help="ファンダメンタル詳細データを追加取得する")
    args = parser.parse_args()
    columns = output_columns(args.detail)

    today = datetime.now().strftime("%Y%m%d")
    log_file = f"japan-stock-radar_error_{today}.log"
    logger = setup_logger(log_file)

    # --- 銘柄リスト構築 ---
    if args.resume and os.path.exists(args.resume):
        results = load_existing_tsv(args.resume, columns)
        tsv_file = detail_filename(args.resume) if args.detail else args.resume
        stock_list = [{"コード": code, "銘柄名": row.get("銘柄名", ""),
                       **{col: row.get(col, "") for col in JPX_EXTRA_COLUMNS}}
                      for code, row in results.items()]
        skip_count = sum(
            1 for r in results.values()
            if not needs_fetch(r) and (not args.detail or not needs_detail_fetch(r))
        )
        total = len(stock_list)
        print(f"  📂 前回データ読み込み: {args.resume}")
        print(f"     取得済み（スキップ）: {skip_count} 銘柄  残り: {total - skip_count} 銘柄")
        print()
        logger.info(f"Resume from: {args.resume} (skip={skip_count})")
    else:
        stock_list = load_stock_list(args.list, args.s, args.e)
        total = len(stock_list)
        if args.s is not None or args.e is not None:
            s_label = f"{args.s:04d}" if args.s is not None else "0001"
            e_label = f"{args.e:04d}" if args.e is not None else "end"
            tsv_file = f"japan-stock-radar_{today}_{s_label}-{e_label}.tsv"
        else:
            tsv_file = f"japan-stock-radar_{today}.tsv"
        if args.detail:
            tsv_file = detail_filename(tsv_file)
        results = {entry["コード"]: empty_row(entry, columns) for entry in stock_list}

    # --- ヘッダー ---
    print("=" * 65)
    print("  Japan Stock Radar v3.3")
    print("=" * 65)
    print(f"  銘柄リスト  : {args.list}")
    print(f"  対象銘柄数  : {total}")
    if args.detail:
        print("  詳細モード  : ON")
    print(f"  出力TSV     : {tsv_file}")
    print(f"  エラーログ  : {log_file}")
    print(f"  開始時刻    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("  Ctrl+C で中断しても取得済みデータは保存されます")
    print("=" * 65)
    print()
    logger.info(f"=== 実行開始 ({total}銘柄) ===")
    run_t0 = time.perf_counter()

    processed = 0
    skipped = 0

    try:
        for i, entry in enumerate(stock_list, 1):
            code = entry["コード"]
            name = entry["銘柄名"]
            row = results.get(code, empty_row(entry, columns))
            need_yf = needs_fetch(row)
            need_detail = args.detail and needs_detail_fetch(row)

            if not need_yf and not need_detail:
                skipped += 1
                continue

            processed += 1
            pct = (i / total) * 100
            parts = []
            if need_yf:
                parts.append("yf")
            if args.detail and (need_detail or need_yf):
                parts.append("detail")
            sys.stdout.write(f"\r  [{i:3d}/{total}] ({pct:5.1f}%) {code} {name:<18s} [{'+'.join(parts)}]     ")
            sys.stdout.flush()

            stock = None
            yf_info = None
            stock_t0 = time.perf_counter()

            if need_yf:
                yf_data, stock, yf_info = fetch_yfinance(code, logger)
                for k, v in yf_data.items():
                    row[k] = v

            if args.detail and row.get("yfinance状態", "").startswith("OK"):
                if stock is None:
                    stock = yf.Ticker(f"{code}.T")
                detail_data = fetch_fundamental_detail(
                    stock,
                    row.get("1株配当(円)", ""),
                    per=row.get("PER", ""),
                    pbr=row.get("PBR", ""),
                    info=yf_info,
                    logger=logger,
                    code=code,
                    name=name,
                )
                for k, v in detail_data.items():
                    row[k] = v

            stock_elapsed = time.perf_counter() - stock_t0
            logger.info(f"[TIME] {code} {name} 処理時間: {stock_elapsed:.2f}s")
            sys.stdout.write(f"\r  [{i:3d}/{total}] ({pct:5.1f}%) {code} {name:<18s} [{'+'.join(parts)}] {stock_elapsed:.1f}s\n")
            sys.stdout.flush()

            results[code] = row
            save_tsv(results, tsv_file, columns)
            time.sleep(random.uniform(1.0, 2.0))

    except KeyboardInterrupt:
        print("\n\n  ⚠ 中断されました。取得済みデータを保存します。")
        logger.warning("ユーザーによる中断 (Ctrl+C)")
        save_tsv(results, tsv_file, columns)

    # --- 最終集計 ---
    save_tsv(results, tsv_file, columns)

    total_yf_ok = sum(1 for r in results.values() if r.get("yfinance状態", "").startswith("OK"))
    total_yf_fail = sum(1 for r in results.values()
                        if r.get("yfinance状態", "") not in ("", "未取得") and not r["yfinance状態"].startswith("OK"))
    warn_count = sum(1 for r in results.values() if r.get("注意フラグ", ""))
    div_exclude = sum(1 for r in results.values() if r.get("配当ランキング利用可否") == "除外推奨")

    print(f"\n")
    print("=" * 65)
    print("  完了！")
    print("=" * 65)
    print(f"  出力TSV    : {tsv_file}")
    print(f"  エラーログ : {log_file}")
    print()
    print(f"  今回処理: {processed} 銘柄  スキップ: {skipped} 銘柄")
    print()
    print(f"  [yfinance]  成功: {total_yf_ok}  失敗: {total_yf_fail}")
    print(f"  [検証結果]  注意フラグ: {warn_count}件  配当除外推奨: {div_exclude}件")
    print()

    if total_yf_fail > 0:
        print(f"  💡 再開するには:")
        print(f"     python japan-stock-radar.py --resume {tsv_file}")
        print()

    # 配当利回りTOP10
    df = pd.read_csv(tsv_file, sep="\t", dtype=str, encoding="utf-8-sig").fillna("")
    df["_sort"] = pd.to_numeric(df["配当利回り(%)"], errors="coerce")
    df = df.sort_values("_sort", ascending=False, na_position="last")
    df_top = df[(df["_sort"].notna()) & (df.get("配当ランキング利用可否", pd.Series(dtype=str)).isin(["OK", "要確認"]))].head(10)
    if not df_top.empty:
        print("  --- 配当利回り TOP10（除外推奨を除く）---")
        for _, r in df_top.iterrows():
            flag = f" ⚠{r['注意フラグ']}" if r.get("注意フラグ", "") else ""
            print(f"    {r['コード']} {r['銘柄名']:<18s} {float(r['配当利回り(%)']):>5.2f}%{flag}")
        print()

    run_elapsed = time.perf_counter() - run_t0
    run_hour, rem = divmod(run_elapsed, 3600)
    run_min, run_sec = divmod(rem, 60)
    if run_hour >= 1:
        run_time_str = f"{int(run_hour)}時間{int(run_min)}分{run_sec:.1f}秒"
    elif run_min >= 1:
        run_time_str = f"{int(run_min)}分{run_sec:.1f}秒"
    else:
        run_time_str = f"{run_sec:.1f}秒"

    print(f"  完了時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  総実行時間: {run_time_str}")
    print()
    logger.info(f"=== 実行完了 (yf={total_yf_ok}, warns={warn_count}, 総実行時間={run_time_str}) ===")


if __name__ == "__main__":
    main()