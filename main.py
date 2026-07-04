import asyncio
import base64
import datetime
import glob
import io
import json
import os
import ssl
import zipfile
from pathlib import Path

import aiohttp
import certifi

import decky  # type: ignore
from settings import SettingsManager  # type: ignore


class Plugin:
    yt_process: asyncio.subprocess.Process | None = None
    # We need this lock to make sure the process output isn't read by two concurrent readers at once.
    yt_process_lock = asyncio.Lock()
    music_path = f"{decky.DECKY_PLUGIN_RUNTIME_DIR}/music"
    cache_path = f"{decky.DECKY_PLUGIN_RUNTIME_DIR}/cache"
    ssl_context = ssl.create_default_context(cafile=certifi.where())

    async def _main(self):
        self.settings = SettingsManager(
            name="config", settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR
        )
        asyncio.ensure_future(self._ensure_binaries())

    async def _ensure_binaries(self):
        bin_dir = Path(f"{decky.DECKY_PLUGIN_DIR}/bin")
        bin_dir.mkdir(exist_ok=True)

        yt_dlp = bin_dir / "yt-dlp"
        if not yt_dlp.exists():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp",
                        ssl=self.ssl_context
                    ) as resp:
                        resp.raise_for_status()
                        with open(yt_dlp, "wb") as f:
                            async for chunk in resp.content.iter_chunked(65536):
                                f.write(chunk)
                yt_dlp.chmod(0o755)
            except Exception:
                pass

        deno = bin_dir / "deno"
        if not deno.exists():
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip",
                        ssl=self.ssl_context
                    ) as resp:
                        resp.raise_for_status()
                        data = await resp.read()
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    z.extract("deno", bin_dir)
                deno.chmod(0o755)
            except Exception:
                pass

    async def _unload(self):
        # Add a check to make sure the process is still running before trying to terminate to avoid ProcessLookupError
        if self.yt_process is not None and self.yt_process.returncode is None:
            self.yt_process.terminate()
            # Wait for process to terminate.
            async with self.yt_process_lock:
                try:
                    # Allow up to 5 seconds for termination.
                    await asyncio.wait_for(self.yt_process.communicate(), timeout=5)
                except TimeoutError:
                    # Otherwise, send SIGKILL.
                    self.yt_process.kill()

    async def set_setting(self, key, value):
        self.settings.setSetting(key, value)

    async def get_setting(self, key, default):
        return self.settings.getSetting(key, default)

    async def search_yt(self, term: str):
        # Make sure the yt-dlp binary is executable
        try:
            path = Path(f"{decky.DECKY_PLUGIN_DIR}/bin/yt-dlp")
            path.chmod(0o755) if path.exists() else None
        except:
            exit(1)

        # Add a check to make sure the process is still running before trying to terminate to avoid ProcessLookupError
        if self.yt_process is not None and self.yt_process.returncode is None:
            self.yt_process.terminate()
            # Wait for process to terminate.
            async with self.yt_process_lock:
                await self.yt_process.communicate()
        self.yt_process = await asyncio.create_subprocess_exec(
            f"{decky.DECKY_PLUGIN_DIR}/bin/yt-dlp",
            f"ytsearch10:{term}",
            "-j",
            "-f",
            "bestaudio",
            "--match-filters",
            f"duration<?{20*60}",  # 20 minutes is too long.
            stdout=asyncio.subprocess.PIPE,
            # The returned JSON can get rather big, so we set a generous limit of 10 MB.
            limit=10 * 1024**2,
            env={**os.environ, "LD_LIBRARY_PATH": "/usr/lib:/usr/lib64:/lib:/lib64", "PATH": f"{decky.DECKY_PLUGIN_DIR}/bin:" + os.environ.get("PATH", "/usr/bin:/bin")},
        )

    async def next_yt_result(self):
        async with self.yt_process_lock:
            if (
                not self.yt_process
                or not (output := self.yt_process.stdout)
                or not (line := (await output.readline()).strip())
            ):
                return None
            entry = json.loads(line)
            return self.entry_to_info(entry)

    @staticmethod
    def entry_to_info(entry):
        return {
            "url": entry["url"],
            "title": entry["title"],
            "id": entry["id"],
            "thumbnail": entry["thumbnail"],
        }

    def local_match(self, id: str) -> str | None:
        local_matches = [
            x for x in glob.glob(f"{self.music_path}/{id}.*") if os.path.isfile(x)
        ]
        if len(local_matches) == 0:
            return None

        assert (
            len(local_matches) == 1
        ), "More than one downloaded audio with same ID found."
        return local_matches[0]

    async def single_yt_url(self, id: str):
        # Download first if not available locally — streaming URLs from yt-dlp
        # fail to play in Steam's CEF context, but base64 data URLs work reliably.
        local_match = self.local_match(id)
        if local_match is None:
            await self.download_yt_audio(id)
            local_match = self.local_match(id)
        if local_match is not None:
            extension = local_match.split(".")[-1]
            with open(local_match, "rb") as file:
                return f"data:audio/{extension};base64,{base64.b64encode(file.read()).decode()}"
        return None

    async def download_yt_audio(self, id: str):
        if self.local_match(id) is not None:
            # Already downloaded—there's nothing we need to do.
            return
        process = await asyncio.create_subprocess_exec(
            f"{decky.DECKY_PLUGIN_DIR}/bin/yt-dlp",
            f"{id}",
            "-f",
            "bestaudio",
            "-o",
            "%(id)s.%(ext)s",
            "-P",
            self.music_path,
            env={**os.environ, "LD_LIBRARY_PATH": "/usr/lib:/usr/lib64:/lib:/lib64", "PATH": f"{decky.DECKY_PLUGIN_DIR}/bin:" + os.environ.get("PATH", "/usr/bin:/bin")},
        )
        await process.communicate()

        # Simple fix to make any lingering m4a files usable. Does nothing if fails.
        music_path = Path(self.music_path)
        try:
            (f"{music_path}/{id}.m4a").rename(f"{music_path}/{id}.webm")
        except:
            pass

    async def download_url(self, url: str, id: str):
        async with aiohttp.ClientSession() as session:
            res = await session.get(url, ssl=self.ssl_context)
            res.raise_for_status()
            with open(f"{self.music_path}/{id}.webm", "wb") as file:
                async for chunk in res.content.iter_chunked(1024):
                    file.write(chunk)

    async def clear_downloads(self):
        for file in glob.glob(f"{self.music_path}/*"):
            if os.path.isfile(file):
                os.remove(file)

    async def export_cache(self, cache: dict):
        os.makedirs(self.cache_path, exist_ok=True)
        filename = f"backup-{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}.json"
        with open(f"{self.cache_path}/{filename}", "w") as file:
            json.dump(cache, file)

    async def list_cache_backups(self):
        return [
            file.split("/")[-1].rsplit(".", 1)[0]
            for file in glob.glob(f"{self.cache_path}/*")
        ]

    async def import_cache(self, name: str):
        with open(f"{self.cache_path}/{name}.json", "r") as file:
            return json.load(file)

    async def clear_cache(self):
        for file in glob.glob(f"{self.cache_path}/*"):
            if os.path.isfile(file):
                os.remove(file)
