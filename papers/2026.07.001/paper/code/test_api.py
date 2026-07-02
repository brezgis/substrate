import requests
import json

def test_connection():
    url = "http://127.0.0.1:11434/api/generate"
    payload = {
        "model": "gemma4:12b",
        "prompt": "Say 'Connection successful' if you can hear me.",
        "stream": False
    }
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        print(response.json()['response'])
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_connection()
