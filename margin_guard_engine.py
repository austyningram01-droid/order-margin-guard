import warnings
warnings.filterwarnings("ignore")

import json, csv, os, time, urllib3
from urllib.parse import urlencode
from datetime import datetime, timezone

http = urllib3.PoolManager()

os.makedirs("config", exist_ok=True)
os.makedirs("logs", exist_ok=True)

COGS_FILE = "sku_cogs.json"
SETTINGS_FILE = "settings.json"
LEDGER_FILE = "logs/daily_profit_ledger.csv"

with open(COGS_FILE, "r") as f:
    SKU_COSTS = json.load(f)

with open(SETTINGS_FILE, "r") as f:
    SETTINGS = json.load(f)


def refresh_shopify_token() -> str:
    """Automatically mint a fresh token using saved Client ID & Secret if needed."""
    domain = SETTINGS.get("shopify_domain", "ticktron.myshopify.com")
    client_id = SETTINGS.get("shopify_client_id", "")
    client_secret = SETTINGS.get("shopify_client_secret", "")
    if not client_id or not client_secret:
        return SETTINGS.get("shopify_access_token", "")

    url = f"https://{domain}/admin/oauth/access_token"
    body = urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret
    })
    resp = http.request(
        "POST",
        url,
        body=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "MarginGuardBot/1.0"
        }
    )
    if resp.status == 200:
        token = json.loads(resp.data.decode("utf-8")).get("access_token", "")
        if token:
            SETTINGS["shopify_access_token"] = token
            with open(SETTINGS_FILE, "w") as f:
                json.dump(SETTINGS, f, indent=2)
            return token
    return SETTINGS.get("shopify_access_token", "")


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
        code_lbl = codes[0] if codes else "PROMO"
        reasons.append(f"HEAVY_DISCOUNT_LEAK ({code_lbl}: {pct_off}% OFF)")

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
        status = edge["node"].get("status", "")
        if status == "ON_HOLD":
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
        errors = hold_res.get("userErrors", [])
        if not errors and hold_res.get("fulfillmentOrder"):
            new_status = hold_res["fulfillmentOrder"]["status"]
            held_ids.append(f"{fo_id.split('/')[-1]}({new_status})")
        else:
            return f"HOLD_ERROR: {errors or m_data.get('errors')}"

    return f"LIVE_HOLD_PLACED [{', '.join(held_ids)}]" if held_ids else "HOLD_API_ERROR"


def send_instant_alert(res: dict, hold_status: str) -> str:
    webhook_url = SETTINGS.get("alert_webhook_url", "")
    msg = (
        f"🚨 **LIVE SHOPIFY MARGIN GUARD HOLD** 🚨\n"
        f"**Store:** `Tik-Tron` | **Order:** `{res['order_number']}` (ID: `{res['order_id']}`)\n"
        f"**Gross Revenue:** ${res['gross_revenue']:.2f} | **Net Profit:** ${res['net_profit']:.2f} ({res['margin_pct']}%)\n"
        f"**Reasons:** `{res['hold_reasons']}`\n"
        f"**Shopify Fulfillment Status:** `{hold_status}`\n"
        f"🔗 **Review in Admin:** https://admin.shopify.com/store/ticktron/orders/{res['order_id']}"
    )

    payload = {"content": msg}
    resp = http.request(
        "POST",
        webhook_url,
        body=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "MarginGuardBot/1.0"
        }
    )
    return f"WEBHOOK_SENT (HTTP {resp.status})"


def log_to_ledger(row: dict):
    exists = os.path.isfile(LEDGER_FILE) and os.path.getsize(LEDGER_FILE) > 0
    with open(LEDGER_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def create_and_guard_live_test_order():
    domain = SETTINGS.get("shopify_domain", "").replace("https://", "").strip("/")
    token = refresh_shopify_token()
    url = f"https://{domain}/admin/api/2026-01/orders.json"
    headers = {
        "Content-Type": "application/json",
        "X-Shopify-Access-Token": token,
        "User-Agent": "MarginGuardBot/1.0"
    }

    print("\n[*] Creating a live leaked-discount test order on Tik-Tron...")
    order_body = {
        "order": {
            "line_items": [
                {
                    "title": "Signature Heavyweight Hoodie (Black)",
                    "price": "40.00",
                    "sku": "SKU-HOODIE-BLK",
                    "quantity": 2,
                    "requires_shipping": True
                }
            ],
            "discount_codes": [
                {
                    "code": "VIP40_STACKED_LEAK",
                    "amount": "38.00",
                    "type": "fixed_amount"
                }
            ],
            "financial_status": "paid",
            "note": "Automated test order created by Margin Guard Engine"
        }
    }

    resp = http.request("POST", url, body=json.dumps(order_body).encode("utf-8"), headers=headers)
    if resp.status not in (200, 201):
        print(f"[X] Could not create test order (HTTP {resp.status}): {resp.data.decode('utf-8')}")
        return

    live_order = json.loads(resp.data.decode("utf-8")).get("order", {})
    print(f"[✓] Live Order Created on Tik-Tron: {live_order.get('name')} (ID: {live_order.get('id')})")

    time.sleep(1.5)  # Allow Shopify 1.5s to initialize the FulfillmentOrder

    res = evaluate_order_margin(live_order)
    if res["action"] == "HOLD_FULFILLMENT":
        print("[*] Negative profit detected! Locking fulfillment in Shopify Admin...")
        res["shopify_hold_status"] = hold_shopify_fulfillment(res["order_id"], res["hold_reasons"])
        res["alert_status"] = send_instant_alert(res, res["shopify_hold_status"])
    else:
        res["shopify_hold_status"] = "NOT_REQUIRED"
        res["alert_status"] = "NOT_REQUIRED"

    log_to_ledger(res)
    print("\n=== END-TO-END LIVE EXECUTION RESULT ===")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    create_and_guard_live_test_order()
