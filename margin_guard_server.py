import dashboard
import os, json
os.makedirs("config", exist_ok=True)
os.makedirs("logs", exist_ok=True)
if not os.path.exists("sku_cogs.json"):
    with open("sku_cogs.json", "w") as _f:
        json.dump({}, _f)
if not os.path.exists("settings.json"):
    with open("settings.json", "w") as _f:
        json.dump({
            "shopify_store_url": os.environ.get("SHOPIFY_STORE_URL", ""),
            "shopify_access_token": os.environ.get("SHOPIFY_ACCESS_TOKEN", ""),
            "shopify_shared_secret": os.environ.get("SHOPIFY_SHARED_SECRET", ""),
            "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", "")
        }, _f)

import warnings
warnings.filterwarnings("ignore")

import json, csv, os, urllib3
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone

http = urllib3.PoolManager()
SETTINGS_FILE = "settings.json"
COGS_FILE = "sku_cogs.json"
LEDGER_FILE = "logs/daily_profit_ledger.csv"

with open(COGS_FILE, "r") as f:
    SKU_COSTS = json.load(f)

with open(SETTINGS_FILE, "r") as f:
    SETTINGS = json.load(f)


def evaluate_order_margin(order: dict) -> dict:
    rev = float(order.get("total_price", 0.0))
    disc = float(order.get("total_discounts", 0.0))
    codes = [d.get("code") for d in order.get("discount_codes", []) if d.get("code")]

    cogs = sum(
        SKU_COSTS.get(i.get("sku") or "DEFAULT", SKU_COSTS["DEFAULT"])["unit_cost"] * int(i.get("quantity", 1))
        for i in order.get("line_items", [])
    )
    ship = sum(
        SKU_COSTS.get(i.get("sku") or "DEFAULT", SKU_COSTS["DEFAULT"])["est_shipping"] * int(i.get("quantity", 1))
        for i in order.get("line_items", [])
    )
    fee = round((rev * 0.029) + 0.30, 2) if rev > 0 else 0.0
    profit = round(rev - cogs - ship - fee, 2)
    margin = round((profit / rev) * 100, 2) if rev > 0 else 0.0

    min_margin = float(SETTINGS.get("min_margin_pct", 15.0))
    reasons = []
    if profit <= 0:
        reasons.append(f"NEGATIVE_PROFIT (${profit})")
    elif margin < min_margin:
        reasons.append(f"LOW_MARGIN ({margin}%)")

    if len(codes) > 1:
        reasons.append(f"STACKED_CODES ({', '.join(codes)})")
    elif disc > 0 and (disc / (rev + disc)) >= 0.35:
        pct_off = round((disc / (rev + disc)) * 100, 1)
        reasons.append(f"HEAVY_DISCOUNT_LEAK ({codes[0] if codes else 'PROMO'}: {pct_off}% OFF)")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "order_id": str(order.get("id", "UNKNOWN")),
        "order_number": order.get("name", "#0000"),
        "gross_revenue": rev,
        "total_discounts": disc,
        "total_cogs": round(cogs, 2),
        "est_shipping_cost": round(ship, 2),
        "processing_fee": fee,
        "net_profit": profit,
        "margin_pct": margin,
        "action": "HOLD_FULFILLMENT" if reasons else "APPROVE",
        "hold_reasons": " | ".join(reasons) if reasons else "NONE"
    }


def hold_shopify_fulfillment(order_id: str, reason_notes: str) -> str:
    domain = SETTINGS.get("shopify_domain", "").replace("https://", "").strip("/")
    token = SETTINGS.get("shopify_access_token", "")
    url = f"https://{domain}/admin/api/2026-01/graphql.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
        "User-Agent": "MarginGuardBot/1.0"
    }

    gid = order_id if str(order_id).startswith("gid://") else f"gid://shopify/Order/{order_id}"
    query_payload = {
        "query": """
        query($orderId: ID!) {
          order(id: $orderId) {
            fulfillmentOrders(first: 5) {
              edges { node { id status } }
            }
          }
        }
        """,
        "variables": {"orderId": gid}
    }

    resp = http.request("POST", url, body=json.dumps(query_payload).encode("utf-8"), headers=headers)
    data = json.loads(resp.data.decode("utf-8"))
    edges = data.get("data", {}).get("order", {}).get("fulfillmentOrders", {}).get("edges", [])

    if not edges:
        return "NO_FULFILLMENT_ORDER_FOUND"

    mutation = """
    mutation fulfillmentOrderHold($fulfillmentHold: FulfillmentOrderHoldInput!, $id: ID!) {
      fulfillmentOrderHold(fulfillmentHold: $fulfillmentHold, id: $id) {
        fulfillmentOrder { id status }
        userErrors { field message }
      }
    }
    """
    held_ids = []
    for edge in edges:
        fo_id = edge["node"]["id"]
        if edge["node"].get("status") == "ON_HOLD":
            held_ids.append(f"{fo_id.split('/')[-1]}(ON_HOLD)")
            continue
        mut_payload = {
            "query": mutation,
            "variables": {
                "id": fo_id,
                "fulfillmentHold": {
                    "reason": "OTHER",
                    "reasonNotes": f"Margin Guard Hold: {reason_notes}"
                }
            }
        }
        m_resp = http.request("POST", url, body=json.dumps(mut_payload).encode("utf-8"), headers=headers)
        m_data = json.loads(m_resp.data.decode("utf-8"))
        hold_res = m_data.get("data", {}).get("fulfillmentOrderHold", {})
        if not hold_res.get("userErrors") and hold_res.get("fulfillmentOrder"):
            held_ids.append(f"{fo_id.split('/')[-1]}(ON_HOLD)")

    return f"LIVE_HOLD_PLACED [{', '.join(held_ids)}]" if held_ids else "HOLD_API_ERROR"


def send_instant_alert(res: dict, hold_status: str):
    webhook_url = SETTINGS.get("alert_webhook_url", "")
    if not webhook_url: return
    msg = (
        f"🚨 **LIVE WEBHOOK MARGIN GUARD HOLD** 🚨\n"
        f"**Order:** `{res['order_number']}` (ID: `{res['order_id']}`)\n"
        f"**Gross Revenue:** ${res['gross_revenue']:.2f} | **Net Profit:** ${res['net_profit']:.2f} ({res['margin_pct']}%)\n"
        f"**Reasons:** `{res['hold_reasons']}`\n"
        f"**Shopify Status:** `{hold_status}`\n"
        f"🔗 https://admin.shopify.com/store/ticktron/orders/{res['order_id']}"
    )
    http.request("POST", webhook_url, body=json.dumps({"content": msg}).encode("utf-8"), headers={"Content-Type": "application/json"})


def log_to_ledger(row: dict):
    exists = os.path.isfile(LEDGER_FILE) and os.path.getsize(LEDGER_FILE) > 0
    with open(LEDGER_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists: writer.writeheader()
        writer.writerow(row)


class WebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        return dashboard.handle_get(self)

    def do_POST(self):
        if self.path.startswith("/api/"):
            return dashboard.handle_post(self)
        if self.path.startswith("/webhooks/compliance"):
            return dashboard.handle_compliance_webhook(self)
        dashboard.sync_state(self.headers.get("X-Shopify-Shop-Domain"))
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        
        try:
            order_data = json.loads(body.decode('utf-8'))
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            return

        print(f"\n[⚡] INCOMING SHOPIFY WEBHOOK: Order {order_data.get('name', '#0000')} received!")
        res = evaluate_order_margin(order_data)
        
        if res["action"] == "HOLD_FULFILLMENT":
            print(f"    [!] Margin anomaly detected! Placing fulfillment hold...")
            res["shopify_hold_status"] = hold_shopify_fulfillment(res["order_id"], res["hold_reasons"])
            send_instant_alert(res, res["shopify_hold_status"])
        else:
            print(f"    [✓] Order profitable and clean. Approved.")
            res["shopify_hold_status"] = "NOT_REQUIRED"
            res["alert_status"] = "NOT_REQUIRED"

        log_to_ledger(res)
        dashboard.record_order(self.headers.get("X-Shopify-Shop-Domain"), order_data, res)
        
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return  # Suppress default noisy HTTP access logs


if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', PORT), WebhookHandler)
    print(f"\n[🟢] Margin Guard Webhook Listener active on port {PORT}...")
    print("     Ready to receive live checkout events from Shopify.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] Shutting down server...")
        server.server_close()
