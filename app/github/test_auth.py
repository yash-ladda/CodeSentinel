"""
Run this script directly to test auth — not through FastAPI.
Usage: python app/github/test_auth.py
"""
from auth import get_installation_token, test_api_call

if __name__ == "__main__":
    print("Step 1: Generating JWT...")
    print("Step 2: Exchanging for installation token...")
    
    token = get_installation_token()
    
    print("\nStep 3: Testing the token with a real API call...")
    test_api_call(token)
    
    print("\nAuth is working correctly.")