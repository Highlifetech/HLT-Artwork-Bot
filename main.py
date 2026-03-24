import os
import json
import uuid
import base64
import requests
from flask import Flask, request, jsonify
from datetime import datetime
import threading
import time

app = Flask(__name__)

# In-memory approval store
approval_store = {}

# Table ID cache
_table_id_cache = []
_table_cache_time = 0
TABLE_CACHE_TTL = 300

# Lark Base deep link
LARK_BASE_URL = "https://ojpglhhzxlvc.jp.larksuite.com/base/VcAlbwImaab1KlsFLBVjunTNp1c"


# ══════════════════════════════════════════════════════
# CHANNEL ROUTING
# ══════════════════════════════════════════════════════

def get_notify_channel(assigned_to: str) -> str:
    assigned = (assigned_to or "").strip().lower()
    if "hannah" in assigned:
        return os.environ.get("HANNAH_CHANNEL_ID", os.environ["BRENDAN_CHANNEL_ID"])
    if "lucy" in assigned:
        return os.environ.get("LUCY_CHANNEL_ID", os.environ["BRENDAN_CHANNEL_ID"])
    return os.environ["BRENDAN_CHANNEL_ID"]


def record_link(table_id: str, record_id: str) -> str:
    return f"{LARK_BASE_URL}?table={table_id}&record={record_id}"


# ══════════════════════════════════════════════════════
# LARK HELPERS
# ══════════════════════════════════════════════════════

def get_lark_token():
    res = requests.post(
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
        json={
            "app_id": os.environ["LARK_APP_ID"],
            "app_secret": os.environ["LARK_APP_SECRET"],
        },
    )
    return res.json()["tenant_access_token"]


def get_all_table_ids():
    global _table_id_cache, _table_cache_time
    now = time.time()
    if _table_id_cache and (now - _table_cache_time) < TABLE_CACHE_TTL:
        return _table_id_cache
    token = get_lark_token()
    res = requests.get(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables",
        headers={"Authorization": f"Bearer {token}"},
        params={"page_size": 100},
    )
    data = res.json()
    if data.get("code") != 0:
        return _table_id_cache
    _table_id_cache = [t["table_id"] for t in data.get("data", {}).get("items", [])]
    _table_cache_time = now
    return _table_id_cache


def post_to_lark(channel_id: str, message: str):
    token = get_lark_token()
    requests.post(
        "https://open.larksuite.com/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": channel_id,
            "msg_type": "text",
            "content": json.dumps({"text": message}),
        },
    )


def update_record(table_id: str, record_id: str, fields: dict):
    token = get_lark_token()
    res = requests.put(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/"
        f"{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"fields": fields},
    )
    print(f"DEBUG update_record response: {res.status_code} {res.text}")


def post_comment(table_id: str, record_id: str, text: str):
    token = get_lark_token()
    requests.post(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/"
        f"{table_id}/records/{record_id}/comments",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "content": {
                "type": "doc",
                "body": {
                    "blocks": [{
                        "type": "paragraph",
                        "paragraph": {
                            "elements": [{
                                "type": "text_run",
                                "text_run": {"content": text},
                            }]
                        },
                    }]
                },
            }
        },
    )


# ══════════════════════════════════════════════════════
# FETCH ART FILES FROM LARK RECORD
# ══════════════════════════════════════════════════════

def get_art_files_from_record(table_id: str, record_id: str):
    """
    Fetches the full record from Lark and extracts Art Files attachment tokens.
    Downloads each file and returns base64-encoded attachments for Resend.
    Tries both 'Art Files' and 'Production Drawing' field names.
    """
    attachments = []
    token = get_lark_token()

    # Fetch the full record
    res = requests.get(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    print(f"DEBUG get_record response: {res.status_code}")

    if res.status_code != 200:
        print(f"DEBUG get_record failed: {res.text}")
        return attachments

    data = res.json()
    if data.get("code") != 0:
        print(f"DEBUG get_record Lark error: {data}")
        return attachments

    fields = data.get("data", {}).get("record", {}).get("fields", {})
    print(f"DEBUG record fields keys: {list(fields.keys())}")

    # Try these field names in order
    for field_name in ["Production Artwork", "Art Files", "Production Drawing", "Artwork", "Art File"]:
        art_files = fields.get(field_name)
        if art_files:
            print(f"DEBUG found attachments in field '{field_name}': {art_files}")
            break
    else:
        print("DEBUG no art file field found in record")
        return attachments

    if not isinstance(art_files, list):
        art_files = [art_files]

    for f in art_files:
        if not isinstance(f, dict):
            continue

        file_token = f.get("file_token") or f.get("token")
        file_name  = f.get("name", "artwork")

        if not file_token:
            print(f"DEBUG no file_token in: {f}")
            continue

        try:
            dl = requests.get(
                f"https://open.larksuite.com/open-apis/drive/v1/medias/{file_token}/download",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if dl.status_code == 200:
                encoded = base64.b64encode(dl.content).decode("utf-8")
                attachments.append({
                    "filename": file_name,
                    "content":  encoded,
                })
                print(f"DEBUG downloaded '{file_name}' ({len(dl.content)} bytes)")
            else:
                print(f"DEBUG download failed {dl.status_code}: {dl.text}")
        except Exception as e:
            print(f"DEBUG download exception: {e}")

    return attachments


# ══════════════════════════════════════════════════════
# EMAIL FOOTER
# ══════════════════════════════════════════════════════

EMAIL_FOOTER = """
      <hr style="border:none;border-top:1px solid #eee;margin:30px 0;">
      <p style="color:#999;font-size:12px;">
        If you have any questions, please contact us with your order number at
        <a href="mailto:orders@highlifetech.co" style="color:#999;">orders@highlifetech.co</a>.<br>
        For any new inquiries, contact your sales rep or email us at
        <a href="mailto:sales@highlifetech.co" style="color:#999;">sales@highlifetech.co</a>.
      </p>
"""

PAGE_FOOTER = """
      <p style="color:#666;font-size:14px;margin-top:30px;">
        If you have any questions, please contact us with your order number at
        <a href="mailto:orders@highlifetech.co">orders@highlifetech.co</a>.<br>
        For any new inquiries, contact your sales rep or email us at
        <a href="mailto:sales@highlifetech.co">sales@highlifetech.co</a>.
      </p>
"""


# ══════════════════════════════════════════════════════
# EMAIL VIA RESEND
# ══════════════════════════════════════════════════════

def send_artwork_email(to_email, order_number, approval_url,
                       attachments=None, is_followup=False):
    prefix   = "Follow-up: " if is_followup else ""
    reminder = (
        "<p><strong>Friendly reminder</strong> - we have not heard back yet.</p>"
        if is_followup else ""
    )

    attachment_note = (
        "<p>Please find your artwork file attached to this email.</p>"
        if attachments else
        "<p>Please use the buttons below to approve or request changes.</p>"
    )

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#000;">Your artwork is ready for review</h2>
      <p>Hello,</p>
      {reminder}
      <p>Your artwork for order <strong>{order_number}</strong>
         is ready for your approval.</p>
      {attachment_note}
      <p>Once reviewed please select:</p>
      <div style="text-align:center;margin:30px 0;">
        <a href="{approval_url}?decision=approved"
           style="background:#22c55e;color:#fff;padding:14px 32px;
                  text-decoration:none;border-radius:4px;
                  font-weight:bold;display:inline-block;margin-right:12px;">
          Approve
        </a>
        <a href="{approval_url}?decision=changes"
           style="background:#ef4444;color:#fff;padding:14px 32px;
                  text-decoration:none;border-radius:4px;
                  font-weight:bold;display:inline-block;">
          Request Changes
        </a>
      </div>
      <p style="color:#666;font-size:14px;">
        Please respond within 24 hours to keep your project on schedule.
      </p>
      {EMAIL_FOOTER}
    </body>
    </html>
    """

    payload = {
        "from":    f"High Life Tech <{os.environ['EMAIL_ADDRESS']}>",
        "to":      [to_email],
        "subject": f"{prefix}Artwork Approval - {order_number}",
        "html":    html,
    }

    if attachments:
        payload["attachments"] = [
            {"filename": a["filename"], "content": a["content"]}
            for a in attachments
        ]

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}",
            "Content-Type":  "application/json",
        },
        json=payload,
    )

    if response.status_code not in (200, 201):
        raise Exception(f"Resend error {response.status_code}: {response.text}")

    return response.json()


# ══════════════════════════════════════════════════════
# ARTWORK TRIGGER
# ══════════════════════════════════════════════════════

@app.route("/artwork-trigger", methods=["POST"])
def artwork_trigger():
    data         = request.json or {}
    record_id    = data.get("record_id", "")
    table_id     = data.get("table_id", "")
    order_number = data.get("order_number", "")
    client       = data.get("client", "")
    client_email = "".join(data.get("client_email", "").split())
    in_hand_date = data.get("in_hand_date", "")
    assigned_to  = data.get("assigned_to", "")
    product_type = data.get("product_type", "")

    notify_channel = get_notify_channel(assigned_to)
    print(f"DEBUG email: {repr(client_email)}")
    print(f"DEBUG record_id: {repr(record_id)} table_id: {repr(table_id)}")

    if not client_email:
        post_to_lark(
            notify_channel,
            f"Cannot send artwork for {order_number} - {client}. "
            f"No client email on the card. Please add it and try again.",
        )
        return jsonify({"code": 0})

    if not table_id:
        table_ids = get_all_table_ids()
        table_id  = table_ids[0] if table_ids else ""

    # Fetch art files directly from the Lark record
    attachments = get_art_files_from_record(table_id, record_id)
    print(f"DEBUG attachments count: {len(attachments)}")

    token = str(uuid.uuid4())
    approval_store[token] = {
        "record_id":      record_id,
        "table_id":       table_id,
        "order_number":   order_number,
        "client":         client,
        "client_email":   client_email,
        "in_hand_date":   in_hand_date,
        "assigned_to":    assigned_to,
        "product_type":   product_type,
        "notify_channel": notify_channel,
        "sent_at":        datetime.now().isoformat(),
        "followup_sent":  False,
    }

    base_url     = os.environ.get("BOT_URL", "https://your-bot.railway.app")
    approval_url = f"{base_url}/approve/{token}"
    link         = record_link(table_id, record_id)

    send_artwork_email(client_email, order_number, approval_url, attachments)

    post_comment(
        table_id, record_id,
        f"Artwork sent to client - {datetime.now().strftime('%b %d %Y %I:%M %p')}"
    )

    post_to_lark(
        notify_channel,
        f"Artwork sent to {order_number} | {client} | {product_type}\n"
        f"In-Hand Date: {in_hand_date}\n"
        f"Awaiting client approval...\n"
        f"{link}",
    )

    return jsonify({"code": 0})


# ══════════════════════════════════════════════════════
# APPROVAL PAGE
# ══════════════════════════════════════════════════════

@app.route("/approve/<token>", methods=["GET", "POST"])
def approve(token):
    if token not in approval_store:
        return "<h2>This link has expired or is no longer valid.</h2>", 404

    project        = approval_store[token]
    decision       = request.args.get("decision", "")
    notify_channel = project["notify_channel"]

    if decision == "changes" and request.method == "GET":
        return f"""
        <html>
        <body style="font-family:Arial,sans-serif;max-width:500px;
                     margin:60px auto;padding:20px;">
          <h2>Request Changes</h2>
          <p>Please describe the changes needed for
             <strong>{project['order_number']}</strong>:</p>
          <form method="POST">
            <input type="hidden" name="decision" value="changes">
            <textarea name="notes" rows="6"
              style="width:100%;padding:12px;border:1px solid #ddd;
                     border-radius:4px;font-size:16px;"
              placeholder="Describe the changes you need..."></textarea>
            <br><br>
            <button type="submit"
              style="background:#ef4444;color:#fff;padding:12px 24px;
                     border:none;border-radius:4px;font-size:16px;
                     cursor:pointer;">
              Submit Changes
            </button>
          </form>
        </body>
        </html>
        """, 200

    if decision == "approved" or request.method == "POST":
        notes          = request.form.get("notes", "")
        final_decision = request.form.get("decision", decision)
        now_str        = datetime.now().strftime("%b %d %Y %I:%M %p")
        tid            = project["table_id"]
        rid            = project["record_id"]
        link           = record_link(tid, rid)

        if final_decision == "approved":
            update_record(tid, rid, {"Status": "ARTWORK CONFIRMED"})
            post_comment(tid, rid,
                f"{project['client']} approved artwork - {now_str}. "
                f"Production can begin."
            )
            post_to_lark(
                notify_channel,
                f"Approved - {project['order_number']}\n"
                f"Status -> ARTWORK CONFIRMED\n"
                f"This order will now begin production.\n"
                f"{link}",
            )
            del approval_store[token]
            return f"""
            <html>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:80px 20px;">
              <h1 style="color:#22c55e;">Approved!</h1>
              <p>Thank you &mdash; we will begin production now.</p>
              {PAGE_FOOTER}
            </body>
            </html>
            """, 200

        else:
            update_record(tid, rid, {"Status": "WAITING ART"})
            post_comment(tid, rid,
                f"{project['client']} requested changes - {now_str}. "
                f"Revision notes: {notes}"
            )
            post_to_lark(
                notify_channel,
                f"{project['client']} requested changes on "
                f"{project['order_number']}\n"
                f"Revision notes: {notes}\n"
                f"Status -> WAITING ART\n"
                f"{link}",
            )
            del approval_store[token]
            return f"""
            <html>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:80px 20px;">
              <h1>Got it!</h1>
              <p>We have received your feedback and will send
                 a revised proof shortly.</p>
              {PAGE_FOOTER}
            </body>
            </html>
            """, 200

    return "<h2>Invalid request.</h2>", 400


# ══════════════════════════════════════════════════════
# 48HR FOLLOW-UP
# ══════════════════════════════════════════════════════

def check_pending_approvals():
    while True:
        time.sleep(3600)
        now = datetime.now()
        for token, project in list(approval_store.items()):
            sent_at       = datetime.fromisoformat(project["sent_at"])
            hours_waiting = (now - sent_at).total_seconds() / 3600
            if hours_waiting >= 48 and not project["followup_sent"]:
                base_url     = os.environ.get("BOT_URL", "https://your-bot.railway.app")
                approval_url = f"{base_url}/approve/{token}"
                try:
                    attachments = get_art_files_from_record(
                        project["table_id"], project["record_id"]
                    )
                    send_artwork_email(
                        project["client_email"],
                        project["order_number"],
                        approval_url,
                        attachments,
                        is_followup=True,
                    )
                    post_comment(
                        project["table_id"], project["record_id"],
                        "Follow-up email sent - no client response after 48hrs"
                    )
                    post_to_lark(
                        project["notify_channel"],
                        f"Follow-up sent to {project['client']} - "
                        f"{project['order_number']} still awaiting approval\n"
                        f"{record_link(project['table_id'], project['record_id'])}",
                    )
                    approval_store[token]["followup_sent"] = True
                    approval_store[token]["sent_at"] = now.isoformat()
                except Exception as e:
                    print(f"Follow-up error: {e}")


threading.Thread(target=check_pending_approvals, daemon=True).start()


# ══════════════════════════════════════════════════════
# LARK WEBHOOK
# ══════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    if "challenge" in data:
        return jsonify({"challenge": data["challenge"]})
    return jsonify({"code": 0})


# ══════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
