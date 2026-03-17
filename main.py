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
    requests.put(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/"
        f"{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"fields": fields},
    )


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
# LARK FILE DOWNLOAD
# ══════════════════════════════════════════════════════

def get_lark_file_attachments(art_files_raw):
    """
    art_files_raw is whatever Lark sends for an attachment field.
    It may be a list of dicts with file_token, name, type etc.
    Returns a list of dicts: {filename, content (base64 str), type}
    """
    attachments = []

    # Lark attachment fields come as a list of file objects
    if not art_files_raw:
        return attachments

    # If it's a string, try to parse as JSON
    if isinstance(art_files_raw, str):
        try:
            art_files_raw = json.loads(art_files_raw)
        except Exception:
            print(f"DEBUG art_files could not be parsed: {repr(art_files_raw)}")
            return attachments

    if not isinstance(art_files_raw, list):
        art_files_raw = [art_files_raw]

    token = get_lark_token()

    for f in art_files_raw:
        if not isinstance(f, dict):
            continue

        file_token = f.get("file_token") or f.get("token")
        file_name  = f.get("name", "artwork")
        file_type  = f.get("type", "application/octet-stream")

        if not file_token:
            print(f"DEBUG no file_token in: {f}")
            continue

        # Download the file from Lark
        try:
            res = requests.get(
                f"https://open.larksuite.com/open-apis/drive/v1/medias/{file_token}/download",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if res.status_code == 200:
                encoded = base64.b64encode(res.content).decode("utf-8")
                attachments.append({
                    "filename": file_name,
                    "content":  encoded,
                    "type":     file_type,
                })
                print(f"DEBUG downloaded {file_name} ({len(res.content)} bytes)")
            else:
                print(f"DEBUG file download failed {res.status_code}: {res.text}")
        except Exception as e:
            print(f"DEBUG file download exception: {e}")

    return attachments


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

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#000;">Your artwork is ready for review</h2>
      <p>Hello,</p>
      {reminder}
      <p>Your artwork for order <strong>{order_number}</strong>
         is ready for your approval. Please find the artwork file attached.</p>
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
      <hr style="border:none;border-top:1px solid #eee;margin:30px 0;">
      <p style="color:#999;font-size:12px;">
        High Life Tech - orders@highlifetech.co
      </p>
    </body>
    </html>
    """

    payload = {
        "from":    f"High Life Tech <{os.environ['EMAIL_ADDRESS']}>",
        "to":      [to_email],
        "subject": f"{prefix}Artwork Approval - {order_number}",
        "html":    html,
    }

    # Attach files if present
    if attachments:
        payload["attachments"] = [
            {
                "filename": a["filename"],
                "content":  a["content"],  # base64 string
            }
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
    art_files_raw = data.get("art_files")
    in_hand_date = data.get("in_hand_date", "")
    assigned_to  = data.get("assigned_to", "")

    notify_channel = get_notify_channel(assigned_to)
    print(f"DEBUG email received: repr={repr(client_email)} len={len(client_email)}")
    print(f"DEBUG art_files raw: {repr(art_files_raw)}")

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

    # Download art files from Lark
    attachments = get_lark_file_attachments(art_files_raw)
    if not attachments:
        print("DEBUG no attachments retrieved — sending email without attachment")

    token        = str(uuid.uuid4())
    approval_store[token] = {
        "record_id":      record_id,
        "table_id":       table_id,
        "order_number":   order_number,
        "client":         client,
        "client_email":   client_email,
        "art_files_raw":  art_files_raw,
        "in_hand_date":   in_hand_date,
        "assigned_to":    assigned_to,
        "notify_channel": notify_channel,
        "sent_at":        datetime.now().isoformat(),
        "followup_sent":  False,
    }

    base_url     = os.environ.get("BOT_URL", "https://your-bot.railway.app")
    approval_url = f"{base_url}/approve/{token}"

    send_artwork_email(client_email, order_number, approval_url, attachments)

    post_comment(
        table_id, record_id,
        f"Artwork sent to client - {datetime.now().strftime('%b %d %Y %I:%M %p')}"
    )

    post_to_lark(
        notify_channel,
        f"Artwork sent to {client} for {order_number}\n"
        f"In-Hand Date: {in_hand_date}\n"
        f"Awaiting client approval...",
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

        if final_decision == "approved":
            update_record(tid, rid, {"Status": "ARTWORK CONFIRMED"})
            post_comment(tid, rid,
                f"{project['client']} approved artwork - {now_str}. "
                f"Production can begin."
            )
            post_to_lark(
                notify_channel,
                f"{project['client']} approved {project['order_number']}\n"
                f"Status -> ARTWORK CONFIRMED\n"
                f"Ready to move to Part Confirmed.",
            )
            del approval_store[token]
            return """
            <html>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:80px 20px;">
              <h1 style="color:#22c55e;">Approved!</h1>
              <p>Thank you - we will begin production now.</p>
              <p style="color:#666;">You will receive a shipping notification
                 when your order is on its way.</p>
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
                f"Status -> WAITING ART",
            )
            del approval_store[token]
            return """
            <html>
            <body style="font-family:Arial,sans-serif;text-align:center;padding:80px 20px;">
              <h1>Got it!</h1>
              <p>We have received your feedback and will send
                 a revised proof shortly.</p>
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
                    # Re-download files for follow-up
                    attachments = get_lark_file_attachments(project.get("art_files_raw"))
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
                        f"{project['order_number']} still awaiting approval",
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
