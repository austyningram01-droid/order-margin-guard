import os, json, sqlite3, sys, hmac, hashlib, urllib.request
from urllib.parse import parse_qs, urlparse, urlencode

DB_PATH = "margin_guard.db"
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://order-margin-guard.onrender.com").rstrip("/")
SCOPES = "read_orders,read_fulfillments,write_fulfillments,read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders"

def load_base_settings():
    if os.path.exists("settings.json"):
        try:
            with open("settings.json", "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS merchants (
        shop_domain TEXT PRIMARY KEY,
        min_margin_pct REAL DEFAULT 15.0,
        alert_webhook_url TEXT DEFAULT '',
        access_token TEXT DEFAULT ''
    )""")
    c.execute("PRAGMA table_info(merchants)")
    cols = [r[1] for r in c.fetchall()]
    if "access_token" not in cols:
        c.execute("ALTER TABLE merchants ADD COLUMN access_token TEXT DEFAULT ''")

    c.execute("""CREATE TABLE IF NOT EXISTS sku_costs (
        shop_domain TEXT,
        sku TEXT,
        unit_cost REAL DEFAULT 10.0,
        est_shipping REAL DEFAULT 5.0,
        PRIMARY KEY (shop_domain, sku)
    )""")

    settings = load_base_settings()
    shop = settings.get("shopify_domain") or settings.get("shopify_store_url") or "default_store"
    min_m = float(settings.get("min_margin_pct", 15.0))
    webhook = settings.get("alert_webhook_url") or settings.get("discord_webhook_url") or ""
    token = settings.get("shopify_access_token", "")

    c.execute("INSERT OR IGNORE INTO merchants (shop_domain, min_margin_pct, alert_webhook_url, access_token) VALUES (?, ?, ?, ?)",
              (shop, min_m, webhook, token))
    if token:
        c.execute("UPDATE merchants SET access_token = ? WHERE shop_domain = ? AND (access_token IS NULL OR access_token = '')", (token, shop))

    cogs = {"DEFAULT": {"unit_cost": 10.0, "est_shipping": 5.0}}
    if os.path.exists("sku_cogs.json"):
        try:
            with open("sku_cogs.json", "r") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict) and loaded:
                    cogs.update(loaded)
        except Exception:
            pass
    for sku, data in cogs.items():
        if isinstance(data, dict):
            uc = float(data.get("unit_cost", 10.0))
            es = float(data.get("est_shipping", 5.0))
            c.execute("INSERT OR IGNORE INTO sku_costs VALUES (?, ?, ?, ?)", (shop, sku, uc, es))
    conn.commit()
    conn.close()

def sync_state(target_shop=None):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if target_shop:
        c.execute("SELECT shop_domain, min_margin_pct, alert_webhook_url, access_token FROM merchants WHERE shop_domain = ?", (target_shop,))
        row = c.fetchone()
    else:
        row = None
    if not row:
        c.execute("SELECT shop_domain, min_margin_pct, alert_webhook_url, access_token FROM merchants LIMIT 1")
        row = c.fetchone()

    shop, min_m, webhook, token = row if row else ("default_store", 15.0, "", "")

    c.execute("SELECT sku, unit_cost, est_shipping FROM sku_costs WHERE shop_domain = ?", (shop,))
    skus = {r[0]: {"unit_cost": r[1], "est_shipping": r[2]} for r in c.fetchall()}
    if "DEFAULT" not in skus:
        skus["DEFAULT"] = {"unit_cost": 10.0, "est_shipping": 5.0}
    conn.close()

    for mod_name in ["__main__", "margin_guard_server", "margin_guard_engine"]:
        mod = sys.modules.get(mod_name)
        if mod:
            if hasattr(mod, "SETTINGS") and isinstance(mod.SETTINGS, dict):
                mod.SETTINGS["shopify_domain"] = shop
                mod.SETTINGS["shopify_store_url"] = shop
                mod.SETTINGS["min_margin_pct"] = min_m
                mod.SETTINGS["alert_webhook_url"] = webhook
                mod.SETTINGS["discord_webhook_url"] = webhook
                if token:
                    mod.SETTINGS["shopify_access_token"] = token
            if hasattr(mod, "SKU_COSTS") and isinstance(mod.SKU_COSTS, dict):
                mod.SKU_COSTS.clear()
                mod.SKU_COSTS.update(skus)

    try:
        with open("sku_cogs.json", "w") as f:
            json.dump(skus, f, indent=2)
    except Exception:
        pass
    return shop, min_m, webhook, skus

def register_order_webhook(shop, access_token):
    url = f"https://{shop}/admin/api/2024-10/webhooks.json"
    payload = json.dumps({
        "webhook": {
            "topic": "orders/create",
            "address": f"{APP_URL}/",
            "format": "json"
        }
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": access_token
    })
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[!] Webhook auto-registration notice for {shop}: {e}")

def handle_get(handler):
    parsed = urlparse(handler.path)
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    settings = load_base_settings()
    client_id = settings.get("shopify_client_id", "")
    client_secret = settings.get("shopify_client_secret", "")

    if parsed.path == "/install":
        shop = qs.get("shop", "").strip().lower()
        if not shop.endswith(".myshopify.com"):
            shop = f"{shop}.myshopify.com" if shop else ""
        if not shop or not client_id:
            handler.send_response(400)
            handler.end_headers()
            handler.wfile.write(b"Missing shop parameter or shopify_client_id in settings.json")
            return
        redirect_uri = f"{APP_URL}/auth/callback"
        auth_url = f"https://{shop}/admin/oauth/authorize?" + urlencode({
            "client_id": client_id,
            "scope": SCOPES,
            "redirect_uri": redirect_uri
        })
        handler.send_response(302)
        handler.send_header("Location", auth_url)
        handler.end_headers()
        return

    if parsed.path == "/auth/callback":
        shop = qs.get("shop", "").strip().lower()
        code = qs.get("code", "").strip()
        if not shop or not code or not client_id or not client_secret:
            handler.send_response(400)
            handler.end_headers()
            handler.wfile.write(b"Invalid OAuth callback parameters")
            return
        token_url = f"https://{shop}/admin/oauth/access_token"
        payload = json.dumps({
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code
        }).encode("utf-8")
        req = urllib.request.Request(token_url, data=payload, method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                access_token = data.get("access_token", "")
        except Exception as e:
            handler.send_response(500)
            handler.end_headers()
            handler.wfile.write(f"OAuth token exchange failed: {e}".encode("utf-8"))
            return

        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO merchants (shop_domain, min_margin_pct, alert_webhook_url, access_token) VALUES (?, 15.0, '', ?)", (shop, access_token))
        c.execute("UPDATE merchants SET access_token = ? WHERE shop_domain = ?", (access_token, shop))
        c.execute("INSERT OR IGNORE INTO sku_costs VALUES (?, 'DEFAULT', 10.0, 5.0)", (shop,))
        conn.commit()
        conn.close()

        register_order_webhook(shop, access_token)
        handler.send_response(302)
        handler.send_header("Location", f"/?shop={shop}")
        handler.end_headers()
        return

    target_shop = qs.get("shop")
    shop, min_m, webhook, skus = sync_state(target_shop)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT shop_domain FROM merchants ORDER BY shop_domain")
    all_shops = [r[0] for r in c.fetchall()]
    conn.close()

    shop_options = "".join(f'<option value="{s}" {"selected" if s == shop else ""}>{s}</option>' for s in all_shops)
    rows_html = "".join(
        f"<tr><td><code>{s}</code></td><td>${d['unit_cost']:.2f}</td><td>${d['est_shipping']:.2f}</td>"
        f"<td>{'' if s == 'DEFAULT' else f'<form method=\"POST\" action=\"/api/sku/delete\" style=\"margin:0\"><input type=\"hidden\" name=\"shop\" value=\"{shop}\"><input type=\"hidden\" name=\"sku\" value=\"{s}\"><button class=\"btn-del\">Delete</button></form>'}</td></tr>"
        for s, d in sorted(skus.items())
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Margin Guard Dashboard</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; max-width: 820px; margin: 40px auto; padding: 0 20px; }}
.card {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 20px; margin-bottom: 24px; }}
h1 {{ margin-top: 0; color: #38bdf8; }} h2 {{ margin-top: 0; font-size: 18px; color: #e2e8f0; }}
label {{ display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; }}
input, select {{ width: 100%; padding: 8px 10px; margin-bottom: 12px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; box-sizing: border-box; }}
button {{ background: #0284c7; color: #fff; border: none; padding: 9px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; }}
button:hover {{ background: #0369a1; }}
.btn-del {{ background: #dc2626; padding: 5px 10px; font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #334155; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
.top-bar {{ display: flex; gap: 12px; align-items: flex-end; }}
</style></head><body>
<h1>🛡️ Margin Guard App Dashboard</h1>
<div class="card">
  <h2>Connected Stores & OAuth Installer</h2>
  <form method="GET" action="/" class="top-bar">
    <div style="flex:1"><label>Active Store</label><select name="shop" onchange="this.form.submit()">{shop_options}</select></div>
  </form>
  <form method="GET" action="/install" class="top-bar" style="margin-top:8px">
    <div style="flex:1"><label>Install New Shopify Store (OAuth)</label><input type="text" name="shop" placeholder="new-store.myshopify.com" style="margin-bottom:0" required></div>
    <button type="submit">Connect Store</button>
  </form>
</div>
<div class="card">
  <h2>Margin & Alert Settings ({shop})</h2>
  <form method="POST" action="/api/settings">
    <input type="hidden" name="shop" value="{shop}">
    <label>Minimum Profit Margin (%)</label>
    <input type="number" step="0.1" name="min_margin_pct" value="{min_m}" required>
    <label>Discord Alert Webhook URL</label>
    <input type="url" name="alert_webhook_url" value="{webhook}" required>
    <button type="submit">Save Settings</button>
  </form>
</div>
<div class="card">
  <h2>SKU Cost & Shipping Manager ({shop})</h2>
  <form method="POST" action="/api/sku">
    <input type="hidden" name="shop" value="{shop}">
    <div class="grid">
      <div><label>SKU (or DEFAULT)</label><input type="text" name="sku" placeholder="SKU-123" required></div>
      <div><label>Unit Cost ($)</label><input type="number" step="0.01" name="unit_cost" placeholder="10.00" required></div>
      <div><label>Est. Shipping ($)</label><input type="number" step="0.01" name="est_shipping" placeholder="5.00" required></div>
    </div>
    <button type="submit">Add / Update SKU</button>
  </form>
  <table>
    <thead><tr><th>SKU</th><th>Unit Cost</th><th>Est. Shipping</th><th>Action</th></tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>
</body></html>"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(html.encode("utf-8"))

def handle_post(handler):
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length).decode("utf-8")
    form = {k: v[0] for k, v in parse_qs(body).items()}
    target_shop = form.get("shop")
    shop, _, _, _ = sync_state(target_shop)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if handler.path == "/api/settings":
        c.execute("UPDATE merchants SET min_margin_pct = ?, alert_webhook_url = ? WHERE shop_domain = ?",
                  (float(form.get("min_margin_pct", 15.0)), form.get("alert_webhook_url", ""), shop))
    elif handler.path == "/api/sku":
        sku = form.get("sku", "").strip()
        if sku:
            c.execute("INSERT OR REPLACE INTO sku_costs VALUES (?, ?, ?, ?)",
                      (shop, sku, float(form.get("unit_cost", 10.0)), float(form.get("est_shipping", 5.0))))
    elif handler.path == "/api/sku/delete":
        sku = form.get("sku", "").strip()
        if sku and sku != "DEFAULT":
            c.execute("DELETE FROM sku_costs WHERE shop_domain = ? AND sku = ?", (shop, sku))
    conn.commit()
    conn.close()
    sync_state(shop)
    handler.send_response(303)
    handler.send_header("Location", f"/?shop={shop}")
    handler.end_headers()
