#!/usr/bin/env python3
"""
Daily reading email: picks a random file from the Drive library,
summarises it with Claude, and sends it via Gmail API (OAuth).

Required env vars:
  ANTHROPIC_API_KEY       - Anthropic API key
  GOOGLE_CREDENTIALS_JSON - JSON string with drive.readonly + gmail.send scopes
"""

import base64
import io
import json
import os
import random
from datetime import datetime, timedelta, timezone
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

DRIVE_FOLDER_ID = "0B-HXEnqsbXXSZU1Bekpld3UzRVE"
RECIPIENT_EMAIL = "christos.schizas@gmail.com"
SENDER_EMAIL = "christos.schizas@gmail.com"
HISTORY_FILE = Path("data/sent_history.json")

COOLDOWN_PRIMARY = timedelta(days=90)
COOLDOWN_FALLBACK = timedelta(days=30)

READABLE_MIME_TYPES = {
    "application/pdf",
    "application/vnd.google-apps.document",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "image/jpeg",
    "image/jpg",
    "image/png",
}
EXCLUDED_MIME_TYPES = {
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.drawing",
}
IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png"}


# ── Google auth ───────────────────────────────────────────────────────────────

def get_google_credentials():
    data = json.loads(os.environ["GOOGLE_CREDENTIALS_JSON"])
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data["refresh_token"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes"),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds


# ── Sent history ──────────────────────────────────────────────────────────────

def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return {}


def save_history(history, file_id, title):
    history[file_id] = {
        "title": title,
        "last_sent": datetime.now(timezone.utc).isoformat(),
    }
    HISTORY_FILE.parent.mkdir(exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2))


def pick_file(files, history):
    now = datetime.now(timezone.utc)

    def last_sent(f):
        entry = history.get(f["id"])
        if not entry:
            return None
        dt = datetime.fromisoformat(entry["last_sent"])
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    primary = [f for f in files if last_sent(f) is None or last_sent(f) < now - COOLDOWN_PRIMARY]
    if primary:
        print(f"Primary pool ({len(primary)} files — not sent in 90d).")
        return random.choice(primary)

    fallback = [f for f in files if last_sent(f) is None or last_sent(f) < now - COOLDOWN_FALLBACK]
    if fallback:
        print(f"Fallback pool ({len(fallback)} files — not sent in 30d).")
        return random.choice(fallback)

    print("All files sent recently — picking the oldest-sent file.")
    return min(files, key=lambda f: last_sent(f) or datetime.min.replace(tzinfo=timezone.utc))


# ── Drive helpers ─────────────────────────────────────────────────────────────

def list_readable_files(service, folder_id):
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
            mime = f["mimeType"]
            if mime == "application/vnd.google-apps.folder":
                files.extend(list_readable_files(service, f["id"]))
            elif mime in READABLE_MIME_TYPES:
                files.append(f)
            elif mime in EXCLUDED_MIME_TYPES:
                print(f"  Skipped ({mime}): {f['name']}")
            else:
                print(f"  Skipped (unknown type {mime}): {f['name']}")
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
    # Always return raw bytes for images (we need them for embedding)
    return buf.read()


# ── Summarisation ─────────────────────────────────────────────────────────────

def generate_summary(file, content, mime):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    instruction = (
        "Write in a casual, conversational tone — like you're telling a smart friend about something interesting you read. No jargon, no fluff.\n\n"
        "EXECUTIVE SUMMARY\n"
        "2 sentences max. What's the core idea and why does it matter?\n\n"
        "KEY CONCEPTS\n"
        "3-5 bullet points (start each with '- '). One punchy line each. Bold the concept name."
    )
    if mime in IMAGE_MIME_TYPES:
        messages = [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(content).decode()}},
                {"type": "text", "text": instruction},
            ],
        }]
    else:
        text_content = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        messages = [{
            "role": "user",
            "content": f'Summarise this reading: "{file["name"]}"\n\n{text_content[:15000]}\n\n{instruction}',
        }]
    resp = client.messages.create(model="claude-opus-4-8", max_tokens=1000, messages=messages)
    return resp.content[0].text


# ── Email building ────────────────────────────────────────────────────────────

def _parse_summary(raw):
    exec_sum, kc_html = raw, ""
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
        kc_html = "\n".join(items)
    return exec_sum, kc_html


def build_mime_message(file, summary, image_bytes=None, image_mime=None):
    today = datetime.now().strftime("%A, %-d %B %Y")
    title = file["name"]
    for ext in (".pdf", ".jpg", ".jpeg", ".png", ".docx", ".doc"):
        title = title.replace(ext, "")
    drive_link = file.get("webViewLink", f"https://drive.google.com/file/d/{file['id']}/view")
    exec_sum, kc_html = _parse_summary(summary)

    img_tag = '<img src="cid:reading_img" style="max-width:100%;border-radius:6px;margin-top:4px;">' if image_bytes else ""

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8">
<style>
  body{{font-family:Georgia,serif;max-width:660px;margin:0 auto;color:#1a1a1a}}
  .hdr{{background:#1a2744;color:#fff;padding:22px 28px;border-radius:8px 8px 0 0}}
  .hdr h1{{margin:0 0 4px;font-size:18px;font-weight:bold}}
  .hdr p{{margin:0;font-size:12px;color:#aec6f0}}
  .body{{padding:24px 28px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px}}
  .lbl{{font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
        color:#1a2744;margin:20px 0 8px;border-bottom:2px solid #1a2744;padding-bottom:3px}}
  .summ{{background:#f4f7fb;border-left:4px solid #1a2744;padding:12px 16px;
         font-size:14px;line-height:1.6;margin:0}}
  ul{{list-style:none;padding:0;margin:0}}
  li{{padding:8px 0;border-bottom:1px solid #f0f0f0;font-size:13px;line-height:1.55}}
  li:last-child{{border-bottom:none}}
  .btn{{display:inline-block;background:#1a2744;color:#fff!important;padding:9px 18px;
        border-radius:5px;text-decoration:none;font-size:13px;font-weight:700}}
  .foot{{margin-top:20px;font-size:11px;color:#aaa;text-align:center;
         border-top:1px solid #eee;padding-top:12px}}
</style>
</head>
<body>
<div class="hdr"><h1>{title}</h1><p>{today}</p></div>
<div class="body">
  <div class="lbl">The gist</div>
  <p class="summ">{exec_sum}</p>
  <div class="lbl">Key takeaways</div>
  <ul>{kc_html}</ul>
  {f'<div class="lbl">Visual</div>{img_tag}' if image_bytes else ""}
  <div class="lbl">Read it</div>
  <p><a class="btn" href="{drive_link}">Open in Google Drive →</a></p>
  <div class="foot">Daily reading · {RECIPIENT_EMAIL}</div>
</div>
</body></html>"""

    subject = f"Today's read: {title}"

    # Build MIME structure
    if image_bytes:
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(summary, "plain"))
        alt.attach(MIMEText(html, "html"))
        msg.attach(alt)
        subtype = (image_mime or "image/jpeg").split("/")[1].replace("jpg", "jpeg")
        img_part = MIMEImage(image_bytes, _subtype=subtype)
        img_part.add_header("Content-ID", "<reading_img>")
        img_part.add_header("Content-Disposition", "inline")
        msg.attach(img_part)
    else:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(summary, "plain"))
        msg.attach(MIMEText(html, "html"))

    msg["Subject"] = subject
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECIPIENT_EMAIL
    return msg


# ── Sending via Gmail API ─────────────────────────────────────────────────────

def send_email(gmail_service, msg):
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    gmail_service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Sent: {msg['Subject']}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Authenticating with Google...")
    creds = get_google_credentials()
    drive_service = build("drive", "v3", credentials=creds)
    gmail_service = build("gmail", "v1", credentials=creds)

    print("Loading sent history...")
    history = load_history()
    print(f"  {len(history)} file(s) in history.")

    print("Listing files in Drive library...")
    files = list_readable_files(drive_service, DRIVE_FOLDER_ID)
    if not files:
        raise RuntimeError("No readable files found in the Drive folder.")
    print(f"Found {len(files)} readable file(s).")

    chosen = pick_file(files, history)
    print(f"Selected: {chosen['name']} ({chosen['mimeType']})")

    print("Reading content...")
    content = read_file_content(drive_service, chosen)

    mime = chosen["mimeType"]
    image_bytes = content if mime in IMAGE_MIME_TYPES else None
    image_mime = mime if mime in IMAGE_MIME_TYPES else None

    print("Generating summary with Claude...")
    summary = generate_summary(chosen, content, mime)

    print("Building and sending email...")
    msg = build_mime_message(chosen, summary, image_bytes=image_bytes, image_mime=image_mime)
    send_email(gmail_service, msg)

    print("Updating sent history...")
    save_history(history, chosen["id"], chosen["name"])
    print("Done.")


if __name__ == "__main__":
    main()
