import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
from furl import furl
import logging
from pathlib import Path
import re
import typing as t

import aiohttp
import asyncclick as click
import keyring

from ipod import Shuffler


@dataclass(frozen=True)
class Episode:
    order: int
    uuid: str
    url: furl

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
        return f"{self.order:02d}_{self.uuid}{self.extension}"


@dataclass
class PocketCasts:
    session: aiohttp.ClientSession

    async def json(self, method: str, *args, **kwargs) -> t.Dict:
        method_call = getattr(self.session, method)
        async with method_call(*args, **kwargs) as response:
            return await response.json()

    async def up_next(self) -> t.List[Episode]:
        response = await self.json("post", "https://api.pocketcasts.com/up_next/list", json={"version": 2})
        return [Episode.from_dict(*item) for item in enumerate(response["episodes"])]


@asynccontextmanager
async def pocket_casts():
    username: str = keyring.get_credential("Pocket Casts", "username").password
    password: str = keyring.get_credential("Pocket Casts", username).password
    async with aiohttp.ClientSession() as session:
        async with session.post('https://api.pocketcasts.com/user/login',
                                json={"email": username, "password": password}) as response:
            content = await response.json()
            token = content["token"]
        session.headers.update({"Authorization": f"Bearer {token}"})
        yield PocketCasts(session)


async def get_url(session: aiohttp.ClientSession, url: furl) -> bytes:
    async with session.get(str(url)) as response:
        return await response.read()


async def download_episode(session: aiohttp.ClientSession, playlist_path: Path, episode: Episode) -> t.Tuple[bool, Episode]:
    try:
        contents = await get_url(session, episode.url)
        with (playlist_path / episode.filename).open("wb") as output:
            output.write(contents)
    except Exception:
        logging.exception("Error downloading %s", episode.url)
        return False, episode
    return True, episode


@click.command()
@click.option("-i", "--ipod-dir", default="/Volumes/IPOD", help="path to iPod mount point")
@click.option("-p", "--playlist", default="shuffleupagus", help="name of synced playlist")
async def sync(ipod_dir, playlist):
    async with pocket_casts() as api:
        async with aiohttp.ClientSession() as session:
            # Get list of episodes in "up next"
            episodes_from_pocketcasts: t.List[Episode] = await api.up_next()

            # Get files in the playlist on the iPod
            ipod_by_uuid: t.Dict[str, Path] = {}
            playlist_path = Path(ipod_dir) / "iPod_Control" / "Music" / playlist
            playlist_path.mkdir(parents=True, exist_ok=True)
            filename_regex = re.compile(r"\d{2}_([a-f0-9-]+)\..+")
            for file in playlist_path.iterdir():
                filename_match = filename_regex.match(file.name)
                if not filename_match:
                    logging.error("Cannot parse filename %s", file)
                    continue
                ipod_by_uuid[filename_match.group(1)] = file

            pocketcasts_by_uuid: t.Dict[str, Episode] = {episode.uuid: episode for episode in episodes_from_pocketcasts}

            uuids_on_ipod = set(ipod_by_uuid.keys())
            uuids_from_pocketcasts = set(pocketcasts_by_uuid.keys())

            uuids_to_delete = uuids_on_ipod - uuids_from_pocketcasts
            uuids_to_rename = uuids_on_ipod & uuids_from_pocketcasts
            uuids_to_download = uuids_from_pocketcasts - uuids_on_ipod

            for uuid in uuids_to_delete:
                ipod_by_uuid[uuid].unlink()
            for uuid in uuids_to_rename:
                old_path = ipod_by_uuid[uuid]
                new_path = old_path.with_name(pocketcasts_by_uuid[uuid].filename)
                old_path.rename(new_path)

            for result in asyncio.as_completed([
                    download_episode(session, playlist_path, pocketcasts_by_uuid[uuid]) for uuid in uuids_to_download
                    ]):
                success, episode = await result
                if success:
                    logging.info("Downloaded %s", episode.filename)

if __name__ == "__main__":
    sync(_anyio_backend="asyncio")
