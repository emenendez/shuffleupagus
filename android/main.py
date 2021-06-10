import re
from pathlib import Path
import time
import typing as t
from logging import Logger
from multiprocessing import Process

from kivy.app import App
from kivy.uix.checkbox import CheckBox
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.widget import Widget
from kivy.uix.screenmanager import ScreenManager, Screen

from android.permissions import check_permission, Permission, request_permissions

from flask import abort, Flask, request, send_file
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

from config import MUSIC_DIR, USERNAME, PASSWORD, PLAYLIST_NAME, PLAYLIST_ID
from pocketcasts import pocket_casts, Episode, download_episode


app = Flask(__name__)
PERMISSIONS = [Permission.ACCESS_NETWORK_STATE, Permission.INTERNET, Permission.WRITE_EXTERNAL_STORAGE]
episodes = []


def auth(f):
    @wraps(f)
    def inner():
        if request.args.get("u") == USERNAME and request.args.get("p") == PASSWORD:
            return f()
        abort(401)

    return inner


def make_response(dict):
    response = {
      "subsonic-response": {
            "status": "ok",
            "version": "1.10.2",
        },
    }
    response["subsonic-response"].update(dict)
    return response


@app.route('/rest/ping')
def ping():
    return make_response({})


@app.route('/rest/scrobble')
def scrobble():
    return make_response({})


@app.route('/rest/getPlaylists')
def get_playlists():
    return make_response({
        "playlists": {
            "playlist": [
                {
                  "id": PLAYLIST_ID,
                  "name": PLAYLIST_NAME,
                  "songCount": len(episodes),
                },
            ],
        },
    })


@app.route('/rest/getPlaylist')
def get_playlist():
    playlist_id = request.args.get('id')
    if not playlist_id:
        abort(400)

    entries = []
    if playlist_id == str(PLAYLIST_ID):
        entries = [{
          "contentType": "audio/mpeg",
          "duration": episode.duration,
          "id": episode.uuid,
          "title": episode.title,
        }
        for episode in episodes.values()]

    return make_response({
        "playlist": {
            "entry": entries,
            "id": playlist_id,
            "name": PLAYLIST_NAME,
            "songCount": len(entries),
        },
    })


@app.route('/rest/stream')
def stream():
    episode_id = request.args.get('id')
    if not episode_id:
        abort(400)

    return send_file(episodes[episode_id].path)


class EpisodeWidget(Label):

    def __init__(self, episode, selected, **kwargs):
        super(EpisodeWidget, self).__init__(**kwargs)
        self.episode = episode
        self.text = episode.title
        self.selected = selected
        self.update_display()


    def on_touch_down(self, touch):
        if self.collide_point(*touch.pos):
            self.selected = not self.selected
            self.update_display()

    def update_display(self):
        self.bold = self.selected


class SyncLayout(BoxLayout):

    def __init__(self, episodes_from_pocketcasts, **kwargs):
        super(SyncLayout, self).__init__(**kwargs)
        self.orientation = 'vertical'

        selected = False
        for episode in episodes_from_pocketcasts:
            self.add_widget(EpisodeWidget(episode, selected))
            if not selected:
                selected = True

        self.btn = Button(text="Download")
        self.btn.bind(on_press=self.download_episodes)
        self.add_widget(self.btn)


    def download_episodes(self, instance):
        selected_episodes = [
            widget.episode
            for widget in self.walk(restrict=True)
            if isinstance(widget, EpisodeWidget) and widget.selected
        ]

        pocketcasts_by_uuid: t.Dict[str, Episode] = {
            episode.uuid: episode
            for episode in selected_episodes
        }

        # Get previously-downloaded files
        ipod_by_uuid: t.Dict[str, Path] = {}
        playlist_path = MUSIC_DIR
        playlist_path.mkdir(parents=True, exist_ok=True)
        filename_regex = re.compile(r"([a-f0-9-]+)\..+")
        for file in playlist_path.iterdir():
            filename_match = filename_regex.match(file.name)
            if not filename_match:
                log.error("Cannot parse filename %s", file)
                continue
            ipod_by_uuid[filename_match.group(1)] = file


        uuids_on_ipod = set(ipod_by_uuid.keys())
        uuids_from_pocketcasts = set(pocketcasts_by_uuid.keys())

        uuids_to_delete = uuids_on_ipod - uuids_from_pocketcasts
        uuids_to_download = uuids_from_pocketcasts - uuids_on_ipod

        for uuid in uuids_to_delete:
            ipod_by_uuid[uuid].unlink()

        episodes_to_download = [pocketcasts_by_uuid[uuid] for uuid in uuids_to_download]

        # Download episodes
        self.btn.text = "Downloading"
        for success, episode in (download_episode(e) for e in episodes_to_download):
            if success:
                tags = EasyID3(episode.path)
                tags["title"] = episode.title
                tags["genre"] = "Podcast"
                try:
                    tags["album"] = titles_by_uuid[episode.podcast]
                except KeyError:
                    tags["album"] = "Mystery podcast"
                tags.save()

        global episodes
        episodes = pocketcasts_by_uuid

        # Prompt to start flask
        self.btn.text = "Serve"
        self.btn.unbind(on_press=self.download_episodes)
        self.btn.bind(on_press=self.start_process)

    def start_process(self, instance):
        self.btn.text = "Serving"
        self.btn.unbind(on_press=self.start_process)
        self.btn.bind(on_press=self.stop_process)
        self.process = Process(target=self.serve)
        self.process.daemon = True
        self.process.start()

    def stop_process(self, instance):
        self.process.terminate()

    def serve(self):
        # Start flask
        print("Starting flask...")
        Logger.manager.loggerDict['werkzeug'] = Logger.manager.loggerDict['kivy']
        app.run(host='0.0.0.0', port=5000)


class SyncApp(App):
    def build(self):
        request_permissions(PERMISSIONS, callback=lambda p, r: print(p, r))

        while not all(check_permission(permission) for permission in PERMISSIONS):
            print("Waiting for permissions...")
            time.sleep(1)

        with pocket_casts() as api:
            # Get all podcast titles
            titles_by_uuid: t.Dict[str, str] = api.podcasts()

            # Get list of episodes in "up next"
            episodes_from_pocketcasts: t.List[Episode] = api.up_next()

            # Create download screen
            return SyncLayout(
                episodes_from_pocketcasts,
            )


if __name__ == '__main__':
    SyncApp().run()
