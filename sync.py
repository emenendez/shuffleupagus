from contextlib import contextmanager
from dataclasses import dataclass, fields
from functools import wraps
import logging
import os
import re
import tempfile
import typing as t
from functools import partial
from multiprocessing.pool import ThreadPool
from pathlib import Path

import click
import ffmpeg
import inquirer
import keyring
import requests
from flask import abort, Flask, request, send_file
from furl import furl
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3
from rich.logging import RichHandler

from ipod import Shuffler


app = Flask(__name__)

logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
log = logging.getLogger(__name__)

# Config
MUSIC_DIR = Path('music')
PLAYLIST_NAME = 'Shuffleupagus'
PLAYLIST_ID = '95f83b1d-1d5d-4626-bcdd-bf3a1a8a0b6e'
USERNAME = keyring.get_credential("Pocket Casts", "username").password
PASSWORD = keyring.get_credential("Pocket Casts", USERNAME).password


@dataclass(frozen=True)
class Episode:
    order: int
    uuid: str
    url: furl
    title: str
    podcast: str

    @classmethod
    def from_dict(cls, order: int, dict: t.Dict) -> "Episode":
        input_dict = dict.copy()
        input_dict["order"] = order
        args = {field.name: field.type(input_dict[field.name]) for field in fields(cls)}
        return cls(**args)

    @property
    def extension(self) -> str:
        return Path(self.url.path.segments[-1]).suffix

    @property
    def filename(self) -> str:
        return f"{self.uuid}{self.extension}"

    @property
    def path(self):
        return MUSIC_DIR / self.filename
   
    @property
    def duration(self) -> int:
        return int(MP3(self.path).info.length)
    

@dataclass
class PocketCasts:
    session: requests.Session

    def json(self, method: str, *args, **kwargs) -> t.Dict:
        method_call = getattr(self.session, method)
        with method_call(*args, **kwargs) as response:
            return response.json()

    def up_next(self) -> t.List[Episode]:
        response = self.json("post", "https://api.pocketcasts.com/up_next/list", json={"version": 2})
        return [Episode.from_dict(*item) for item in enumerate(response["episodes"])]

    def podcasts(self) -> t.Dict[str, str]:
        response = self.json("post", "https://api.pocketcasts.com/user/podcast/list", json={"v": 1})
        return {podcast['uuid']: podcast['title'] for podcast in response['podcasts']}


@contextmanager
def pocket_casts():
    with requests.Session() as session:
        with session.post('https://api.pocketcasts.com/user/login',
                          json={"email": USERNAME, "password": PASSWORD}) as response:
            content = response.json()
            token = content["token"]
        session.headers.update({"Authorization": f"Bearer {token}"})
        yield PocketCasts(session)


def speedup(input: str, output: str) -> None:
    options = {}
    try:
        options['audio_bitrate'] = ffmpeg.probe(input)['format']['bit_rate']
    except Exception:
        pass

    (
        ffmpeg.input(input)
        .audio
        .filter_('atempo', '1.3')
        .output(output, **options)
        .run(quiet=True)
    )


def download_episode(episode: Episode) -> t.Tuple[bool, Episode]:
    try:
        contents = requests.get(str(episode.url)).content
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            input_filename = temp_file.name
            temp_file.write(contents)

        speedup(input_filename, str(episode.path.resolve()))

    except Exception:
        log.exception("Error downloading or speeding up %s", episode.url)
        return False, episode

    finally:
        try:
            os.unlink(input_filename)
        except Exception:
            pass

    return True, episode


def sync():
    with pocket_casts() as api:
        # Get all podcast titles
        titles_by_uuid: t.Dict[str, str] = api.podcasts()

        # Get list of episodes in "up next"
        episodes_from_pocketcasts: t.List[Episode] = api.up_next()
        selected_episodes = inquirer.prompt([inquirer.Checkbox(
            name='episodes',
            message='Select episodes to download:',
            default=[episode.uuid for episode in episodes_from_pocketcasts[1:]],
            choices=[(episode.title, episode.uuid) for episode in episodes_from_pocketcasts],
        )])

        pocketcasts_by_uuid: t.Dict[str, Episode] = {
            episode.uuid: episode
            for episode in episodes_from_pocketcasts
            if episode.uuid in selected_episodes['episodes']
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

        if episodes_to_download:
            log.info("Downloading %r", [episode.title for episode in episodes_to_download])
        with ThreadPool() as pool:
            for success, episode in pool.imap_unordered(download_episode, episodes_to_download):
                if success:
                    log.info("Downloaded %s: %s", episode.title, episode.filename)
                    try:
                        tags = EasyID3(episode.path)
                        tags["title"] = episode.title
                        tags["genre"] = "Podcast"
                        try:
                            tags["album"] = titles_by_uuid[episode.podcast]
                        except KeyError:
                            tags["album"] = "Mystery podcast"
                        tags.save()
                    except Exception:
                        log.exception("Could not rename %r", episode)

        return pocketcasts_by_uuid


episodes = sync()


def auth(f):
    @wraps(f)
    def inner():
        if request.args.get("u") == USERNAME and request.args.get("p") == PASSWORD:
            return f()
        abort(401)

    return inner


def make_response(dict):
    return {
      "subsonic-response": {
            "status": "ok",
            "version": "1.10.2",
        } | dict,
    }


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
    if not (playlist_id := request.args.get('id')):
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
    if not (episode_id := request.args.get('id')):
        abort(400)

    return send_file(episodes[episode_id].path)
