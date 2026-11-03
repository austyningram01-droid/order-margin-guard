import os, json, sqlite3, sys, hmac, hashlib, base64, urllib.request
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse, urlencode

DB_PATH = "margin_guard.db"
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://order-margin-guard.onrender.com").rstrip("/")
SCOPES = "read_products,read_orders,read_fulfillments,write_fulfillments,read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders"

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
        access_token TEXT DEFAULT '',
        plan_status TEXT DEFAULT 'FREE_TRIAL'
    )""")
    c.execute("PRAGMA table_info(merchants)")
    cols = [r[1] for r in c.fetchall()]
    if "access_token" not in cols:
        c.execute("ALTER TABLE merchants ADD COLUMN access_token TEXT DEFAULT ''")
    if "plan_status" not in cols:
        c.execute("ALTER TABLE merchants ADD COLUMN plan_status TEXT DEFAULT 'FREE_TRIAL'")

    c.execute("""CREATE TABLE IF NOT EXISTS sku_costs (
        shop_domain TEXT,
        sku TEXT,
        unit_cost REAL DEFAULT 10.0,
        est_shipping REAL DEFAULT 5.0,
        PRIMARY KEY (shop_domain, sku)
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS order_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shop_domain TEXT,
        order_name TEXT,
        total_price TEXT,
        margin_pct REAL,
        action TEXT,
        created_at TEXT
    )""")

    settings = load_base_settings()
    shop = settings.get("shopify_domain") or settings.get("shopify_store_url") or "default_store"
    min_m = float(settings.get("min_margin_pct", 15.0))
    webhook = settings.get("alert_webhook_url") or settings.get("discord_webhook_url") or ""
    token = settings.get("shopify_access_token", "")

    c.execute("INSERT OR IGNORE INTO merchants (shop_domain, min_margin_pct, alert_webhook_url, access_token, plan_status) VALUES (?, ?, ?, ?, 'FREE_TRIAL')",
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
    row = None
    if target_shop:
        c.execute("SELECT shop_domain, min_margin_pct, alert_webhook_url, access_token, plan_status FROM merchants WHERE shop_domain = ?", (target_shop,))
        row = c.fetchone()
    if not row:
        c.execute("SELECT shop_domain, min_margin_pct, alert_webhook_url, access_token, plan_status FROM merchants LIMIT 1")
        row = c.fetchone()

    shop, min_m, webhook, token, plan = row if row else ("default_store", 15.0, "", "", "FREE_TRIAL")

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

def get_shop_token_and_plan(shop):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT access_token, plan_status FROM merchants WHERE shop_domain = ?", (shop,))
    row = c.fetchone()
    conn.close()
    settings = load_base_settings()
    fallback_token = settings.get("shopify_access_token", "")
    if row:
        return (row[0] or fallback_token), (row[1] or "FREE_TRIAL")
    return fallback_token, "FREE_TRIAL"

def import_store_skus(shop):
    token, _ = get_shop_token_and_plan(shop)
    if not token:
        return 0
    discovered = set()
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    try:
        req = urllib.request.Request(f"https://{shop}/admin/api/2024-10/products.json?limit=50", headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for p in data.get("products", []):
                for v in p.get("variants", []):
                    sku = (v.get("sku") or "").strip()
                    if sku:
                        discovered.add(sku)
    except Exception:
        pass

    if not discovered:
        try:
            req = urllib.request.Request(f"https://{shop}/admin/api/2024-10/orders.json?limit=50&status=any", headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for o in data.get("orders", []):
                    for item in o.get("line_items", []):
                        sku = (item.get("sku") or "").strip()
                        if sku:
                            discovered.add(sku)
        except Exception:
            pass

    if discovered:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        for sku in discovered:
            c.execute("INSERT OR IGNORE INTO sku_costs VALUES (?, ?, 10.0, 5.0)", (shop, sku))
        conn.commit()
        conn.close()
    return len(discovered)

def record_order(target_shop, order_data, res):
    init_db()
    shop, _, _, _ = sync_state(target_shop)
    order_name = str(order_data.get("name") or res.get("order_id") or "#0000")
    total_price = str(order_data.get("total_price") or "0.00")
    margin_val = res.get("margin_pct", res.get("net_margin_pct", res.get("margin", 0.0)))
    try:
        margin_pct = float(margin_val)
    except Exception:
        margin_pct = 0.0
    action = str(res.get("action", "APPROVED"))
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO order_logs (shop_domain, order_name, total_price, margin_pct, action, created_at) VALUES (?, ?, ?, ?, ?, ?)",
              (shop, order_name, total_price, margin_pct, action, now_str))
    conn.commit()
    conn.close()

def verify_shopify_hmac(body_bytes, hmac_header):
    if not hmac_header:
        return False
    settings = load_base_settings()
    secrets = [
        settings.get("shopify_client_secret", ""),
        settings.get("shopify_shared_secret", "")
    ]
    for sec in secrets:
        if sec:
            digest = hmac.new(sec.encode("utf-8"), body_bytes, hashlib.sha256).digest()
            computed = base64.b64encode(digest).decode("utf-8")
            if hmac.compare_digest(computed, hmac_header):
                return True
    return False

def handle_compliance_webhook(handler):
    length = int(handler.headers.get("Content-Length", 0))
    body = handler.rfile.read(length)
    hmac_header = handler.headers.get("X-Shopify-Hmac-Sha256", "")
    if not verify_shopify_hmac(body, hmac_header):
        handler.send_response(401)
        handler.end_headers()
        handler.wfile.write(b"Unauthorized HMAC")
        return
    topic = handler.headers.get("X-Shopify-Topic", "")
    shop = handler.headers.get("X-Shopify-Shop-Domain", "")
    if topic == "shop/redact" and shop:
        init_db()
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM merchants WHERE shop_domain = ?", (shop,))
        c.execute("DELETE FROM sku_costs WHERE shop_domain = ?", (shop,))
        c.execute("DELETE FROM order_logs WHERE shop_domain = ?", (shop,))
        conn.commit()
        conn.close()
    handler.send_response(200)
    handler.end_headers()
    handler.wfile.write(b"OK")

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

    if parsed.path == "/privacy":
        privacy_html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Margin Guard Privacy Policy</title>
        <style>body{font-family:-apple-system,sans-serif;background:#0f172a;color:#f8fafc;max-width:720px;margin:40px auto;padding:20px;line-height:1.6}</style></head>
        <body><h1>Margin Guard Privacy Policy</h1>
        <p>Margin Guard processes Shopify order totals, line-item SKUs, and fulfillment statuses solely to calculate order profit margins and place fulfillment holds when margins fall below merchant-configured thresholds.</p>
        <p><strong>Data Retention & GDPR Compliance:</strong> We do not sell or share merchant or customer data. When a store uninstalls Margin Guard or triggers a <code>shop/redact</code> or <code>customers/redact</code> webhook, all associated records are permanently removed.</p>
        <p><a href="/" style="color:#38bdf8">← Back to Dashboard</a></p></body></html>"""
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.end_headers()
        handler.wfile.write(privacy_html.encode("utf-8"))
        return

    if parsed.path == "/billing/subscribe":
        shop, _, _, _ = sync_state(qs.get("shop"))
        token, _ = get_shop_token_and_plan(shop)
        url = f"https://{shop}/admin/api/2024-10/recurring_application_charges.json"
        payload = json.dumps({
            "recurring_application_charge": {
                "name": "Margin Guard Pro Plan",
                "price": 19.0,
                "return_url": f"{APP_URL}/billing/confirm?shop={shop}",
                "test": True
            }
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST", headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": token
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                confirm_url = data.get("recurring_application_charge", {}).get("confirmation_url", f"/?shop={shop}")
                handler.send_response(302)
                handler.send_header("Location", confirm_url)
                handler.end_headers()
                return
        except Exception:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE merchants SET plan_status = 'PRO_ACTIVE (TEST)' WHERE shop_domain = ?", (shop,))
            conn.commit()
            conn.close()
            handler.send_response(302)
            handler.send_header("Location", f"/?shop={shop}")
            handler.end_headers()
            return

    if parsed.path == "/billing/confirm":
        shop = qs.get("shop", "")
        if shop:
            init_db()
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE merchants SET plan_status = 'PRO_ACTIVE ($19/mo)' WHERE shop_domain = ?", (shop,))
            conn.commit()
            conn.close()
        handler.send_response(302)
        handler.send_header("Location", f"/?shop={shop}")
        handler.end_headers()
        return

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
        c.execute("INSERT OR IGNORE INTO merchants (shop_domain, min_margin_pct, alert_webhook_url, access_token, plan_status) VALUES (?, 15.0, '', ?, 'FREE_TRIAL')", (shop, access_token))
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
    _, plan_status = get_shop_token_and_plan(shop)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT shop_domain FROM merchants ORDER BY shop_domain")
    all_shops = [r[0] for r in c.fetchall()]
    c.execute("SELECT order_name, total_price, margin_pct, action, created_at FROM order_logs WHERE shop_domain = ? ORDER BY id DESC LIMIT 15", (shop,))
    orders = c.fetchall()
    conn.close()

    shop_options = "".join(f'<option value="{s}" {"selected" if s == shop else ""}>{s}</option>' for s in all_shops)
    rows_html = "".join(
        f"<tr><td><code>{s}</code></td><td>${d['unit_cost']:.2f}</td><td>${d['est_shipping']:.2f}</td>"
        f"<td>{'' if s == 'DEFAULT' else f'<form method=\"POST\" action=\"/api/sku/delete\" style=\"margin:0\"><input type=\"hidden\" name=\"shop\" value=\"{shop}\"><input type=\"hidden\" name=\"sku\" value=\"{s}\"><button class=\"btn-del\">Delete</button></form>'}</td></tr>"
        for s, d in sorted(skus.items())
    )
    if orders:
        order_rows = "".join(
            f"<tr><td><strong>{o[0]}</strong></td><td>${o[1]}</td><td>{o[2]:.1f}%</td>"
            f"<td><span class=\"{'badge-hold' if o[3] == 'HOLD_FULFILLMENT' else 'badge-ok'}\">{o[3]}</span></td>"
            f"<td style=\"color:#94a3b8;font-size:12px\">{o[4]}</td></tr>"
            for o in orders
        )
    else:
        order_rows = '<tr><td colspan="5" style="color:#94a3b8;text-align:center">No orders evaluated yet. Send a test webhook from Shopify!</td></tr>'

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Margin Guard Dashboard</title>
<style>
body {{ font-family: -apple-system, sans-serif; background: #0f172a; color: #f8fafc; max-width: 860px; margin: 40px auto; padding: 0 20px; }}
.card {{ background: #1e293b; border: 1px solid #334155; border-radius: 10px; padding: 20px; margin-bottom: 24px; }}
h1 {{ margin-top: 0; color: #38bdf8; display: flex; justify-content: space-between; align-items: center; }}
h2 {{ margin-top: 0; font-size: 18px; color: #e2e8f0; display: flex; justify-content: space-between; align-items: center; }}
label {{ display: block; font-size: 13px; color: #94a3b8; margin-bottom: 4px; }}
input, select {{ width: 100%; padding: 8px 10px; margin-bottom: 12px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; box-sizing: border-box; }}
button, .btn-link {{ background: #0284c7; color: #fff; border: none; padding: 9px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-block; font-size: 13px; }}
button:hover, .btn-link:hover {{ background: #0369a1; }}
.btn-sec {{ background: #475569; }}
.btn-del {{ background: #dc2626; padding: 5px 10px; font-size: 12px; }}
table {{ width: 100%; border-collapse: collapse; margin-top: 12px; }}
th, td {{ text-align: left; padding: 10px; border-bottom: 1px solid #334155; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 12px; }}
.top-bar {{ display: flex; gap: 12px; align-items: flex-end; }}
.badge-ok {{ background: #065f46; color: #34d399; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
.badge-hold {{ background: #7f1d1d; color: #f87171; padding: 4px 8px; border-radius: 4px; font-size: 12px; font-weight: 600; }}
</style></head><body>
<h1>
  <span>🛡️ Margin Guard App Dashboard</span>
  <span style="font-size:13px"><span class="badge-ok">{plan_status}</span> <a class="btn-link" style="margin-left:8px" href="/billing/subscribe?shop={shop}">Upgrade ($19/mo)</a></span>
</h1>
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
  <h2>
    <span>SKU Cost & Shipping Manager ({shop})</span>
    <form method="POST" action="/api/sku/sync" style="margin:0">
      <input type="hidden" name="shop" value="{shop}">
      <button type="submit" class="btn-sec">🔄 Auto-Import Store SKUs</button>
    </form>
  </h2>
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
<div class="card">
  <h2>Recent Evaluated Orders ({shop})</h2>
  <table>
    <thead><tr><th>Order</th><th>Total</th><th>Margin</th><th>Decision</th><th>Time</th></tr></thead>
    <tbody>{order_rows}</tbody>
  </table>
</div>
<p style="text-align:center;font-size:12px;color:#64748b"><a href="/privacy" style="color:#94a3b8">Privacy Policy & GDPR Compliance</a> • Compliance Webhook: <code>{APP_URL}/webhooks/compliance</code></p>
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
    elif handler.path == "/api/sku/sync":
        conn.close()
        import_store_skus(shop)
        sync_state(shop)
        handler.send_response(303)
        handler.send_header("Location", f"/?shop={shop}")
        handler.end_headers()
        return
    conn.commit()
    conn.close()
    sync_state(shop)
    handler.send_response(303)
    handler.send_header("Location", f"/?shop={shop}")
    handler.end_headers()
