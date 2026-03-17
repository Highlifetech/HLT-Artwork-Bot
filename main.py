import os
import json
import uuid
import smtplib
import requests
from flask import Flask, request, jsonify
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import threading
import time

app = Flask(__name__)

# In-memory store — replace with Redis for production
approval_store = {}


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


def post_to_lark(channel_id, message):
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


def update_record(record_id, fields):
    token = get_lark_token()
    requests.put(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/"
        f"{os.environ['PROJECTS_TABLE_ID']}/records/{record_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"fields": fields},
    )


def post_comment(record_id, text):
    token = get_lark_token()
    requests.post(
        f"https://open.larksuite.com/open-apis/bitable/v1/apps/"
        f"{os.environ['LARK_BASE_APP_TOKEN']}/tables/"
        f"{os.environ['PROJECTS_TABLE_ID']}/records/{record_id}/comments",
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
# EMAIL
# ══════════════════════════════════════════════════════

def send_artwork_email(to_email, client, order_number,
                       art_file_url, approval_url, is_followup=False):
    msg = MIMEMultipart("alternative")
    prefix = "Follow-up: " if is_followup else ""
    msg["Subject"] = f"{prefix}Artwork Approval — {order_number}"
    msg["From"] = os.environ["EMAIL_ADDRESS"]
    msg["To"] = to_email

    reminder = (
        "<p><strong>Friendly reminder</strong> — we haven't heard back yet.</p>"
        if is_followup else ""
    )

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;max-width:600px;
                 margin:0 auto;padding:20px;">
      <h2 style="color:#000;">Your artwork is ready for review</h2>
      <p>Hi {client},</p>
      {reminder}
      <p>Your artwork for order <strong>{order_number}</strong>
         is ready for your approval.</p>
      <div style="text-align:center;margin:40px 0;">
        <a href="{art_file_url}"
           style="background:#000;color:#fff;padding:14px 28px;
                  text-decoration:none;border-radius:4px;
                  font-weight:bold;display:inline-block;
                  margin-bottom:20px;">
          View Artwork
        </a>
      </div>
      <p>Once reviewed please select:</p>
      <div style="text-align:center;margin:30px 0;">
        <a href="{approval_url}?decision=approved"
           style="background:#22c55e;color:#fff;padding:14px 32px;
                  text-decoration:none;border-radius:4px;
                  font-weight:bold;display:inline-block;
                  margin-right:12px;">
          ✓ Approve
        </a>
        <a href="{approval_url}?decision=changes"
           style="background:#ef4444;color:#fff;padding:14px 32px;
                  text-decoration:none;border-radius:4px;
                  font-weight:bold;display:inline-block;">
          ✎ Request Changes
        </a>
      </div>
      <p style="color:#666;font-size:14px;">
        Please respond within 24 hours to keep your project on schedule.
      </p>
      <hr style="border:none;border-top:1px solid #eee;margin:30px 0;">
      <p style="color:#999;font-size:12px;">
        High Life Tech — orders@highlifetech.co
      </p>
    </body>
    </html>
    """

    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_ADDRESS"], os.environ["EMAIL_APP_PASSWORD"])
        server.sendmail(os.environ["EMAIL_ADDRESS"], to_email, msg.as_string())


# ══════════════════════════════════════════════════════
# ARTWORK TRIGGER
# Called by Lark automation when button is clicked
# ══════════════════════════════════════════════════════

@app.route("/artwork-trigger", methods=["POST"])
def artwork_trigger():
    data         = request.json or {}
    record_id    = data.get("record_id", "")
    order_number = data.get("order_number", "")
    client       = data.get("client", "")
    client_email = data.get("client_email", "")
    art_file_url = data.get("art_files", "")
    in_hand_date = data.get("in_hand_date", "")

    if not client_email:
        post_to_lark(
            os.environ["DIGEST_CHANNEL_ID"],
            f"⚠️ Cannot send artwork for {order_number} — {client}\n"
            f"No client email on the card. Please add it and try again.",
        )
        return jsonify({"code": 0})

    token = str(uuid.uuid4())
    approval_store[token] = {
        "record_id":     record_id,
        "order_number":  order_number,
        "client":        client,
        "client_email":  client_email,
        "art_file_url":  art_file_url,
        "in_hand_date":  in_hand_date,
        "sent_at":       datetime.now().isoformat(),
        "followup_sent": False,
    }

    base_url     = os.environ.get("BOT_URL", "https://your-bot.railway.app")
    approval_url = f"{base_url}/approve/{token}"

    send_artwork_email(client_email, client, order_number, art_file_url, approval_url)

    post_comment(
        record_id,
        f"📨 Artwork sent to client — "
        f"{datetime.now().strftime('%b %d %Y %I:%M %p')}"
    )

    post_to_lark(
        os.environ["DIGEST_CHANNEL_ID"],
        f"📨 Artwork sent to {client} for {order_number}\n"
        f"In-Hand Date: {in_hand_date}\n"
        f"Awaiting client approval...",
    )

    return jsonify({"code": 0})


# ══════════════════════════════════════════════════════
# APPROVAL PAGE
# What the client sees when they click the email link
# ══════════════════════════════════════════════════════

@app.route("/approve/<token>", methods=["GET", "POST"])
def approve(token):
    if token not in approval_store:
        return "<h2>This link has expired or is no longer valid.</h2>", 404

    project  = approval_store[token]
    decision = request.args.get("decision", "")

    # Show revision form
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

    # Process decision
    if decision == "approved" or request.method == "POST":
        notes          = request.form.get("notes", "")
        final_decision = request.form.get("decision", decision)
        now_str        = datetime.now().strftime("%b %d %Y %I:%M %p")

        if final_decision == "approved":
            update_record(project["record_id"], {"Status": "ARTWORK CONFIRMED"})
            post_comment(
                project["record_id"],
                f"✅ {project['client']} approved artwork — {now_str}\n"
                f"Production can begin."
            )
            post_to_lark(
                os.environ["DIGEST_CHANNEL_ID"],
                f"✅ {project['client']} approved artwork for "
                f"{project['order_number']}\n"
                f"Status → ARTWORK CONFIRMED\n"
                f"Ready to move to Part Confirmed.",
            )
            del approval_store[token]
            return """
            <html>
            <body style="font-family:Arial,sans-serif;text-align:center;
                         padding:80px 20px;">
              <h1 style="color:#22c55e;">✓ Approved!</h1>
              <p>Thank you — we'll begin production now.</p>
              <p style="color:#666;">You'll receive a shipping notification
                 when your order is on its way.</p>
            </body>
            </html>
            """, 200

        else:
            update_record(project["record_id"], {"Status": "WAITING ART"})
            post_comment(
                project["record_id"],
                f"✏️ {project['client']} requested changes — {now_str}\n"
                f"Revision notes: {notes}"
            )
            post_to_lark(
                os.environ["DIGEST_CHANNEL_ID"],
                f"✏️ {project['client']} requested changes on "
                f"{project['order_number']}\n"
                f"Notes: {notes}\n"
                f"Status → WAITING ART",
            )
            del approval_store[token]
            return """
            <html>
            <body style="font-family:Arial,sans-serif;text-align:center;
                         padding:80px 20px;">
              <h1>Got it!</h1>
              <p>We've received your feedback and will send
                 a revised proof shortly.</p>
            </body>
            </html>
            """, 200

    return "<h2>Invalid request.</h2>", 400


# ══════════════════════════════════════════════════════
# 48HR FOLLOW-UP
# Runs in background, checks every hour
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
                    send_artwork_email(
                        project["client_email"],
                        project["client"],
                        project["order_number"],
                        project["art_file_url"],
                        approval_url,
                        is_followup=True,
                    )
                    post_comment(
                        project["record_id"],
                        "⏰ Follow-up email sent — no client response after 48hrs"
                    )
                    post_to_lark(
                        os.environ["DIGEST_CHANNEL_ID"],
                        f"⏰ Follow-up sent to {project['client']} — "
                        f"{project['order_number']} still awaiting approval",
                    )
                    approval_store[token]["followup_sent"] = True
                    approval_store[token]["sent_at"] = now.isoformat()
                except Exception as e:
                    print(f"Follow-up error: {e}")


threading.Thread(target=check_pending_approvals, daemon=True).start()


# ══════════════════════════════════════════════════════
# LARK WEBHOOK — required for Lark URL verification
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
