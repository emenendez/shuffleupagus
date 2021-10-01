from contextlib import contextmanager
from dataclasses import dataclass, fields
from furl import furl
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
import keyring
import requests
from mutagen.easyid3 import EasyID3
from rich.logging import RichHandler

from ipod import Shuffler


logging.basicConfig(
    level="INFO",
    format="%(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True)]
)
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Episode:
    order: int
    uuid: str
    url: furl
    title: str
    podcast: str

    filename_regex = re.compile(r"\d+_([a-f0-9-]+)\..+")


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
        return f"{self.order:02}_{self.uuid}{self.extension}"

    @classmethod
    def uuid_from_filename(cls, filename):
        filename_match = cls.filename_regex.match(filename)
        if not filename_match:
            raise ValueError(f"Cannot parse filename {file}")
        return filename_match.group(1)


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
    username: str = keyring.get_credential("Pocket Casts", "username").password
    password: str = keyring.get_credential("Pocket Casts", username).password
    with requests.Session() as session:
        with session.post('https://api.pocketcasts.com/user/login',
                          json={"email": username, "password": password}) as response:
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


def download_episode(playlist_path: Path, episode: Episode) -> t.Tuple[bool, Episode]:
    try:
        contents = requests.get(str(episode.url)).content
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            input_filename = temp_file.name
            temp_file.write(contents)

        speedup(input_filename, str((playlist_path / episode.filename).resolve()))

    except Exception:
        log.exception("Error downloading or speeding up %s", episode.url)
        return False, episode

    finally:
        try:
            os.unlink(input_filename)
        except Exception:
            pass

    return True, episode


@click.command()
@click.option("-i", "--ipod-dir", default="/Volumes/IPOD", help="path to iPod mount point")
@click.option("-p", "--playlist", default="shuffleupagus", help="name of synced playlist")
@click.option("-G/-g", "--generate/--no-geerate", default=True)
def sync(ipod_dir, playlist, generate):
    with pocket_casts() as api:
        # Get all podcast titles
        titles_by_uuid: t.Dict[str, str] = api.podcasts()

        # Get list of episodes in "up next"
        episodes_from_pocketcasts: t.List[Episode] = api.up_next()

        # Get files in the playlist on the iPod
        ipod_by_uuid: t.Dict[str, Path] = {}
        playlist_path = Path(ipod_dir) / "iPod_Control" / "Music" / playlist
        playlist_path.mkdir(parents=True, exist_ok=True)
        for file in playlist_path.iterdir():
            try:
                uuid = Episode.uuid_from_filename(file.name)
            except ValueError as e:
                log.error(e)
                continue
            ipod_by_uuid[uuid] = file

        pocketcasts_by_uuid: t.Dict[str, Episode] = {episode.uuid: episode for episode in episodes_from_pocketcasts}

        uuids_on_ipod = set(ipod_by_uuid.keys())
        uuids_from_pocketcasts = set(pocketcasts_by_uuid.keys())

        uuids_to_delete = uuids_on_ipod - uuids_from_pocketcasts
        uuids_to_download = uuids_from_pocketcasts - uuids_on_ipod

        for uuid in uuids_to_delete:
            ipod_by_uuid[uuid].unlink()

        with ThreadPool() as pool:
            for success, episode in pool.imap_unordered(
                partial(download_episode, playlist_path),
                (pocketcasts_by_uuid[uuid] for uuid in uuids_to_download),
            ):
                if success:
                    log.info("Downloaded %s: %s", episode.title, episode.filename)
                    try:
                        tags = EasyID3(playlist_path / episode.filename)
                        tags["title"] = episode.title
                        tags["genre"] = "Podcast"
                        try:
                            tags["album"] = titles_by_uuid[episode.podcast]
                        except KeyError:
                            tags["album"] = "Mystery podcast"
                        tags.save()
                    except Exception:
                        log.exception("Could not rename %r", episode)

    if generate: 
        shuffle = Shuffler(ipod_dir,
                           track_voiceover=False,
                           playlist_voiceover=False,
                           rename=True,
                           trackgain=0,
                           auto_dir_playlists=0,
                           auto_id3_playlists=None)
        shuffle.initialize()
        shuffle.populate()
        shuffle.write_database()


if __name__ == "__main__":
    sync()
