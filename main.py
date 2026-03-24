import os
import json
import uuid
import base64
import io
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


def upload_image_to_lark(image_bytes: bytes, filename: str = "artwork.png") -> str:
    """Upload an image to Lark and return the image_key for use in cards."""
    token = get_lark_token()
    try:
        res = requests.post(
            "https://open.larksuite.com/open-apis/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            files={"image": (filename, io.BytesIO(image_bytes), "image/png")},
            data={"image_type": "message"},
        )
        data = res.json()
        print(f"DEBUG upload_image response: {res.status_code} {data}")
        if data.get("code") == 0:
            return data.get("data", {}).get("image_key", "")
    except Exception as e:
        print(f"DEBUG upload_image error: {e}")
    return ""


def post_card_to_lark(channel_id: str, title: str, color: str, fields: list,
                      link_url: str = "", link_text: str = "Open Record",
                      image_key: str = ""):
    """Send a rich interactive message card to a Lark chat.
    color: blue, green, red, orange, grey
    fields: list of dicts with 'label' and 'value' keys
    image_key: optional Lark image_key to display artwork preview
    """
    elements = []

    # Show artwork image at top if provided
    if image_key:
        elements.append({
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": "Artwork Preview"},
        })

    # Build field rows (2 columns)
    for i in range(0, len(fields), 2):
        cols = []
        for f in fields[i:i+2]:
            cols.append({
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "vertical_align": "top",
                "elements": [{"tag": "markdown", "content": f"**{f['label']}**\n{f['value']}"}]
            })
        elements.append({"tag": "column_set", "flex_mode": "bisect", "columns": cols})

    # Add link button
    if link_url:
        elements.append({"tag": "action", "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": link_text},
            "type": "primary",
            "url": link_url,
        }]})

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }

    token = get_lark_token()
    res = requests.post(
        "https://open.larksuite.com/open-apis/im/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        params={"receive_id_type": "chat_id"},
        json={
            "receive_id": channel_id,
            "msg_type": "interactive",
            "content": json.dumps(card),
        },
    )
    print(f"DEBUG post_card response: {res.status_code}")


def update_record(table_id: str, record_id: str, fields: dict):
    token = get_lark_token()
    res = requests.put(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/"
        f"{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"fields": fields},
    )
    print(f"DEBUG update_record response: {res.status_code} {res.text[:200]}")


def get_record_field(table_id: str, record_id: str, field_name: str) -> str:
    """Get a single field value from a Lark Base record."""
    token = get_lark_token()
    res = requests.get(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if res.status_code == 200:
        data = res.json()
        if data.get("code") == 0:
            fields = data.get("data", {}).get("record", {}).get("fields", {})
            return str(fields.get(field_name, ""))
    return ""


# ══════════════════════════════════════════════════════
# FETCH ART FILES FROM LARK RECORD
# ══════════════════════════════════════════════════════

def get_art_files_from_record(table_id: str, record_id: str):
    """Fetches artwork attachments from a Lark Base record.
    Returns list of dicts with filename, content (base64), and raw_bytes.
    """
    attachments = []
    token = get_lark_token()
    res = requests.get(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/{table_id}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    print(f"DEBUG get_record response: {res.status_code}")
    if res.status_code != 200:
        return attachments

    data = res.json()
    if data.get("code") != 0:
        return attachments

    fields = data.get("data", {}).get("record", {}).get("fields", {})
    print(f"DEBUG record fields keys: {list(fields.keys())}")

    for field_name in ["Production Artwork", "Art Files", "Production Drawing",
                       "Artwork", "Art File"]:
        art_files = fields.get(field_name)
        if art_files:
            print(f"DEBUG found attachments in field '{field_name}'")
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
        file_name = f.get("name", "artwork")
        if not file_token:
            continue
        try:
            dl = None
            auth_hdr = {"Authorization": f"Bearer {token}"}
            att_url = f.get("url", "")
            if att_url:
                dl = requests.get(att_url, headers=auth_hdr, timeout=30)
            if (dl is None or dl.status_code != 200) and f.get("tmp_url"):
                dl = requests.get(f["tmp_url"], timeout=30)
            if (dl is None or dl.status_code != 200) and f.get("tmp_url"):
                dl = requests.get(f["tmp_url"], headers=auth_hdr, timeout=30)
            if dl is None or dl.status_code != 200:
                dl = requests.get(
                    f"https://open.larksuite.com/open-apis/drive/v1/medias/{file_token}/download",
                    headers=auth_hdr, timeout=30,
                )
            if dl and dl.status_code == 200 and len(dl.content) > 0:
                encoded = base64.b64encode(dl.content).decode("utf-8")
                attachments.append({
                    "filename": file_name,
                    "content": encoded,
                    "raw_bytes": dl.content,
                })
                print(f"DEBUG downloaded '{file_name}' ({len(dl.content)} bytes)")
            else:
                status = dl.status_code if dl else 'no response'
                print(f"DEBUG download failed for '{file_name}': {status}")
        except Exception as e:
            print(f"DEBUG download exception for '{file_name}': {e}")
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
    prefix = "Follow-up: " if is_followup else ""
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
        "from": f"High Life Tech <{os.environ['EMAIL_ADDRESS']}>",
        "to": [to_email],
        "subject": f"{prefix}Artwork Approval - {order_number}",
        "html": html,
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
            "Content-Type": "application/json",
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
    data = request.json or {}
    record_id = data.get("record_id", "")
    table_id = data.get("table_id", "")
    order_number = data.get("order_number", "")
    client = data.get("client", "")
    client_email = "".join(data.get("client_email", "").split())
    in_hand_date = data.get("in_hand_date", "")
    assigned_to = data.get("assigned_to", "")
    product_type = data.get("product_type", "")

    notify_channel = get_notify_channel(assigned_to)
    print(f"DEBUG trigger: email={repr(client_email)} record={repr(record_id)} table={repr(table_id)}")

    if not client_email:
        post_card_to_lark(
            notify_channel,
            title=f"Missing Email - {order_number}",
            color="red",
            fields=[
                {"label": "Client", "value": client or "-"},
                {"label": "Issue", "value": "No client email on the card"},
                {"label": "Action Needed", "value": "Add email and click Send Artwork again"},
            ],
        )
        return jsonify({"code": 0})

    if not table_id:
        table_ids = get_all_table_ids()
        table_id = table_ids[0] if table_ids else ""

    attachments = get_art_files_from_record(table_id, record_id)
    print(f"DEBUG attachments count: {len(attachments)}")

    # Upload first artwork image to Lark for card preview
    image_key = ""
    if attachments:
        image_key = upload_image_to_lark(
            attachments[0]["raw_bytes"], attachments[0]["filename"]
        )

    token = str(uuid.uuid4())
    approval_store[token] = {
        "record_id": record_id,
        "table_id": table_id,
        "order_number": order_number,
        "client": client,
        "client_email": client_email,
        "in_hand_date": in_hand_date,
        "assigned_to": assigned_to,
        "product_type": product_type,
        "notify_channel": notify_channel,
        "sent_at": datetime.now().isoformat(),
        "followup_sent": False,
        "image_key": image_key,
    }

    base_url = os.environ.get("BOT_URL", "https://your-bot.railway.app")
    approval_url = f"{base_url}/approve/{token}"
    link = record_link(table_id, record_id)

    send_artwork_email(client_email, order_number, approval_url, attachments)

    update_record(table_id, record_id, {
        "Status": "WAITING ART",
        "Last Updated": datetime.now().strftime("%m-%d-%Y"),
    })

    post_card_to_lark(
        notify_channel,
        title=f"Artwork Sent - {order_number}",
        color="blue",
        fields=[
            {"label": "Client", "value": client or "-"},
            {"label": "Product Type", "value": product_type or "-"},
            {"label": "In-Hand Date", "value": in_hand_date or "-"},
            {"label": "Status", "value": "Awaiting client approval..."},
        ],
        link_url=link,
        image_key=image_key,
    )

    return jsonify({"code": 0})


# ══════════════════════════════════════════════════════
# APPROVAL PAGE
# ══════════════════════════════════════════════════════

@app.route("/approve/<token>", methods=["GET", "POST"])
def approve(token):
    if token not in approval_store:
        return "<h2>This link has expired or is no longer valid.</h2>", 404

    project = approval_store[token]
    decision = request.args.get("decision", "")
    notify_channel = project["notify_channel"]
    image_key = project.get("image_key", "")

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
        notes = request.form.get("notes", "")
        final_decision = request.form.get("decision", decision)
        now_str = datetime.now().strftime("%b %d %Y %I:%M %p")
        tid = project["table_id"]
        rid = project["record_id"]
        link = record_link(tid, rid)

        if final_decision == "approved":
            update_record(tid, rid, {
                "Status": "ARTWORK CONFIRMED",
                "Last Updated": datetime.now().strftime("%m-%d-%Y"),
            })
            post_card_to_lark(
                notify_channel,
                title=f"Approved - {project['order_number']}",
                color="green",
                fields=[
                    {"label": "Client", "value": project.get("client", "-")},
                    {"label": "Status", "value": "ARTWORK CONFIRMED"},
                    {"label": "Product Type", "value": project.get("product_type", "-")},
                    {"label": "Next Step", "value": "Production can begin"},
                ],
                link_url=link,
                image_key=image_key,
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
            # Store revision notes in Description field on the record
            existing_desc = get_record_field(tid, rid, "Description")
            note_entry = f"[{now_str}] CUSTOMER REVISION NOTES: {notes}"
            new_desc = f"{existing_desc}\n{note_entry}" if existing_desc else note_entry

            update_record(tid, rid, {
                "Status": "WAITING ART",
                "Last Updated": datetime.now().strftime("%m-%d-%Y"),
                "Description": new_desc,
            })
            post_card_to_lark(
                notify_channel,
                title=f"Changes Requested - {project['order_number']}",
                color="red",
                fields=[
                    {"label": "Client", "value": project.get("client", "-")},
                    {"label": "Status", "value": "WAITING ART"},
                    {"label": "CUSTOMER REVISION NOTES", "value": f"**{notes or 'No notes provided'}**"},
                    {"label": "Product Type", "value": project.get("product_type", "-")},
                ],
                link_url=link,
                image_key=image_key,
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
            sent_at = datetime.fromisoformat(project["sent_at"])
            hours_waiting = (now - sent_at).total_seconds() / 3600
            if hours_waiting >= 48 and not project["followup_sent"]:
                base_url = os.environ.get("BOT_URL", "https://your-bot.railway.app")
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
                    post_card_to_lark(
                        project["notify_channel"],
                        title=f"Follow-up Sent - {project['order_number']}",
                        color="orange",
                        fields=[
                            {"label": "Client", "value": project.get("client", "-")},
                            {"label": "Status", "value": "Still awaiting approval"},
                            {"label": "Note", "value": "48hr follow-up email sent"},
                        ],
                        link_url=record_link(project["table_id"], project["record_id"]),
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
