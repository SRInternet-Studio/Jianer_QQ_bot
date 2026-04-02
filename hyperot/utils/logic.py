import json
import os
import shutil
import asyncio
import aiohttp
import time
import requests
import inspect
from functools import wraps

from .. import hyperogger
from .hypetyping import Any, Union


class Cacher:
    def __init__(self, cache_time: int = 5):
        self.cache_time: int = cache_time
        self.cached: dict = {}

    def cache(self, func):
        def wrapper(*args, **kwargs):
            if kwargs.get("no_cache", False):
                kwargs.pop("no_cache")
                return func(*args, **kwargs)
            if f"{str(args)}{str(kwargs)}" not in list(self.cached.keys()):
                ret = func(*args, **kwargs)
                self.cached[f"{str(args)}{str(kwargs)}"] = ret
                if len(self.cached) >= self.cache_time:
                    for i in self.cached:
                        del self.cached[i]
                        break
                return ret
            else:
                return self.cached[f"{str(args)}{str(kwargs)}"]

        return wrapper

    def cache_async(self, func):
        async def wrapper(*args, **kwargs):
            if kwargs.get("no_cache", False):
                kwargs.pop("no_cache")
                return await func(*args, **kwargs)
            if f"{str(args)}{str(kwargs)}" not in list(self.cached.keys()):
                ret = await func(*args, **kwargs)
                self.cached[f"{str(args)}{str(kwargs)}"] = ret
                if len(self.cached) >= self.cache_time:
                    for i in self.cached:
                        del self.cached[i]
                        break
                return ret
            else:
                return self.cached[f"{str(args)}{str(kwargs)}"]

        return wrapper


class ErrorHandler:
    def __init__(self, level=hyperogger.levels.ERROR, retries: int = 0):
        self.level = level
        self.logger = hyperogger.Logger()
        self.retries = retries

    def handle(self, func):
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except:
                self.logger.log(self.logger.format_exec(), level=self.level)

        return wrapper

    def handle_async(self, func):
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except:
                self.logger.log(self.logger.format_exec(), level=self.level)

        return wrapper

    def auto_retry(self, func):
        def wrapper(*args, **kwargs):
            retried = 0
            while retried < self.retries:
                retried += 1
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    self.logger.log(f"出现错误：\n{str(e)}, 正在重试...", level=self.level)
            self.logger.log(f"错误在{retried}次重试后失败", level=self.level)

        return wrapper

    def auto_retry_async(self, func):
        async def wrapper(*args, **kwargs):
            retried = 0
            while retried < self.retries:
                retried += 1
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    self.logger.log(f"出现错误：\n{str(e)}, 正在重试...", level=self.level)
            self.logger.log(f"错误在{retried}次重试后失败", level=self.level)

        return wrapper


class ProgressBar:
    def __init__(self, p_iter,
                 total: int = 0, num_per_time: int = 1,
                 desc: str = "", on_done: str = "Done",
                 empty: str = "\033[38;5;243m━\033[0m", full: str = "\033[31m━\033[0m", done: str = "\033[32m━\033[0m"):
        self.iter = p_iter
        self.total = total
        self.num_per_time = num_per_time
        self.desc = desc
        self.start_time = 0
        self.empty = empty
        self.full = full
        self.done = done
        self.on_done = on_done

    def __update(self, progress: int):
        time_spent = time.time() - self.start_time
        if self.total == 0:
            progress_percents = ""
            eta = "unknown"
            speed = "unknown"
        else:
            progress_percents = str(float(format(progress / self.total, ".3f")) * 100)[:5]
            if time_spent == 0:
                speed = 1
            else:
                speed = progress / time_spent
            eta = str(format((self.total - progress) / speed, ".2f"))[:5]

        if float(progress_percents) >= 100.0:
            progress_percents = "100.0"
            bar = f"|{self.done * 25}|"
        else:
            bar = f"|{self.full * int(float(progress_percents) * 0.25)}{self.empty * (25 - int(float(progress_percents) * 0.25))}|"
        if speed == "unknown":
            speed_text = ""
        elif speed > 100:
            speed_text = f"\033[36m{int(speed)}\033[0m it/s "
        elif 100 >= speed >= 50:
            speed_text = f"\033[32m{int(speed)}\033[0m it/s "
        elif 50 > speed >= 20:
            speed_text = f"\033[33m{int(speed)}\033[0m it/s "
        else:
            speed_text = f"\033[31m{int(speed)}\033[0m it/s "
        print(
            f"\r{self.desc}: {bar} {progress_percents}% [{speed_text}\033[31m{int(time_spent)}s\033[0m spent, \033[36m{eta}s\033[0m eta]",
            end="")

    def __iter__(self):
        self.start_time = time.time()
        progress = 0
        for i in self.iter:
            progress += self.num_per_time
            yield i
            self.__update(progress)
        print(f"\n{self.on_done}")

    async def __aiter__(self):
        self.start_time = time.time()
        progress = 0
        async for i in self.iter:
            progress += self.num_per_time
            yield i
            self.__update(progress)
        print(f"\n{self.on_done}")


class TempDownloader:
    def __init__(self, url: str, part: list[int], index: int, silent: bool = False):
        self.url = url
        self.part = part
        self.index = index
        self.silent = silent

    async def get_bytes(self, ua: str = None) -> dict[str, bytes]:
        result_to_re = {}
        this: bytes = bytes()
        headers = {"Range": f"bytes={self.part[0]}-{self.part[1]}"}
        if ua:
            headers["User-Agent"] = ua

        async with aiohttp.ClientSession() as session:
            async with session.get(self.url, headers=headers) as resp:
                if not self.silent:
                    async for chunk in ProgressBar(resp.content.iter_chunked(1024 * 64),
                                                   total=self.part[1] - self.part[0],
                                                   num_per_time=1024 * 64,
                                                   desc=f"In thread {self.index}"
                                                   ):
                        this += chunk
                else:
                    async for chunk in resp.content.iter_chunked(1024 * 64):
                        this += chunk

        result_to_re[str(self.index)] = this
        return result_to_re


class Downloader:
    def __init__(self, url: str, path_to: str, threads: int = 1, silent: bool = False):
        self.url = url
        self.path = path_to
        self.threads = threads
        self.silent = silent

    def check(self) -> bool:
        if not isinstance(self.threads, int) or not self.threads >= 1:
            return False

        return True

    def __get_file_size(self) -> int:
        response = requests.head(self.url)
        size = response.headers["Content-Length"]
        return int(size)

    def __get_parts(self) -> list[list[int]]:
        size = self.__get_file_size()
        step = size // self.threads
        arr = list(range(0, size, step))
        result: list = []
        for i in range(len(arr) - 1):
            s_pos, e_pos = arr[i], arr[i + 1] - 1
            result.append([s_pos, e_pos])
        result[-1][-1] = size - 1
        return result

    async def download(self, ua: str = None):
        if not self.check():
            raise Exception
        if ua:
            headers = {
                "User-Agent": ua
            }
            response = requests.get(self.url, stream=True, verify=False, headers=headers)
        else:
            response = requests.get(self.url, stream=True, verify=False)
        if response.status_code != 200:
            raise ConnectionError

        if self.threads == 1:
            with open(self.path, "wb") as f:
                if not self.silent:
                    for chunk in ProgressBar(response.iter_content(chunk_size=1024 * 64),
                                             total=int(response.headers.get("Content-Length")),
                                             num_per_time=1024 * 64
                                             ):
                        if chunk:
                            f.write(chunk)
                else:
                    for chunk in response.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
        else:
            with open(self.path, "wb"):
                pass
            with open(self.path, "rb+") as f:
                parts = self.__get_parts()
                result = {}
                tasks = []
                for i in parts:
                    _ = TempDownloader(url=self.url, part=i, index=parts.index(i), silent=self.silent)

                    tasks.append(asyncio.create_task(_.get_bytes(ua=ua)))

                await asyncio.gather(*tasks)
                for i in tasks:
                    result[list(i.result().keys())[0]] = i.result()[list(i.result().keys())[0]]
                r: bytes = bytes()
                for i in range(0, len(result)):
                    r += result[str(i)]
                f.write(r)


class KeyQueue:
    def __init__(self):
        self.contents = {}

    def put(self, key: str, obj: Any) -> None:
        if key in list(self.contents.keys()):
            return
        self.contents[key] = obj

    def get(self, key: str) -> Any:
        while 1:
            try:
                return self.contents[key]
            except KeyError:
                pass


class SimpleQueue:
    def __init__(self):
        self.contents = []

    def put(self, obj: Any) -> None:
        self.contents.append(obj)

    def get(self) -> Any:
        while 1:
            try:
                return self.contents.pop(0)
            except IndexError:
                pass

class Matcher:
    def __init__(self, target: Any):
        self.target = target

    def match(self, var: Any) -> bool:
        return var == self.target

