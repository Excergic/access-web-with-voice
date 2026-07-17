"""
Run once to complete Gmail OAuth2 and save token.json.
Usage: uv run python auth_gmail.py
"""

from rails.api.gmail import _get_credentials

if __name__ == "__main__":
    creds = _get_credentials()
    print(f"Authenticated as: {creds.client_id[:20]}...")
    print("token.json saved. You can now start the server.")
