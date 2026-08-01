# -*- coding: utf-8 -*-
"""
くまもとペット捜索マップ - 令和8年(2026年)熊本地震 災害対応版
迷子ペット(犬・猫)の捜索と、保護情報の掲示を1つの地図で繋ぐ無料サービス。
Python 3.12 stdlib のみ。起動: python server.py (PORT環境変数対応)
環境変数: ANTHROPIC_API_KEY(任意・SNS/チラシ文面生成。未設定でも定型文で動作)
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import zipfile
import tempfile
import shutil
import string
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "app.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

JST = timezone(timedelta(hours=9))
LAT_RANGE = (31.8, 33.6)     # 熊本県近郊
LNG_RANGE = (129.8, 131.6)

RATE_LIMITS = {
    "register": (10, 86400),  # 災害時につき通常より緩め: 登録10件/日
    "intake":   (8, 86400),   # チラシ読み取り登録8件/日(AI費用対策)
    "contact":  (5, 86400),   # お問い合わせ5件/日
    "sighting": (30, 3600),
    "generate": (10, 3600),
    "upload":   (30, 3600),
    "flag":     (10, 3600),
    "searched": (60, 3600),
    "update":   (20, 3600),
}
FLAG_HIDE_THRESHOLD = 3

GRID_LAT = 0.00135
GRID_LNG = 0.00162
CELL_KEY_RE = re.compile(r"^(-?\d{1,7})_(-?\d{1,7})$")
MAX_CELLS_PER_POST = 500

KINDS = ("lost", "found")                 # さがしています / 保護しています
SPECIES = ("dog", "cat", "other")
LOST_STATUS = ("searching", "reunited", "closed")
FOUND_STATUS = ("sheltering", "reunited", "closed")

ADMIN_KEY = os.environ.get("ADMIN_KEY", "").strip()

NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "").strip()  # (任意)Slack/Discord Webhook URL
# メール通知: 既定で info@crk.jp 宛て・Lolipop経由。SMTP_PASS(メールパスワード)の設定だけで有効化
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.lolipop.jp").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "info@crk.jp").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").strip()
NOTIFY_TO = os.environ.get("NOTIFY_TO", "info@crk.jp").strip()


def notify_admin(text):
    """運営への通知(webhook/メール)。失敗してもサービス本体には影響させない"""
    def _send():
        if NOTIFY_WEBHOOK:
            try:
                key = "content" if "discord.com" in NOTIFY_WEBHOOK else "text"
                body = json.dumps({key: text[:1800]}).encode()
                req = urllib.request.Request(NOTIFY_WEBHOOK, data=body,
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=8)
            except Exception as e:
                print("[notify] webhook失敗:", e)
        if SMTP_HOST and SMTP_USER and SMTP_PASS and NOTIFY_TO:
            try:
                import smtplib
                from email.mime.text import MIMEText
                from email.header import Header
                msg = MIMEText(text, "plain", "utf-8")
                msg["Subject"] = Header("【ペット捜索マップ】通知", "utf-8")
                msg["From"] = SMTP_USER
                msg["To"] = NOTIFY_TO
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as sv:
                    sv.login(SMTP_USER, SMTP_PASS)
                    sv.send_message(msg)
            except Exception as e:
                print("[notify] メール失敗:", e)
    threading.Thread(target=_send, daemon=True).start()

_db_lock = threading.Lock()


def now_iso():
    return datetime.now(JST).isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_pets_columns(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pets)")}
    for col in ("source", "source_url", "photo_ext", "related_id"):
        if col not in cols:
            conn.execute("ALTER TABLE pets ADD COLUMN " + col + " TEXT DEFAULT ''")
    conn.commit()


def init_db():
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS pets(
          id TEXT PRIMARY KEY,
          admin_token TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'lost',      -- lost / found
          species TEXT NOT NULL DEFAULT 'dog',    -- dog / cat / other
          name TEXT DEFAULT '',                   -- 迷子: 名前 / 保護: 空でも可
          breed TEXT DEFAULT '',
          size TEXT DEFAULT 'medium',             -- small/medium/large(犬のみ意味あり)
          color TEXT DEFAULT '',
          features TEXT DEFAULT '',
          event_at TEXT NOT NULL,                 -- 迷子: いなくなった日時 / 保護: 保護した日時
          lat REAL, lng REAL,
          address TEXT DEFAULT '',
          collar INTEGER DEFAULT 0,
          microchip INTEGER DEFAULT 0,
          contact TEXT DEFAULT '',
          contact_public INTEGER DEFAULT 0,
          shelter_info TEXT DEFAULT '',           -- 保護のみ: 現在の預かり場所・届出状況
          photos TEXT DEFAULT '[]',
          status TEXT DEFAULT 'searching',
          flags INTEGER DEFAULT 0,
          hidden INTEGER DEFAULT 0,
          created_at TEXT,
          source TEXT DEFAULT '',
          source_url TEXT DEFAULT '',
          photo_ext TEXT DEFAULT '',
          related_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS inquiries(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL,
          body TEXT NOT NULL,
          contact TEXT DEFAULT '',
          url TEXT DEFAULT '',
          status TEXT DEFAULT 'new',
          created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS news(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          body TEXT NOT NULL,
          created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sightings(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          pet_id TEXT NOT NULL,
          lat REAL NOT NULL, lng REAL NOT NULL,
          seen_at TEXT NOT NULL,
          memo TEXT DEFAULT '',
          contact TEXT DEFAULT '',
          flags INTEGER DEFAULT 0, hidden INTEGER DEFAULT 0,
          created_at TEXT, ip_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS searched_cells(
          pet_id TEXT NOT NULL,
          cell TEXT NOT NULL,
          searched_at TEXT NOT NULL,
          ip_hash TEXT,
          PRIMARY KEY(pet_id, cell)
        );
        CREATE TABLE IF NOT EXISTS updates(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          pet_id TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS rate_limits(ip_hash TEXT, action TEXT, ts TEXT);
        CREATE INDEX IF NOT EXISTS idx_sightings_pet ON sightings(pet_id);
        CREATE INDEX IF NOT EXISTS idx_updates_pet ON updates(pet_id);
        CREATE INDEX IF NOT EXISTS idx_rate ON rate_limits(ip_hash, action, ts);
        CREATE INDEX IF NOT EXISTS idx_pets_kind ON pets(kind, status, hidden);
        """
    )
    conn.commit()
    _ensure_pets_columns(conn)
    conn.close()


def short_id(n=8):
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def ip_hash(ip):
    return hashlib.sha256(("kpm-salt-" + ip).encode()).hexdigest()[:16]


def check_rate(conn, iph, action):
    limit, window = RATE_LIMITS[action]
    cutoff = (datetime.now(JST) - timedelta(seconds=window)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM rate_limits WHERE ts < ?", (cutoff,))
    row = conn.execute(
        "SELECT COUNT(*) c FROM rate_limits WHERE ip_hash=? AND action=? AND ts>=?",
        (iph, action, cutoff)).fetchone()
    if row["c"] >= limit:
        return False
    conn.execute("INSERT INTO rate_limits(ip_hash, action, ts) VALUES(?,?,?)",
                 (iph, action, now_iso()))
    return True


def pet_public(row, include_contact=False):
    d = {
        "id": row["id"], "kind": row["kind"], "species": row["species"],
        "name": row["name"], "breed": row["breed"], "size": row["size"],
        "color": row["color"], "features": row["features"],
        "event_at": row["event_at"], "lat": row["lat"], "lng": row["lng"],
        "address": row["address"], "collar": bool(row["collar"]),
        "microchip": bool(row["microchip"]),
        "shelter_info": row["shelter_info"],
        "photos": json.loads(row["photos"] or "[]"),
        "status": row["status"], "created_at": row["created_at"],
        "source": (row["source"] if "source" in row.keys() else ""),
        "source_url": (row["source_url"] if "source_url" in row.keys() else ""),
        "photo_ext": (row["photo_ext"] if "photo_ext" in row.keys() else ""),
        "related_id": (row["related_id"] if "related_id" in row.keys() else ""),
        "contact_public": bool(row["contact_public"]),
        "contact": row["contact"] if (include_contact and row["contact_public"]) else "",
    }
    return d


SPECIES_JP = {"dog": "犬", "cat": "猫", "other": "その他"}
SIZE_JP = {"small": "小型", "medium": "中型", "large": "大型"}


def template_texts(pet):
    sp = SPECIES_JP.get(pet["species"], "")
    date = (pet["event_at"] or "")[:16].replace("T", " ")
    tag = {"dog": "#迷い犬", "cat": "#迷い猫"}.get(pet["species"], "#迷子ペット")
    if pet["kind"] == "found":
        x = (f"【保護しています】{sp}を保護中です\n"
             f"{pet['color']} {pet['breed'] or sp}。{date}頃 {pet['address'] or '熊本'}で保護。\n"
             f"心当たりのある飼い主さんはリンク先をご確認ください。\n"
             f"{tag} #迷子犬 #迷子猫 #熊本 #拡散希望")
        long = (f"【保護しています/熊本】\n\n■種類: {pet['breed'] or sp}\n■毛色: {pet['color']}\n"
                f"■特徴: {pet['features']}\n■保護日時: {date}頃\n■保護場所: {pet['address']}\n"
                f"■現在: {pet['shelter_info'] or 'ページ参照'}\n\n"
                f"飼い主さんをさがしています。心当たりのある方はページからご連絡ください。")
        return {"x": x, "long": long, "flyer_catch": "保護しています",
                "flyer_note": "飼い主さんをさがしています。心当たりのある方はご連絡ください。", "source": "template"}
    x = (f"【拡散希望】迷子の{sp}をさがしています\n"
         f"{pet['name']}({pet['breed'] or sp}・{pet['color']})\n"
         f"{date}頃 {pet['address'] or '熊本'}で行方不明に。\n"
         f"目撃情報はリンク先から投稿できます🙏\n{tag} #迷子犬 #迷子猫 #熊本 #拡散希望")
    long = (f"【迷子の{sp}をさがしています/熊本】\n\n■名前: {pet['name']}\n"
            f"■種類: {pet['breed'] or sp}\n■毛色: {pet['color']}\n■特徴: {pet['features']}\n"
            f"■いなくなった日時: {date}頃\n■場所: {pet['address'] or '熊本'}\n"
            f"■首輪: {'あり' if pet['collar'] else 'なし'}\n\n"
            + ("怖がって隠れている可能性が高いです。物陰で見かけたら、追いかけず・大声を出さず、その場で目撃情報をお寄せください。"
               if pet["species"] == "cat" else
               "見かけた方は、追いかけたり大声を出したりせず、その場で目撃場所と時間をお知らせください。"))
    return {"x": x, "long": long, "flyer_catch": "さがしています",
            "flyer_note": ("驚かせると逃げてしまいます。そっと距離を保ち、目撃情報をお寄せください。"
                           if pet["species"] == "cat" else
                           "見かけても追いかけないでください。逃げてしまいます。その場から目撃情報をお寄せください。"),
            "source": "template"}


def claude_texts(pet):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    sp = SPECIES_JP.get(pet["species"], "")
    mode = "保護したペットの飼い主さがし" if pet["kind"] == "found" else "迷子ペットの捜索"
    prompt = (
        f"熊本で{mode}の広報文を作成。誇張せず、感情に配慮。"
        "(1)X用投稿(140字以内、#迷子犬 #迷子猫 #熊本 #拡散希望 と種別タグを含む)、"
        "(2)Instagram/Facebook用長文、(3)チラシ用キャッチ(10字以内)、(4)チラシ注意書き(50字以内"
        + ("、猫は驚かせない・追いかけない旨" if pet["species"] == "cat" else "") + ")。\n"
        f"種別:{sp} 名前:{pet['name']} 品種:{pet['breed']} 毛色:{pet['color']} 特徴:{pet['features']} "
        f"日時:{pet['event_at']} 場所:{pet['address']} 預かり:{pet['shelter_info']}\n"
        '必ずJSONのみ: {"x":"...","long":"...","flyer_catch":"...","flyer_note":"..."}'
    )
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 1024,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        out = json.loads(re.sub(r"```json|```", "", text).strip())
        out["source"] = "claude"
        return out
    except Exception as e:
        print("[generate] Claude失敗->テンプレ:", e)
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "KumamotoPetMap/1.0"

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "")
        return fwd.split(",")[0].strip() if fwd else self.client_address[0]

    def send_json(self, obj, status=200):
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self, max_bytes=6 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > max_bytes:
            return None
        try:
            return json.loads(self.rfile.read(length).decode())
        except Exception:
            return None

    def qs(self, key, default=""):
        q = self.path.split("?", 1)[1] if "?" in self.path else ""
        for pair in q.split("&"):
            if pair.startswith(key + "="):
                return pair.split("=", 1)[1]
        return default

    def serve_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        if path.startswith(UPLOAD_DIR):
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (now_iso(), fmt % args))

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self.serve_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
        if path.startswith("/uploads/"):
            name = os.path.basename(path)
            if not re.fullmatch(r"[a-z0-9]+\.(jpg|png|webp)", name):
                return self.send_json({"error": "not found"}, 404)
            ext = name.rsplit(".", 1)[1]
            return self.serve_file(os.path.join(UPLOAD_DIR, name),
                                   {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[ext])
        if path == "/health":
            return self.send_json({"ok": True})
        if path == "/ogp.png":
            return self.serve_file(os.path.join(STATIC_DIR, "ogp.png"), "image/png")
        if path == "/api/admin/list":
            return self.api_admin_list()
        if path == "/api/admin/backup":
            return self.api_admin_backup()
        if path == "/api/pets":
            return self.api_list_pets()
        if path == "/api/news":
            return self.api_news()
        if path == "/api/stats":
            return self.api_stats()
        m = re.fullmatch(r"/api/pets/([a-z0-9]{8})", path)
        if m:
            return self.api_get_pet(m.group(1))
        self.send_json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/pets":
            return self.api_create_pet()
        if path == "/api/generate":
            return self.api_generate()
        if path == "/api/upload":
            return self.api_upload()
        if path == "/api/admin/moderate":
            return self.api_admin_moderate()
        if path == "/api/intake":
            return self.api_intake()
        if path == "/api/contact":
            return self.api_contact()
        if path == "/api/admin/news":
            return self.api_admin_news()
        if path == "/api/admin/inquiry":
            return self.api_admin_inquiry()
        m = re.fullmatch(r"/api/pets/([a-z0-9]{8})/(sightings|searched|updates|flag)", path)
        if m:
            return {"sightings": self.api_create_sighting,
                    "searched": self.api_mark_searched,
                    "updates": self.api_create_update,
                    "flag": self.api_flag_pet}[m.group(2)](m.group(1))
        m = re.fullmatch(r"/api/sightings/(\d+)/flag", path)
        if m:
            return self.api_flag_sighting(m.group(1))
        self.send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        m = re.fullmatch(r"/api/pets/([a-z0-9]{8})", self.path.split("?")[0])
        if m:
            return self.api_update_pet(m.group(1))
        self.send_json({"error": "not found"}, 404)

    # ---------- API ----------
    def api_stats(self):
        with _db_lock:
            conn = db()
            rows = conn.execute(
                "SELECT kind, status, COUNT(*) c FROM pets WHERE hidden=0 GROUP BY kind, status").fetchall()
            conn.close()
        stats = {"lost_active": 0, "found_active": 0, "reunited": 0}
        for r in rows:
            if r["kind"] == "lost" and r["status"] == "searching":
                stats["lost_active"] += r["c"]
            elif r["kind"] == "found" and r["status"] == "sheltering":
                stats["found_active"] += r["c"]
            elif r["status"] == "reunited":
                stats["reunited"] += r["c"]
        self.send_json(stats)

    def api_list_pets(self):
        kind = self.qs("kind", "lost")
        if kind not in KINDS:
            kind = "lost"
        status = self.qs("status", "")
        species = self.qs("species", "")
        default_status = "searching" if kind == "lost" else "sheltering"
        valid = LOST_STATUS if kind == "lost" else FOUND_STATUS
        if status not in valid and status != "all":
            status = default_status
        with _db_lock:
            conn = db()
            sql = "SELECT * FROM pets WHERE hidden=0 AND kind=?"
            params = [kind]
            if status != "all":
                sql += " AND status=?"; params.append(status)
            if species in SPECIES:
                sql += " AND species=?"; params.append(species)
            sql += " ORDER BY created_at DESC LIMIT 300"
            rows = conn.execute(sql, params).fetchall()
            counts = {r["pet_id"]: r["c"] for r in conn.execute(
                "SELECT pet_id, COUNT(*) c FROM sightings WHERE hidden=0 GROUP BY pet_id").fetchall()}
            conn.close()
        pets = []
        for r in rows:
            d = pet_public(r)
            d["sighting_count"] = counts.get(r["id"], 0)
            pets.append(d)
        self.send_json({"pets": pets})

    def api_create_pet(self):
        body = self.read_json()
        if body is None:
            return self.send_json({"error": "invalid json"}, 400)
        if (body.get("website") or "").strip():  # ハニーポット
            return self.send_json({"id": short_id(), "admin_token": secrets.token_urlsafe(16)})
        kind = body.get("kind") if body.get("kind") in KINDS else "lost"
        species = body.get("species") if body.get("species") in SPECIES else "dog"
        name = (body.get("name") or "").strip()[:30]
        event_at = (body.get("event_at") or "").strip()[:25]
        if kind == "lost" and not name:
            return self.send_json({"error": "名前を入力してください"}, 400)
        if not event_at:
            return self.send_json({"error": "日時を入力してください"}, 400)
        lat, lng = body.get("lat"), body.get("lng")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            return self.send_json({"error": "場所を地図で指定してください"}, 400)
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
            return self.send_json({"error": "熊本県の周辺のみ対応しています"}, 400)
        if kind == "found" and not (body.get("contact") or "").strip():
            return self.send_json({"error": "保護情報には連絡手段が必要です(飼い主さんが連絡できるように)"}, 400)
        size = body.get("size") if body.get("size") in SIZE_JP else "medium"
        photos = body.get("photos") or []
        if not isinstance(photos, list):
            photos = []
        photos = [p for p in photos[:3] if re.fullmatch(r"[a-z0-9]+\.(jpg|png|webp)", str(p))]
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            if not check_rate(conn, iph, "register"):
                conn.commit(); conn.close()
                return self.send_json({"error": "登録回数の上限に達しました。時間をおいてお試しください"}, 429)
            pid = short_id()
            token = secrets.token_urlsafe(16)
            related_id = ""
            rel = str(body.get("related") or "")
            if kind == "found" and re.fullmatch(r"[a-z0-9]{8}", rel):
                lost_row = conn.execute(
                    "SELECT id FROM pets WHERE id=? AND kind='lost' AND hidden=0", (rel,)).fetchone()
                if lost_row:
                    related_id = rel
            conn.execute(
                """INSERT INTO pets(id, admin_token, kind, species, name, breed, size, color,
                   features, event_at, lat, lng, address, collar, microchip, contact,
                   contact_public, shelter_info, photos, status, created_at, related_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, token, kind, species, name,
                 (body.get("breed") or "").strip()[:30], size,
                 (body.get("color") or "").strip()[:30],
                 (body.get("features") or "").strip()[:500],
                 event_at, float(lat), float(lng),
                 (body.get("address") or "").strip()[:100],
                 1 if body.get("collar") else 0,
                 1 if body.get("microchip") else 0,
                 (body.get("contact") or "").strip()[:100],
                 1 if body.get("contact_public") else 0,
                 (body.get("shelter_info") or "").strip()[:200],
                 json.dumps(photos),
                 "searching" if kind == "lost" else "sheltering",
                 now_iso(), related_id))
            conn.commit(); conn.close()
        self.send_json({"id": pid, "admin_token": token})

    def api_get_pet(self, pid):
        with _db_lock:
            conn = db()
            row = conn.execute("SELECT * FROM pets WHERE id=? AND hidden=0", (pid,)).fetchone()
            if not row:
                conn.close()
                return self.send_json({"error": "not found"}, 404)
            sightings = conn.execute(
                "SELECT id, lat, lng, seen_at, memo FROM sightings "
                "WHERE pet_id=? AND hidden=0 ORDER BY seen_at ASC LIMIT 300", (pid,)).fetchall()
            cells = conn.execute(
                "SELECT cell, searched_at FROM searched_cells WHERE pet_id=? LIMIT 8000", (pid,)).fetchall()
            ups = conn.execute(
                "SELECT body, created_at FROM updates WHERE pet_id=? ORDER BY id DESC LIMIT 50", (pid,)).fetchall()
            # 迷子↔保護の相互マッチング候補(同種・掲載中・新しい順)
            other_kind = "found" if row["kind"] == "lost" else "lost"
            other_status = "sheltering" if other_kind == "found" else "searching"
            similar = conn.execute(
                "SELECT * FROM pets WHERE hidden=0 AND kind=? AND status=? AND species=? "
                "ORDER BY created_at DESC LIMIT 6",
                (other_kind, other_status, row["species"])).fetchall()
            rescues = []
            related_name = ""
            if row["kind"] == "lost":
                rescues = conn.execute(
                    "SELECT id, name, status, created_at, address FROM pets "
                    "WHERE related_id=? AND kind='found' AND hidden=0 ORDER BY created_at DESC LIMIT 5",
                    (pid,)).fetchall()
            elif ("related_id" in row.keys()) and row["related_id"]:
                rl = conn.execute("SELECT name FROM pets WHERE id=?", (row["related_id"],)).fetchone()
                related_name = (rl["name"] if rl else "") or "(名前なし)"
            conn.close()
        d = pet_public(row, include_contact=True)
        d["rescues"] = [dict(r) for r in rescues]
        d["related_name"] = related_name
        d["sightings"] = [dict(s) for s in sightings]
        d["searched"] = [dict(c) for c in cells]
        d["updates"] = [dict(u) for u in ups]
        d["similar"] = [pet_public(s) for s in similar]
        self.send_json(d)

    def api_update_pet(self, pid):
        token = self.qs("token")
        body = self.read_json() or {}
        with _db_lock:
            conn = db()
            row = conn.execute("SELECT admin_token, kind FROM pets WHERE id=?", (pid,)).fetchone()
            if not row or not secrets.compare_digest(row["admin_token"], token):
                conn.close()
                return self.send_json({"error": "権限がありません"}, 403)
            valid = LOST_STATUS if row["kind"] == "lost" else FOUND_STATUS
            updates, params = [], []
            if body.get("status") in valid:
                updates.append("status=?"); params.append(body["status"])
            for f, lim in (("features", 500), ("contact", 100), ("breed", 30),
                           ("color", 30), ("shelter_info", 200), ("address", 100)):
                if isinstance(body.get(f), str):
                    updates.append(f + "=?"); params.append(body[f].strip()[:lim])
            if "contact_public" in body:
                updates.append("contact_public=?"); params.append(1 if body["contact_public"] else 0)
            if updates:
                params.append(pid)
                conn.execute("UPDATE pets SET " + ", ".join(updates) + " WHERE id=?", params)
                conn.commit()
            conn.close()
        self.send_json({"ok": True})

    def api_create_sighting(self, pid):
        body = self.read_json()
        if body is None:
            return self.send_json({"error": "invalid json"}, 400)
        if (body.get("website") or "").strip():
            return self.send_json({"ok": True})
        lat, lng = body.get("lat"), body.get("lng")
        seen_at = (body.get("seen_at") or "").strip()[:25]
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)) or not seen_at:
            return self.send_json({"error": "目撃場所と日時は必須です"}, 400)
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
            return self.send_json({"error": "熊本県の周辺のみ対応しています"}, 400)
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            pet = conn.execute("SELECT id FROM pets WHERE id=? AND hidden=0", (pid,)).fetchone()
            if not pet:
                conn.close()
                return self.send_json({"error": "not found"}, 404)
            if not check_rate(conn, iph, "sighting"):
                conn.commit(); conn.close()
                return self.send_json({"error": "投稿回数の上限に達しました"}, 429)
            conn.execute(
                "INSERT INTO sightings(pet_id, lat, lng, seen_at, memo, contact, created_at, ip_hash) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (pid, float(lat), float(lng), seen_at,
                 (body.get("memo") or "").strip()[:300],
                 (body.get("contact") or "").strip()[:100], now_iso(), iph))
            conn.commit(); conn.close()
        self.send_json({"ok": True})

    def api_mark_searched(self, pid):
        body = self.read_json()
        if body is None:
            return self.send_json({"error": "invalid json"}, 400)
        cells = body.get("cells")
        if not isinstance(cells, list) or not cells or len(cells) > MAX_CELLS_PER_POST:
            return self.send_json({"error": "セルが不正です"}, 400)
        valid = []
        for c in cells:
            m = CELL_KEY_RE.match(str(c))
            if not m:
                continue
            la = int(m.group(1)) * GRID_LAT
            ln = int(m.group(2)) * GRID_LNG
            if LAT_RANGE[0] <= la <= LAT_RANGE[1] and LNG_RANGE[0] <= ln <= LNG_RANGE[1]:
                valid.append(str(c))
        if not valid:
            return self.send_json({"error": "対応エリア外です"}, 400)
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            pet = conn.execute("SELECT id FROM pets WHERE id=? AND hidden=0", (pid,)).fetchone()
            if not pet:
                conn.close()
                return self.send_json({"error": "not found"}, 404)
            if not check_rate(conn, iph, "searched"):
                conn.commit(); conn.close()
                return self.send_json({"error": "保存回数の上限に達しました"}, 429)
            ts = now_iso()
            conn.executemany(
                "INSERT INTO searched_cells(pet_id, cell, searched_at, ip_hash) VALUES(?,?,?,?) "
                "ON CONFLICT(pet_id, cell) DO UPDATE SET searched_at=excluded.searched_at",
                [(pid, c, ts, iph) for c in valid])
            conn.commit(); conn.close()
        self.send_json({"ok": True, "saved": len(valid)})

    def api_create_update(self, pid):
        token = self.qs("token")
        body = self.read_json() or {}
        text = (body.get("body") or "").strip()[:500]
        if not text:
            return self.send_json({"error": "内容を入力してください"}, 400)
        with _db_lock:
            conn = db()
            row = conn.execute("SELECT admin_token FROM pets WHERE id=?", (pid,)).fetchone()
            if not row or not secrets.compare_digest(row["admin_token"], token):
                conn.close()
                return self.send_json({"error": "権限がありません"}, 403)
            conn.execute("INSERT INTO updates(pet_id, body, created_at) VALUES(?,?,?)",
                         (pid, text, now_iso()))
            conn.commit(); conn.close()
        self.send_json({"ok": True})

    def _flag(self, table, col, item_id):
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            if not check_rate(conn, iph, "flag"):
                conn.commit(); conn.close()
                return self.send_json({"error": "しばらく時間をおいてください"}, 429)
            conn.execute(f"UPDATE {table} SET flags=flags+1 WHERE {col}=?", (item_id,))
            conn.execute(f"UPDATE {table} SET hidden=1 WHERE {col}=? AND flags>=?",
                         (item_id, FLAG_HIDE_THRESHOLD))
            conn.commit(); conn.close()
        self.send_json({"ok": True, "message": "通報を受け付けました"})

    def api_flag_pet(self, pid):
        return self._flag("pets", "id", pid)

    def api_flag_sighting(self, sid):
        return self._flag("sightings", "id", sid)

    def api_generate(self):
        body = self.read_json() or {}
        pid = str(body.get("pet_id") or "")
        if not re.fullmatch(r"[a-z0-9]{8}", pid):
            return self.send_json({"error": "invalid pet_id"}, 400)
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            if not check_rate(conn, iph, "generate"):
                conn.commit(); conn.close()
                return self.send_json({"error": "生成回数の上限に達しました"}, 429)
            row = conn.execute("SELECT * FROM pets WHERE id=? AND hidden=0", (pid,)).fetchone()
            conn.commit(); conn.close()
        if not row:
            return self.send_json({"error": "not found"}, 404)
        pet = pet_public(row, include_contact=True)
        self.send_json(claude_texts(pet) or template_texts(pet))

    # ---------- 管理(通報対応) ----------
    def api_admin_backup(self):
        """全データ(DB+写真)のzipバックアップをダウンロード(管理者のみ)"""
        if not ADMIN_KEY or not secrets.compare_digest(self.qs("key"), ADMIN_KEY):
            return self.send_json({"error": "権限がありません"}, 403)
        tmpdir = tempfile.mkdtemp(prefix="petmap-bk-")
        try:
            # WAL中でも安全なスナップショットを作る(SQLite backup API)
            db_copy = os.path.join(tmpdir, "app.db")
            with _db_lock:
                src = sqlite3.connect(DB_PATH)
                dst = sqlite3.connect(db_copy)
                src.backup(dst)
                dst.close(); src.close()
            stamp = datetime.now(JST).strftime("%Y%m%d-%H%M")
            zpath = os.path.join(tmpdir, f"petmap-backup-{stamp}.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(db_copy, "app.db")
                if os.path.isdir(UPLOAD_DIR):
                    for fn in os.listdir(UPLOAD_DIR):
                        fp = os.path.join(UPLOAD_DIR, fn)
                        if os.path.isfile(fp):
                            z.write(fp, "uploads/" + fn)
            with open(zpath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition",
                             f'attachment; filename="petmap-backup-{stamp}.zip"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def api_admin_list(self):
        if not ADMIN_KEY or not secrets.compare_digest(self.qs("key"), ADMIN_KEY):
            return self.send_json({"error": "権限がありません"}, 403)
        with _db_lock:
            conn = db()
            pets = conn.execute(
                "SELECT id, kind, species, name, color, address, status, flags, hidden, created_at "
                "FROM pets WHERE flags>0 OR hidden=1 ORDER BY hidden DESC, flags DESC LIMIT 100").fetchall()
            sightings = conn.execute(
                "SELECT id, pet_id, seen_at, memo, flags, hidden FROM sightings "
                "WHERE flags>0 OR hidden=1 ORDER BY hidden DESC, flags DESC LIMIT 100").fetchall()
            inquiries = conn.execute(
                "SELECT id, category, body, contact, url, status, created_at FROM inquiries "
                "ORDER BY (status='new') DESC, id DESC LIMIT 100").fetchall()
            news = conn.execute("SELECT id, body, created_at FROM news ORDER BY id DESC LIMIT 20").fetchall()
            conn.close()
        self.send_json({"pets": [dict(r) for r in pets],
                        "sightings": [dict(r) for r in sightings],
                        "inquiries": [dict(r) for r in inquiries],
                        "news": [dict(r) for r in news]})

    def api_admin_moderate(self):
        body = self.read_json() or {}
        if not ADMIN_KEY or not secrets.compare_digest(str(body.get("key") or ""), ADMIN_KEY):
            return self.send_json({"error": "権限がありません"}, 403)
        table = body.get("table")
        action = body.get("action")
        item_id = str(body.get("id") or "")
        if table not in ("pets", "sightings") or action not in ("restore", "hide"):
            return self.send_json({"error": "不正なリクエストです"}, 400)
        with _db_lock:
            conn = db()
            if action == "restore":
                conn.execute(f"UPDATE {table} SET hidden=0, flags=0 WHERE id=?", (item_id,))
            else:
                conn.execute(f"UPDATE {table} SET hidden=1 WHERE id=?", (item_id,))
            conn.commit(); conn.close()
        self.send_json({"ok": True})

    # ---------- お知らせ・お問い合わせ ----------
    CONTACT_CATEGORIES = ("feature", "delete", "bug", "other")

    def api_news(self):
        with _db_lock:
            conn = db()
            rows = conn.execute("SELECT id, body, created_at FROM news ORDER BY id DESC LIMIT 10").fetchall()
            conn.close()
        self.send_json({"news": [dict(r) for r in rows]})

    def api_contact(self):
        body = self.read_json() or {}
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            ok = check_rate(conn, iph, "contact")
            conn.commit()
            if not ok:
                conn.close()
                return self.send_json({"error": "本日の送信上限に達しました"}, 429)
            cat = str(body.get("category") or "")
            text = str(body.get("body") or "").strip()
            if cat not in self.CONTACT_CATEGORIES:
                conn.close()
                return self.send_json({"error": "種類を選んでください"}, 400)
            if not (5 <= len(text) <= 2000):
                conn.close()
                return self.send_json({"error": "内容は5文字以上2000文字以内で入力してください"}, 400)
            if body.get("website"):  # ハニーポット
                conn.close()
                return self.send_json({"ok": True})
            conn.execute(
                "INSERT INTO inquiries(category, body, contact, url, created_at) VALUES(?,?,?,?,?)",
                (cat, text[:2000], str(body.get("contact") or "")[:100],
                 str(body.get("url") or "")[:300], now_iso()))
            conn.commit(); conn.close()
        CAT_JP = {"feature": "機能要望", "delete": "削除依頼", "bug": "不具合", "other": "意見"}
        notify_admin("💬 お問い合わせ【" + CAT_JP.get(cat, cat) + "】\n" + text[:300] +
                     ("\n対象: " + str(body.get("url") or "") if body.get("url") else "") +
                     ("\n連絡先: " + str(body.get("contact") or "") if body.get("contact") else "") +
                     "\n→ 管理画面: /#/admin")
        self.send_json({"ok": True})

    def api_admin_news(self):
        body = self.read_json() or {}
        if not ADMIN_KEY or not secrets.compare_digest(str(body.get("key") or ""), ADMIN_KEY):
            return self.send_json({"error": "権限がありません"}, 403)
        action = body.get("action")
        with _db_lock:
            conn = db()
            if action == "add":
                text = str(body.get("body") or "").strip()
                if not (1 <= len(text) <= 500):
                    conn.close()
                    return self.send_json({"error": "1〜500文字で入力してください"}, 400)
                conn.execute("INSERT INTO news(body, created_at) VALUES(?,?)", (text, now_iso()))
            elif action == "delete":
                conn.execute("DELETE FROM news WHERE id=?", (int(body.get("id") or 0),))
            else:
                conn.close()
                return self.send_json({"error": "不正なリクエストです"}, 400)
            conn.commit(); conn.close()
        self.send_json({"ok": True})

    def api_admin_inquiry(self):
        body = self.read_json() or {}
        if not ADMIN_KEY or not secrets.compare_digest(str(body.get("key") or ""), ADMIN_KEY):
            return self.send_json({"error": "権限がありません"}, 403)
        with _db_lock:
            conn = db()
            conn.execute("UPDATE inquiries SET status='done' WHERE id=?", (int(body.get("id") or 0),))
            conn.commit(); conn.close()
        self.send_json({"ok": True})

    def api_intake(self):
        body = self.read_json() or {}
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            ok = check_rate(conn, iph, "intake")
            conn.commit(); conn.close()
        if not ok:
            return self.send_json({"error": "本日の読み取り回数の上限に達しました。手動での登録は引き続き可能です"}, 429)
        photo = re.sub(r"[^a-z0-9.]", "", str(body.get("photo") or ""))
        img_path = os.path.join(UPLOAD_DIR, photo)
        if not photo or not os.path.isfile(img_path):
            return self.send_json({"error": "先に画像をアップロードしてください"}, 400)
        info, err = claude_flyer_extract(img_path)
        if err:
            return self.send_json({"error": err}, 502)
        if not info.get("valid", True):
            return self.send_json({"error": "ペット捜索のチラシとして読み取れませんでした。チラシ全体が写ったスクショでもう一度お試しください"}, 422)
        kind = "found" if str(info.get("kind", "")).startswith("found") else "lost"
        species = info.get("species") if info.get("species") in ("dog", "cat", "other") else "other"
        name = str(info.get("name") or "").strip()[:30]
        place = strip_banchi(str(info.get("place") or "")[:100])
        contact = str(info.get("contact") or "").strip()[:100]
        # 重複検出: 名前だけ・電話だけでは同一とみなさない
        # (同じ団体が多数の子のチラシを出す/同名の別の子がいるため)
        # 同一と判定する条件: 種別・区分が同じで、
        #   A) 名前が一致 かつ (場所が一致 or 電話が一致)
        #   B) 名前なし同士で、電話が一致 かつ 場所が一致
        def norm_name(x):
            return re.sub(r"[\s・.。、]", "", (x or "")).lower()
        def place_match(a, b):
            a, b = (a or "").strip(), (b or "").strip()
            return bool(a) and bool(b) and (a in b or b in a)
        new_phones = phones_in(contact)
        nn = norm_name(name)
        dup = None
        with _db_lock:
            conn = db()
            for r in conn.execute("SELECT id, name, species, kind, contact, address FROM pets WHERE hidden=0 ORDER BY created_at DESC LIMIT 500"):
                if r["kind"] != kind or r["species"] != species:
                    continue
                phone_hit = bool(new_phones) and bool(new_phones & phones_in(r["contact"] or ""))
                if nn and len(nn) >= 2 and norm_name(r["name"]) == nn and (place_match(place, r["address"]) or phone_hit):
                    dup = r; break
                if not nn and not norm_name(r["name"]) and phone_hit and place_match(place, r["address"]):
                    dup = r; break
            if dup:
                conn.close()
                return self.send_json({"ok": True, "action": "duplicate",
                                       "match": {"id": dup["id"], "name": dup["name"] or "(名前なし)"}})
            try:
                from city_sync import parse_event_at
                event_at = parse_event_at(str(info.get("event_date") or ""))
            except Exception:
                event_at = ""
            if not event_at:
                event_at = datetime.now(JST).strftime("%Y-%m-%dT12:00")
            lat, lng = sns_geocode(place)
            pid = short_id(8)
            token = short_id(24)
            features = str(info.get("features") or "")[:400]
            if place:
                features = (features + "\n※地図はおおよその位置です(SNSチラシからの転載)")[:500]
            conn.execute(
                """INSERT INTO pets(id, admin_token, kind, species, name, breed, size, color,
                   features, event_at, lat, lng, address, collar, microchip, contact,
                   contact_public, shelter_info, photos, status, created_at, source, source_url, photo_ext)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, token, kind, species, name, str(info.get("breed") or "")[:40], "medium",
                 str(info.get("color") or "")[:40], features, event_at, lat, lng, place,
                 0, 0, contact, 1, "SNSで拡散されている捜索チラシの情報",
                 json.dumps([photo]), "sheltering" if kind == "found" else "searching",
                 datetime.now(JST).isoformat(timespec="seconds"), "sns", "", ""))
            conn.commit(); conn.close()
        notify_admin("📸 スクショから新規掲載: " + ("さがしています" if kind == "lost" else "保護しています") +
                     " / " + (name or "(名前なし)") + " / " + (place or "場所不明") + "\n/#/pet/" + pid)
        return self.send_json({"ok": True, "action": "added", "id": pid, "token": token,
                               "summary": {"kind": kind, "species": species, "name": name,
                                           "place": place, "contact": contact}})

    def api_upload(self):
        body = self.read_json(max_bytes=4 * 1024 * 1024)
        if body is None:
            return self.send_json({"error": "画像が大きすぎます(2MB以下)"}, 400)
        m = re.match(r"data:image/(jpeg|png|webp);base64,(.+)", body.get("data") or "", re.DOTALL)
        if not m:
            return self.send_json({"error": "JPEG/PNG/WebPのみ対応しています"}, 400)
        try:
            raw = base64.b64decode(m.group(2), validate=True)
        except Exception:
            return self.send_json({"error": "画像の読み込みに失敗しました"}, 400)
        if len(raw) > 2 * 1024 * 1024:
            return self.send_json({"error": "画像は2MB以下にしてください"}, 400)
        ext = None
        if raw[:3] == b"\xff\xd8\xff":
            ext = "jpg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
            ext = "webp"
        if not ext:
            return self.send_json({"error": "画像ファイルとして認識できません"}, 400)
        iph = ip_hash(self.client_ip())
        with _db_lock:
            conn = db()
            ok = check_rate(conn, iph, "upload")
            conn.commit(); conn.close()
        if not ok:
            return self.send_json({"error": "アップロード上限に達しました"}, 429)
        name = short_id(16) + "." + ext
        with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
            f.write(raw)
        self.send_json({"file": name})



# ---------- SNSチラシ取込(管理者用) ----------
SNS_EXTRA_COORDS = {
    "宇城市": (32.6460, 130.6840), "八代市": (32.5060, 130.6010),
    "玉名市": (32.9280, 130.5590), "山鹿市": (33.0170, 130.6910),
    "菊池市": (32.9790, 130.8120), "大津町": (32.8780, 130.8710),
    "西原村": (32.8420, 130.9040), "南阿蘇村": (32.8180, 131.0350),
    "阿蘇市": (32.9520, 131.1210), "人吉市": (32.2100, 130.7620),
    "天草市": (32.4580, 130.1930), "氷川町": (32.5830, 130.6720),
    "美里町": (32.6360, 130.7940), "玉東町": (32.9260, 130.6620),
}


def sns_geocode(place):
    try:
        from city_sync import WARD_COORDS
        table = dict(WARD_COORDS)
    except Exception:
        table = {}
    table.update(SNS_EXTRA_COORDS)
    best = None
    for name, ll in table.items():
        if name in (place or "") and (best is None or len(name) > len(best[0])):
            best = (name, ll)
    return best[1] if best else (32.8032, 130.7079)


def strip_banchi(place):
    # 個人宅特定を避けるため番地以降を落とす(例: 幾久富1758 → 幾久富)
    return re.sub(r"[0-90-9][0-90-9\-ー−の丁目番地号\s]*$", "", (place or "").strip()).strip()


def phones_in(text):
    return set(re.sub(r"[^0-9]", "", m) for m in re.findall(r"0\d{1,4}[-\s]?\d{1,4}[-\s]?\d{3,4}", text or ""))


def claude_flyer_extract(img_path):
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None, "ANTHROPIC_API_KEYが未設定です(Renderの環境変数を確認)"
    ext = img_path.rsplit(".", 1)[-1].lower()
    media = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(ext)
    if not media:
        return None, "対応していない画像形式です"
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = (
        "画像が迷子ペット捜索または保護ペットのチラシ/SNS投稿かどうかをまず判定してください。"
        "違う場合は {\"valid\":false} のみ出力。チラシの場合は次のJSONを出力: "
        "記載内容だけを読み取り、必ずJSONのみで出力してください(推測で創作しない。不明は空文字)。\n"
        '{"valid":true,"kind":"lost(探しています)またはfound(保護しています)",'
        '"species":"dog/cat/other","name":"ペットの名前","breed":"品種",'
        '"color":"毛色","features":"特徴(性別・年齢・体格・首輪・健康上の注意など)",'
        '"event_date":"いなくなった/保護した日付(例 2026年7月28日)",'
        '"place":"場所。市区町村+町名まで。番地・丁目以降の数字は含めない",'
        '"contact":"チラシ記載の連絡先(電話番号や団体名。個人宅住所は含めない)"}'
    )
    body = json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 800,
                       "messages": [{"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                           {"type": "text", "text": prompt}]}]}).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            data = json.loads(res.read().decode())
        text = "".join(b.get("text", "") for b in data.get("content", []))
        return json.loads(re.sub(r"```json|```", "", text).strip()), None
    except Exception as e:
        return None, "読み取りに失敗しました: " + str(e)[:120]


def start_city_sync_thread():
    """環境変数 CITY_SYNC=1 のとき、愛護センター取込を定期実行(既定3時間おき)"""
    if os.environ.get("CITY_SYNC", "") != "1":
        print("愛護センター自動取込: 無効(CITY_SYNC=1 で有効化。有効化前に city_sync.py --dry-run で確認を)")
        return
    interval = float(os.environ.get("SYNC_INTERVAL_HOURS", "3")) * 3600
    def loop():
        import time as _t
        _t.sleep(30)  # 起動直後を避ける
        while True:
            try:
                import city_sync
                print("[city_sync] 取込開始")
                city_sync.sync()
            except Exception as e:
                print("[city_sync] エラー(次回に再試行):", e)
            _t.sleep(interval)
    threading.Thread(target=loop, daemon=True).start()
    print(f"愛護センター自動取込: 有効({interval/3600:.0f}時間おき)")


def main():
    init_db()
    start_city_sync_thread()
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"くまもとペット捜索マップ 起動: http://0.0.0.0:{port}")
    print(f"AI生成: {'有効' if os.environ.get('ANTHROPIC_API_KEY') else '無効(テンプレートで動作)'}")
    print(f"管理画面: {'有効(/#/admin)' if ADMIN_KEY else '無効(ADMIN_KEY未設定)'}")
    mail_on = bool(SMTP_PASS)
    hook_on = bool(NOTIFY_WEBHOOK)
    print("通知: " + ("メール→" + NOTIFY_TO if mail_on else "") + (" / " if mail_on and hook_on else "") +
          ("Webhook" if hook_on else "") + ("" if (mail_on or hook_on) else "無効(SMTP_PASSまたはNOTIFY_WEBHOOKを設定)"))
    server.serve_forever()


if __name__ == "__main__":
    main()
