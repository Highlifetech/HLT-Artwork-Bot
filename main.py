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

# In-memory approval store
approval_store = {}

# Cache table IDs so we don't fetch them on every request
_table_id_cache = []
_table_cache_time = 0
TABLE_CACHE_TTL = 300  # refresh every 5 minutes


# ══════════════════════════════════════════════════════
# LARK HELPERS
# ══════════════════════════════════════════════════════

def get_lark_token():
    res = requests.post(
        "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
