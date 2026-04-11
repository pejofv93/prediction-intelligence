from google_auth_oauthlib.flow import InstalledAppFlow
import json

SCOPES = [
    'https://www.googleapis.com/auth/youtube.upload',
    'https://www.googleapis.com/auth/youtube',
    'https://www.googleapis.com/auth/youtube.readonly',
    'https://www.googleapis.com/auth/yt-analytics.readonly'
]

flow = InstalledAppFlow.from_client_secrets_file('client_secrets.json', SCOPES)
creds = flow.run_local_server(port=8080)

with open('token.json', 'w') as f:
    f.write(creds.to_json())

print('token.json generado correctamente') 
