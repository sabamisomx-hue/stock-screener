# screener.py -- 銘柄「深掘り」ツール（Streamlit / 全部Python・本物のyfinanceデータ）
#
# 使い方:
#   1) このファイルを起動する（VS Codeのターミナルか、run_screener.bat）
#        python -m streamlit run screener.py
#   2) ブラウザが自動で開く（開かなければ http://localhost:8501）
#   3) LINEで届いた銘柄コード（例 7203.T）を入れて「分析」を押す
#   止めるとき: ターミナルで Ctrl+C
#
# 役割: 朝のLINEツール(portfolio.py)が「発掘銘柄」を教えてくれる → それを“本物の数字”で深掘りする専用UI。
#       portfolio.py の分析関数（セクター変換・社名和訳）を import して再利用している（コード重複なし）。

import streamlit as st
import yfinance as yf
import pandas as pd
import requests


# yfinanceの英語セクター名 → 日本語（クラウドでも1ファイルで動くようここに同梱）
SECTOR_EN_TO_JP = {
    "Technology": "テクノロジー",
    "Financial Services": "金融",
    "Energy": "エネルギー",
    "Healthcare": "ヘルスケア",
    "Consumer Cyclical": "一般消費財",
    "Consumer Defensive": "生活必需品",
    "Industrials": "資本財",
    "Basic Materials": "素材",
    "Utilities": "公益",
    "Real Estate": "不動産",
    "Communication Services": "通信",
}


# Google翻訳の無料エンドポイント（sl=元の言語, tl=訳す言語）。失敗時は原文のまま。
def _gtranslate(text, sl, tl):
    if not text:
        return text
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": sl, "tl": tl, "dt": "t", "q": text},
                         timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and seg[0])
    except Exception:
        return text


def translate_ja(text):   # 英語 → 日本語（社名表示用）
    return _gtranslate(text, "en", "ja")


def translate_en(text):   # 日本語 → 英語（和名での銘柄検索用）
    return _gtranslate(text, "ja", "en")


# ===== データ取得（.info と 株価履歴 を各1回だけ。重いので10分キャッシュ）=====
# Streamlitは操作のたびにスクリプトを丸ごと再実行するので、キャッシュしないと
# メモを打つたびにyfinanceを叩いてしまう。@st.cache_data でそれを防ぐ。
@st.cache_data(ttl=600, show_spinner=False)
def fetch_stock(code):
    t = yf.Ticker(code)
    try:
        info = t.info
    except Exception:
        info = {}
    try:
        hist = t.history(period="1y")["Close"].dropna()   # チャート＆テクニカルに使う（1回で兼用）
    except Exception:
        hist = pd.Series(dtype="float64")

    price = info.get("currentPrice") or info.get("regularMarketPrice")
    if price is None and len(hist) > 0:
        price = float(hist.iloc[-1])
    prev = info.get("regularMarketPreviousClose")
    chg = round((price - prev) / prev * 100, 2) if (price and prev) else None

    # 高値からの下落率 と 25日移動平均かい離%（手元の履歴から計算＝追加の通信なし）
    drop = ma_dev = None
    if len(hist) > 0:
        high = float(hist.max())
        last = float(hist.iloc[-1])
        if high > 0:
            drop = round((high - last) / high * 100, 1)
        if len(hist) >= 25:
            ma = float(hist.tail(25).mean())
            if ma > 0:
                ma_dev = round((last - ma) / ma * 100, 1)

    # 社名（日本語）。日本語名が無ければローマ字社名を翻訳（portfolio.pyの関数を再利用）
    name = info.get("longName") or info.get("shortName") or code
    # 日本株(.T)は英語社名で返ることが多いので日本語へ翻訳（既に日本語ならそのまま）
    if code.endswith(".T") and isinstance(name, str) and name.isascii():
        name = translate_ja(name) or name

    return {
        "code": code,
        "name": name,
        "price": price,
        "currency": info.get("currency", ""),
        "chg": chg,
        "sector": SECTOR_EN_TO_JP.get(info.get("sector")) or info.get("sector") or "-",
        "per": info.get("trailingPE"),
        "pbr": info.get("priceToBook"),
        "yield": info.get("dividendYield"),         # この環境のyfinanceは「3.1」のように%値で返る（検証済み）
        "roe": info.get("returnOnEquity"),          # 分数(0.15=15%)で返るので表示時に×100
        "growth": info.get("revenueGrowth"),        # 分数で返る
        "drop": drop,                               # 高値からの下落率%
        "ma_dev": ma_dev,                           # 25日移動平均かい離%
        "hist": hist,
    }


# JPX（日本取引所）の上場銘柄一覧（証券コード,日本語社名）を読み込む。和名検索に使う。
# リポジトリに同梱した jpx_list.csv を読むだけ（ダウンロード不要・クラウドでも確実）。
# 一覧の更新は jpx_list.csv を作り直して差し替える。失敗時は空リスト。
@st.cache_data(ttl=86400, show_spinner=False)
def load_jpx():
    import os, csv
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jpx_list.csv")
    rows = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            for r in csv.reader(f):
                if len(r) >= 2:
                    rows.append((r[0], r[1]))
    except Exception:
        return []
    return rows


# 会社名・キーワードから銘柄候補を探す（コードが分からない時用）。東証(.T)を上に並べる。
@st.cache_data(ttl=600, show_spinner=False)
def search_symbols(query):
    query = query.strip()
    # 和名（非ASCII）は、まずJPX公式一覧から日本語社名で照合（最も確実）
    if any(ord(ch) > 127 for ch in query):
        jpx_hits = [{"symbol": code + ".T", "name": name, "exchange": "JPX"}
                    for code, name in load_jpx() if query in name]
        if jpx_hits:
            return jpx_hits[:10]
        q = translate_en(query) or query            # 見つからなければ英訳してYahoo検索へ
    else:
        q = query
    try:
        quotes = yf.Search(q).quotes
    except Exception:
        return []
    out = []
    for item in quotes:
        sym = item.get("symbol")
        if not sym:
            continue
        out.append({"symbol": sym,
                    "name": item.get("shortname") or item.get("longname") or sym,
                    "exchange": item.get("exchange", "")})
    out.sort(key=lambda x: not x["symbol"].endswith(".T"))   # 東証銘柄を先頭へ
    return out[:10]


# ===== バリュースコア（100点満点）=====
# 5つの指標を各20点で採点。透明性重視で、しきい値はベタ書き（調整しやすい）。
def value_score(s):
    detail = {}   # 指標名 -> (得点, 満点, 表示テキスト)

    # PER（低いほど割安）
    per = s["per"]
    if per is None or per <= 0: p = 0
    elif per < 10: p = 20
    elif per < 15: p = 15
    elif per < 20: p = 10
    elif per < 30: p = 5
    else: p = 0
    detail["PER（割安度）"] = (p, 20, f"{per:.1f}" if per else "-")

    # PBR（1倍割れが割安の目安）
    pbr = s["pbr"]
    if pbr is None or pbr <= 0: p = 0
    elif pbr < 1: p = 20
    elif pbr < 1.5: p = 15
    elif pbr < 2: p = 10
    elif pbr < 3: p = 5
    else: p = 0
    detail["PBR（割安度）"] = (p, 20, f"{pbr:.2f}" if pbr else "-")

    # 配当利回り（高いほど良い）。この環境では%値で来る
    dy = s["yield"]
    if dy is None: p = 0
    elif dy >= 4: p = 20
    elif dy >= 3: p = 15
    elif dy >= 2: p = 10
    elif dy >= 1: p = 5
    else: p = 0
    detail["配当利回り"] = (p, 20, f"{dy:.1f}%" if dy else "-")

    # ROE（自己資本利益率・稼ぐ力）。分数で来るので×100
    roe = s["roe"]
    roe_pct = roe * 100 if roe is not None else None
    if roe_pct is None: p = 0
    elif roe_pct >= 15: p = 20
    elif roe_pct >= 10: p = 15
    elif roe_pct >= 8: p = 10
    elif roe_pct >= 5: p = 5
    else: p = 0
    detail["ROE（稼ぐ力）"] = (p, 20, f"{roe_pct:.1f}%" if roe_pct is not None else "-")

    # 売上成長率。分数で来るので×100
    g = s["growth"]
    g_pct = g * 100 if g is not None else None
    if g_pct is None: p = 0
    elif g_pct >= 20: p = 20
    elif g_pct >= 10: p = 15
    elif g_pct >= 5: p = 10
    elif g_pct >= 0: p = 5
    else: p = 0
    detail["売上成長率"] = (p, 20, f"{g_pct:.1f}%" if g_pct is not None else "-")

    total = sum(v[0] for v in detail.values())
    return total, detail


def verdict(total):
    if total >= 75: return "割安・好スコア", "🟢"
    if total >= 55: return "やや割安・良好", "🟢"
    if total >= 40: return "適正圏", "🟡"
    if total >= 25: return "やや割高・注意", "🟠"
    return "割高・スコア低", "🔴"


# ===== AI（Anthropic API）部分 =====
ANTHROPIC_MODEL = "claude-opus-4-8"   # 最新・最高性能のモデル

# 投資助言ではなく「客観的な意思決定の支援」だと毎回はっきりさせる土台のプロンプト
AI_SYSTEM = (
    "あなたは個人投資家の意思決定を支援する日本語アシスタントです。"
    "これは投資助言ではありません。最終判断は必ずユーザー自身が行います。"
    "売買を断定せず、客観的な観点・リスク・見るべきポイントを整理することに徹してください。"
    "渡された数値（バリュースコアやPER/PBR等）はあくまで目安であり、"
    "高値からの下落や25日線割れは『売りサイン』ではなく、過去検証ではむしろ反発（押し目）寄りで、"
    "下降局面では落ちるナイフになり得る、という前提を踏まえてください。"
    "断定や煽りを避け、簡潔に。"
)


# Anthropicクライアントを用意する（anthropic_key.txt が無ければ None）。
# @st.cache_resource で一度だけ生成して使い回す。
@st.cache_resource
def get_anthropic_client():
    import os
    if not os.path.exists("anthropic_key.txt"):
        return None
    key = open("anthropic_key.txt", encoding="utf-8").read().strip()
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)


# 分析済みデータをAIに渡す「事実メモ」を組み立てる（数値はこちらで確定させ、AIに推測させない）
def ai_context(s, total, detail):
    lines = [
        f"銘柄：{s['name']}（{s['code']}）",
        f"現在値：{s['price']:,.0f} {s['currency']}" + (f"（前日比 {s['chg']:+.2f}%）" if s['chg'] is not None else ""),
        f"セクター：{s['sector']}",
        f"バリュースコア：{total}/100",
    ]
    for name, (pt, mx, raw) in detail.items():
        lines.append(f"　{name}：{raw}（{pt}/{mx}点）")
    if s.get("drop") is not None:
        lines.append(f"高値からの下落：{s['drop']}%")
    if s.get("ma_dev") is not None:
        lines.append(f"25日線かい離：{s['ma_dev']}%")
    return "\n".join(lines)


# Claudeにストリーミングで答えさせる（st.write_streamにそのまま渡せる文字列ジェネレータ）
def ai_stream(client, system, messages, thinking=False, max_tokens=1500):
    kwargs = dict(model=ANTHROPIC_MODEL, max_tokens=max_tokens, system=system, messages=messages)
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}   # じっくり考えてから答える
    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            yield text


# ========================= 画面 =========================
st.set_page_config(page_title="銘柄深掘りツール", page_icon="📈", layout="wide")
st.title("📈 銘柄 深掘りツール")
st.caption("LINEで届いた銘柄を、本物のyfinanceデータで深掘りする。AI推測ではなく実データ。")

# 分析した銘柄を覚えておく入れもの（比較タブで使う）
if "stocks" not in st.session_state:
    st.session_state.stocks = {}     # code -> データdict
if "memos" not in st.session_state:
    st.session_state.memos = {}      # code -> メモ文字列
if "checklist" not in st.session_state:
    st.session_state.checklist = {}  # code -> AI判断チェックリストの結果
if "chat" not in st.session_state:
    st.session_state.chat = {}       # code -> [{role, content}, ...] 相談の履歴

# --- 入力欄 ---
col_in, col_btn = st.columns([4, 1])
with col_in:
    code_in = st.text_input("銘柄コード（日本株は数字4桁＋.T 例:7203.T／米国株はそのまま 例:PLTR）",
                            value="7203.T").strip().upper()
with col_btn:
    st.write("")  # 高さ合わせ
    go = st.button("分析", use_container_width=True, type="primary")

# 1銘柄を取得して保存する（コード入力からも、名前検索の結果からも呼ぶ）
def do_analyze(code):
    code = (code or "").strip().upper()
    if not code:
        return
    with st.spinner(f"{code} を取得中…"):
        data = fetch_stock(code)
    if data["price"] is None:
        st.error(f"{code} のデータが取得できませんでした。コードを確認してください（日本株は .T を忘れずに）。")
    else:
        st.session_state.stocks[code] = data

if go and code_in:
    do_analyze(code_in)

# --- 銘柄名・会社名で探す（コードが分からない時）---
with st.expander("🔎 銘柄名・会社名で探す（コードが分からない時）"):
    with st.form("searchform", clear_on_submit=False):
        kw = st.text_input("会社名やキーワード", placeholder="例：トヨタ / toyota / sony")
        do_search = st.form_submit_button("検索")
    if do_search and kw.strip():
        st.session_state.search_results = search_symbols(kw.strip())
    results = st.session_state.get("search_results", [])
    if results:
        for r in results:
            c1, c2 = st.columns([5, 1])
            mark = "🇯🇵 " if r["symbol"].endswith(".T") else ""
            c1.write(f"{mark}{r['name']}（{r['symbol']}・{r['exchange']}）")
            if c2.button("分析", key=f"pick_{r['symbol']}"):
                do_analyze(r["symbol"])
    elif do_search:
        st.info("候補が見つかりませんでした。別のキーワードで試してください。")

# --- 表示する銘柄を選ぶ（分析済みから）---
if st.session_state.stocks:
    codes = list(st.session_state.stocks.keys())
    sel = st.selectbox("表示する銘柄", codes, index=len(codes) - 1)
    s = st.session_state.stocks[sel]

    tab_detail, tab_compare = st.tabs(["🔍 詳細", "⚖️ 比較"])

    # ===== 詳細タブ =====
    with tab_detail:
        cur = s["currency"] or ""
        st.subheader(f"{s['name']}（{s['code']}）")
        m1, m2, m3 = st.columns(3)
        m1.metric("現在値", f"{s['price']:,.0f} {cur}",
                  f"{s['chg']:+.2f}%" if s["chg"] is not None else None)
        total, detail = value_score(s)
        label, icon = verdict(total)
        m2.metric("バリュースコア", f"{total} / 100", label)
        m3.metric("セクター", s["sector"])

        st.markdown(f"### {icon} 総合判定：{label}")

        # スコア内訳（バー）
        st.markdown("#### スコア内訳")
        for name, (pt, mx, raw) in detail.items():
            st.write(f"**{name}**　{raw}　（{pt}/{mx}点）")
            st.progress(pt / mx)

        # 株価トレンド（本物の1年・終値）
        st.markdown("#### 株価トレンド（直近1年・終値）")
        if len(s["hist"]) > 0:
            st.line_chart(s["hist"])
        else:
            st.info("株価履歴を取得できませんでした。")

        # テクニカル参考（売りサインではない＝あなたのbacktest.pyの結論を反映）
        st.markdown("#### テクニカル参考")
        c1, c2 = st.columns(2)
        c1.metric("高値からの下落", f"{s.get('drop')}%" if s.get("drop") is not None else "-")
        c2.metric("25日線かい離", f"{s.get('ma_dev')}%" if s.get("ma_dev") is not None else "-")
        st.caption("⚠ 高値からの下落・25日線割れは『売りサイン』ではありません。過去検証では"
                   "むしろ反発（押し目）寄り＝逆張りの目安。ただし全体が下降局面だと"
                   "“落ちるナイフ”になり得るので、地合いと合わせて自分の目で判断を。")

        # メモ（セッション内・このアプリを開いている間だけ保存）
        st.markdown("#### メモ")
        memo = st.text_area("気になる点・判断理由など",
                            value=st.session_state.memos.get(sel, ""),
                            key=f"memo_{sel}", height=100)
        st.session_state.memos[sel] = memo

        # ===== AI 判断サポート =====
        st.markdown("#### 🤖 AI 判断サポート")
        st.caption("AIは投資助言ではなく『考えの整理』です。最終判断はあなた自身が行ってください。")
        client = get_anthropic_client()
        if client is None:
            st.info("AI機能を使うには、E:\\python-practice に anthropic_key.txt（APIキーを1行）を置いてください。")
        else:
            ctx = ai_context(s, total, detail)

            # 買い／見送り判断チェックリスト
            if st.button("買い／見送り 判断チェックリストを作る", key=f"check_{sel}"):
                try:
                    user_msg = (
                        f"次の銘柄について、買い/見送りを判断するためのチェックリストを作ってください。\n\n"
                        f"{ctx}\n\n"
                        "観点を6〜8個、それぞれ ✅(良い)／⚠️(注意)／❌(悪い) のいずれかと一行コメントで。"
                        "最後に『総合判断（強く買い〜強く見送りの5段階）』『確信度（％）』『一行理由』を付けてください。"
                        "マークダウンで簡潔に。"
                    )
                    placeholder = st.empty()
                    with placeholder.container():
                        result = st.write_stream(
                            ai_stream(client, AI_SYSTEM,
                                      [{"role": "user", "content": user_msg}],
                                      thinking=True, max_tokens=1800)
                        )
                    st.session_state.checklist[sel] = result
                except Exception as e:
                    st.error(f"AI呼び出しに失敗しました：{e}")
            elif st.session_state.checklist.get(sel):
                st.markdown(st.session_state.checklist[sel])   # 前回の結果を再表示

            # 相談チャット
            st.markdown("**この銘柄についてAIに相談**")
            history = st.session_state.chat.setdefault(sel, [])
            for m in history:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])
            with st.form(key=f"chatform_{sel}", clear_on_submit=True):
                q = st.text_input("質問", placeholder="例：今の割安感をどう見る？",
                                  label_visibility="collapsed")
                sent = st.form_submit_button("送信")
            if sent and q:
                history.append({"role": "user", "content": q})
                with st.chat_message("user"):
                    st.markdown(q)
                # 会話の先頭に銘柄の事実メモを添えて文脈を与える
                msgs = [{"role": "user", "content": f"【分析中の銘柄データ】\n{ctx}"},
                        {"role": "assistant", "content": "了解しました。この銘柄について質問してください。"}]
                msgs += history
                try:
                    with st.chat_message("assistant"):
                        answer = st.write_stream(
                            ai_stream(client, AI_SYSTEM, msgs, max_tokens=1200)
                        )
                    history.append({"role": "assistant", "content": answer})
                except Exception as e:
                    st.error(f"AI呼び出しに失敗しました：{e}")

        st.caption("数値はyfinance由来。決算直後など反映が遅れることがあるので、最終確認は証券会社の画面で。")

    # ===== 比較タブ =====
    with tab_compare:
        if len(st.session_state.stocks) < 2:
            st.info("2銘柄以上を分析すると、ここで並べて比較できます。")
        else:
            rows = []
            for code, x in st.session_state.stocks.items():
                tot, _ = value_score(x)
                rows.append({
                    "銘柄": x["name"], "コード": code,
                    "現在値": round(x["price"]) if x["price"] else None,
                    "スコア": tot,
                    "PER": round(x["per"], 1) if x["per"] else None,
                    "PBR": round(x["pbr"], 2) if x["pbr"] else None,
                    "配当%": round(x["yield"], 1) if x["yield"] else None,
                    "高値下落%": x.get("drop"),
                })
            df = pd.DataFrame(rows).sort_values("スコア", ascending=False)
            st.dataframe(df, use_container_width=True, hide_index=True)
            st.caption("スコアが高い＝割安寄り。ただしスコアは目安で、買い推奨ではありません。")
else:
    st.info("👆 上に銘柄コードを入れて「分析」を押してください。例：7203.T（トヨタ）、6269.T、PLTR")
