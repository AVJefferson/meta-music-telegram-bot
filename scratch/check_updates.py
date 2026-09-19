import json
import re
import urllib.request

req_file = '/home/avjef/Projects/telegram-music-bot/requirements.txt'

with open(req_file) as f:
    lines = f.readlines()

for line in lines:
    line = line.strip()
    if not line or line.startswith('#'):
        continue
    pkg_match = re.match(r'^([a-zA-Z0-9\-_]+)', line)
    if pkg_match:
        pkg_name = pkg_match.group(1)
        try:
            url = f"https://pypi.org/pypi/{pkg_name}/json"
            response = urllib.request.urlopen(url)
            data = json.loads(response.read())
            latest_version = data['info']['version']
            print(f"{pkg_name}: {latest_version}")
        except Exception as e:
            print(f"Error checking {pkg_name}: {e}")
