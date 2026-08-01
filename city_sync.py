# -*- coding: utf-8 -*-
"""
熊本市動物愛護センター 掲載情報の自動取込(くまもとペット捜索マップ用)
- 対象: 「保護しています」(犬/猫/他) と「飼い主さんが探しています」(犬/猫/他) の6一覧
- 取り込むのは事実情報のみ: 種別・日付・場所・タイトル + 元記事URL(出典リンク)
- 写真は既定で取り込まない(--with-photos で有効化。センター許諾後に使用)
- 実行: python3 city_sync.py [--dry-run] [--with-photos]
- 推奨頻度: 災害時 2〜3時間おき / 平時 1日1回(cron / GitHub Actions / タスクスケジューラ)
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://www.city.kumamoto.jp"
LISTS = [
    # (URL, kind, species)
    ("/doubutuaigo/list03612.html", "found", "dog"),    # 迷子犬を保護しています
    ("/doubutuaigo/list03615.html", "found", "cat"),    # 迷子猫を保護しています
    ("/doubutuaigo/list03632.html", "found", "other"),  # 犬猫以外を保護しています
    ("/doubutuaigo/list03635.html", "lost", "dog"),     # 不明犬
    ("/doubutuaigo/list03636.html", "lost", "cat"),     # 不明猫
    ("/doubutuaigo/list03637.html", "lost", "other"),   # 不明動物(犬・猫以外)
]
UA = "KumamotoPetSearchMap/1.0 (disaster pet reunion; contact: info@crk.jp)"
FETCH_INTERVAL = 2.0  # 秒。市サイトに負荷をかけない

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "app.db")

JST = timezone(timedelta(hours=9))

# 区名→おおよその中心座標(記事は町名までなので区レベルのピン。ページ上で「おおよその位置」と明示)
WARD_COORDS = {
    "中央区": (32.8032, 130.7079),
    "東区":   (32.7900, 130.7750),
    "西区":   (32.7920, 130.6650),
    "南区":   (32.7380, 130.6900),
    "北区":   (32.8700, 130.7200),
    "御船町": (32.7150, 130.8020),
    "益城町": (32.7920, 130.8180),
    "菊陽町": (32.8630, 130.8280),
    "合志市": (32.8860, 130.7890),
    "宇土市": (32.6870, 130.6590),
    "嘉島町": (32.7440, 130.7530),
}
DEFAULT_COORD = (32.8032, 130.7079)  # 不明時は熊本市中心


def fetch(url):
    req = urllib.request.Request(BASE + url if url.startswith("/") else url,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.read().decode("utf-8", errors="replace")


def strip_tags(html):
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.I)
    html = re.sub(r"<[^>]+>", "", html)
    return re.sub(r"[ \t\u3000]+", " ", html)


def parse_list(html):
    """一覧ページから記事(kiji)リンクとタイトルを抽出"""
    items = []
    for m in re.finditer(
            r'href="(?:https?://www\.city\.kumamoto\.jp)?(/doubutuaigo/(kiji\d+)/index\.html)"[^>]*>(.*?)</a>',
            html, re.S):
        title = strip_tags(m.group(3)).strip()
        if not title or "もっと見る" in title:
            continue
        items.append({"url": m.group(1), "kiji_id": m.group(2), "title": title[:80]})
    # 重複除去(サイドメニュー等で同一リンクが複数回出る)
    seen, out = set(), []
    for it in items:
        if it["kiji_id"] in seen:
            continue
        seen.add(it["kiji_id"])
        out.append(it)
    return out


def parse_article(html):
    """記事ページから 日付・場所・本文 を抽出(事実情報のみ)"""
    text = strip_tags(html)
    d = {}
    m = re.search(r"(?:不明日|保護日|収容日)[::]?\s*([^\n]{1,40})", text)
    if m:
        d["event_date"] = m.group(1).strip()
    m = re.search(r"場\s*所[::]?\s*([^\n]{1,60})", text)
    if m:
        d["place"] = m.group(1).strip()
    # 特徴っぽい行(毛色・種類など)を控えめに拾う
    feats = []
    KEYS = ("種類", "毛色", "性別", "首輪", "体格", "推定年齢", "特徴")
    stop = r"(?=\s*(?:" + "|".join(KEYS) + r"|不明日|保護日|収容日|場\s*所)[::]|\n|$)"
    for key in KEYS:
        m = re.search(key + r"[::]\s*(.{1,50}?)" + stop, text)
        if m and m.group(1).strip():
            feats.append(key + ": " + m.group(1).strip())
    d["features"] = " / ".join(feats)[:300]
    # 写真URL: 記事フォルダ内の実写真を最優先。次にダイジェスト画像(UploadFileOutput)。
    # 市の共通ロゴ・OGP既定画像(og_image等)は絶対に採用しない
    photos = re.findall(r'src="([^"]*kiji\d+[^"]*\.(?:jpe?g|png|gif))"', html, re.I)
    m = re.search(r'property="og:image"\s+content="([^"]+)"', html) or \
        re.search(r'content="([^"]+)"\s+property="og:image"', html)
    if m and "UploadFileOutput" in m.group(1):
        photos.append(m.group(1))
    BAD = ("og_image", "/ogp/", "/common/images/", "loading", "newwin", "btnplus", "footer_title")
    d["photos"] = [p for p in photos if not any(b in p for b in BAD)][:4]
    return d


def guess_coord(place):
    if place:
        for name, ll in WARD_COORDS.items():
            if name in place:
                return ll
    return DEFAULT_COORD


def parse_event_at(s, fallback_year=None):
    """「2026年7月17日」「7月27日」等をISOに"""
    if not s:
        return ""
    y = fallback_year or datetime.now(JST).year
    m = re.search(r"(?:(\d{4})年)?\s*(\d{1,2})月\s*(\d{1,2})日", s)
    if not m:
        return ""
    year = int(m.group(1)) if m.group(1) else y
    try:
        return f"{year:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}T12:00"
    except ValueError:
        return ""


def photo_mode():
    # 0=写真なし / 1=市サイト画像の直リンク表示(既定・複製しない) / 2=保存(センター許諾後のみ)
    return os.environ.get("CITY_PHOTOS", "1").strip()


def norm_url(p, kiji_id=""):
    p = p.replace("&amp;", "&").strip()
    if p.startswith("//"):
        p = "https:" + p
    elif p.startswith("/"):
        p = BASE + p
    elif not p.startswith("http"):
        p = BASE + "/doubutuaigo/" + kiji_id + "/" + p if kiji_id else ""
    return p if p.startswith(BASE) else ""


def first_photo_url(art, kiji_id=""):
    for p in art.get("photos", []):
        url = norm_url(p, kiji_id)
        if url:
            return url
    return ""


def photo_looks_valid(url, kiji_id):
    # 記事自身の画像(kijiフォルダ or UploadFileOutput)以外は無効。市ロゴ等の共通画像も無効
    if not url:
        return False
    if any(b in url for b in ("og_image", "/ogp/", "/common/images/")):
        return False
    return kiji_id in url or "UploadFileOutput" in url


def ensure_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pets)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE pets ADD COLUMN source TEXT DEFAULT ''")
    if "source_url" not in cols:
        conn.execute("ALTER TABLE pets ADD COLUMN source_url TEXT DEFAULT ''")
    if "photo_ext" not in cols:
        conn.execute("ALTER TABLE pets ADD COLUMN photo_ext TEXT DEFAULT ''")
    conn.commit()


def sync(dry_run=False, with_photos=False):
    if not os.path.exists(DB_PATH):
        print("DBがありません。先に server.py を一度起動してください:", DB_PATH)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    existing = {r["source_url"] for r in conn.execute(
        "SELECT source_url FROM pets WHERE source='city'")}
    added, checked, active_urls = 0, 0, set()

    for list_url, kind, species in LISTS:
        try:
            html = fetch(list_url)
        except Exception as e:
            print(f"[skip] 一覧取得失敗 {list_url}: {e}")
            continue
        time.sleep(FETCH_INTERVAL)
        items = parse_list(html)
        print(f"[list] {list_url} ({kind}/{species}): {len(items)}件")
        for it in reversed(items):  # 古い記事から処理(新しい記事が後=一覧の上に並ぶ)
            src_url = BASE + it["url"]
            active_urls.add(src_url)
            checked += 1
            posted_at = parse_event_at(it["title"])  # タイトル冒頭の日付=記事掲載日
            if src_url in existing:
                if not dry_run:
                    # 既存レコードの並び順を修正(旧バージョンで取り込んだ分の是正)
                    if posted_at:
                        conn.execute("UPDATE pets SET created_at=? WHERE source_url=? AND source='city'",
                                     (posted_at, src_url))
                    # 写真未設定なら記事から補完(直リンクモード時)
                    if photo_mode() == "1":
                        row = conn.execute("SELECT photo_ext FROM pets WHERE source_url=? AND source='city'",
                                           (src_url,)).fetchone()
                        if row is not None and not photo_looks_valid(row["photo_ext"], it["kiji_id"]):
                            try:
                                art2 = parse_article(fetch(it["url"]))
                                time.sleep(FETCH_INTERVAL)
                                purl = first_photo_url(art2, it["kiji_id"])
                                conn.execute("UPDATE pets SET photo_ext=? WHERE source_url=? AND source='city'",
                                             (purl, src_url))
                                if purl:
                                    print("  [写真補完] " + it["title"])
                            except Exception as e:
                                print("  [写真補完skip]", e)
                continue
            try:
                art = parse_article(fetch(it["url"]))
            except Exception as e:
                print(f"  [skip] 記事取得失敗 {it['url']}: {e}")
                continue
            time.sleep(FETCH_INTERVAL)
            lat, lng = guess_coord(art.get("place", ""))
            event_at = parse_event_at(art.get("event_date", "")) or datetime.now(JST).strftime("%Y-%m-%dT12:00")
            photos = []
            photo_ext = first_photo_url(art, it["kiji_id"]) if photo_mode() == "1" else ""
            if with_photos or photo_mode() == "2":
                # TODO: センター許諾後に有効化。市サイトの画像を取得して /uploads に保存する
                pass
            row = {
                "kind": kind, "species": species,
                "title": it["title"],
                "place": art.get("place", ""),
                "features": art.get("features", ""),
                "event_at": event_at,
                "lat": lat, "lng": lng,
                "src": src_url,
            }
            print(f"  [+] {it['title']} / {row['place']} / {event_at[:10]}")
            if dry_run:
                added += 1
                continue
            import secrets, string
            pid = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
            conn.execute(
                """INSERT INTO pets(id, admin_token, kind, species, name, breed, size, color,
                   features, event_at, lat, lng, address, collar, microchip, contact,
                   contact_public, shelter_info, photos, status, created_at, source, source_url, photo_ext)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, "", kind, species,
                 it["title"][:30] if kind == "lost" else "",
                 "", "medium", "",
                 (row["features"] + ("\n※おおよその位置を表示しています。詳細は元記事(熊本市動物愛護センター)をご確認ください" if row["place"] else ""))[:500],
                 event_at, lat, lng, row["place"][:100], 0, 0,
                 "熊本市動物愛護センター 096-380-2153", 1,
                 "熊本市動物愛護センターの掲載情報" if kind == "found" else "",
                 "[]",
                 "sheltering" if kind == "found" else "searching",
                 posted_at or datetime.now(JST).isoformat(timespec="seconds"),
                 "city", src_url, photo_ext))
            added += 1

    # 市サイトから消えた記事(=返還・掲載終了)は自動クローズ
    closed = 0
    if not dry_run:
        for r in conn.execute("SELECT id, source_url FROM pets WHERE source='city' AND status IN ('sheltering','searching')").fetchall():
            if r["source_url"] not in active_urls and active_urls:
                conn.execute("UPDATE pets SET status='closed' WHERE id=?", (r["id"],))
                closed += 1
        conn.commit()
    conn.close()
    print(f"\n完了: 確認{checked}件 / 新規{added}件 / クローズ{closed}件" + (" (dry-run)" if dry_run else ""))


if __name__ == "__main__":
    sync(dry_run="--dry-run" in sys.argv, with_photos="--with-photos" in sys.argv)
