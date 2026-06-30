#!/usr/bin/env python3
"""
配当＆優待データ取得スクリプト（v3.1: 実データフィードバック追加反映版）
======================================================
銘柄リストTSVを読み込み、yfinance（配当）→ みんかぶ（優待）を
1銘柄ずつ交互に実行。1セット完了ごとに3〜5秒のランダム間隔を空ける。

【v1からの主な改善点】
  - 配当利回りを自前計算（1株配当÷株価×100）に変更
  - 異常値検知（注意フラグ・要確認理由・ランキング利用可否）追加
  - dividendYield/dividendRate は raw データとして保持
  - 優待あり判定を「実データが取れたかどうか」で判定するよう修正
  - yfinance状態を細分化（OK/OK_高利回り要確認/OK_異常値疑い等）

【v2からの主な改善点】
  - raw利回り解釈を「自前計算値に近い方」で判定する方式に変更
  - PER/PBR/時価総額等の数値項目を safe_float/safe_round で安全化
  - yfinanceデータなし時に注意フラグ・ランキング利用可否を必ずセット
  - 配当未取得を注意フラグに追加、配当状態列を新設
  - みんかぶ10連続エラー時の実停止フラグを追加
  - 優待判定不能をresume再取得対象に変更
  - HTTP 404を「対象外」として扱い再取得しない
  - みんかぶ取得失敗時に優待有無=未確認を明示
  - ランキング利用可否を配当/優待/総合の3列に分離
  - 記事掲載区分を追加

【v3.1 実データフィードバック反映】
  - 注意フラグ・要確認理由を上書きせずマージする方式に変更（split_flags）
  - yfinanceデータなし時の「yfinanceデータなし」フラグが再検証で消えない
  - フォールバック値を配当利回り_自前計算に入れない（配当フォールバック使用列で区別）
  - 配当未取得と無配推定を区別（dividendYield=0なら無配推定）
  - みんかぶ全エラーハンドラに注意フラグ・要確認理由を追加
  - 配当フォールバック使用銘柄を配当ランキングで要確認に判定
  - 0判定をfloat完全一致(==0)からis_zero_like()に変更（浮動小数点誤差対策）
  - normalize_raw_dividend_yieldで0相当の値をNoneではなく0.0として返す

【セットアップ】
  pip install yfinance pandas requests beautifulsoup4

【コマンドオプション】
  --list FILE   銘柄リストTSVファイルを指定（デフォルト: stock_list.tsv）
  --s N         開始行番号（ヘッダー込み1-indexed、省略時は先頭から）
  --e N         終了行番号（ヘッダー込み1-indexed、省略時は末尾まで）
  --resume FILE 前回の出力TSVを指定して途中再開（未取得・エラー銘柄のみ再取得）

【使い方】
  # 基本（stock_list.tsv の全銘柄を取得）
  python nikkei225_dividend_fetcher.py

  # 銘柄リストを指定
  python nikkei225_dividend_fetcher.py --list standard225.tsv

  # 行範囲を指定して実行（ヘッダー込みの行番号）
  python nikkei225_dividend_fetcher.py --s 1 --e 500
  python nikkei225_dividend_fetcher.py --s 501 --e 1000

  # 片方だけの指定もOK（--s だけなら末尾まで、--e だけなら先頭から）
  python nikkei225_dividend_fetcher.py --s 501
  python nikkei225_dividend_fetcher.py --e 500

  # 前回の出力TSVから途中再開
  python nikkei225_dividend_fetcher.py --resume dividend_20260625.tsv
"""

import yfinance as yf
import pandas as pd
import requests
from bs4 import BeautifulSoup
import time
import sys
import random
import logging
import argparse
import os
import re
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
# UA ローテーション
# ============================================================
HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "ja,en-US;q=0.7,en;q=0.3"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "ja-JP,ja;q=0.9"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
     "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "ja,en-US;q=0.7,en;q=0.3"},
]

# ============================================================
# ロガー設定
# ============================================================
def setup_logger(log_file: str) -> logging.Logger:
    logger = logging.getLogger("stock_fetcher")
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
COLUMNS = [
    "コード", "銘柄名",
    "市場・商品区分", "33業種コード", "33業種区分",
    "17業種コード", "17業種区分", "規模コード", "規模区分",
    "株価", "配当利回り(%)", "1株配当(円)",
    "配当利回り_raw", "1株配当_raw", "配当利回り_自前計算", "配当状態", "配当フォールバック使用",
    "PER", "PBR", "時価総額(億円)", "52週高値", "52週安値", "セクター",
    "優待有無", "優待内容", "優待最低株数", "優待判定信頼度",
    "注意フラグ", "要確認理由",
    "配当ランキング利用可否", "優待ランキング利用可否", "総合ランキング利用可否",
    "記事掲載区分",
    "yfinance状態", "みんかぶ状態",
]


def empty_row(entry: dict) -> dict:
    row = {col: "" for col in COLUMNS}
    row["コード"] = entry["コード"]
    row["銘柄名"] = entry["銘柄名"]
    for col in JPX_EXTRA_COLUMNS:
        row[col] = entry.get(col, "")
    row["yfinance状態"] = "未取得"
    row["みんかぶ状態"] = "未取得"
    return row


def load_existing_tsv(filepath: str) -> dict:
    df = pd.read_csv(filepath, sep="\t", dtype=str, encoding="utf-8-sig").fillna("")
    existing = {}
    for _, row in df.iterrows():
        d = row.to_dict()
        for col in COLUMNS:
            if col not in d:
                d[col] = ""
        existing[str(d["コード"])] = d
    return existing


def save_tsv(results: dict, filename: str):
    rows = list(results.values())
    df = pd.DataFrame(rows)
    out_cols = [c for c in COLUMNS if c in df.columns]
    extra = [c for c in df.columns if c not in COLUMNS]
    df = df[out_cols + extra]
    df.to_csv(filename, index=False, sep="\t", encoding="utf-8-sig")


# ============================================================
# スキップ判定（v3 §6: 優待判定不能は再取得対象）
# ============================================================
def needs_fetch(row: dict) -> tuple:
    yf_status = row.get("yfinance状態", "")
    mk_status = row.get("みんかぶ状態", "")
    need_yf = not yf_status.startswith("OK")
    # 「対象外」のみ再取得しない。優待判定不能は再取得する
    need_mk = not mk_status.startswith("OK") and mk_status != "対象外"
    return need_yf, need_mk


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

    # --- ランキング利用可否 3分割（v3 §10 / v3.1 §4: フォールバック考慮）---
    # 配当ランキング
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

    # 優待ランキング
    mk_ok = data.get("みんかぶ状態", "").startswith("OK")
    yutai_clear = data.get("優待有無") in ("あり", "なし")
    if mk_ok and yutai_clear:
        data["優待ランキング利用可否"] = "OK"
    else:
        data["優待ランキング利用可否"] = "要確認"

    # 総合ランキング
    if data["配当ランキング利用可否"] == "OK" and data["優待ランキング利用可否"] == "OK":
        data["総合ランキング利用可否"] = "OK"
    elif data["配当ランキング利用可否"] == "除外推奨":
        data["総合ランキング利用可否"] = "除外推奨"
    else:
        data["総合ランキング利用可否"] = "要確認"

    # --- 記事掲載区分（v3 §11）---
    if data["総合ランキング利用可否"] == "除外推奨":
        data["記事掲載区分"] = "掲載非推奨"
    elif data["総合ランキング利用可否"] == "要確認":
        data["記事掲載区分"] = "手動確認後に掲載"
    else:
        data["記事掲載区分"] = "通常掲載可"

    return data


# ============================================================
# yfinance 取得（v3 §2, §3 反映）
# ============================================================
def fetch_yfinance(code: str, logger: logging.Logger) -> dict:
    ticker = f"{code}.T"
    data = {}
    try:
        stock = yf.Ticker(ticker)
        info = stock.info
        if not info or info.get("regularMarketPrice") is None:
            logger.warning(f"yfinance データなし: {code}")
            return {
                "yfinance状態": "データなし",
                "注意フラグ": "yfinanceデータなし",
                "要確認理由": "yfinanceで株価・配当データが取得できなかった",
                "配当状態": "配当未取得",
                "配当ランキング利用可否": "除外推奨",
                "優待ランキング利用可否": "要確認",
                "総合ランキング利用可否": "除外推奨",
                "記事掲載区分": "掲載非推奨",
            }

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
        msg = str(e)[:80]
        data["yfinance状態"] = f"エラー: {msg}"
        logger.error(f"yfinance エラー: {code} - {msg}")

    data = validate_dividend_data(data)
    return data


# ============================================================
# みんかぶ優待スクレイピング（v3 §5,§6,§7,§8 反映）
# ============================================================
def fetch_minkabu_yutai(code: str, session: requests.Session, logger: logging.Logger) -> dict:
    url = f"https://minkabu.jp/stock/{code}/yutai"
    data = {}

    try:
        headers = random.choice(HEADERS_LIST)
        resp = session.get(url, headers=headers, timeout=15)

        if resp.status_code == 403:
            logger.warning(f"みんかぶ 403 ブロック: {code} - URL: {url}")
            return {"優待有無": "未確認", "優待判定信頼度": "低", "みんかぶ状態": "403 ブロック",
                    "注意フラグ": "みんかぶ403ブロック", "要確認理由": "みんかぶからアクセスをブロックされた"}
        if resp.status_code == 429:
            logger.warning(f"みんかぶ 429 レート制限: {code} - URL: {url}")
            return {"優待有無": "未確認", "優待判定信頼度": "低", "みんかぶ状態": "429 レート制限",
                    "注意フラグ": "みんかぶ429レート制限", "要確認理由": "みんかぶのレート制限に達した"}
        # v3 §7: HTTP 404は「対象外」（再取得しない）
        if resp.status_code == 404:
            logger.warning(f"みんかぶ HTTP 404: {code} - URL: {url}")
            return {"優待有無": "対象外", "優待内容": "", "優待最低株数": "",
                    "優待判定信頼度": "低", "みんかぶ状態": "対象外"}
        if resp.status_code != 200:
            logger.warning(f"みんかぶ HTTP {resp.status_code}: {code} - URL: {url}")
            return {"優待有無": "未確認", "優待判定信頼度": "低",
                    "みんかぶ状態": f"HTTP {resp.status_code}",
                    "注意フラグ": "みんかぶHTTPエラー",
                    "要確認理由": f"みんかぶがHTTP {resp.status_code}を返した"}

        soup = BeautifulSoup(resp.text, "html.parser")
        page_text = soup.get_text()

        if "株主優待情報はありません" in page_text:
            data["優待有無"] = "なし"
            data["優待内容"] = ""
            data["優待最低株数"] = ""
            data["優待判定信頼度"] = "高"
            data["みんかぶ状態"] = "OK"
            logger.debug(f"みんかぶ OK（優待なし）: {code}")
            return data

        yutai_summary = ""
        min_shares = ""
        detail_texts = []

        el_summary = soup.find(id="yutai_summary")
        if el_summary:
            yutai_summary = el_summary.get_text(strip=True)
        el_unit = soup.find(id="yutai_valuations_unit")
        if el_unit:
            min_shares = el_unit.get_text(strip=True)
        yutai_box = soup.find(class_="md_yutai_box")
        if yutai_box:
            detail_table = yutai_box.find("table", class_="md_table")
            if detail_table:
                for row in detail_table.find_all("tr"):
                    cells = row.find_all(["th", "td"])
                    if cells and cells[0].name == "td":
                        parts = [c.get_text(separator=" ", strip=True) for c in cells]
                        detail_texts.append(" | ".join(parts))
            if not detail_texts:
                h3 = yutai_box.find("h3")
                if h3:
                    detail_texts.append(h3.get_text(strip=True))

        has_summary = bool(yutai_summary)
        has_unit = bool(min_shares)
        has_details = bool(detail_texts)

        if has_summary or has_unit or has_details:
            data["優待有無"] = "あり"
            data["優待判定信頼度"] = "高"
            if yutai_summary and detail_texts:
                content = f"{yutai_summary}: {' / '.join(detail_texts)}"
            elif yutai_summary:
                content = yutai_summary
            elif detail_texts:
                content = " / ".join(detail_texts)
            else:
                content = ""
            data["優待内容"] = content[:300]
            data["優待最低株数"] = min_shares
            data["みんかぶ状態"] = "OK"
            logger.debug(f"みんかぶ OK（優待あり）: {code} - {yutai_summary}")
        else:
            data["優待有無"] = "要確認"
            data["優待内容"] = "（優待判定不能・ページで確認）"
            data["優待最低株数"] = ""
            data["優待判定信頼度"] = "低"
            data["みんかぶ状態"] = "優待判定不能"
            logger.info(f"みんかぶ 優待判定不能: {code} - {url}")

    except requests.exceptions.Timeout:
        logger.warning(f"みんかぶ タイムアウト: {code} - URL: {url}")
        return {"優待有無": "未確認", "優待判定信頼度": "低", "みんかぶ状態": "タイムアウト",
                "注意フラグ": "みんかぶタイムアウト", "要確認理由": "みんかぶへの接続がタイムアウトした"}
    except requests.exceptions.ConnectionError:
        logger.warning(f"みんかぶ 接続エラー: {code} - URL: {url}")
        return {"優待有無": "未確認", "優待内容": "", "優待最低株数": "",
                "優待判定信頼度": "低", "みんかぶ状態": "接続エラー",
                "注意フラグ": "みんかぶ接続エラー", "要確認理由": "みんかぶへの接続に失敗したため優待情報未確認"}
    except Exception as e:
        msg = str(e)[:80]
        logger.error(f"みんかぶ 例外: {code} - {msg}")
        return {"優待有無": "未確認", "優待判定信頼度": "低", "みんかぶ状態": f"エラー: {msg}",
                "注意フラグ": "みんかぶ例外エラー", "要確認理由": f"みんかぶ取得中に例外発生: {msg}"}

    return data


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="配当＆優待データ取得 v3.1")
    parser.add_argument("--resume", type=str, default=None, help="前回の出力TSVを指定して途中再開")
    parser.add_argument("--list", type=str, default=STOCK_LIST_FILE, help=f"銘柄リストTSV（デフォルト: {STOCK_LIST_FILE}）")
    parser.add_argument("--s", type=int, default=None, help="開始行番号")
    parser.add_argument("--e", type=int, default=None, help="終了行番号")
    args = parser.parse_args()

    today = datetime.now().strftime("%Y%m%d")
    log_file = f"dividend_error_{today}.log"
    logger = setup_logger(log_file)

    # --- 銘柄リスト構築 ---
    if args.resume and os.path.exists(args.resume):
        results = load_existing_tsv(args.resume)
        tsv_file = args.resume
        stock_list = [{"コード": code, "銘柄名": row.get("銘柄名", ""),
                       **{col: row.get(col, "") for col in JPX_EXTRA_COLUMNS}}
                      for code, row in results.items()]
        skip_count = sum(1 for r in results.values() if not needs_fetch(r)[0] and not needs_fetch(r)[1])
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
            tsv_file = f"dividend_{today}_{s_label}-{e_label}.tsv"
        else:
            tsv_file = f"dividend_{today}.tsv"
        results = {entry["コード"]: empty_row(entry) for entry in stock_list}

    # --- ヘッダー ---
    print("=" * 65)
    print("  配当＆優待データ取得スクリプト v3.1")
    print("=" * 65)
    print(f"  銘柄リスト  : {args.list}")
    print(f"  対象銘柄数  : {total}")
    print(f"  出力TSV     : {tsv_file}")
    print(f"  エラーログ  : {log_file}")
    print(f"  開始時刻    : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("  Ctrl+C で中断しても取得済みデータは保存されます")
    print("=" * 65)
    print()
    logger.info(f"=== 実行開始 ({total}銘柄) ===")

    session = requests.Session()
    processed = 0
    skipped = 0
    yf_ok = 0
    mk_ok = 0
    consecutive_mk_errors = 0
    minkabu_disabled = False  # v3 §5: 実停止フラグ

    try:
        for i, entry in enumerate(stock_list, 1):
            code = entry["コード"]
            name = entry["銘柄名"]
            row = results.get(code, empty_row(entry))
            need_yf, need_mk = needs_fetch(row)

            # v3 §5: みんかぶ停止フラグ
            if minkabu_disabled:
                need_mk = False

            if not need_yf and not need_mk:
                skipped += 1
                continue

            processed += 1
            pct = (i / total) * 100
            parts = []
            if need_yf: parts.append("yf")
            if need_mk: parts.append("mk")
            sys.stdout.write(f"\r  [{i:3d}/{total}] ({pct:5.1f}%) {code} {name:<18s} [{'+'.join(parts)}]     ")
            sys.stdout.flush()

            if need_yf:
                yf_data = fetch_yfinance(code, logger)
                for k, v in yf_data.items():
                    row[k] = v
                if yf_data.get("yfinance状態", "").startswith("OK"):
                    yf_ok += 1

            if need_mk:
                mk_data = fetch_minkabu_yutai(code, session, logger)
                for k, v in mk_data.items():
                    row[k] = v
                # validate再実行（優待ランキング利用可否を更新）
                row = validate_dividend_data(row)

                mk_status = mk_data.get("みんかぶ状態", "")
                if mk_status.startswith("OK") or mk_status in ("優待判定不能", "対象外"):
                    mk_ok += 1
                    consecutive_mk_errors = 0
                else:
                    consecutive_mk_errors += 1
                    if consecutive_mk_errors >= 5:
                        print(f"\n  🚨 みんかぶ {consecutive_mk_errors}連続エラー！")
                        print(f"     最後のエラー: {mk_status}")
                        logger.warning(f"みんかぶ {consecutive_mk_errors}連続エラー。最後: {mk_status}")
                        if consecutive_mk_errors >= 10:
                            print(f"\n  🛑 みんかぶ 10連続エラー → みんかぶ取得を停止します")
                            logger.error("みんかぶ 10連続エラー → 自動停止")
                            minkabu_disabled = True

            results[code] = row
            save_tsv(results, tsv_file)
            time.sleep(random.uniform(3.0, 5.0))

    except KeyboardInterrupt:
        print("\n\n  ⚠ 中断されました。取得済みデータを保存します。")
        logger.warning("ユーザーによる中断 (Ctrl+C)")
        save_tsv(results, tsv_file)

    # --- 最終集計 ---
    save_tsv(results, tsv_file)

    total_yf_ok = sum(1 for r in results.values() if r.get("yfinance状態", "").startswith("OK"))
    total_yf_fail = sum(1 for r in results.values()
                        if r.get("yfinance状態", "") not in ("", "未取得") and not r["yfinance状態"].startswith("OK"))
    total_mk_ok = sum(1 for r in results.values()
                      if r.get("みんかぶ状態", "").startswith("OK") or r.get("みんかぶ状態") in ("優待判定不能", "対象外"))
    total_mk_fail = sum(1 for r in results.values()
                        if r.get("みんかぶ状態", "") not in ("", "未取得", "対象外", "優待判定不能")
                        and not r["みんかぶ状態"].startswith("OK"))
    total_mk_pending = sum(1 for r in results.values() if r.get("みんかぶ状態") in ("未取得", ""))
    yutai_ari = sum(1 for r in results.values() if r.get("優待有無") == "あり")
    yutai_nashi = sum(1 for r in results.values() if r.get("優待有無") == "なし")
    yutai_check = sum(1 for r in results.values() if r.get("優待有無") in ("要確認", "未確認"))
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
    print(f"  [みんかぶ]  成功: {total_mk_ok}  失敗: {total_mk_fail}  未取得: {total_mk_pending}")
    print(f"  [優待集計]  あり: {yutai_ari}  なし: {yutai_nashi}  要確認: {yutai_check}")
    print(f"  [検証結果]  注意フラグ: {warn_count}件  配当除外推奨: {div_exclude}件")
    print()

    if total_mk_fail > 0 or total_mk_pending > 0:
        print(f"  💡 再開するには:")
        print(f"     python nikkei225_dividend_fetcher.py --resume {tsv_file}")
        print()

    # 配当利回りTOP10
    df = pd.read_csv(tsv_file, sep="\t", dtype=str, encoding="utf-8-sig").fillna("")
    df["_sort"] = pd.to_numeric(df["配当利回り(%)"], errors="coerce")
    df = df.sort_values("_sort", ascending=False, na_position="last")
    df_top = df[(df["_sort"].notna()) & (df.get("配当ランキング利用可否", pd.Series(dtype=str)).isin(["OK", "要確認"]))].head(10)
    if not df_top.empty:
        print("  --- 配当利回り TOP10（除外推奨を除く）---")
        for _, r in df_top.iterrows():
            yutai = f" [優待{r['優待有無']}]" if r["優待有無"] else ""
            flag = f" ⚠{r['注意フラグ']}" if r.get("注意フラグ", "") else ""
            print(f"    {r['コード']} {r['銘柄名']:<18s} {float(r['配当利回り(%)']):>5.2f}%{yutai}{flag}")
        print()

    print(f"  完了時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    logger.info(f"=== 実行完了 (yf={total_yf_ok}, mk={total_mk_ok}, warns={warn_count}) ===")


if __name__ == "__main__":
    main()