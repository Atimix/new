# -*- coding: utf-8 -*-
"""
台股波段查詢 — 本機/雲端網頁版
資料源：
  - 即時報價：證交所基本市況報導 API (mis.twse.com.tw)
  - 歷史日線：證交所 STOCK_DAY API (www.twse.com.tw)，用來算 MA5/20/60 與抵扣值

判斷邏輯：
  趨勢結構：MA5 > MA20 > MA60 視為完整多頭排列
  進場（須同時符合）：
    - MACD 黃金交叉（MACD 線由下往上穿越訊號線）
    - MA20 未來5日抵扣值呈遞減（中期均線壓力減輕，領先確認）
  出場：MACD 死亡交叉，或 MA5 跌破 MA20
  停損參考：近期低點
  賣出點位（壓力位）：現價之上、最近的前波高點

執行方式：
    pip install streamlit requests
    streamlit run app.py
"""

import time
from datetime import datetime, timezone, timedelta

import requests
import streamlit as st

st.set_page_config(page_title="台股波段查詢", page_icon="📈", layout="centered")

TAIPEI = timezone(timedelta(hours=8))
HEADERS = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------------
# 即時報價
# ---------------------------------------------------------------------
def fetch_realtime(code: str):
    for market, label in [("tse", "上市"), ("otc", "上櫃")]:
        url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        params = {"ex_ch": f"{market}_{code}.tw", "json": 1, "delay": 0}
        try:
            res = requests.get(url, params=params, timeout=5, headers=HEADERS)
            data = res.json()
        except Exception:
            continue
        rows = data.get("msgArray") or []
        if rows:
            row = rows[0]

            def f(key):
                v = row.get(key)
                if v in (None, "-", ""):
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            return {
                "code": row.get("c"), "name": row.get("n"), "market": label,
                "price": f("z"), "open": f("o"), "high": f("h"), "low": f("l"),
                "prev_close": f("y"), "volume": f("v"),
                "updated_ms": row.get("tlong"),
            }
    return None


# ---------------------------------------------------------------------
# 歷史日線（近 3 個月，供 MA/KD 計算）
# ---------------------------------------------------------------------
def fetch_history(code: str, months: int = 12):
    rows = []
    today = datetime.now(TAIPEI)
    for i in range(months):
        month_date = (today.replace(day=1) - timedelta(days=1)) if i > 0 else today
        # 往回推 i 個月的月份字串
        y, m = today.year, today.month
        m -= i
        while m <= 0:
            m += 12
            y -= 1
        date_str = f"{y}{m:02d}01"
        url = "https://www.twse.com.tw/exchangeReport/STOCK_DAY"
        params = {"response": "json", "date": date_str, "stockNo": code}
        try:
            res = requests.get(url, params=params, timeout=6, headers=HEADERS)
            data = res.json()
        except Exception:
            continue
        for r in data.get("data", []):
            try:
                # 民國年日期轉西元, 格式如 113/01/05
                y_roc, mm, dd = r[0].split("/")
                date = f"{int(y_roc)+1911}-{mm}-{dd}"
                rows.append({
                    "date": date,
                    "volume": float(r[1].replace(",", "")),
                    "open": float(r[3].replace(",", "")),
                    "high": float(r[4].replace(",", "")),
                    "low": float(r[5].replace(",", "")),
                    "close": float(r[6].replace(",", "")),
                })
            except (ValueError, IndexError):
                continue
        time.sleep(0.3)  # 避免過快請求被證交所限流

    rows.sort(key=lambda r: r["date"])
    # 去重（同一天可能重複抓到）
    seen, dedup = set(), []
    for r in rows:
        if r["date"] not in seen:
            seen.add(r["date"])
            dedup.append(r)
    return dedup


# ---------------------------------------------------------------------
# 指標計算
# ---------------------------------------------------------------------
def sma(values, n):
    out = [None] * len(values)
    s = 0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def dedu_series(closes, n, k=5):
    """
    對每一天 t，計算未來 k 個交易日 MA(n) 將被「扣除」的舊收盤價序列。
    每天均線往前滾動一天，就會扣掉 n 天前的價格、換上今天的價格。
    這個序列代表未來 k 天分別會被扣掉的價格 —— 如果這串數字持續走低，
    代表未來幾天即使股價持平，MA(n) 也會因為扣掉的舊價格變低而自然上彎。
    傳回：每天一個 list（不足 k 天資料時為 None）
    """
    out = [None] * len(closes)
    for t in range(len(closes)):
        vals = []
        ok = True
        for j in range(1, k + 1):
            idx = t - n + j
            if idx < 0:
                ok = False
                break
            vals.append(closes[idx])
        out[t] = vals if ok else None
    return out


def dedu_is_descending(vals, strict=False):
    """抵扣值序列是否呈現遞減（對多頭有利：均線壓力持續減輕）"""
    if not vals or len(vals) < 2:
        return False
    down_steps = sum(1 for a, b in zip(vals, vals[1:]) if b < a)
    need = len(vals) - 1 if strict else max(1, (len(vals) - 1) // 2)
    return down_steps >= need and vals[-1] < vals[0]


def ema(values, n):
    k = 2 / (n + 1)
    out = [None] * len(values)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def macd(closes, fast=12, slow=26, signal_n=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema(macd_line, signal_n)
    hist = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


def find_resistance(rows, idx, price):
    """
    尋找「現價之上、距離最近」的前波高點，作為波段壓力位（賣出參考點）。
    在 idx（含）之前的歷史高點中，找出所有高於 price 的值，取其中最低者
    ——也就是離現價最近、最先會被挑戰到的壓力關卡。
    若歷史資料中找不到更高的高點（代表股價已創近期新高），回傳 None。
    """
    candidates = [r["high"] for r in rows[:idx + 1] if r["high"] > price]
    return min(candidates) if candidates else None


def build_analysis(rows):
    dedu_strict = False
    closes = [r["close"] for r in rows]
    ma5 = sma(closes, 5)
    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    dedu20 = dedu_series(closes, 20, 5)
    macd_line, signal_line, hist = macd(closes)

    merged = []
    for i, r in enumerate(rows):
        merged.append({
            **r, "ma5": ma5[i], "ma20": ma20[i], "ma60": ma60[i], "dedu20": dedu20[i],
            "macd": macd_line[i], "macd_signal": signal_line[i], "macd_hist": hist[i],
        })

    warmup = 35  # MACD 需要足夠天數才穩定（EMA26 + Signal EMA9）
    for i, cur in enumerate(merged):
        cur["buy"] = cur["sell"] = False
        if i <= warmup:
            continue
        prev = merged[i - 1]

        dedu_ok = dedu_is_descending(cur["dedu20"], strict=dedu_strict) if cur["dedu20"] else False
        macd_golden = prev["macd"] <= prev["macd_signal"] and cur["macd"] > cur["macd_signal"]
        macd_dead = prev["macd"] >= prev["macd_signal"] and cur["macd"] < cur["macd_signal"]
        trend_break = (cur["ma5"] is not None and cur["ma20"] is not None
                        and prev["ma5"] is not None and prev["ma20"] is not None
                        and cur["ma5"] < cur["ma20"] and prev["ma5"] >= prev["ma20"])

        # MACD 黃金交叉是進場的時機觸發點；抵扣值遞減是確認未來均線壓力減輕。
        # 不要求 MA5/MA20 同一天也翻多——MACD 通常比均線更早反應，
        # 若硬性要求同步，訊號幾乎不會出現。均線結構只作為額外資訊顯示。
        cur["buy"] = macd_golden and dedu_ok
        cur["sell"] = macd_dead or trend_break

    in_position, entry_idx = False, -1
    for i, r in enumerate(merged):
        if not in_position and r["buy"]:
            in_position, entry_idx = True, i
        elif in_position and r["sell"]:
            in_position, entry_idx = False, -1

    last = merged[-1]
    bull_full = (last["ma5"] is not None and last["ma20"] is not None and last["ma60"] is not None
                 and last["ma5"] > last["ma20"] > last["ma60"])
    bull_partial = (last["ma5"] is not None and last["ma20"] is not None and last["ma5"] > last["ma20"])
    dedu20_descending = dedu_is_descending(last["dedu20"], strict=dedu_strict) if last["dedu20"] else False
    macd_above_signal = last["macd"] is not None and last["macd"] > last["macd_signal"]
    recent_low20 = min(r["low"] for r in rows[-20:])

    reasons = []
    entry_hint = stop_hint = profit_hint = None
    new_high = False

    if in_position:
        verdict, color = "持有中", "green"
        reasons.append(f"已於 {merged[entry_idx]['date']} 附近進場，依訊號續抱")
        reasons.append("均線仍多頭排列" if bull_partial else "均線尚未轉強，屬 MACD 領先反應")
        reasons.append("MACD 仍在訊號線之上" if macd_above_signal else "MACD 動能轉弱，留意死亡交叉")
        stop_hint = min(last["ma20"] or recent_low20, recent_low20)
        profit_hint = find_resistance(rows, len(rows) - 1, last["close"])
        new_high = profit_hint is None
    elif last["buy"]:
        verdict, color = "進場訊號", "green"
        reasons.append("MACD 黃金交叉，動能轉多")
        reasons.append("MA20 未來抵扣值呈遞減，中期均線壓力同步減輕")
        if bull_full:
            reasons.append("均線完整多頭排列（MA5>MA20>MA60），趨勢確認度較高")
        elif bull_partial:
            reasons.append("均線已呈多頭排列（MA5>MA20）")
        else:
            reasons.append("均線尚未翻多，屬偏早期的反轉訊號，波動風險較高，可考慮分批進場")
        entry_hint = last["close"]
        stop_hint = min(last["ma20"] or recent_low20, recent_low20)
        profit_hint = find_resistance(rows, len(rows) - 1, entry_hint)
        new_high = profit_hint is None
    elif macd_above_signal and dedu20_descending:
        verdict, color = "觀望（動能已翻多）", "orange"
        reasons.append("MACD 已在訊號線之上、抵扣值也呈遞減，趨勢條件都轉正")
        reasons.append("但黃金交叉那個時間點已過，訊號稍微落後，可斟酌是否仍要追")
        entry_hint = last["close"]
        stop_hint = recent_low20
        profit_hint = find_resistance(rows, len(rows) - 1, entry_hint)
        new_high = profit_hint is None
    elif dedu20_descending:
        verdict, color = "觀望（醞釀中）", "orange"
        reasons.append("MA20 未來抵扣值呈遞減，均線止跌/翻揚機會提高")
        reasons.append("但 MACD 尚未出現黃金交叉，建議等動能確認再進場")
        stop_hint = recent_low20
    else:
        verdict, color = "不建議做多", "red"
        reasons.append("MACD 未轉多，抵扣值也未呈遞減趨勢")
        reasons.append("等待轉強訊號（MACD 黃金交叉 + 抵扣值遞減）再評估")

    return {
        "verdict": verdict, "color": color, "reasons": reasons,
        "entry_hint": entry_hint, "stop_hint": stop_hint, "profit_hint": profit_hint,
        "new_high": new_high,
        "last": last, "in_position": in_position, "dedu20_descending": dedu20_descending,
    }


# ---------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------
st.title("📈 台股波段查詢")
st.caption("資料來源：證交所。歷史日線用於計算 MA5/20/60、MACD 與抵扣值。僅支援台股上市/上櫃代號。")

tab1, tab2 = st.tabs(["單檔查詢", "自選股掃描"])

with tab1:
    code = st.text_input("輸入股票代號", placeholder="例如 2330 / 0050")
    search = st.button("查詢", type="primary", use_container_width=True)

    if search and code:
        with st.spinner("查詢即時報價中…"):
            rt = fetch_realtime(code)
        if not rt:
            st.error("查無此股票代號，請確認是否為台股上市/上櫃代號")
        else:
            with st.spinner("擷取歷史資料並計算指標中…（約需 6-10 秒）"):
                hist = fetch_history(code, months=12)

            if len(hist) < 90:
                st.warning("歷史資料不足（可能是新上市股票），無法計算完整的 MA60/MACD 指標，僅顯示即時報價")
                hist = None

            price = rt["price"] if rt["price"] is not None else rt["prev_close"]
            change = (price - rt["prev_close"]) if (price is not None and rt["prev_close"]) else None
            change_pct = (change / rt["prev_close"] * 100) if (change is not None and rt["prev_close"]) else None

            st.subheader(f"{rt['code']} {rt['name']}　·　{rt['market']}")
            if rt["updated_ms"]:
                dt = datetime.fromtimestamp(int(rt["updated_ms"]) / 1000, tz=TAIPEI)
                st.caption(f"更新時間 {dt.strftime('%Y-%m-%d %H:%M:%S')}")

            st.metric(
                "成交價" if rt["price"] is not None else "昨收（尚未開盤成交）",
                f"{price:.2f}" if price else "-",
                f"{change:+.2f} ({change_pct:+.2f}%)" if change is not None else None,
            )

            if hist:
                analysis = build_analysis(hist)
                color = analysis["color"]
                st.markdown(f"### :{color}[● {analysis['verdict']}]")
                for r in analysis["reasons"]:
                    st.write(f"- {r}")

                c1, c2, c3 = st.columns(3)
                c1.metric("參考進場價", f"{analysis['entry_hint']:.2f}" if analysis["entry_hint"] else "-")
                c2.metric("參考停損價", f"{analysis['stop_hint']:.2f}" if analysis["stop_hint"] else "-")
                if analysis["new_high"]:
                    c3.metric("壓力位賣出點", "創新高")
                    c3.caption("歷史資料中無更高點，暫無壓力位參考，追蹤 MACD 死叉出場")
                else:
                    c3.metric("壓力位賣出點", f"{analysis['profit_hint']:.2f}" if analysis["profit_hint"] else "-")

                last = analysis["last"]
                c4, c5, c6 = st.columns(3)
                c4.metric("MA5", f"{last['ma5']:.1f}" if last["ma5"] else "-")
                c5.metric("MA20", f"{last['ma20']:.1f}" if last["ma20"] else "-")
                c6.metric("MA60", f"{last['ma60']:.1f}" if last["ma60"] else "-")

                c7, c8 = st.columns(2)
                c7.metric("MACD", f"{last['macd']:.2f}" if last["macd"] is not None else "-")
                c8.metric("訊號線", f"{last['macd_signal']:.2f}" if last["macd_signal"] is not None else "-")

                st.markdown("---")
                st.markdown("**抵扣值分析**")
                st.metric("MA20 未來抵扣值", "遞減（助漲）" if analysis["dedu20_descending"] else "非遞減")
                dedu_list = last["dedu20"]
                if dedu_list:
                    st.caption("未來5日將扣抵的舊價格：" + " → ".join(f"{v:.1f}" for v in dedu_list)
                               + "（愈往右愈低，代表均線壓力愈輕）")

            st.caption(
                "本頁根據 MACD、MA5/20/60 均線結構與抵扣值做規則式技術面判斷，"
                "壓力位賣出點取現價之上最近的前波高點，皆為歷史資料推算，"
                "不構成投資建議，進出場請自行評估風險。"
            )
    elif search and not code:
        st.warning("請輸入股票代號")

with tab2:
    st.caption(
        "貼上你關注的股票代號（用逗號、空格或換行分隔），一次掃描哪些目前符合進場條件。"
        "證交所 API 一次只能查一檔，掃太多檔會等比變慢，建議一次不超過 30 檔。"
    )
    watchlist_text = st.text_area(
        "自選股代號清單",
        value="2330, 2317, 2454, 2412, 1301, 2308, 2882, 2891, 3711, 2603",
        height=90,
    )
    only_buy = st.checkbox("只顯示有進場訊號的股票", value=True)
    scan = st.button("開始掃描", type="primary", use_container_width=True)

    if scan:
        codes = [c.strip() for c in watchlist_text.replace("，", ",").replace("\n", ",").split(",") if c.strip()]
        codes = list(dict.fromkeys(codes))  # 去重但保留順序

        if not codes:
            st.warning("請至少輸入一檔股票代號")
        else:
            progress = st.progress(0, text="掃描中…")
            results = []
            for idx, c in enumerate(codes):
                progress.progress((idx + 1) / len(codes), text=f"掃描中… {c}（{idx + 1}/{len(codes)}）")
                hist = fetch_history(c, months=5)  # 掃描模式抓較短區間，加快速度
                if len(hist) < 90:
                    results.append({"code": c, "name": "-", "verdict": "資料不足", "color": "gray"})
                    continue
                analysis = build_analysis(hist)
                name = hist[-1].get("name", c)  # STOCK_DAY 無名稱欄位，先用代號
                results.append({
                    "code": c, "name": c, "verdict": analysis["verdict"], "color": analysis["color"],
                    "close": hist[-1]["close"],
                    "entry_hint": analysis["entry_hint"], "stop_hint": analysis["stop_hint"],
                    "profit_hint": analysis["profit_hint"], "new_high": analysis["new_high"],
                })
            progress.empty()

            shown = [r for r in results if (not only_buy or r["verdict"] == "進場訊號")]

            if not shown:
                st.info("掃描完成，目前自選股中沒有符合進場條件的標的" if only_buy else "掃描完成")
            else:
                st.success(f"掃描完成，共 {len(codes)} 檔，符合顯示條件 {len(shown)} 檔")
                for r in shown:
                    if r["verdict"] in ("資料不足",):
                        st.write(f"**{r['code']}** — {r['verdict']}")
                        continue
                    with st.container(border=True):
                        st.markdown(f"**{r['code']}**　:{r['color']}[● {r['verdict']}]　現價 {r['close']:.2f}")
                        cols = st.columns(3)
                        cols[0].metric("進場價", f"{r['entry_hint']:.2f}" if r["entry_hint"] else "-")
                        cols[1].metric("停損價", f"{r['stop_hint']:.2f}" if r["stop_hint"] else "-")
                        if r["new_high"]:
                            cols[2].metric("壓力位", "創新高")
                        else:
                            cols[2].metric("壓力位", f"{r['profit_hint']:.2f}" if r["profit_hint"] else "-")

            st.caption("掃描模式為節省時間，使用較短的歷史區間（5個月），壓力位與單檔查詢頁的結果可能略有差異，建議看到進場訊號後，再到「單檔查詢」分頁看完整分析。")
