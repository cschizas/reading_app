#!/usr/bin/env python3
"""
Daily reading email: picks a random file from the Drive library,
summarises it with Claude, and sends it via Gmail SMTP.

Required env vars:
  ANTHROPIC_API_KEY       – Anthropic API key
  GMAIL_APP_PASSWORD      – 16-char Gmail app password (not your account password)
  GOOGLE_CREDENTIALS_JSON – JSON string produced by scripts/get_google_token.py
"""

import base64
import io
import json
import os
import random
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

DRIVE_FOLDER_ID = "0B-HXEnqsbXXSZU1Bekpld3UzRVE"
RECIPIENT_EMAIL = "christos.schizas@gmail.com"
SENDER_EMAIL = "christos.schizas@gmail.com"

READABLE_MIME_TYPES = {
    "application/pdf",
    "application/vnd.google-apps.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/jpg",
    "image/png",
}
IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png"}


# ── Drive helpers ────────────────────────────────────────────────────────────

def get_drive_service():
    data = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data["refresh_token"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes", ["https://www.googleapis.com/auth/drive.readonly"]),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def list_readable_files(service, folder_id):
    """Recursively collect all supported files in the folder tree."""
    files = []
    page_token = None
    while True:
        resp = (
            service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, webViewLink)",
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in resp.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                files.extend(list_readable_files(service, f["id"]))
            elif f["mimeType"] in READABLE_MIME_TYPES:
                files.append(f)
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def read_file_content(service, file):
    mime = file["mimeType"]
    fid = file["id"]
    if mime == "application/vnd.google-apps.document":
        raw = service.files().export(fileId=fid, mimeType="text/plain").execute()
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw
    request = service.files().get_media(fileId=fid, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    buf.seek(0)
    if mime in IMAGE_MIME_TYPES:
        return base64.b64encode(buf.read()).decode("utf-8")
    return buf.read().decode("utf-8", errors="replace")


# ── Summarisation ────────────────────────────────────────────────────────────

def generate_summary(file, content, mime):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    instruction = (
        "Analyse this reading material and produce exactly two sections:\n\n"
        "EXECUTIVE SUMMARY\n"
        "3-4 sentences capturing the core takeaway.\n\n"
        "KEY CONCEPTS\n"
        "4-6 bullet points (each starting with '- ') with the main ideas and "
        "memorable details. Bold the concept name before the dash or colon."
    )

    if mime in IMAGE_MIME_TYPES:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": content},
                    },
                    {"type": "text", "text": instruction},
                ],
            }
        ]
    else:
        messages = [
            {
                "role": "user",
                "content": (
                    f'Summarise this reading titled "{file["name"]}".\n\n'
                    f"{content[:15000]}\n\n{instruction}"
                ),
            }
        ]

    resp = client.messages.create(
        model="claude-opus-4-8",
        max_tokens=2000,
        messages=messages,
    )
    return resp.content[0].text


# ── Email building ───────────────────────────────────────────────────────────

def _parse_summary(raw):
    exec_sum, key_concepts_html = raw, ""
    if "EXECUTIVE SUMMARY" in raw and "KEY CONCEPTS" in raw:
        after_exec = raw.split("EXECUTIVE SUMMARY", 1)[1]
        exec_part, kc_part = after_exec.split("KEY CONCEPTS", 1)
        exec_sum = exec_part.strip().lstrip(":").strip()
        items = []
        for line in kc_part.strip().lstrip(":").splitlines():
            line = line.strip().lstrip("-•*").lstrip("0123456789.)").strip()
            if not line:
                continue
            if " — " in line:
                a, b = line.split(" — ", 1)
                line = f"<strong>{a.strip()}</strong> — {b.strip()}"
            elif ": " in line and len(line.split(": ", 1)[0]) < 50:
                a, b = line.split(": ", 1)
                line = f"<strong>{a.strip()}</strong>: {b.strip()}"
            items.append(f"<li>{line}</li>")
        key_concepts_html = "\n".join(items)
    return exec_sum, key_concepts_html


def build_email(file, summary):
    today = datetime.now().strftime("%A, %-d %B %Y")
    title = file["name"]
    for ext in (".pdf", ".jpg", ".jpeg", ".png", ".docx", ".doc"):
        title = title.replace(ext, "")
    drive_link = file.get(
        "webViewLink", f"https://drive.google.com/file/d/{file['id']}/view"
    )
    exec_sum, kc_html = _parse_summary(summary)

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
  body{{font-family:Georgia,serif;max-width:680px;margin:0 auto;color:#1a1a1a}}
  .hdr{{background:#1a2744;color:#fff;padding:28px 32px;border-radius:8px 8px 0 0}}
  .hdr h1{{margin:0 0 6px;font-size:20px}}.hdr p{{margin:0;font-size:13px;color:#aec6f0}}
  .body{{padding:28px 32px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px}}
  .lbl{{font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
        color:#1a2744;margin:24px 0 10px;border-bottom:2px solid #1a2744;padding-bottom:4px}}
  .summ{{background:#f4f7fb;border-left:4px solid #1a2744;padding:16px 18px;
         font-size:15px;line-height:1.7;margin:0}}
  ul{{list-style:none;padding:0;margin:0}}
  li{{padding:10px 0;border-bottom:1px solid #f0f0f0;font-size:14px;line-height:1.65}}
  li:last-child{{border-bottom:none}}
  .btn{{display:inline-block;background:#1a2744;color:#fff!important;padding:10px 20px;
        border-radius:5px;text-decoration:none;font-size:13px;font-weight:700}}
  .foot{{margin-top:24px;font-size:11px;color:#aaa;text-align:center;
         border-top:1px solid #eee;padding-top:14px}}
</style>
</head>
<body>
<div class="hdr"><h1>{title}</h1><p>{today}</p></div>
<div class="body">
  <div class="lbl">Executive Summary</div>
  <p class="summ">{exec_sum}</p>
  <div class="lbl">Key Concepts</div>
  <ul>{kc_html}</ul>
  <div class="lbl">Source</div>
  <p><a class="btn" href="{drive_link}">Open in Google Drive →</a></p>
  <div class="foot">Daily reading · {RECIPIENT_EMAIL}</div>
</div>
</body></html>"""

    subject = f"Today's reading: {title}"
    return subject, html, summary  # plain-text fallback = raw summary


# ── Sending ──────────────────────────────────────────────────────────────────

def send_email(subject, html_body, plain_body):
    pwd = os.environ["GMAIL_APP_PASSWORD"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, pwd)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())
    print(f"Sent: {subject}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("Connecting to Drive...")
    service = get_drive_service()

    print("Listing files...")
    files = list_readable_files(service, DRIVE_FOLDER_ID)
    if not files:
        raise RuntimeError("No readable files found in the Drive folder.")
    print(f"Found {len(files)} files.")

    chosen = random.choice(files)
    print(f"Selected: {chosen['name']} ({chosen['mimeType']})")

    print("Reading content...")
    content = read_file_content(service, chosen)

    print("Generating summary with Claude...")
    summary = generate_summary(chosen, content, chosen["mimeType"])

    print("Building and sending email...")
    subject, html, plain = build_email(chosen, summary)
    send_email(subject, html, plain)


if __name__ == "__main__":
    main()
