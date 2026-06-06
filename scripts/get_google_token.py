#!/usr/bin/env python3
"""
Run this ONCE locally to generate the GOOGLE_CREDENTIALS_JSON secret.

Steps:
  1. pip install google-auth-oauthlib
  2. Download your OAuth 2.0 client secret from Google Cloud Console
     (APIs & Services → Credentials → OAuth 2.0 Client ID → Download JSON)
     Save it as client_secret.json in this directory.
  3. python scripts/get_google_token.py
  4. Copy the printed JSON into GitHub → Settings → Secrets → GOOGLE_CREDENTIALS_JSON
"""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

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

print("\n✓ Add this as your GOOGLE_CREDENTIALS_JSON GitHub secret:\n")
print(json.dumps(output, indent=2))
