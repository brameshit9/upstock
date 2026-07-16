"""
Nifty 50 — Live VWAP / EMA9 Signal Dashboard (Streamlit + Upstox API v3)
=========================================================================

Deploy target: Streamlit Community Cloud (or run locally with `streamlit run app.py`).

WHAT THIS DOES
--------------
For every symbol in NIFTY50_SYMBOLS, streams live ticks from Upstox and
tracks 4 conditions in real time:
    1. Price ABOVE VWAP        2. Price ABOVE EMA9
    3. Price BELOW VWAP        4. Price BELOW EMA9
rendered as a color-coded card grid that auto-refreshes.

CREDENTIALS — DO NOT HARDCODE
------------------------------
Nothing here is hardcoded. Two separate things are needed:

1. UPSTOX_API_KEY / UPSTOX_API_SECRET — long-lived, from your Upstox
   developer app. Set these via Streamlit secrets (see .streamlit/
   secrets.toml.example in this folder), NEVER commit them to git.

2. Access token — Upstox tokens expire at end of day, so they can't be a
   static secret. Instead this app does the OAuth login flow itself: the
   sidebar gives you a "Log in to Upstox" link, you authenticate in the
   browser (Upstox will prompt for TOTP/PIN as usual), Upstox redirects
   back to this app's own URL with a `?code=...` param, and the app
   exchanges that for a fresh access token automatically. You do this
   once per trading day.

SETUP
-----
    pip install -r requirements.txt

Local dev:
    cp .streamlit/secrets.toml.example .streamlit/secrets.toml
    # fill in UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI
    streamlit run app.py

Streamlit Cloud:
    - Push this folder to a GitHub repo (secrets.toml is gitignored —
      only secrets.toml.example gets committed).
    - In the Streamlit Cloud app settings, paste the same three keys into
      "Secrets".
    - In your Upstox developer app config, set the Redirect URI to your
      deployed app's URL (e.g. https://your-app.streamlit.app), and use
      that exact same value for UPSTOX_REDIRECT_URI in secrets.

VWAP NOTE
---------
Individual NSE equities have real traded volume, so this uses the
exchange-computed average trade price ("atp") from Upstox's "full" feed
as the true session VWAP when available, falling back to an accumulated
volume*price calc, then to an unweighted running-mean proxy if neither
volume field is present. See parse_full_feed() — Upstox's decoded field
names can shift across SDK versions, so verify against your installed
`upstox_client` version if atp/vtt come back None.
"""

import gzip
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, time as dtime

import requests
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import upstox_client
from upstox_client.rest import ApiException

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False

# ============================== CONFIG ==============================
EMA_PERIOD = 9
CHART_WINDOW = 200
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
SEED_REQUEST_SLEEP = 0.25
GRID_COLS = 7
INSTRUMENT_MASTER_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
REFRESH_MS = 2000

NIFTY50_SYMBOLS = [
    "SHRIRAMFIN", "BHARTIARTL", "AXISBANK", "SUNPHARMA", "CIPLA",
    "HDFCLIFE", "APOLLOHOSP", "JIOFIN", "LT", "TMPV",
    "ITC", "ICICIBANK", "INDIGO", "BAJAJ-AUTO", "NESTLEIND",
    "BAJAJFINSV", "TATASTEEL", "ADANIPORTS", "DRREDDY", "GRASIM",
    "ONGC", "TRENT", "HDFCBANK", "ADANIENT", "KOTAKBANK",
    "JSWSTEEL", "ASIANPAINT", "SBILIFE", "MARUTI", "RELIANCE",
    "EICHERMOT", "ULTRACEMCO", "HINDUNILVR", "SBIN", "MAXHEALTH",
    "BAJFINANCE", "TITAN", "COALINDIA", "POWERGRID", "NTPC",
    "TATACONSUM", "M&M", "HINDALCO", "BEL", "ETERNAL",
    "TCS", "HCLTECH", "WIPRO", "INFY", "TECHM",
]

SIGNAL_COLORS = {
    "BULLISH": "#2ecc71",
    "BEARISH": "#e74c3c",
    "MIXED_1": "#f39c12",
    "MIXED_2": "#e67e22",
    "NEUTRAL": "#7f8c8d",
}
# ======================================================================

st.set_page_config(page_title="Nifty 50 VWAP/EMA9 Dashboard", layout="wide")


def get_secret(name, default=None):
    """Read from Streamlit secrets first, then environment variables.
    Lets the same code run on Streamlit Cloud (secrets.toml) and in
    plain GitHub Actions / local shells (env vars) without changes."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


API_KEY = get_secret("UPSTOX_API_KEY")
API_SECRET = get_secret("UPSTOX_API_SECRET")
REDIRECT_URI = get_secret("UPSTOX_REDIRECT_URI")

if not API_KEY or not API_SECRET or not REDIRECT_URI:
    st.error(
        "Missing UPSTOX_API_KEY / UPSTOX_API_SECRET / UPSTOX_REDIRECT_URI.\n\n"
        "Add them via .streamlit/secrets.toml (local) or the Streamlit Cloud "
        "'Secrets' settings panel. See .streamlit/secrets.toml.example."
    )
    st.stop()


# =============================================================================
# OAuth — daily login flow (access tokens are NOT long-lived, so this is not
# something we can just put in secrets.toml once and forget)
# =============================================================================
def exchange_code_for_token(code: str):
    try:
        resp = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            data={
                "code": code,
                "client_id": API_KEY,
                "client_secret": API_SECRET,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"accept": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        st.sidebar.error(f"Token exchange failed: {e}")
        return None


if "access_token" not in st.session_state:
    st.session_state.access_token = None

query_params = st.query_params
if "code" in query_params and not st.session_state.access_token:
    token = exchange_code_for_token(query_params["code"])
    if token:
        st.session_state.access_token = token
        st.query_params.clear()
        st.rerun()

with st.sidebar:
    st.header("Upstox Session")
    if not st.session_state.access_token:
        login_url = (
            "https://api.upstox.com/v2/login/authorization/dialog"
            f"?response_type=code&client_id={API_KEY}&redirect_uri={REDIRECT_URI}"
        )
        st.markdown(f"[**Log in to Upstox →**]({login_url})")
        st.caption("Redirects back here automatically once you authenticate.")
    else:
        st.success("Authenticated for today's session")
        if st.button("Log out"):
            st.session_state.access_token = None
            st.rerun()

if not st.session_state.access_token:
    st.title("Nifty 50 — Live VWAP / EMA9 Signal Dashboard")
    st.info("Log in via the sidebar to start streaming.")
    st.stop()

configuration = upstox_client.Configuration()
configuration.access_token = st.session_state.access_token


# =============================================================================
# Instrument master — resolve trading symbols to Upstox instrument keys
# =============================================================================
@st.cache_data(ttl=86400, show_spinner="Downloading Upstox instrument master...")
def load_instrument_master():
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=60)
    resp.raise_for_status()
    return json.loads(gzip.decompress(resp.content))


@st.cache_data(ttl=86400, show_spinner="Resolving symbols...")
def resolve_instrument_keys(symbols):
    master = load_instrument_master()
    lookup = {}
    for row in master:
        if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ":
            lookup[row.get("trading_symbol", "").upper()] = row.get("instrument_key")

    resolved, missing = {}, []
    for sym in symbols:
        key = lookup.get(sym.upper())
        if key:
            resolved[sym] = key
        else:
            missing.append(sym)
    return resolved, missing


# =============================================================================
# Market data manager — background WebSocket + shared state, singleton per
# access token so the socket survives across Streamlit reruns
# =============================================================================
class MarketDataManager:
    def __init__(self, access_token, symbol_to_key):
        self.access_token = access_token
        self.symbol_to_key = symbol_to_key
        self.key_to_symbol = {v: k for k, v in symbol_to_key.items()}
        self.state_lock = threading.Lock()
        self.stock_state = {s: self._blank_state() for s in symbol_to_key}
        self.streamer = None

        cfg = upstox_client.Configuration()
        cfg.access_token = access_token
        self.configuration = cfg

        self._seed_all()
        self._start_streaming()

    @staticmethod
    def _blank_state():
        return {
            "times": deque(maxlen=CHART_WINDOW),
            "prices": deque(maxlen=CHART_WINDOW),
            "vwaps": deque(maxlen=CHART_WINDOW),
            "emas": deque(maxlen=CHART_WINDOW),
            "candles": deque(maxlen=400),  # 1-min OHLC bars: {time,open,high,low,close}
            "cum_pv": 0.0,
            "cum_vol": 0.0,
            "last_cum_vol": 0.0,
            "vwap_mode": "proxy",
            "ema_prev": None,
            "ltp": None,
            "vwap": None,
            "ema": None,
            "signal": "NEUTRAL",
            "last_signal": None,
        }

    # ---------------- seeding ----------------
    def _seed_all(self):
        history_api = upstox_client.HistoryV3Api(upstox_client.ApiClient(self.configuration))
        for symbol, key in self.symbol_to_key.items():
            self._seed_stock(symbol, key, history_api)
            time.sleep(SEED_REQUEST_SLEEP)

    def _seed_stock(self, symbol, instrument_key, history_api):
        st_ = self.stock_state[symbol]
        try:
            response = history_api.get_intra_day_candle_data(instrument_key, "minutes", "1")
        except ApiException:
            return

        candles = getattr(response.data, "candles", []) if hasattr(response, "data") else []
        if not candles:
            return

        candles = list(reversed(candles))
        total_vol = sum(c[5] for c in candles)
        st_["vwap_mode"] = "accumulated" if total_vol > 0 else "proxy"
        k = 2 / (EMA_PERIOD + 1)

        with self.state_lock:
            for c in candles:
                ts, o, h, l, close, vol, _oi = c
                typical_price = (h + l + close) / 3
                if st_["vwap_mode"] == "accumulated":
                    st_["cum_pv"] += typical_price * vol
                    st_["cum_vol"] += vol
                else:
                    st_["cum_pv"] += typical_price
                    st_["cum_vol"] += 1
                vwap = st_["cum_pv"] / st_["cum_vol"] if st_["cum_vol"] else close
                st_["ema_prev"] = close if st_["ema_prev"] is None else close * k + st_["ema_prev"] * (1 - k)

                candle_time = pd.to_datetime(ts)
                st_["times"].append(candle_time)
                st_["prices"].append(close)
                st_["vwaps"].append(vwap)
                st_["emas"].append(st_["ema_prev"])
                st_["candles"].append({
                    "time": candle_time, "open": o, "high": h, "low": l, "close": close,
                })

            st_["last_cum_vol"] = total_vol
            st_["ltp"] = st_["prices"][-1] if st_["prices"] else None
            st_["vwap"] = st_["vwaps"][-1] if st_["vwaps"] else None
            st_["ema"] = st_["emas"][-1] if st_["emas"] else None

    # ---------------- live ticks ----------------
    @staticmethod
    def _classify(ltp, vwap, ema):
        above_vwap, above_ema = ltp > vwap, ltp > ema
        below_vwap, below_ema = ltp < vwap, ltp < ema
        if above_vwap and above_ema:
            return "BULLISH"
        if below_vwap and below_ema:
            return "BEARISH"
        if above_vwap and below_ema:
            return "MIXED_1"
        if below_vwap and above_ema:
            return "MIXED_2"
        return "NEUTRAL"

    def _update(self, symbol, ltp, atp=None, cum_day_vol=None):
        st_ = self.stock_state.get(symbol)
        if st_ is None:
            return
        k = 2 / (EMA_PERIOD + 1)

        with self.state_lock:
            if atp is not None:
                vwap = atp
                st_["vwap_mode"] = "atp"
            elif cum_day_vol is not None and cum_day_vol > st_["last_cum_vol"]:
                delta_vol = cum_day_vol - st_["last_cum_vol"]
                st_["cum_pv"] += ltp * delta_vol
                st_["cum_vol"] += delta_vol
                st_["last_cum_vol"] = cum_day_vol
                vwap = st_["cum_pv"] / st_["cum_vol"] if st_["cum_vol"] else ltp
                st_["vwap_mode"] = "accumulated"
            else:
                st_["cum_pv"] += ltp
                st_["cum_vol"] += 1
                vwap = st_["cum_pv"] / st_["cum_vol"]
                if st_["vwap_mode"] not in ("atp", "accumulated"):
                    st_["vwap_mode"] = "proxy"

            st_["ema_prev"] = ltp if st_["ema_prev"] is None else ltp * k + st_["ema_prev"] * (1 - k)
            ema = st_["ema_prev"]

            now = datetime.now()
            st_["times"].append(now)
            st_["prices"].append(ltp)
            st_["vwaps"].append(vwap)
            st_["emas"].append(ema)
            st_["ltp"], st_["vwap"], st_["ema"] = ltp, vwap, ema
            st_["signal"] = self._classify(ltp, vwap, ema)
            st_["last_signal"] = st_["signal"]

            # roll ltp ticks into a live-forming 1-min OHLC candle
            minute = now.replace(second=0, microsecond=0)
            candles = st_["candles"]
            if candles and candles[-1]["time"] == minute:
                bar = candles[-1]
                bar["high"] = max(bar["high"], ltp)
                bar["low"] = min(bar["low"], ltp)
                bar["close"] = ltp
            else:
                candles.append({"time": minute, "open": ltp, "high": ltp, "low": ltp, "close": ltp})

    @staticmethod
    def _parse_full_feed(feed):
        """NOTE: Upstox's decoded 'full' feed field names vary by SDK
        version/segment. Adjust here if atp/vtt come back None for your
        installed upstox_client version — check FeedResponse.proto."""
        full = feed.get("fullFeed", {})
        market_ff = full.get("marketFF", {}) or full.get("eqFF", {})
        ltpc = market_ff.get("ltpc") or feed.get("ltpc") or {}
        ltp = ltpc.get("ltp")
        atp = market_ff.get("atp")
        cum_day_vol = market_ff.get("vtt")
        return (
            float(ltp) if ltp is not None else None,
            float(atp) if atp is not None else None,
            float(cum_day_vol) if cum_day_vol is not None else None,
        )

    def _on_message(self, message):
        try:
            feeds = message.get("feeds", {})
            for instrument_key, feed in feeds.items():
                symbol = self.key_to_symbol.get(instrument_key)
                if symbol is None:
                    continue
                ltp, atp, cum_day_vol = self._parse_full_feed(feed)
                if ltp is None:
                    continue
                self._update(symbol, ltp, atp=atp, cum_day_vol=cum_day_vol)
        except Exception:
            pass

    def _start_streaming(self):
        self.streamer = upstox_client.MarketDataStreamerV3(
            upstox_client.ApiClient(self.configuration),
            list(self.symbol_to_key.values()), "full",
        )
        self.streamer.on("open", lambda: self.streamer.subscribe(
            list(self.symbol_to_key.values()), "full"))
        self.streamer.on("message", self._on_message)

        def run():
            while True:
                try:
                    self.streamer.connect()
                except Exception:
                    pass
                time.sleep(5)

        threading.Thread(target=run, daemon=True).start()

    def snapshot(self):
        with self.state_lock:
            return {
                s: {
                    "ltp": v["ltp"], "vwap": v["vwap"], "ema": v["ema"],
                    "signal": v["signal"], "vwap_mode": v["vwap_mode"],
                }
                for s, v in self.stock_state.items()
            }

    def get_chart_data(self, symbol):
        with self.state_lock:
            st_ = self.stock_state.get(symbol)
            if st_ is None:
                return None
            return {
                "candles": list(st_["candles"]),
                "times": list(st_["times"]),
                "vwaps": list(st_["vwaps"]),
                "emas": list(st_["emas"]),
                "vwap_mode": st_["vwap_mode"],
            }


@st.cache_resource(show_spinner="Connecting to Upstox & seeding today's data...")
def get_manager(access_token, symbols_tuple):
    resolved, missing = resolve_instrument_keys(list(symbols_tuple))
    if missing:
        st.session_state["_missing_symbols"] = missing
    return MarketDataManager(access_token, resolved)


# =============================================================================
# UI
# =============================================================================
st.title("Nifty 50 — Live VWAP / EMA9 Signal Dashboard")

manager = get_manager(st.session_state.access_token, tuple(NIFTY50_SYMBOLS))

missing = st.session_state.get("_missing_symbols", [])
if missing:
    st.warning(f"Could not resolve {len(missing)} symbol(s), skipped: {missing}")

within_market = MARKET_OPEN <= datetime.now().time() <= MARKET_CLOSE
if not within_market:
    st.caption("⏸ Outside market hours (09:15–15:30 IST) — showing last known values.")

if HAS_AUTOREFRESH:
    st_autorefresh(interval=REFRESH_MS, key="dashboard_refresh")
else:
    st.caption("Tip: `pip install streamlit-autorefresh` for auto-updating cards; "
               "otherwise refresh the page manually.")

snapshot = manager.snapshot()
symbols = list(snapshot.keys())

if "selected_symbol" not in st.session_state:
    st.session_state.selected_symbol = symbols[0] if symbols else None

st.session_state.selected_symbol = st.selectbox(
    "Chart — 1 minute candles",
    symbols,
    index=symbols.index(st.session_state.selected_symbol)
    if st.session_state.selected_symbol in symbols else 0,
)


def render_stock_chart(manager, symbol):
    data = manager.get_chart_data(symbol)
    if not data or not data["candles"]:
        st.info(f"No candle data yet for {symbol}.")
        return

    df = pd.DataFrame(data["candles"])
    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df["time"], open=df["open"], high=df["high"], low=df["low"], close=df["close"],
        name=symbol, increasing_line_color="#2ecc71", decreasing_line_color="#e74c3c",
        showlegend=False,
    ))
    if data["times"] and data["vwaps"]:
        fig.add_trace(go.Scatter(
            x=data["times"], y=data["vwaps"], mode="lines",
            name=f"VWAP ({data['vwap_mode']})", line=dict(color="#00e5ff", width=1.5),
        ))
    if data["times"] and data["emas"]:
        fig.add_trace(go.Scatter(
            x=data["times"], y=data["emas"], mode="lines",
            name="EMA9", line=dict(color="#ffeb3b", width=1.5),
        ))

    latest_vwap = data["vwaps"][-1] if data["vwaps"] else None
    latest_ema = data["emas"][-1] if data["emas"] else None
    subtitle = ""
    if latest_vwap is not None and latest_ema is not None:
        subtitle = f" — VWAP {latest_vwap:.2f} | EMA9 {latest_ema:.2f}"

    fig.update_layout(
        title=f"{symbol}{subtitle}",
        template="plotly_dark",
        xaxis_rangeslider_visible=False,
        height=480,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    st.plotly_chart(fig, use_container_width=True)


if st.session_state.selected_symbol:
    render_stock_chart(manager, st.session_state.selected_symbol)

st.divider()
st.caption("Overview grid — click a stock in the dropdown above to see its chart")

for row_start in range(0, len(symbols), GRID_COLS):
    row_symbols = symbols[row_start:row_start + GRID_COLS]
    cols = st.columns(GRID_COLS)
    for col, sym in zip(cols, row_symbols):
        data = snapshot[sym]
        color = SIGNAL_COLORS.get(data["signal"], "#7f8c8d")
        ltp_txt = f"{data['ltp']:.1f}" if data["ltp"] is not None else "--"
        vwap_txt = f"{data['vwap']:.1f}" if data["vwap"] is not None else "--"
        ema_txt = f"{data['ema']:.1f}" if data["ema"] is not None else "--"
        col.markdown(
            f"""
            <div style="background-color:{color};border-radius:8px;padding:10px 6px;
                        text-align:center;color:white;margin-bottom:8px;">
              <div style="font-weight:700;font-size:13px;">{sym}</div>
              <div style="font-size:17px;margin:2px 0;">{ltp_txt}</div>
              <div style="font-size:10px;opacity:0.9;">V:{vwap_txt} &nbsp; E:{ema_txt}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

st.divider()
legend_cols = st.columns(5)
for col, (label, color) in zip(legend_cols, SIGNAL_COLORS.items()):
    col.markdown(
        f'<div style="background-color:{color};border-radius:4px;padding:4px;'
        f'text-align:center;color:white;font-size:11px;">{label}</div>',
        unsafe_allow_html=True,
    )
