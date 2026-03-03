import base64
import time
import httpx
import json
import urllib
from urllib import parse

from ..listener import Actions
from ..common import Ret

def _uid_and_uid_key(uin, pskey):
    url = "https://docs.qq.com/api/user/qq/login"

    headers = {
        'User-Agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) QQ/9.9.15-27187 Chrome/126.0.6478.36 Electron/31.0.1 Safari/537.36 OS/win32,x64,10.0.22631,Windows 11 Home China",
        'Accept': "application/json, text/plain, */*",
        'referer': "https://docs.qq.com",
        'Cookie': f"p_uin={uin}; uin={uin}; p_skey={pskey}"
    }

    response = httpx.get(url, headers=headers, verify=False).json()

    if response["retcode"] == 0:
        return response["result"]
    else:
        return response["msg"]


def _music_card(uid, uidkey, uin, pskey, data):
    title = data["title"]
    desc = data["desc"]
    jumpUrl = data["jumpUrl"]
    musicUrl = data["musicUrl"]
    source_icon = data["source_icon"]
    tag = data["tag"]
    preview = data["preview"]

    ark = {
        "app": "com.tencent.tdoc.qqpush",
        "config": {
            "ctime": int(time.time()), "forward": 1, "token": "", "type": "normal"
        },
        "extra": {
            "app_type": 1, "appid": 0, "uin": uin
        },
        "meta": {
            "music": {
                "action": "",
                "android_pkg_name": "",
                "app_type": 1,
                "appid": 0,
                "ctime": int(time.time()),
                "desc": desc,
                "jumpUrl": jumpUrl,
                "musicUrl": musicUrl,
                "preview": preview,
                "sourceMsgId": "0",
                "source_icon": source_icon,
                "source_url": "",
                "tag": tag,
                "title": title,
                "uin": uin
            }
        },
        "prompt": f"[分享]{title}", "ver": "0.0.0.1", "view": "music"
    }
    return _any_card(uid, uidkey, uin, pskey, ark)


def _any_card(uid: str, uidkey: str, uin: int, pskey: str, ark: dict):
    url = "https://docs.qq.com/v2/push/ark_sig"

    payload = {
        "ark": "",
        "object_id": "YjONkUwkdtFr"  # 请不要动人家qaq
    }

    payload["ark"] = json.dumps(ark)
    payload = json.dumps(payload)

    headers = {
        'User-Agent': "",
        'Content-Type': "application/json",
        'Cookie': f"uid={uid}; uid_key={uidkey}; p_uin={uin}; p_skey={pskey}"
    }

    # noinspection PyTypeChecker
    response = httpx.post(url, data=payload, headers=headers, verify=False)

    return response.json()


def gtk(skey):
    t = 5381
    for i in skey:
        t += (t << 5) + ord(i)
    return t & 2147483647


async def get_pic(actions: Actions, uin: int, path: str) -> str:
    echo = await actions.custom.get_cookies(domain="qun.qq.com")
    pskey = Ret.fetch(echo).data.cookies
    skey = "@"
    url = "https://qun.qq.com/cgi-bin/hw/util/image"

    with open(path, "rb") as image_file:
        base = base64.b64encode(image_file.read()).decode('utf-8')
    base = urllib.parse.quote(base)

    payload = f"pic={base}&client_type=1&bkn=" + str(gtk(skey))

    headers = {
        'Content-Type': "application/x-www-form-urlencoded",
        'Referer': "https://qun.qq.com/homework/p/features/index.html",
        'Cookie': f"p_uin={uin}; p_skey={pskey}; skey={skey}; uin={uin}"
    }

    # noinspection PyTypeChecker
    response = httpx.post(url, data=payload, headers=headers, verify=False).json()
    if response["retcode"] == 0:
        return response["data"]["url"]["origin"].replace("p.qpic.cn", "p.qlogo.cn")
    else:
        return response["msg"]


class Card:
    def __init__(self, title: str, desc: str, jump_url: str, music_url: str, source_icon: str, tag: str, preview: str):
        self.title = title
        self.desc = desc
        self.jump_url = jump_url
        self.music_url = music_url
        self.source_icon = source_icon
        self.tag = tag
        self.preview = preview
        self.is_m = True
        self.raw = {}

    @classmethod
    def any(cls, ark: dict):
        res = cls("", "", "", "", "", "", "")
        res.is_m = False
        res.raw = ark
        return res

    def get_raw(self) -> dict:
        if self.is_m:
            return {
                "title": self.title,
                "desc": self.desc,
                "jumpUrl": self.jump_url,
                "musicUrl": self.music_url,
                "source_icon": self.source_icon,
                "tag": self.tag,
                "preview": self.preview
            }
        else:
            return self.raw

    async def get_sig(self, actions: Actions, uin: int) -> str:
        echo = await actions.custom.get_cookies(domain="docs.qq.com")
        p_skey = Ret.fetch(echo).data.cookies
        data = _uid_and_uid_key(uin, p_skey)
        if self.is_m:
            return (
                _music_card(data["uid"], data["uid_key"], uin, p_skey, self.get_raw())
                .get("result")
                .get("ark_with_sig")
            )
        else:
            return (
                _any_card(data["uid"], data["uid_key"], uin, p_skey, self.get_raw())
                .get("result")
                .get("ark_with_sig")
            )
