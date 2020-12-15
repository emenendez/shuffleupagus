1. read config file
2. download each
3. as they download:
   a. if podcast is new:
      - show paginated summary list of items, select which ones to download
      - ask to subscribe?
   b. otherwise if subscribed:
      - select all since last_seen
   c. otherwise not subscribed:
      - select which to download
4. downlaod all selected
5. get unplayed from ipod
6. ask to put in order
7. write to ipod
   /Volumes/IPOD/iPod_Control/Music/shuffleupagus


s = requests.Session()
r = s.post("https://api.pocketcasts.com/user/login", json={"email": "...", "password": "..."})
t = r.json()["token"]
s.headers.update({"Authorization": f"Bearer {t}"})

s.post("https://api.pocketcasts.com/up_next/list", json={"version": 2})

POST https://api.pocketcasts.com/sync/update_episode
{"uuid":"4ffb25af-d45d-4eaa-ad10-9193044cc181","podcast":"b6264df0-b4c5-0132-32ce-0b39892d38e0","status":2,"position":1657} <-- number of seconds

status: 3 <-- played
status: 1 <-- unplayed

POST https://api.pocketcasts.com/up_next/remove
{"version":2,"uuids":["4ffb25af-d45d-4eaa-ad10-9193044cc181"]}
POST https://api.pocketcasts.com/sync/update_episodes_archive
{"episodes":[{"uuid":"4ffb25af-d45d-4eaa-ad10-9193044cc181","podcast":"b6264df0-b4c5-0132-32ce-0b39892d38e0"}],"archive":true}
