# Nifty 50 — Live VWAP/EMA9 Signal Dashboard

## 1. Upstox developer app
In your Upstox developer app settings, set **Redirect URI** to:
- Local dev: `http://localhost:8501`
- Streamlit Cloud: `https://<your-app-name>.streamlit.app`

(Must match `UPSTOX_REDIRECT_URI` exactly, including trailing slash or lack thereof.)

## 2. Local run
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit .streamlit/secrets.toml with your real API key/secret + redirect URI
streamlit run app.py
```

## 3. Deploy to Streamlit Cloud
1. Push this folder to a GitHub repo. `.streamlit/secrets.toml` is gitignored —
   only `secrets.toml.example` gets committed, which is safe.
2. Create a new app on share.streamlit.io pointing at `app.py`.
3. In the app's **Settings → Secrets**, paste:
   ```toml
   UPSTOX_API_KEY = "..."
   UPSTOX_API_SECRET = "..."
   UPSTOX_REDIRECT_URI = "https://<your-app-name>.streamlit.app"
   ```
4. Update the Upstox developer app's Redirect URI to match.

## 4. Daily use
Upstox access tokens expire every day (no long-lived tokens). Each trading
day, open the app and click **"Log in to Upstox"** in the sidebar — it
redirects through Upstox's login (with your usual TOTP/PIN), then back into
the app, which exchanges the code for a fresh token automatically. No manual
token copy-pasting needed.

## Notes
- `NIFTY50_SYMBOLS` in `app.py` includes `TMPV` and `MAXHEALTH` as given —
  if these don't resolve against Upstox's instrument master (e.g. due to a
  recent listing/rename not yet reflected there), the app will warn and
  skip them rather than fail.
- VWAP uses the exchange's `atp` field from the "full" feed when available
  (most accurate), falling back to accumulated volume, then to an
  unweighted proxy. See the comment on `_parse_full_feed()` in `app.py` if
  those fields come back empty for your SDK version.
