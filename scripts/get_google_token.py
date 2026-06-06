#!/usr/bin/env python3
"""
Run this ONCE locally to generate the GOOGLE_CREDENTIALS_JSON secret.

Steps:
  1. pip install google-auth-oauthlib
  2. Go to Google Cloud Console > APIs & Services > Credentials
     Create an OAuth 2.0 Client ID (Desktop app type)
     Enable both the Google Drive API and Gmail API in your project.
     Download the JSON and save it as client_secret.json here.
  3. python scripts/get_google_token.py
     A browser window will open — sign in and grant access to Drive + Gmail.
  4. Copy the printed JSON into:
     GitHub repo > Settings > Secrets and variables > Actions > GOOGLE_CREDENTIALS_JSON
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
creds = flow.run_local_server(port=0)

output = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes),
}

print("\nAdd this as your GOOGLE_CREDENTIALS_JSON GitHub secret:\n")
print(json.dumps(output, indent=2))
