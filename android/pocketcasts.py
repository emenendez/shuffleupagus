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
import urllib.parse

# import ffmpeg
import requests

from config import *




@dataclass(frozen=True)
class Episode:
    order: int
    uuid: str
    url: str
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
        return Path(self.url.path).suffix

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

        # speedup(input_filename, str(episode.path.resolve()))
        os.path.rename(input_filename, str(episode.path.resolve()))

    except Exception:
        return False, episode

    finally:
        try:
            os.unlink(input_filename)
        except Exception:
            pass

    return True, episode