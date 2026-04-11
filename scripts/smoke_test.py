import requests

base = 'http://127.0.0.1:8000'
print(requests.get(f'{base}/health', timeout=5).json())
print(requests.get(f'{base}/api/v1/version', timeout=5).json())
print(requests.post(f'{base}/api/v1/chat', json={'message': 'Help me think clearly about my business.'}, timeout=10).json())
