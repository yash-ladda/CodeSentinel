import time
import jwt
import requests
import os
from dotenv import load_dotenv

load_dotenv()

GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_PRIVATE_KEY_PATH = os.getenv("GITHUB_PRIVATE_KEY_PATH")


def load_private_key() -> str:
    """
    Read the GitHub App private key (.pem) from disk.
    """
    with open(GITHUB_PRIVATE_KEY_PATH, "r") as f:
        return f.read()


def generate_jwt() -> str:
    """
    Generate a GitHub App JWT.

    GitHub uses this to verify that we ARE the registered App.

    Valid for max 10 minutes.
    We use 9 minutes to stay safe.
    """
    private_key = load_private_key()

    now = int(time.time())

    payload = {
        "iat": now - 60,       # issued 60s ago (clock skew buffer)
        "exp": now + (9 * 60), # expires in 9 mins
        "iss": GITHUB_APP_ID,  # GitHub App ID
    }

    token = jwt.encode(
        payload,
        private_key,
        algorithm="RS256",
    )

    return token


def get_installation_token(installation_id: int) -> str:
    """
    Exchange App JWT for an Installation Access Token.

    This is the token used for actual GitHub API requests.

    Valid for ~1 hour.

    Args:
        installation_id: GitHub App installation ID from webhook payload.
    """
    jwt_token = generate_jwt()

    url = (
        f"https://api.github.com/app/installations/"
        f"{installation_id}/access_tokens"
    )

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = requests.post(url, headers=headers)

    # Always validate API response
    if response.status_code != 201:
        print(
            f"ERROR getting installation token: "
            f"{response.status_code}"
        )
        print(response.text)

        raise Exception(
            f"Failed to get installation token: "
            f"{response.status_code}"
        )

    data = response.json()

    token = data["token"]
    expires_at = data["expires_at"]

    print(
        f"Got installation token. "
        f"Expires at: {expires_at}"
    )

    return token


def test_api_call(token: str) -> None:
    """
    Simple GitHub API test.

    Lists repos this installation can access.
    Useful for debugging auth issues.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    response = requests.get(
        "https://api.github.com/installation/repositories",
        headers=headers,
    )

    if response.status_code == 200:
        repos = response.json()["repositories"]

        print(
            f"\nInstallation has access to "
            f"{len(repos)} repo(s):"
        )

        for repo in repos:
            print(f"  - {repo['full_name']}")

    else:
        print(
            f"ERROR: {response.status_code} "
            f"— {response.text}"
        )