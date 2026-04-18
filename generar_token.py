from google_auth_oauthlib.flow import InstalledAppFlow
import base64

flow = InstalledAppFlow.from_client_secrets_file(
    'client_secrets.json',
    scopes=[
        'https://www.googleapis.com/auth/youtube.upload',
        'https://www.googleapis.com/auth/youtube',
        'https://www.googleapis.com/auth/yt-analytics.readonly'
    ]
)
creds = flow.run_local_server(port=8080)
token_b64 = base64.b64encode(creds.to_json().encode()).decode()
open('nuevo_token.txt', 'w').write(token_b64)
print("Token guardado en nuevo_token.txt")
