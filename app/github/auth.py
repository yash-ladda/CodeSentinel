import time
import jwt
import requests
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY_PATH = os.getenv("GITHUB_PRIVATE_KEY_PATH")
GITHUB_INSTALLATION_ID = os.getenv("GITHUB_INSTALLATION_ID")


def load_private_key() -> str:
    """Read the private key PEM file from disk."""
    with open(GITHUB_PRIVATE_KEY_PATH, "r") as f:
        return f.read()

def generate_jwt() -> str:
    """
    Generate a JWT signed with our App's private key.
    GitHub uses this to verify we ARE the registered App.
    Valid for 10 minutes maximum — we use 9 to be safe.
    """
    private_key = load_private_key()
    
    now = int(time.time())
    
    payload = {
        "iat": now - 60,      # issued at: 60 seconds ago (clock skew buffer)
        "exp": now + (9 * 60), # expires: 9 minutes from now
        "iss": GITHUB_APP_ID,  # issuer: our App ID
    }
    
    # Sign the payload with RS256 algorithm using our private key
    # RS256 = RSA Signature with SHA-256
    token = jwt.encode(payload, private_key, algorithm="RS256")
    
    return token

def get_installation_token() -> str:
    """
    Exchange our JWT for an Installation Access Token.
    This is the token we actually use for GitHub API calls.
    Valid for 1 hour.
    """
    jwt_token = generate_jwt()
    
    url = f"https://api.github.com/app/installations/{GITHUB_INSTALLATION_ID}/access_tokens"
    
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    
    response = requests.post(url, headers=headers)
    
    # Always check for errors — don't assume success
    if response.status_code != 201:
        print(f"ERROR getting installation token: {response.status_code}")
        print(response.text)
        raise Exception(f"Failed to get installation token: {response.status_code}")
    
    data = response.json()
    token = data["token"]
    expires_at = data["expires_at"]
    
    print(f"Got installation token. Expires at: {expires_at}")
    
    return token

def test_api_call(token: str) -> None:
    """
    Make a simple GitHub API call to confirm our token works.
    Lists repos the installation has access to.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    
    response = requests.get(
        "https://api.github.com/installation/repositories",
        headers=headers
    )
    
    if response.status_code == 200:
        repos = response.json()["repositories"]
        print(f"\nInstallation has access to {len(repos)} repo(s):")
        for repo in repos:
            print(f"  - {repo['full_name']}")
    else:
        print(f"ERROR: {response.status_code} — {response.text}")