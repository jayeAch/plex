#!/usr/bin/env python3
"""
plex_extract.py - dump Plex free live TV channels to playlist.m3u + epg.xml (XMLTV).
No server, no Docker. Only dependency: requests

    pip install requests
    python3 plex_extract.py                      # local region, next 3 days of guide
    python3 plex_extract.py --days 1 --gz        # next 24h, also write epg.xml.gz
    python3 plex_extract.py --region nyc -o out  # other region, custom output dir

Notes:
  * --days is a rolling window from now (now .. now + days*24h), not calendar days.
  * Exits non-zero (writing nothing) if too much of the fetch failed, so a CI job
    never overwrites a good epg with a partial one.
  * Stream URLs in the M3U embed an anonymous token that expires (~6h).
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
RETRY_STATUS = (429, 500, 502, 503, 504)
MIN_GRID_OK = 0.98      # fraction of grid requests that must succeed
MIN_CH_WITH_EPG = 0.90  # fraction of channels that must have >= 1 programme


def clean(s):
    return BAD_XML.sub("", s) if s else s


def die(r, what):
    if r is None:
        sys.exit(f"{what}: no response (network error / timeout)")
    sys.exit(f"{what}: HTTP {r.status_code}, Content-Type {r.headers.get('Content-Type')!r}\n"
             f"body starts: {r.text[:300]!r}")


def req(method, url, *, want_json=False, want_xml=False, tries=5, timeout=30, **kw):
    """requests with backoff. Retries network errors, 429/5xx, and (optionally) bodies
    that aren't JSON/XML. Returns the last response (or None if there never was one)."""
    last = None
    for i in range(tries):
        try:
            r = requests.request(method, url, timeout=timeout, **kw)
            last = r
            if r.status_code not in RETRY_STATUS:
                if r.status_code != 200:
                    return r
                if want_json:
                    try:
                        r.json()
                        return r
                    except ValueError:
                        pass
                elif want_xml:
                    if "xml" in r.headers.get("Content-Type", "").lower():
                        return r
                else:
                    return r
        except requests.RequestException:
            pass
        time.sleep(min(2 ** i, 20) + random.random())
    return last


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default="local", choices=FORWARD)
    ap.add_argument("--days", type=int, default=3, help="rolling window of guide data from now (default 3)")
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
    r = req("POST", TOKEN_URL, params=params, headers=headers)
    if r is None or r.status_code not in (200, 201):
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
    r = req("GET", API + "/", params=params, headers=headers, want_json=True)
    if r is None or r.status_code != 200:
        die(r, "genre request failed")
    genres = {}
    for feat in r.json().get("MediaProvider", {}).get("Feature", []):
        if "GridChannelFilter" in feat:
            genres = {g["identifier"]: g["title"] for g in feat["GridChannelFilter"]}
            break
    if not genres:
        sys.exit("no genres returned; refusing to continue")
    print(f"{len(genres)} genres")

    chans = {}  # slug -> channel dict (a channel can appear under several genres)
    bad_genres = []
    for gslug, gtitle in genres.items():
        r = req("GET", f"{API}/lineups/plex/channels", params={**params, "genre": gslug},
                headers=headers, want_json=True)
        if r is None or r.status_code != 200:
            bad_genres.append(gslug)
            print(f"  genre {gslug} FAILED: {'no response' if r is None else r.status_code}", file=sys.stderr)
            continue
        for c in r.json().get("MediaContainer", {}).get("Channel") or []:
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
    if bad_genres:  # a missing genre silently drops channels; better to fail and keep the old files
        sys.exit(f"{len(bad_genres)} genre lineups failed: {bad_genres}")
    channels = sorted(chans.values(), key=lambda c: c["name"].lower())
    print(f"{len(channels)} channels (DRM skipped)")

    # 3. EPG first (so a bad run writes nothing at all)
    grid_headers = {
        "x-plex-client-identifier": client_id, "x-plex-platform-version": "132.0",
        "x-plex-provider-version": "7.2", "x-plex-token": token, "x-plex-version": "4.145.1",
    }
    if fwd:
        grid_headers["X-Forwarded-For"] = fwd

    def fetch_grid(job):
        ch, date = job
        r = req("GET", f"{API}/grid", params={"channelGridKey": ch["gridKey"], "date": date},
                headers=grid_headers, want_xml=True, tries=4, timeout=20)
        if r is None or r.status_code != 200:
            return ch, date, None, ("no response" if r is None else f"HTTP {r.status_code}")
        # strip illegal control chars (e.g. U+0008) that Plex sometimes emits
        text = clean(r.content.decode("utf-8", "replace"))
        try:
            return ch, date, ET.fromstring(text.encode("utf-8")), None
        except ET.ParseError:
            return ch, date, None, "XML parse error"

    now = datetime.now(timezone.utc)
    end = now + timedelta(days=a.days)
    now_ts, end_ts = int(now.timestamp()), int(end.timestamp())
    dates, d = [], now.date()
    while d <= end.date():
        dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    jobs = [(c, d) for c in channels if c["gridKey"] for d in dates]
    print(f"window {now:%F %H:%M}Z -> {end:%F %H:%M}Z, dates {dates}, {len(jobs)} grid requests")

    results, failed = [], []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for n, res in enumerate(ex.map(fetch_grid, jobs), 1):
            (results if res[2] is not None else failed).append(res)
            if n % 100 == 0:
                print(f"  grid {n}/{len(jobs)} ({len(failed)} failed)")

    if failed:  # slow second pass: fewer workers, after letting rate limits cool off
        print(f"retrying {len(failed)} failed grid requests slowly")
        time.sleep(15)
        retry_jobs = [(ch, d) for ch, d, _, _ in failed]
        failed = []
        with ThreadPoolExecutor(max_workers=2) as ex:
            for res in ex.map(fetch_grid, retry_jobs):
                (results if res[2] is not None else failed).append(res)
    ok_ratio = len(results) / len(jobs) if jobs else 0
    reasons = {}
    for _, _, _, why in failed:
        reasons[why] = reasons.get(why, 0) + 1
    print(f"grid ok {len(results)}/{len(jobs)} ({ok_ratio:.1%}); failures: {reasons or 'none'}")

    fmt = lambda t: datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")
    seen, progs = set(), []
    for ch, _, root, _ in results:
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
                try:
                    bi, ei = int(b), int(e)
                except ValueError:
                    continue
                if ei <= now_ts or bi >= end_ts:  # outside the rolling window
                    continue
                seen.add((ch["id"], b))
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
                progs.append((bi, str(ch["id"]), p))

    with_epg = {cid for _, cid, _ in progs}
    ch_ratio = len(with_epg) / len(channels) if channels else 0
    print(f"{len(progs)} programmes, {len(with_epg)}/{len(channels)} channels have guide data ({ch_ratio:.1%})")
    if ok_ratio < MIN_GRID_OK or ch_ratio < MIN_CH_WITH_EPG or not progs:
        sys.exit("too much of the fetch failed; NOT writing output (keeping previous files)")

    # 4. write everything only after the checks pass
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

    tv = ET.Element("tv", {"generator-info-name": "plex_extract"})
    for c in channels:
        ce = ET.SubElement(tv, "channel", {"id": str(c["id"])})
        ET.SubElement(ce, "display-name").text = clean(c["name"])
        if c["logo"]:
            ET.SubElement(ce, "icon", {"src": c["logo"]})
    progs.sort(key=lambda t: (t[1], t[0]))
    tv.extend(p for _, _, p in progs)
    ET.ElementTree(tv).write(out / "epg.xml", encoding="utf-8", xml_declaration=True)
    print(f"wrote {out / 'epg.xml'}")
    if a.gz:
        with open(out / "epg.xml", "rb") as f, gzip.open(out / "epg.xml.gz", "wb") as g:
            g.write(f.read())
        print(f"wrote {out / 'epg.xml.gz'}")


if __name__ == "__main__":
    main()
