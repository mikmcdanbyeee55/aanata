import asyncio
import concurrent.futures
import json
import re
from concurrent.futures import as_completed
from os import getenv
from typing import Generic, Optional, Tuple, Type, TypeVar

import aiohttp
import structlog
from pydantic import BaseModel
from structlog.contextvars import bind_contextvars

from grima import human
from grima.debrid import magnet
from grima.debrid.models import StreamLink
from grima.debrid.pm_models import DirectDL, DirectDLResponse
from grima.torrent import Torrent

log = structlog.get_logger(__name__)

ROOT_URL = "https://www.premiumize.me/api"

T = TypeVar("T", bound=BaseModel)


async def make_request(
    api_token: str,
    url: str,
    method: str,
    model: Type[T],
    params: dict[str, str] = {},
    headers: dict[str, str] = {},
    data: Optional[dict[str, str]] = None,
) -> Tuple[T, aiohttp.ClientResponse]:
    full_url: str = f"{ROOT_URL}{url}"
    async with aiohttp.ClientSession() as session:
        params["apikey"] = api_token
        async with session.request(
            method=method,
            url=full_url,
            params=params,
            data=data,
            headers=headers,
        ) as response:
            raw: dict = await response.json()
            return model.model_validate(raw), response


async def select_stream_file(
    files: list[DirectDL],
    season_episode: list[int],
) -> StreamLink | None:
    sorted_files: list[DirectDL] = sorted(files, key=lambda f: f.size, reverse=True)
    if len(sorted_files) == 0:
        return None
    if len(sorted_files) == 1 or not season_episode:
        """If there is only one file, or no season_episode is provided, return the first file"""
        f: DirectDL = sorted_files[0]
        return StreamLink(name=f.path.split("/")[-1], size=f.size, url=f.link)

    for file in sorted_files:
        path = file.path.split("/")[-1].lower()
        if human.match_season_episode(season_episode=season_episode, file=path):
            log.info("path matches season and episode", path=path, season_episode=season_episode)
            return StreamLink(name=file.path.split("/")[-1], size=file.size, url=file.link)
    log.info("no file found for season and episode", season_episode=season_episode)
    return None


async def get_stream_link(
    magnet_link: str,
    debrid_token: str,
    season_episode: list[int] = [],
) -> StreamLink | None:
    info_hash: str | None = magnet.get_info_hash(magnet_link)
    if not info_hash:
        log.error("magnet is not a valid magnet link", magnet_link=magnet_link)
        return None

    dl, res = await make_request(
        api_token=debrid_token,
        method="POST",
        model=DirectDLResponse,
        url="/transfer/directdl",
        data={"src": magnet_link},
    )
    if res.status not in range(200, 299):
        log.error(
            "failed to lookup cache", info_hash=info_hash, status=res.status, body=await res.text()
        )
        return None

    if not dl.content:
        log.info("magnet has no cached content", info_hash=info_hash)
        return None

    return await select_stream_file(dl.content, season_episode)


async def get_stream_links(
    torrents: list[Torrent],
    debrid_token: str,
    season_episode: list[int],
    max_results: int = 5,
) -> list[StreamLink]:
    """
    Generates a list of stream links for each torrent link.
    """

    def __run(torrent: Torrent) -> Optional[StreamLink]:
        return asyncio.run(
            get_stream_link(
                magnet_link=torrent.url,
                season_episode=season_episode,
                debrid_token=debrid_token,
            )
        )

    with concurrent.futures.ThreadPoolExecutor() as executor:
        results = list(executor.map(__run, torrents))

    links: dict[str, StreamLink] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(__run, torrent) for torrent in torrents]

        for future in as_completed(futures):
            link: StreamLink | None = future.result()
            if link:
                links[link.url] = link
                if len(links.keys()) >= max_results:
                    break
        # cancel the remaining futures
        for future in futures:
            future.cancel()

    return list(links.values())[:max_results]