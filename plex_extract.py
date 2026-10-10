#!/usr/bin/env python3
"""
plex_extract.py - dump Plex free live TV channels to playlist.m3u + epg.xml (XMLTV).
No server, no Docker. Only dependency: requests

    pip install requests
    python3 plex_extract.py                      # local region, 3 days of guide
    python3 plex_extract.py --days 7 --gz        # also write epg.xml.gz
    python3 plex_extract.py --region nyc -o out  # other region, custom output dir

Notes:
  * Stream URLs in the M3U embed an anonymous token that expires (~6h per the
    original project), so re-run this at least every few hours.
  * Based on the endpoints used by jgomez177/plex-for-channels.
"""
import argparse
import gzip
import random
import re
import string
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://epg.provider.plex.tv"
TOKEN_URL = "https://clients.plex.tv/api/v2/users/anonymous"
FORWARD = {  # X-Forwarded-For per region (same values the original project uses)
    "local": "",
    "clt": "108.82.206.181",
    "sea": "159.148.218.183",
    "dfw": "76.203.9.148",
    "nyc": "85.254.181.50",
    "la": "76.81.9.69",
}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")
BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def clean(s):
    return BAD_XML.sub("", s) if s else s


def die(r, what):
    sys.exit(f"{what}: HTTP {r.status_code}, Content-Type {r.headers.get('Content-Type')!r}\n"
             f"body starts: {r.text[:300]!r}")


def body_json(r, what):
    try:
        return r.json()
    except ValueError:
        die(r, f"{what} did not return JSON")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="local", choices=FORWARD)
    ap.add_argument("--days", type=int, default=3, help="days of guide data (default 3)")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--gz", action="store_true", help="also write epg.xml.gz")
    ap.add_argument("-o", "--out", default=".", help="output directory")
    a = ap.parse_args()

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    fwd = FORWARD[a.region]
    client_id = "".join(random.choices(string.ascii_lowercase + string.digits, k=24))

    params = {
        "X-Plex-Product": "Plex Web", "X-Plex-Version": "4.145.0",
        "X-Plex-Platform": "Chrome", "X-Plex-Platform-Version": "132.0",
        "X-Plex-Features": "external-media,indirect-media,hub-style-list",
        "X-Plex-Model": "standalone", "X-Plex-Device": "OSX",
        "X-Plex-Device-Screen-Resolution": "1758x627,1920x1080",
        "X-Plex-Provider-Version": "7.2", "X-Plex-Text-Format": "plain",
        "X-Plex-Drm": "widevine", "X-Plex-Language": "en",
        "X-Plex-Client-Identifier": client_id,
    }
    headers = {
        "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://app.plex.tv", "Referer": "https://app.plex.tv/",
        "User-Agent": UA, "x-plex-client-identifier": client_id,
        "Connection": "keep-alive", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "sec-ch-ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
        "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"macOS"',
    }
    if fwd:
        headers["X-Forwarded-For"] = fwd

    # 1. anonymous token
    r = requests.post(TOKEN_URL, params=params, headers=headers, timeout=30)
    if r.status_code not in (200, 201):
        die(r, "token request failed")
    try:
        token = r.json()["authToken"]
    except (ValueError, KeyError):
        m = re.search(r'authToken="([^"]+)"', r.text)  # Plex may answer in XML
        if not m:
            die(r, "token response had no authToken")
        token = m.group(1)
    params["X-Plex-Token"] = token
    print(f"token ok ({a.region})")

    # 2. genres -> channel lineups
    r = requests.get(API + "/", params=params, headers=headers, timeout=60)
    if r.status_code != 200:
        die(r, "genre request failed")
    genres = {}
    for feat in body_json(r, "genre request").get("MediaProvider", {}).get("Feature", []):
        if "GridChannelFilter" in feat:
            genres = {g["identifier"]: g["title"] for g in feat["GridChannelFilter"]}
            break
    print(f"{len(genres)} genres")

    chans = {}  # slug -> channel dict (a channel can appear under several genres)
    for gslug, gtitle in genres.items():
        r = requests.get(f"{API}/lineups/plex/channels", params={**params, "genre": gslug},
                         headers=headers, timeout=60)
        if r.status_code != 200:
            print(f"  skip genre {gslug}: HTTP {r.status_code}", file=sys.stderr)
            continue
        try:
            lineup = r.json()
        except ValueError:
            print(f"  skip genre {gslug}: not JSON ({r.text[:100]!r})", file=sys.stderr)
            continue
        for c in lineup.get("MediaContainer", {}).get("Channel") or []:
            media = c.get("Media", [])
            if any(m.get("drm") for m in media):
                continue
            keys = [p["key"] for m in media for p in m.get("Part", []) if p.get("key")]
            slug = c.get("slug")
            if slug in chans:
                chans[slug]["groups"].append(gtitle)
                continue
            chans[slug] = {
                "id": c.get("id"), "slug": slug, "name": c.get("title") or c.get("callSign") or slug,
                "call_sign": c.get("callSign"), "logo": c.get("thumb"),
                "gridKey": c.get("gridKey"), "key": keys[0] if keys else "", "groups": [gtitle],
            }
    channels = sorted(chans.values(), key=lambda c: c["name"].lower())
    print(f"{len(channels)} channels (DRM skipped)")

    # 3. playlist.m3u
    lines = ["#EXTM3U", ""]
    for c in channels:
        if not c["key"]:
            continue
        info = f'#EXTINF:-1 tvg-id="{c["id"]}"'
        if c["logo"]:
            info += f' tvg-logo="{c["logo"]}"'
        if c["call_sign"]:
            info += f' tvg-name="{clean(c["call_sign"])}"'
        info += f' group-title="{clean(c["groups"][0])}",{clean(c["name"])}'
        lines += [info, f'{API}{c["key"]}?X-Plex-Token={token}', ""]
    (out / "playlist.m3u").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out / 'playlist.m3u'}")

    # 4. epg.xml (XMLTV)
    grid_headers = {
        "x-plex-client-identifier": client_id, "x-plex-platform-version": "132.0",
        "x-plex-provider-version": "7.2", "x-plex-token": token, "x-plex-version": "4.145.1",
    }
    if fwd:
        grid_headers["X-Forwarded-For"] = fwd

    def fetch_grid(job):
        ch, date = job
        for i in range(3):
            try:
                r = requests.get(f"{API}/grid", params={"channelGridKey": ch["gridKey"], "date": date},
                                 headers=grid_headers, timeout=15)
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(2 ** i)
                    continue
                if r.status_code == 200 and "xml" in r.headers.get("Content-Type", "").lower():
                    # strip illegal control chars (e.g. U+0008) that Plex sometimes emits
                    text = clean(r.content.decode("utf-8", "replace"))
                    try:
                        return ch, ET.fromstring(text.encode("utf-8"))
                    except ET.ParseError:
                        return ch, None
                return ch, None
            except requests.RequestException:
                time.sleep(2 ** i)
        return ch, None

    today = datetime.now(timezone.utc)
    dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(a.days)]
    jobs = [(c, d) for c in channels if c["gridKey"] for d in dates]

    tv = ET.Element("tv", {"generator-info-name": "plex_extract"})
    for c in channels:
        ce = ET.SubElement(tv, "channel", {"id": str(c["id"])})
        ET.SubElement(ce, "display-name").text = clean(c["name"])
        if c["logo"]:
            ET.SubElement(ce, "icon", {"src": c["logo"]})

    seen, progs, done = set(), [], 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for ch, root in ex.map(fetch_grid, jobs):
            done += 1
            if done % 100 == 0:
                print(f"  grid {done}/{len(jobs)}")
            if root is None:
                continue
            for v in root.iter("Video"):
                genres_ = [g.get("tag") for g in v.findall("Genre") if g.get("tag")]
                if v.get("type", "").lower() == "movie":
                    title, sub = v.get("title", ""), None
                    if v.get("year") and not any(g.lower() == "news" for g in genres_):
                        title += f' ({v.get("year")})'
                else:
                    title, sub = v.get("grandparentTitle", "Unknown Title"), v.get("title")
                for m in v.findall("Media"):
                    b, e = m.get("beginsAt"), m.get("endsAt")
                    if not (b and e) or (ch["id"], b) in seen:
                        continue
                    seen.add((ch["id"], b))
                    fmt = lambda t: datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")
                    p = ET.Element("programme", {"start": fmt(b), "stop": fmt(e), "channel": str(ch["id"])})
                    ET.SubElement(p, "title").text = clean(title)
                    if sub:
                        ET.SubElement(p, "sub-title").text = clean(sub)
                    ET.SubElement(p, "desc").text = clean(v.get("summary", ""))
                    for g in genres_:
                        ET.SubElement(p, "category").text = clean(g)
                    pi, ix = v.get("parentIndex"), v.get("index")
                    try:
                        if pi and ix:
                            ET.SubElement(p, "episode-num", {"system": "onscreen"}).text = f"S{pi}E{ix}"
                            ET.SubElement(p, "episode-num", {"system": "xmltv_ns"}).text = f"{int(pi)-1}.{int(ix)-1}."
                        elif ix:
                            ET.SubElement(p, "episode-num", {"system": "onscreen"}).text = f"E{ix}"
                    except ValueError:
                        pass
                    r_ = ET.SubElement(p, "rating")
                    ET.SubElement(r_, "value").text = clean(v.get("contentRating", "NR"))
                    progs.append((int(b), str(ch["id"]), p))

    progs.sort(key=lambda t: (t[1], t[0]))
    tv.extend(p for _, _, p in progs)
    ET.ElementTree(tv).write(out / "epg.xml", encoding="utf-8", xml_declaration=True)
    print(f"wrote {out / 'epg.xml'} ({len(progs)} programmes)")
    if a.gz:
        with open(out / "epg.xml", "rb") as f, gzip.open(out / "epg.xml.gz", "wb") as g:
            g.write(f.read())
        print(f"wrote {out / 'epg.xml.gz'}")


if __name__ == "__main__":
    main()
