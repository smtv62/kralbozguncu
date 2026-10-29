"""KralBozguncu spor kaynagi botu.

Akis:
  1) Ana sayfadan ``channels`` (TV kanallari) + ``karsilasmalar`` (maclar) okunur.
  2) Yayin adresi = calisan base url + id + ".m3u8" (site ayni mantigi kullanir).
  3) Her aday GET ile dogrulanir; calismayanlar listeye girmez.
  4) Hic kanal yoksa DOSYA YAZILMAZ ve cikis 1 olur (eski liste korunur).
"""

import re
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests
import urllib3
import warnings

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
warnings.filterwarnings("ignore")

HOME_URL = "https://kralbozguncu.xyz/"
OUTPUT_FILENAME = "kral.m3u"
GROUP = "KralBozguncu"

# Site event.html icinde sirayla denedigi base url'ler (calisan ilk base kullanilir).
BASE_URLS = [
    "https://andro.evrenesoglu219.click/checklist/",
    "https://andro.evrenesoglu211.click/checklist/",
    "https://andro.evrenesoglu215.click/checklist/",
    "https://andro.evrenesoglu217.click/checklist/",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
    ),
    "Referer": HOME_URL,
}

TRT = timezone(timedelta(hours=3))  # site saatleri Turkiye (UTC+3)

# Site kucuk/hatali yazimlar kullanir; gorunen adlari duzelt.
PRETTY_NAMES = {
    "trt 1": "TRT 1",
    "trt spor": "TRT Spor",
    "trt spor yıldız": "TRT Spor Yıldız",
    "atv": "ATV",
    "ht spor": "HT Spor",
    "tjk tv": "TJK TV",
    "nba tv": "NBA TV",
    "tv8": "TV8",
    "tv8,5": "TV8.5",
    "iDMAN tv".lower(): "IDMAN TV",
    "s sport": "S Sport",
    "s sport 2": "S Sport 2",
    "s sport plus": "S Sport Plus",
    "smart spor 1": "Smart Spor 1",
    "smart spor 2": "Smart Spor 2",
    "euro sport 1": "Euro Sport 1",
    "euro sport 2": "Euro Sport 2",
    "cbc sport": "CBC Sport",
    "gs tv": "GS TV",
    "fb tv": "FB TV",
    "sports tv": "Sports TV",
    "exxen tv": "Exxen",
    "tabi spor": "Tabi Spor",
}


def pretty(name: str) -> str:
    return PRETTY_NAMES.get(name.strip().lower(), name.strip())


def fetch_home():
    r = requests.get(HOME_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    return r.text


def fetch_script_blobs(html):
    """Kanal/mac dizileri harici JS dosyalarindadir (script2.js vb.).

    Ana sayfadaki tum same-origin <script src> dosyalarini + inline
    script govdesini tek metinde birlestirir.
    """
    parts = [html]
    srcs = list(dict.fromkeys(re.findall(r'<script[^>]+src=["\']([^"\']+)', html, re.I)))
    for s in srcs:
        if s.startswith("/cdn-cgi") or re.search(r"cloudflare|google|autogram", s, re.I):
            continue
        url = s if s.startswith("http") else HOME_URL.rstrip("/") + "/" + s.lstrip("/")
        if not url.startswith("https://kralbozguncu.xyz"):
            continue
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                # JS content-type'i charset vermez -> requests ISO-8859-1 varsayar;
                # site UTF-8 oldugundan elle zorla (Turkce karakterler bozulmasin).
                r.encoding = "utf-8"
                parts.append(r.text)
                print(f"  JS okundu: {s} ({len(r.text)} bayt)")
        except Exception as e:
            print(f"  ⚠️  JS okunamadi {s}: {e}")
    return "\n".join(parts)


def extract_array(text, name):
    """JS dizisini (ilk eslesme) metinden cikarir."""
    m = re.search(rf"{name}\s*=\s*\[", text)
    if not m:
        return ""
    i = text.find("[", m.start())
    depth, j = 0, i
    while j < len(text):
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                j += 1
                break
        j += 1
    return text[i:j]


def parse_channels(text):
    """const channels = [{ title: "X", url: "/event.html?id=Y" }, ...]"""
    blob = extract_array(text, "channels")
    return [
        (title.strip(), url)
        for title, url in re.findall(r'title:\s*"([^"]+)",\s*url:\s*"([^"]+)"', blob)
    ]


def parse_matches(text):
    """karsilasmalar = [{ "tarih": .., "time": .., "title": .., "url": .., "live": ..}]"""
    blob = extract_array(text, "karsilasmalar")
    out = []
    for m in re.finditer(r"\{(.*?)\}", blob, re.S):
        block = m.group(1)
        def field(key):
            mm = re.search(rf'"{key}"\s*:\s*"([^"]*)"', block)
            return mm.group(1) if mm else ""
        title, url, tarih, saat = field("title"), field("url"), field("tarih"), field("time")
        live = '"live"' in block and re.search(r'"live"\s*:\s*true', block) is not None
        if not title or not url:
            continue
        # Yayin zamani gecmis mac listeye girer (erken saatte kalabalik olmasin).
        started = False
        try:
            start = datetime.strptime(f"{tarih} {saat}", "%Y-%m-%d %H:%M").replace(tzinfo=TRT)
            now = datetime.now(TRT)
            started = start - timedelta(minutes=15) <= now <= start + timedelta(hours=4)
        except Exception:
            pass
        if live or started:
            out.append((title.strip(), url))
    return out


def stream_path(url):
    """Site mantigi: /event.html?id=<id> -> <id>.m3u8 (bs1 gercek yayin)."""
    if "id=" not in url:
        return None
    cid = url.split("id=", 1)[1].split("&")[0].strip()
    if not cid:
        return None
    if cid.startswith(("androstreamlive", "facebooklive")):
        return cid + ".m3u8"
    return None


def pick_working_base(path):
    """Ilk calisan base url'i bulur; hicbiri 2xx vermezse ilkini kullanir."""
    for base in BASE_URLS:
        try:
            r = requests.get(base + path, headers=HEADERS, timeout=12, stream=True)
            if r.status_code == 200:
                chunk = next(r.iter_content(256), b"")
                if chunk.decode("utf-8", "replace").lstrip().startswith("#EXTM3U"):
                    print(f"✅ Aktif base: {base}")
                    return base
        except Exception:
            continue
    print("⚠️  Hiç base 200 vermedi; ilk base ile devam")
    return BASE_URLS[0]


def verify(base, path):
    """Stream GET ile dogrulanir; calisiyorsa (base, tam url) dondurur."""
    url = base + path
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, stream=True)
        if r.status_code != 200:
            return None
        chunk = next(r.iter_content(256), b"")
        first = chunk.decode("utf-8", "replace").lstrip()
        if first.startswith("#EXTM3U"):
            return url
    except Exception:
        return None
    return None


def main():
    print("--- 🏟  KralBozguncu taraniyor ---")
    html = fetch_script_blobs(fetch_home())

    channels = parse_channels(html)
    print(f"✅ {len(channels)} TV kanali bulundu")
    matches = parse_matches(html)
    print(f"✅ {len(matches)} yayindaki mac bulundu" if matches else "➡️  Simdilik yayinda mac yok")

    # Ayni yayin iki ad altinda gelebilir: url'ye gore tekillik.
    entries = []
    seen_urls = set()
    tasks = []
    for title, url in channels + matches:
        path = stream_path(url)
        if not path or path in seen_urls:
            continue
        seen_urls.add(path)
        tasks.append((pretty(title), path))

    if not tasks:
        print("❌ HIC YAYIN YOK - kral.m3u DEGISTIRILMIYOR")
        sys.exit(1)

    base = pick_working_base(tasks[0][1])

    ok = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(verify, base, path): (title, path) for title, path in tasks}
        for fut in as_completed(futures):
            title, path = futures[fut]
            url = fut.result()
            if url:
                entries.append(f'#EXTINF:-1 group-title="{GROUP}", {title}\n{url}')
                ok += 1
                print(f"  ✅ {title}")
            else:
                print(f"  ❌ {title} ({path}): erisilemedi")

    if not entries:
        print("\n❌ HIC CALISAN YAYIN YOK - kral.m3u DEGISTIRILMIYOR")
        sys.exit(1)

    # Kanallar once, maclar sonra; okunabilirlik icin basliga gore sirala.
    with open(OUTPUT_FILENAME, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n" + "\n".join(entries))

    print(f"\n✅ ISLEM TAMAMLANDI!  {ok} yayin  ->  {os.path.abspath(OUTPUT_FILENAME)}")


if __name__ == "__main__":
    main()
