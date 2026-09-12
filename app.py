from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import requests
import secrets
import shutil
import sqlite3
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parent
DATA = Path(os.getenv("ATV_DATA_DIR", str(ROOT / "data"))).resolve()
PREVIEWS = DATA / "previews"
ORIGINALS = DATA / "originals"
PACKAGES = DATA / "packages"
DB_PATH = DATA / "atv_homologacao.sqlite3"
FRONTEND = ROOT / "frontend"

for p in (DATA, PREVIEWS, ORIGINALS, PACKAGES, FRONTEND):
    p.mkdir(parents=True, exist_ok=True)

APP_ENV = os.getenv("ATV_ENV", "homologation")
COOKIE_SECURE = os.getenv("ATV_COOKIE_SECURE", "0") == "1"
SESSION_HOURS = int(os.getenv("ATV_SESSION_HOURS", "168"))
MAX_UPLOAD_BYTES = int(os.getenv("ATV_MAX_UPLOAD_MB", "80")) * 1024 * 1024
MAX_PREVIEW_BYTES = 10 * 1024 * 1024
ADMIN_PIN_ENV = os.getenv("ATV_ADMIN_PIN")
ADMIN_KEY_ENV = os.getenv("ATV_ADMIN_KEY")
AI_API_KEY = os.getenv("ATV_OPENAI_API_KEY", "").strip()
AI_MODERATION_MODEL = os.getenv("ATV_AI_MODERATION_MODEL", "omni-moderation-latest").strip() or "omni-moderation-latest"
AI_FAKE_SAFE = os.getenv("ATV_AI_FAKE_SAFE", "0") == "1"
if APP_ENV != "local" and (not ADMIN_PIN_ENV or not ADMIN_KEY_ENV):
    raise RuntimeError("ATV_ADMIN_PIN e ATV_ADMIN_KEY são obrigatórios fora do ambiente local.")
ADMIN_PIN = ADMIN_PIN_ENV or "246810"
ADMIN_KEY = ADMIN_KEY_ENV or "AD!!"
if not re.fullmatch(r"\d{6}", ADMIN_PIN):
    raise RuntimeError("ATV_ADMIN_PIN deve conter exatamente 6 dígitos.")
if not re.fullmatch(r"[A-Za-z]{2}[^A-Za-z0-9]{2}", ADMIN_KEY):
    raise RuntimeError("ATV_ADMIN_KEY deve conter exatamente duas letras e dois símbolos.")

USER_COOKIE = "atv_session"
ADMIN_COOKIE = "atv_admin_session"

BUILD_VERSION = "V8.7.3"
BUILD_ID = "2026-08-20-v873-final-network"
app = FastAPI(title="ATV Homologação API", version="1.7.10")
app.mount("/media/previews", StaticFiles(directory=str(PREVIEWS)), name="previews")

# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utcnow().isoformat()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def jload(value: Optional[str], default: Any):
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def slugify(text: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "asset"


def money(cents: int | float) -> float:
    return round(float(cents or 0) / 100.0, 2)


def hash_secret(secret: str, salt: Optional[bytes] = None) -> tuple[str, str]:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 240_000)
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def verify_secret(secret: str, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        got = hashlib.pbkdf2_hmac("sha256", secret.encode("utf-8"), salt, 240_000)
        return hmac.compare_digest(got, expected)
    except Exception:
        return False


def default_settings() -> dict[str, Any]:
    return {
        "brand": "ATV DESIGN",
        "positioning": "Plataforma de recursos digitais",
        "freeDownloadsPerDay": 5,
        "premiumDownloadsPerDay": 10,
        "freeDownloadsPerMonth": 150,
        "premiumDownloadsPerMonth": 450,
        "premiumMonthlyCents": 3490,
        "premiumAnnualCents": 30000,
        "premiumAnnualInstallments": 12,
        "premiumAnnualInstallmentCents": 2500,
        "individualPriceCents": 500,
        "withdrawalMinimumCents": 5000,
        "releaseDays": 0,
        "platformCommission": {"type": "FIXED", "fixedCents": 200, "percent": 0},
        "gatewayFee": {"percent": 0, "fixedCents": 0, "configured": False},
        "premiumCreatorCompensation": {
            "model": "PER_VALID_DOWNLOAD_PREPARED",
            "premiumCents": 60,
            "premiumMaxCents": 70,
            "premiumMaxMinCents": 70,
            "premiumMaxMaxCents": 80,
            "paymentsEnabled": False,
        },
        "planContent": {
            "FREE": {
                "description": "Para usar a ATV gratuitamente e baixar recursos Free.",
                "benefits": ["Acesso à biblioteca de recursos gratuitos.", "Compra avulsa de recursos quando disponível."],
                "showDailyDownloads": True,
            },
            "PREMIUM": {
                "description": "Acesso ampliado à biblioteca ATV Premium.",
                "benefits": ["Recursos Premium e Premium Max inclusos.", "Favoritos e histórico.", "✏️ Editor ATV — Em breve"],
                "showDailyDownloads": True,
            },
        },
        "moderationMode": "MANUAL_REVIEW",
        "resourceTypes": ["PSD", "PNG", "JPG", "CANVA", "VETORES", "FLYERS", "DATAS COMEMORATIVAS", "MOCKUPS", "TEXTURAS", "SELOS 3D", "OUTROS"],
        "watermarkOpacity": 0.18,
        "watermarkScale": 0.22,
        "watermarkRotation": -26,
        "watermarkMode": "repeat",
        "appearance": {"primary": "#5923c8", "secondary": "#35107e", "accent": "#65D17A", "background": "#f8f9fb", "text": "#172033"},
        "branding": {"logoLight": "", "logoDark": "", "favicon": ""},
        "content": {
            "heroKicker": "ATV · RECURSOS DIGITAIS",
            "heroTitle": "CRIE MAIS. PROCURE MENOS.",
            "heroSubtitle": "Encontre PSDs, PNGs, vetores, mockups, texturas e muito mais para dar vida às suas ideias. Recursos gratuitos, Premium e compras avulsas — tudo em um só lugar.",
            "footerDescription": "Plataforma de recursos digitais para transformar ideias em projetos com mais agilidade.",
        },
        "general": {"contactEmail": "", "instagram": "", "behance": "", "youtube": ""},
        "featuredProductIds": [],
        "creatorHighlights": {"mode": "HYBRID", "manualUserIds": [], "max": 5},
        "priceRules": {"minimumCents": 500, "allowIndividual": False},
        "aiModeration": {"enabled": False, "mode": "AUTO_AFTER_TRUST", "startAfterUploads": 10, "provider": "OPENAI", "model": "omni-moderation-latest"},
        "founderPremiumCampaign": {
            "enabled": False,
            "limit": 10,
            "autoClose": True,
            "title": "10 PRIMEIROS COLABORADORES",
            "closedReason": None
        },
    }


def create_demo_psd(path: Path):
    # PSD 1x1 RGB, 8-bit, imagem composta RAW preta.
    if path.exists():
        return
    header = b"8BPS" + (1).to_bytes(2, "big") + b"\x00" * 6
    header += (3).to_bytes(2, "big") + (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
    header += (8).to_bytes(2, "big") + (3).to_bytes(2, "big")
    sections = (0).to_bytes(4, "big") * 3
    image = (0).to_bytes(2, "big") + bytes([20, 20, 20])
    path.write_bytes(header + sections + image)


def detect_file_format(path: Path) -> str:
    """Formato técnico pela assinatura real; extensão é somente fallback."""
    try:
        if path.exists() and path.is_file():
            with path.open("rb") as fh:
                head = fh.read(16)
            if head.startswith(b"8BPS"):
                return "PSD"
            if head.startswith(b"\x89PNG\r\n\x1a\n"):
                return "PNG"
            if head.startswith(b"\xff\xd8\xff"):
                return "JPG"
    except OSError:
        pass
    suffix = path.suffix.lower().lstrip(".")
    return {"jpeg":"JPG","jpg":"JPG","png":"PNG","psd":"PSD"}.get(suffix, "")


def resolve_private_file(stored_path: str | Path | None, folder: Path = ORIGINALS) -> Path | None:
    """Resolve arquivos privados mesmo após a pasta data ser copiada entre versões."""
    if not stored_path:
        return None
    p=Path(str(stored_path))
    if p.exists() and p.is_file():
        return p
    candidate=folder/p.name
    if candidate.exists() and candidate.is_file():
        return candidate
    # Último fallback: nome único no diretório atual de storage.
    try:
        matches=list(folder.glob(p.name))
        if len(matches)==1 and matches[0].is_file():
            return matches[0]
    except OSError:
        pass
    return None


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users(
              id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
              first_name TEXT NOT NULL, last_name TEXT NOT NULL, profile_json TEXT NOT NULL DEFAULT '{}', roles_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'ACTIVE', plan TEXT NOT NULL DEFAULT 'FREE', premium_active INTEGER NOT NULL DEFAULT 0,
              premium_cycle TEXT NOT NULL DEFAULT 'MONTHLY', creator_status TEXT NOT NULL DEFAULT 'ACTIVE', profile_complete INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions(
              token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, csrf_token TEXT NOT NULL,
              expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins(
              id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE',
              pin_salt TEXT NOT NULL, pin_hash TEXT NOT NULL, key_salt TEXT NOT NULL, key_hash TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_sessions(
              token TEXT PRIMARY KEY, admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE, csrf_token TEXT NOT NULL,
              expires_at TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_challenges(
              id TEXT PRIMARY KEY, admin_id TEXT NOT NULL REFERENCES admins(id) ON DELETE CASCADE, expires_at TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS products(
              id TEXT PRIMARY KEY, slug TEXT UNIQUE NOT NULL, owner_id TEXT NULL REFERENCES users(id) ON DELETE SET NULL,
              title TEXT NOT NULL, seller TEXT NOT NULL, category TEXT NOT NULL, formats_json TEXT NOT NULL, description TEXT NOT NULL,
              preview_url TEXT NOT NULL, original_path TEXT NULL, license TEXT NOT NULL, access_tier TEXT NOT NULL,
              individual_purchase_enabled INTEGER NOT NULL DEFAULT 0, individual_price_cents INTEGER NULL, status TEXT NOT NULL,
              tags_json TEXT NOT NULL DEFAULT '[]', featured INTEGER NOT NULL DEFAULT 0, hidden INTEGER NOT NULL DEFAULT 0, admin_removed INTEGER NOT NULL DEFAULT 0,
              sales INTEGER NOT NULL DEFAULT 0, premium_downloads INTEGER NOT NULL DEFAULT 0, free_downloads INTEGER NOT NULL DEFAULT 0,
              created_by_admin INTEGER NOT NULL DEFAULT 0, moderation_json TEXT NOT NULL DEFAULT '{}', history_json TEXT NOT NULL DEFAULT '[]',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS uploads(
              id TEXT PRIMARY KEY, user_id TEXT NULL REFERENCES users(id) ON DELETE CASCADE, admin_id TEXT NULL REFERENCES admins(id) ON DELETE CASCADE,
              original_path TEXT NOT NULL, preview_path TEXT NOT NULL, extension TEXT NOT NULL, metadata_json TEXT NOT NULL,
              preview_method TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), total_cents INTEGER NOT NULL,
              status TEXT NOT NULL, gateway_mode TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS order_items(
              id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
              product_id TEXT NOT NULL REFERENCES products(id), price_cents INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entitlements(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              product_id TEXT NOT NULL REFERENCES products(id), order_id TEXT NULL REFERENCES orders(id), source TEXT NOT NULL, created_at TEXT NOT NULL,
              UNIQUE(user_id, product_id, source)
            );
            CREATE TABLE IF NOT EXISTS download_events(
              id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              product_id TEXT NOT NULL REFERENCES products(id), source TEXT NOT NULL, day TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS download_tokens(
              token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              product_id TEXT NOT NULL REFERENCES products(id), expires_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'FREE'
            );
            CREATE TABLE IF NOT EXISTS favorites(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, PRIMARY KEY(user_id, product_id));
            CREATE TABLE IF NOT EXISTS collections(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, PRIMARY KEY(user_id, product_id));
            CREATE TABLE IF NOT EXISTS follows(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, seller TEXT NOT NULL, PRIMARY KEY(user_id, seller));
            CREATE TABLE IF NOT EXISTS cart(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, PRIMARY KEY(user_id, product_id));
            CREATE TABLE IF NOT EXISTS support_tickets(id TEXT PRIMARY KEY, user_id TEXT NULL, name TEXT NOT NULL, email TEXT NOT NULL, subject TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, public_token TEXT NULL, created_at TEXT NOT NULL, updated_at TEXT NULL);
            CREATE TABLE IF NOT EXISTS support_messages(id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE, sender_type TEXT NOT NULL, sender_id TEXT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_messages(ticket_id,created_at);
            CREATE TABLE IF NOT EXISTS user_notifications(
              id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              type TEXT NOT NULL,
              title TEXT NOT NULL,
              message TEXT NOT NULL,
              action TEXT NOT NULL DEFAULT '',
              product_id TEXT NULL,
              read_at TEXT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(user_id,type,product_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_notifications_user ON user_notifications(user_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS product_reviews(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id,product_id));
            CREATE TABLE IF NOT EXISTS reports(id TEXT PRIMARY KEY, user_id TEXT NULL, name TEXT NOT NULL, email TEXT NOT NULL, resource TEXT NOT NULL, reason TEXT NOT NULL, description TEXT NOT NULL, evidence_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_logs(id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wallets(user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, available_cents INTEGER NOT NULL DEFAULT 0, pending_cents INTEGER NOT NULL DEFAULT 0, blocked_cents INTEGER NOT NULL DEFAULT 0, withdrawn_cents INTEGER NOT NULL DEFAULT 0, total_earned_cents INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS founder_premium_grants(
              user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
              slot INTEGER UNIQUE NOT NULL,
              status TEXT NOT NULL DEFAULT 'ACTIVE',
              granted_at TEXT NOT NULL,
              granted_by TEXT NOT NULL DEFAULT 'CAMPAIGN'
            );
            CREATE TABLE IF NOT EXISTS product_files(
              id TEXT PRIMARY KEY,
              product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
              original_path TEXT NOT NULL,
              extension TEXT NOT NULL,
              filename TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_product_files_product ON product_files(product_id);
            CREATE TABLE IF NOT EXISTS download_compensation_records(
              id TEXT PRIMARY KEY,
              download_event_id TEXT UNIQUE NOT NULL REFERENCES download_events(id) ON DELETE CASCADE,
              owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
              access_tier TEXT NOT NULL,
              compensation_cents INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'PREPARED',
              created_at TEXT NOT NULL
            );
            """
        )
        if not c.execute("SELECT 1 FROM settings WHERE id=1").fetchone():
            c.execute("INSERT INTO settings(id,data,updated_at) VALUES(1,?,?)", (jdump(default_settings()), iso_now()))
        # Migração V6 incremental: preserva configurações personalizadas e corrige apenas defaults legados da V5.
        if not c.execute("SELECT 1 FROM app_meta WHERE key='v6_plan_premium_max_migrated'").fetchone():
            settings_row=c.execute("SELECT data FROM settings WHERE id=1").fetchone()
            saved=jload(settings_row["data"],{}) if settings_row else {}
            defaults=default_settings()
            if int(saved.get("premiumDownloadsPerDay",15)) == 15:
                saved["premiumDownloadsPerDay"]=10
            if not isinstance(saved.get("premiumCreatorCompensation"),dict) or saved.get("premiumCreatorCompensation",{}).get("model") in (None,"UNDEFINED"):
                saved["premiumCreatorCompensation"]=defaults["premiumCreatorCompensation"]
            if "planContent" not in saved:
                saved["planContent"]=defaults["planContent"]
            c.execute("UPDATE settings SET data=?,updated_at=? WHERE id=1",(jdump(saved),iso_now()))
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v6_plan_premium_max_migrated","1",iso_now()))
        # Migração V7 incremental: novas estruturas sem apagar dados V6.
        if not c.execute("SELECT 1 FROM app_meta WHERE key='v7_social_reviews_support_migrated'").fetchone():
            product_cols={r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
            if "admin_removed" not in product_cols:
                c.execute("ALTER TABLE products ADD COLUMN admin_removed INTEGER NOT NULL DEFAULT 0")
            token_cols={r["name"] for r in c.execute("PRAGMA table_info(download_tokens)").fetchall()}
            if "source" not in token_cols:
                c.execute("ALTER TABLE download_tokens ADD COLUMN source TEXT NOT NULL DEFAULT 'FREE'")
            ticket_cols={r["name"] for r in c.execute("PRAGMA table_info(support_tickets)").fetchall()}
            if "public_token" not in ticket_cols:
                c.execute("ALTER TABLE support_tickets ADD COLUMN public_token TEXT NULL")
            if "updated_at" not in ticket_cols:
                c.execute("ALTER TABLE support_tickets ADD COLUMN updated_at TEXT NULL")
            c.execute("CREATE TABLE IF NOT EXISTS support_messages(id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE, sender_type TEXT NOT NULL, sender_id TEXT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_support_messages_ticket ON support_messages(ticket_id,created_at)")
            # Preserva chamados V6: a mensagem original vira a primeira mensagem da conversa no Admin.
            c.execute("UPDATE support_tickets SET updated_at=created_at WHERE updated_at IS NULL")
            for old_ticket in c.execute("SELECT id,user_id,message,created_at FROM support_tickets").fetchall():
                if old_ticket["message"] and not c.execute("SELECT 1 FROM support_messages WHERE ticket_id=? LIMIT 1",(old_ticket["id"],)).fetchone():
                    c.execute("INSERT INTO support_messages(id,ticket_id,sender_type,sender_id,message,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),old_ticket["id"],"USER",old_ticket["user_id"],old_ticket["message"],old_ticket["created_at"]))
            c.execute("CREATE TABLE IF NOT EXISTS product_reviews(id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE, rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(user_id,product_id))")
            srow=c.execute("SELECT data FROM settings WHERE id=1").fetchone(); saved=jload(srow["data"],{}) if srow else {}; defaults=default_settings()
            if "creatorHighlights" not in saved: saved["creatorHighlights"]=defaults["creatorHighlights"]
            old_ai=saved.get("aiModeration") if isinstance(saved.get("aiModeration"),dict) else {}
            saved["aiModeration"]={**defaults["aiModeration"],**old_ai}
            c.execute("UPDATE settings SET data=?,updated_at=? WHERE id=1",(jdump(saved),iso_now()))
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v7_social_reviews_support_migrated","1",iso_now()))
        if not c.execute("SELECT 1 FROM app_meta WHERE key='v8_stabilization_migrated'").fetchone():
            settings_row=c.execute("SELECT data FROM settings WHERE id=1").fetchone()
            saved=jload(settings_row["data"],{}) if settings_row else {}
            defaults=default_settings()
            saved["aiModeration"]={**defaults["aiModeration"],**(saved.get("aiModeration") if isinstance(saved.get("aiModeration"),dict) else {}),"enabled":False}
            c.execute("UPDATE settings SET data=?,updated_at=? WHERE id=1",(jdump(saved),iso_now()))
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v8_stabilization_migrated","1",iso_now()))
        if not c.execute("SELECT 1 FROM app_meta WHERE key='v81_final_stabilization_migrated'").fetchone():
            c.execute("UPDATE products SET individual_purchase_enabled=0 WHERE access_tier='PREMIUM_MAX'")
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v81_final_stabilization_migrated","1",iso_now()))
        if not c.execute("SELECT 1 FROM app_meta WHERE key='v82_format_access_migrated'").fetchone():
            rows=c.execute("SELECT id,access_tier,original_path,formats_json FROM products").fetchall()
            for row in rows:
                suffix=Path(str(row["original_path"] or "")).suffix.lower().lstrip(".")
                primary={"jpeg":"JPG","jpg":"JPG","png":"PNG","psd":"PSD"}.get(suffix)
                raw=[str(x).upper() for x in jload(row["formats_json"],[]) if str(x).strip()]
                if not primary:
                    primary=(raw or [""])[0]
                    if primary=="JPEG": primary="JPG"
                if row["access_tier"]=="PREMIUM_MAX":
                    extras=[str(x["extension"]).upper() for x in c.execute("SELECT extension FROM product_files WHERE product_id=? ORDER BY created_at,id",(row["id"],)).fetchall()]
                    formats=[]
                    for fmt in [primary,*extras]:
                        fmt="JPG" if fmt=="JPEG" else fmt
                        if fmt and fmt not in formats: formats.append(fmt)
                else:
                    formats=[primary] if primary else raw[:1]
                c.execute("UPDATE products SET formats_json=? WHERE id=?",(jdump(formats),row["id"]))
            c.execute("UPDATE products SET individual_purchase_enabled=0 WHERE access_tier='PREMIUM_MAX'")
            c.execute("DELETE FROM cart WHERE product_id IN (SELECT id FROM products WHERE access_tier='PREMIUM_MAX')")
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v82_format_access_migrated","1",iso_now()))
        if not c.execute("SELECT 1 FROM app_meta WHERE key='v83_apply_audit_migrated'").fetchone():
            # V8.3: reaplica reparos mesmo quando V8.2 já foi marcada no banco.
            # Também corrige caminhos absolutos que apontam para a pasta da versão anterior.
            for table, column, folder in (
                ("products", "original_path", ORIGINALS),
                ("uploads", "original_path", ORIGINALS),
                ("uploads", "preview_path", PREVIEWS),
                ("product_files", "original_path", ORIGINALS),
            ):
                rows = c.execute(f"SELECT rowid,{column} value FROM {table} WHERE {column} IS NOT NULL AND {column}!=''").fetchall()
                for item in rows:
                    old = Path(str(item["value"]))
                    candidate = folder / old.name
                    if candidate.exists() and str(old) != str(candidate):
                        c.execute(f"UPDATE {table} SET {column}=? WHERE rowid=?", (str(candidate), item["rowid"]))

            rows = c.execute("SELECT id,access_tier,original_path,formats_json FROM products").fetchall()
            for row in rows:
                original = Path(str(row["original_path"] or ""))
                suffix = original.suffix.lower().lstrip(".")
                primary = {"jpeg":"JPG","jpg":"JPG","png":"PNG","psd":"PSD"}.get(suffix)

                if not primary:
                    try:
                        if original.exists():
                            head = original.read_bytes()[:16]
                            if head.startswith(b"8BPS"):
                                primary = "PSD"
                            elif head.startswith(b"\x89PNG\r\n\x1a\n"):
                                primary = "PNG"
                            elif head.startswith(b"\xff\xd8\xff"):
                                primary = "JPG"
                    except OSError:
                        pass

                raw = [str(x).upper() for x in jload(row["formats_json"], []) if str(x).strip()]
                if not primary:
                    primary = (raw or [""])[0]
                    if primary == "JPEG":
                        primary = "JPG"

                if row["access_tier"] == "PREMIUM_MAX":
                    extras = [
                        ("JPG" if str(x["extension"]).upper() == "JPEG" else str(x["extension"]).upper())
                        for x in c.execute(
                            "SELECT extension FROM product_files WHERE product_id=? ORDER BY created_at,id",
                            (row["id"],)
                        ).fetchall()
                    ]
                    formats = []
                    for fmt in [primary, *extras]:
                        if fmt and fmt not in formats:
                            formats.append(fmt)
                else:
                    formats = [primary] if primary else raw[:1]

                c.execute("UPDATE products SET formats_json=? WHERE id=?", (jdump(formats), row["id"]))

            c.execute("UPDATE products SET individual_purchase_enabled=0 WHERE access_tier='PREMIUM_MAX'")
            c.execute("DELETE FROM cart WHERE product_id IN (SELECT id FROM products WHERE access_tier='PREMIUM_MAX')")
            c.execute(
                "INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",
                ("v83_apply_audit_migrated","1",iso_now())
            )

        if not c.execute("SELECT 1 FROM app_meta WHERE key='v84_root_fixes_migrated'").fetchone():
            # 1) Formato: assinatura binária é fonte de verdade.
            rows = c.execute("SELECT id,access_tier,original_path,formats_json FROM products").fetchall()
            for row in rows:
                primary = detect_file_format(Path(str(row["original_path"] or "")))
                raw = [str(x).upper() for x in jload(row["formats_json"], []) if str(x).strip()]
                if not primary:
                    primary = (raw or [""])[0]
                    if primary == "JPEG":
                        primary = "JPG"

                if row["access_tier"] == "PREMIUM_MAX":
                    extras = []
                    for f in c.execute(
                        "SELECT original_path,extension FROM product_files WHERE product_id=? ORDER BY created_at,id",
                        (row["id"],)
                    ).fetchall():
                        detected = detect_file_format(Path(str(f["original_path"] or "")))
                        fmt = detected or str(f["extension"] or "").upper()
                        if fmt == "JPEG":
                            fmt = "JPG"
                        if fmt and fmt not in extras:
                            extras.append(fmt)
                    formats = []
                    for fmt in [primary, *extras]:
                        if fmt and fmt not in formats:
                            formats.append(fmt)
                else:
                    formats = [primary] if primary else raw[:1]

                c.execute(
                    "UPDATE products SET formats_json=?,individual_purchase_enabled=CASE WHEN access_tier='PREMIUM_MAX' THEN 0 ELSE individual_purchase_enabled END WHERE id=?",
                    (jdump(formats), row["id"])
                )

            c.execute("DELETE FROM cart WHERE product_id IN (SELECT id FROM products WHERE access_tier='PREMIUM_MAX')")

            # 2) Repara bloqueio legado de Colaborador somente quando NÃO existe
            #    um bloqueio explícito de Cliente nos logs administrativos.
            users = c.execute(
                "SELECT id,roles_json,status,creator_status FROM users WHERE status!='ACTIVE' AND creator_status='SUSPENDED' AND roles_json LIKE '%collaborator%'"
            ).fetchall()
            audit_rows = c.execute(
                "SELECT action,details_json,created_at FROM audit_logs WHERE action IN ('CLIENT_STATUS_CHANGED','COLLABORATOR_STATUS_CHANGED') ORDER BY created_at DESC"
            ).fetchall()
            for u in users:
                explicit_client_block = False
                for log in audit_rows:
                    details = jload(log["details_json"], {})
                    if str(details.get("userId")) != str(u["id"]):
                        continue
                    if log["action"] == "CLIENT_STATUS_CHANGED":
                        explicit_client_block = str(details.get("after", "")).upper() in ("BLOCKED","DISABLED")
                        break
                if not explicit_client_block:
                    c.execute("UPDATE users SET status='ACTIVE',updated_at=? WHERE id=?", (iso_now(), u["id"]))

            c.execute(
                "INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",
                ("v84_root_fixes_migrated","1",iso_now())
            )

        if not c.execute("SELECT 1 FROM app_meta WHERE key='v86_final_operational_migrated'").fetchone():
            c.execute("CREATE TABLE IF NOT EXISTS user_notifications(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,type TEXT NOT NULL,title TEXT NOT NULL,message TEXT NOT NULL,action TEXT NOT NULL DEFAULT '',product_id TEXT NULL,read_at TEXT NULL,created_at TEXT NOT NULL,UNIQUE(user_id,type,product_id))")
            c.execute("CREATE INDEX IF NOT EXISTS idx_user_notifications_user ON user_notifications(user_id,created_at DESC)")
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v86_final_operational_migrated","1",iso_now()))

        if not c.execute("SELECT 1 FROM app_meta WHERE key='v87_creator_experience_migrated'").fetchone():
            product_cols={r["name"] for r in c.execute("PRAGMA table_info(products)").fetchall()}
            if "external_url" not in product_cols:
                c.execute("ALTER TABLE products ADD COLUMN external_url TEXT NULL")
            if "delivery_mode" not in product_cols:
                c.execute("ALTER TABLE products ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'FILE'")
            c.execute("""CREATE TABLE IF NOT EXISTS collection_folders(
                id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                UNIQUE(user_id,name)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS collection_items(
                collection_id TEXT NOT NULL REFERENCES collection_folders(id) ON DELETE CASCADE,
                product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,PRIMARY KEY(collection_id,product_id)
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_collection_folders_user ON collection_folders(user_id,updated_at DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_collection_items_product ON collection_items(product_id)")
            settings_row=c.execute("SELECT data FROM settings WHERE id=1").fetchone()
            saved=jload(settings_row["data"],{}) if settings_row else default_settings()
            types=list(saved.get("resourceTypes",[]))
            if "CANVA" not in types:
                # Canva fica perto dos formatos principais sem apagar categorias existentes.
                insert_at=min(3,len(types));types.insert(insert_at,"CANVA")
                saved["resourceTypes"]=list(dict.fromkeys(types))
                c.execute("UPDATE settings SET data=?,updated_at=? WHERE id=1",(jdump(saved),iso_now()))
            # Migra a antiga coleção única apenas para contas Premium; a tabela antiga é preservada.
            premium_users=c.execute("SELECT id FROM users WHERE premium_active=1").fetchall()
            for pu in premium_users:
                old_items=c.execute("SELECT product_id FROM collections WHERE user_id=?",(pu["id"],)).fetchall()
                if not old_items:
                    continue
                folder=c.execute("SELECT id FROM collection_folders WHERE user_id=? ORDER BY created_at LIMIT 1",(pu["id"],)).fetchone()
                if not folder:
                    fid=str(uuid.uuid4());now=iso_now()
                    c.execute("INSERT INTO collection_folders(id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",(fid,pu["id"],"Salvos",now,now));folder={"id":fid}
                for old in old_items:
                    c.execute("INSERT OR IGNORE INTO collection_items(collection_id,product_id,created_at) VALUES(?,?,?)",(folder["id"],old["product_id"],iso_now()))
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v87_creator_experience_migrated","1",iso_now()))

        if not c.execute("SELECT 1 FROM app_meta WHERE key='v871_ui_wiring_migrated'").fetchone():
            # Corrige configurações antigas em que a categoria podia estar
            # salva como "Canva"/"canva", sem tocar em dados financeiros.
            settings_row=c.execute("SELECT data FROM settings WHERE id=1").fetchone()
            saved=jload(settings_row["data"],{}) if settings_row else default_settings()
            raw_types=list(saved.get("resourceTypes",[]))
            normalized_types=[]
            for item in raw_types:
                value=str(item or "").strip()
                if not value:
                    continue
                if value.upper()=="CANVA":
                    value="CANVA"
                if value not in normalized_types:
                    normalized_types.append(value)
            if "CANVA" not in normalized_types:
                normalized_types.insert(min(3,len(normalized_types)),"CANVA")
            saved["resourceTypes"]=normalized_types
            c.execute("UPDATE settings SET data=?,updated_at=? WHERE id=1",(jdump(saved),iso_now()))
            c.execute("UPDATE products SET category='CANVA',updated_at=? WHERE UPPER(TRIM(category))='CANVA' AND category!='CANVA'",(iso_now(),))
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)",("v871_ui_wiring_migrated","1",iso_now()))

        if not c.execute("SELECT 1 FROM admins WHERE id='adm-main'").fetchone():
            ps, ph = hash_secret(ADMIN_PIN)
            ks, kh = hash_secret(ADMIN_KEY)
            c.execute(
                "INSERT INTO admins(id,name,role,status,pin_salt,pin_hash,key_salt,key_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                ("adm-main", "Admin Principal", "SUPER_ADMIN", "ACTIVE", ps, ph, ks, kh, iso_now()),
            )


init_db()

# ---------------------------------------------------------------------
# Security / sessions
# ---------------------------------------------------------------------
RATE: dict[str, list[float]] = {}


def rate_limit(key: str, limit: int, window_seconds: int):
    now = time.time()
    arr = [t for t in RATE.get(key, []) if now - t < window_seconds]
    if len(arr) >= limit:
        raise HTTPException(429, "Muitas tentativas. Aguarde um pouco e tente novamente.")
    arr.append(now)
    RATE[key] = arr


def security_headers(response):
    response.headers["X-ATV-Build"] = BUILD_VERSION
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    content_type = response.headers.get("content-type", "")
    if content_type.startswith("application/json") or content_type.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    else:
        response.headers["Cache-Control"] = response.headers.get("Cache-Control", "public, max-age=300")
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    return security_headers(response)


def get_session(request: Request) -> tuple[Optional[sqlite3.Row], Optional[sqlite3.Row]]:
    token = request.cookies.get(USER_COOKIE)
    if not token:
        return None, None
    with db() as c:
        sess = c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
        if not sess:
            return None, None
        if datetime.fromisoformat(sess["expires_at"]) <= utcnow():
            c.execute("DELETE FROM sessions WHERE token=?", (token,))
            return None, None
        user = c.execute("SELECT * FROM users WHERE id=?", (sess["user_id"],)).fetchone()
        if not user or user["status"] != "ACTIVE":
            return None, None
        return sess, user


def require_user(request: Request, csrf: bool = False, collaborator: bool = False) -> sqlite3.Row:
    sess, user = get_session(request)
    if not user:
        raise HTTPException(401, "Autenticação necessária para esta ação.")
    if csrf and request.headers.get("x-csrf-token") != sess["csrf_token"]:
        raise HTTPException(403, "Token de segurança inválido. Recarregue a página.")
    roles = jload(user["roles_json"], [])
    if collaborator and ("collaborator" not in roles or user["creator_status"] != "ACTIVE"):
        raise HTTPException(403, "Permissão de Colaborador ATV necessária.")
    return user


def get_admin_session(request: Request) -> tuple[Optional[sqlite3.Row], Optional[sqlite3.Row]]:
    token = request.cookies.get(ADMIN_COOKIE)
    if not token:
        return None, None
    with db() as c:
        sess = c.execute("SELECT * FROM admin_sessions WHERE token=?", (token,)).fetchone()
        if not sess:
            return None, None
        if datetime.fromisoformat(sess["expires_at"]) <= utcnow():
            c.execute("DELETE FROM admin_sessions WHERE token=?", (token,))
            return None, None
        admin = c.execute("SELECT * FROM admins WHERE id=?", (sess["admin_id"],)).fetchone()
        if not admin or admin["status"] != "ACTIVE":
            return None, None
        return sess, admin


def require_admin(request: Request, csrf: bool = False, roles: Optional[set[str]] = None) -> sqlite3.Row:
    sess, admin = get_admin_session(request)
    if not admin:
        raise HTTPException(401, "Acesso administrativo não autorizado.")
    if csrf and request.headers.get("x-admin-csrf-token") != sess["csrf_token"]:
        raise HTTPException(403, "Token administrativo inválido. Recarregue o painel.")
    if roles and admin["role"] not in roles:
        raise HTTPException(403, "Seu nível administrativo não permite esta operação.")
    return admin


def create_user_session(user_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(28)
    expires = utcnow() + timedelta(hours=SESSION_HOURS)
    with db() as c:
        c.execute("DELETE FROM sessions WHERE expires_at <= ?", (iso_now(),))
        c.execute("INSERT INTO sessions(token,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (token, user_id, csrf, expires.isoformat(), iso_now()))
    return token, csrf


def attach_user_cookie(response: JSONResponse, token: str):
    response.set_cookie(USER_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="lax", max_age=SESSION_HOURS * 3600, path="/")


def create_admin_session(admin_id: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(40)
    csrf = secrets.token_urlsafe(28)
    expires = utcnow() + timedelta(hours=min(SESSION_HOURS, 12))
    with db() as c:
        c.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (iso_now(),))
        c.execute("INSERT INTO admin_sessions(token,admin_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)", (token, admin_id, csrf, expires.isoformat(), iso_now()))
    return token, csrf


def attach_admin_cookie(response: JSONResponse, token: str):
    response.set_cookie(ADMIN_COOKIE, token, httponly=True, secure=COOKIE_SECURE, samesite="strict", max_age=min(SESSION_HOURS, 12) * 3600, path="/")


def audit(actor: str, action: str, details: Optional[dict] = None):
    with db() as c:
        c.execute("INSERT INTO audit_logs(id,actor,action,details_json,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), actor, action, jdump(details or {}), iso_now()))

# ---------------------------------------------------------------------
# Settings / product views
# ---------------------------------------------------------------------

def get_settings(c: sqlite3.Connection) -> dict[str, Any]:
    row = c.execute("SELECT data FROM settings WHERE id=1").fetchone()
    data = default_settings()
    if row:
        saved = jload(row["data"], {})
        data.update(saved)
        for k in ("appearance", "branding", "content", "general", "priceRules", "aiModeration", "gatewayFee", "platformCommission", "founderPremiumCampaign", "premiumCreatorCompensation", "creatorHighlights"):
            data[k] = {**default_settings().get(k, {}), **saved.get(k, {})}
        default_plans = default_settings().get("planContent", {})
        saved_plans = saved.get("planContent", {})
        data["planContent"] = {
            key: {**default_plans.get(key, {}), **saved_plans.get(key, {})}
            for key in set(default_plans) | set(saved_plans)
        }

    # Compatibilidade com bancos/configurações antigos que possam ter
    # salvo "Canva" ou "canva". A categoria oficial da ATV é CANVA.
    raw_types = list(data.get("resourceTypes", []))
    normalized_types = []
    for item in raw_types:
        value = str(item or "").strip()
        if not value:
            continue
        if value.upper() == "CANVA":
            value = "CANVA"
        if value not in normalized_types:
            normalized_types.append(value)
    if "CANVA" not in normalized_types:
        normalized_types.insert(min(3, len(normalized_types)), "CANVA")
    data["resourceTypes"] = normalized_types
    return data


def save_settings(c: sqlite3.Connection, settings: dict[str, Any]):
    c.execute("UPDATE settings SET data=?,updated_at=? WHERE id=1", (jdump(settings), iso_now()))


def bump_catalog(c: sqlite3.Connection, reason: str = "catalog") -> str:
    value = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    c.execute(
        "INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES('catalog_revision',?,?)",
        (value, iso_now()),
    )
    return value


def catalog_revision(c: sqlite3.Connection) -> str:
    row = c.execute("SELECT value FROM app_meta WHERE key='catalog_revision'").fetchone()
    return row["value"] if row else bump_catalog(c, "bootstrap")


def founder_feature_access(c: sqlite3.Connection, user_id: str, required_plan: str = "PREMIUM") -> bool:
    """Fundadores ativos herdam recursos pagos presentes e futuros sem criar um novo plano hoje."""
    if str(required_plan).upper() == "FREE":
        return True
    return bool(c.execute("SELECT 1 FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'", (user_id,)).fetchone())


def product_rating(c: sqlite3.Connection, product_id: str) -> tuple[Optional[float], int]:
    r=c.execute("SELECT AVG(rating) avg_rating,COUNT(*) n FROM product_reviews WHERE product_id=?",(product_id,)).fetchone()
    n=int(r["n"] or 0)
    return (round(float(r["avg_rating"]),1) if n else None,n)


def highlighted_collaborators(c: sqlite3.Connection, settings: Optional[dict[str,Any]]=None) -> list[dict[str,Any]]:
    s=settings or get_settings(c); cfg={**default_settings()["creatorHighlights"],**s.get("creatorHighlights",{})}
    mode=str(cfg.get("mode","HYBRID")).upper(); maximum=max(1,min(10,int(cfg.get("max",5))))
    manual=[str(x) for x in cfg.get("manualUserIds",[]) if x]
    users={r["id"]:r for r in c.execute("SELECT * FROM users WHERE creator_status='ACTIVE' AND roles_json LIKE '%collaborator%'").fetchall()}
    selected=[]
    if mode in ("HYBRID","MANUAL"):
        selected=[uid for uid in manual if uid in users][:maximum]
    if mode in ("HYBRID","AUTO") and len(selected)<maximum:
        month=utcnow().strftime("%Y-%m")
        scores=[]
        for uid,u in users.items():
            if uid in selected: continue
            published=int(c.execute("SELECT COUNT(*) n FROM products WHERE owner_id=? AND status='PUBLISHED' AND hidden=0 AND admin_removed=0 AND substr(created_at,1,7)=?",(uid,month)).fetchone()["n"])
            downloads=int(c.execute("SELECT COUNT(*) n FROM download_events d JOIN products p ON p.id=d.product_id WHERE p.owner_id=? AND substr(d.created_at,1,7)=?",(uid,month)).fetchone()["n"])
            total=int(c.execute("SELECT COUNT(*) n FROM products WHERE owner_id=? AND status='PUBLISHED' AND hidden=0 AND admin_removed=0",(uid,)).fetchone()["n"])
            scores.append((downloads*5+published*3+total,uid))
        scores.sort(key=lambda x:(-x[0],x[1])); selected += [uid for _,uid in scores[:maximum-len(selected)]]
    out=[]
    for uid in selected[:maximum]:
        u=users[uid]; profile=jload(u["profile_json"],{})
        total=int(c.execute("SELECT COUNT(*) n FROM products WHERE owner_id=? AND status='PUBLISHED' AND hidden=0 AND admin_removed=0",(uid,)).fetchone()["n"])
        downloads=int(c.execute("SELECT COUNT(*) n FROM download_events d JOIN products p ON p.id=d.product_id WHERE p.owner_id=?",(uid,)).fetchone()["n"])
        out.append({"id":uid,"name":f"{u['first_name']} {u['last_name']}".strip(),"photo":profile.get("photo","") or "","publishedAssets":total,"downloads":downloads,"founder":bool(c.execute("SELECT 1 FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'",(uid,)).fetchone())})
    return out


def ai_moderate_preview(title: str, description: str, preview_path: Path, settings: dict[str,Any]) -> dict[str,Any]:
    cfg={**default_settings()["aiModeration"],**settings.get("aiModeration",{})}
    result={"provider":"OPENAI","model":str(cfg.get("model") or AI_MODERATION_MODEL),"checkedAt":iso_now(),"available":False,"flagged":None,"categories":{},"decision":"HUMAN_FALLBACK"}
    if AI_FAKE_SAFE:
        return {**result,"available":True,"flagged":False,"decision":"AUTO_APPROVE","testMode":True}
    if not AI_API_KEY:
        return {**result,"reason":"API_KEY_NOT_CONFIGURED"}
    try:
        data=preview_path.read_bytes()
        mime="image/png" if preview_path.suffix.lower()==".png" else "image/jpeg"
        data_url=f"data:{mime};base64,{base64.b64encode(data).decode()}"
        payload={"model":result["model"],"input":[{"type":"text","text":f"Título: {title}\nDescrição: {description}"},{"type":"image_url","image_url":{"url":data_url}}]}
        r=requests.post("https://api.openai.com/v1/moderations",headers={"Authorization":f"Bearer {AI_API_KEY}","Content-Type":"application/json"},json=payload,timeout=20)
        r.raise_for_status(); body=r.json(); first=(body.get("results") or [{}])[0]
        flagged=bool(first.get("flagged")); cats={k:v for k,v in (first.get("categories") or {}).items() if v}
        return {**result,"available":True,"flagged":flagged,"categories":cats,"decision":"HUMAN_REVIEW" if flagged else "AUTO_APPROVE"}
    except Exception as exc:
        return {**result,"reason":"PROVIDER_ERROR","error":str(exc)[:180]}


def founder_campaign_view(c: sqlite3.Connection, settings: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    s = settings or get_settings(c)
    cfg = {**default_settings()["founderPremiumCampaign"], **s.get("founderPremiumCampaign", {})}
    claimed = c.execute("SELECT COUNT(*) n FROM founder_premium_grants WHERE status='ACTIVE'").fetchone()["n"]
    limit = max(1, int(cfg.get("limit", 10)))
    return {
        "enabled": bool(cfg.get("enabled")) and claimed < limit,
        "configuredEnabled": bool(cfg.get("enabled")),
        "limit": limit,
        "claimed": int(claimed),
        "remaining": max(0, limit - int(claimed)),
        "full": int(claimed) >= limit,
        "autoClose": bool(cfg.get("autoClose", True)),
        "closedReason": cfg.get("closedReason"),
        "benefitBlockedAfterLimit": True,
        "title": cfg.get("title") or "10 PRIMEIROS COLABORADORES",
    }


def maybe_grant_founder_premium(c: sqlite3.Connection, user_id: str) -> Optional[int]:
    existing = c.execute("SELECT slot FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'", (user_id,)).fetchone()
    if existing:
        return int(existing["slot"])

    s = get_settings(c)
    cfg = {**default_settings()["founderPremiumCampaign"], **s.get("founderPremiumCampaign", {})}
    if not cfg.get("enabled"):
        return None

    limit = max(1, int(cfg.get("limit", 10)))
    claimed = c.execute("SELECT COUNT(*) n FROM founder_premium_grants WHERE status='ACTIVE'").fetchone()["n"]
    if claimed >= limit:
        cfg["enabled"] = False
        cfg["closedReason"] = "LIMIT_REACHED"
        s["founderPremiumCampaign"] = cfg
        save_settings(c, s)
        return None

    used = {int(r["slot"]) for r in c.execute("SELECT slot FROM founder_premium_grants WHERE status='ACTIVE'").fetchall()}
    slot = next((i for i in range(1, limit + 1) if i not in used), None)
    if slot is None:
        cfg["enabled"] = False
        cfg["closedReason"] = "LIMIT_REACHED"
        s["founderPremiumCampaign"] = cfg
        save_settings(c, s)
        return None

    now = iso_now()
    c.execute(
        "INSERT INTO founder_premium_grants(user_id,slot,status,granted_at,granted_by) VALUES(?,?,?,?,?)",
        (user_id, slot, "ACTIVE", now, "CAMPAIGN"),
    )
    c.execute(
        "UPDATE users SET plan='PREMIUM',premium_active=1,premium_cycle='LIFETIME',updated_at=? WHERE id=?",
        (now, user_id),
    )
    c.execute(
        "INSERT INTO audit_logs(id,actor,action,details_json,created_at) VALUES(?,?,?,?,?)",
        (str(uuid.uuid4()), "Sistema", "FOUNDER_PREMIUM_GRANTED", jdump({"userId": user_id, "slot": slot}), now),
    )

    if slot >= limit and cfg.get("autoClose", True):
        cfg["enabled"] = False
        cfg["closedReason"] = "LIMIT_REACHED"
        s["founderPremiumCampaign"] = cfg
        save_settings(c, s)
    return slot


def product_price_cents(row: sqlite3.Row, settings: dict[str, Any]) -> int:
    rules = settings.get("priceRules", {})
    if rules.get("allowIndividual") and row["individual_price_cents"] is not None and row["individual_price_cents"] >= int(rules.get("minimumCents", 0)):
        return int(row["individual_price_cents"])
    return int(settings["individualPriceCents"])


def product_files_for(c: sqlite3.Connection, product_id: str) -> list[dict[str, Any]]:
    rows = c.execute("SELECT id,original_path,extension,filename,created_at FROM product_files WHERE product_id=? ORDER BY created_at,id", (product_id,)).fetchall()
    return [{"id": r["id"], "extension": r["extension"], "filename": r["filename"], "createdAt": r["created_at"]} for r in rows]


def enrich_product_view(c: sqlite3.Connection, view: dict[str, Any], row: sqlite3.Row) -> dict[str, Any]:
    avg,count=product_rating(c,row["id"]); view["rating"]=avg; view["ratingCount"]=count
    if row["access_tier"] == "PREMIUM_MAX":
        view["files"] = product_files_for(c, row["id"])
        view["packageFileCount"] = len(view["files"])
    else:
        view["files"] = []
        view["packageFileCount"] = 0 if str(row["delivery_mode"] or "FILE")=="CANVA" else (1 if row["original_path"] else 0)
    return view


def record_download_compensation(c: sqlite3.Connection, event_id: str, product: sqlite3.Row, settings: dict[str, Any], source: str):
    if source != "PREMIUM" or not product["owner_id"] or product["access_tier"] not in ("PREMIUM", "PREMIUM_MAX"):
        return
    cfg = settings.get("premiumCreatorCompensation", {})
    cents = int(cfg.get("premiumCents", 60)) if product["access_tier"] == "PREMIUM" else int(cfg.get("premiumMaxCents", 70))
    c.execute(
        "INSERT OR IGNORE INTO download_compensation_records(id,download_event_id,owner_id,product_id,access_tier,compensation_cents,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), event_id, product["owner_id"], product["id"], product["access_tier"], cents, "PREPARED", iso_now()),
    )


def clean_package_filename(name: str, fallback: str) -> str:
    raw = Path(name or fallback).name
    raw = re.sub(r"[^A-Za-z0-9._()\- ]+", "-", raw).strip(" .-")
    return raw[:120] or fallback


def primary_product_format(row: sqlite3.Row) -> str:
    raw=[str(x).upper() for x in jload(row["formats_json"],[]) if str(x).strip()]
    original=Path(str(row["original_path"] or ""))
    detected=detect_file_format(original)
    if detected:
        return detected
    first=(raw or [""])[0]
    return "JPG" if first=="JPEG" else first


def product_view(row: sqlite3.Row, user: Optional[sqlite3.Row], settings: dict[str, Any]) -> dict[str, Any]:
    price = product_price_cents(row, settings)
    included = bool(user and user["premium_active"] and row["access_tier"] in ("PREMIUM", "PREMIUM_MAX"))
    raw_formats = [str(x).upper() for x in jload(row["formats_json"], []) if str(x).strip()]
    delivery_mode = str(row["delivery_mode"] or "FILE") if "delivery_mode" in row.keys() else "FILE"
    primary = "CANVA" if delivery_mode == "CANVA" else primary_product_format(row)
    if delivery_mode == "CANVA":
        formats = ["CANVA"]
    elif row["access_tier"] == "PREMIUM_MAX":
        formats = []
        for fmt in [primary, *raw_formats]:
            fmt = "JPG" if fmt == "JPEG" else fmt
            if fmt and fmt not in formats:
                formats.append(fmt)
    else:
        formats = [primary] if primary else raw_formats[:1]
    purchase_enabled = bool(row["individual_purchase_enabled"]) and row["access_tier"] != "PREMIUM_MAX"
    purchase_available = bool(purchase_enabled and not included)
    return {
        "id": row["id"], "slug": row["slug"], "ownerId": row["owner_id"], "title": row["title"], "seller": row["seller"], "category": row["category"],
        "formats": formats, "primaryFormat": primary, "rating": None, "sales": row["sales"], "premiumDownloads": row["premium_downloads"],
        "freeDownloads": row["free_downloads"], "description": row["description"], "image": row["preview_url"], "previewKey": None,
        "license": row["license"], "accessTier": row["access_tier"], "individualPurchaseEnabled": purchase_enabled,
        "individualPurchaseAvailableNow": purchase_available, "individualPrice": money(price) if purchase_available else None,
        "individualPriceCents": price, "status": row["status"], "tags": jload(row["tags_json"], []), "featured": bool(row["featured"]),
        "hidden": bool(row["hidden"]), "deliveryMode": delivery_mode, "isCanva": delivery_mode == "CANVA", "includedInCurrentPlan": included, "moderation": jload(row["moderation_json"], {}), "history": jload(row["history_json"], []),
        "createdAt": row["created_at"], "updatedAt": row["updated_at"], "createdByAdmin": bool(row["created_by_admin"]),
    }


def account_view(user: Optional[sqlite3.Row], csrf: Optional[str] = None) -> dict[str, Any]:
    with db() as c:
        settings = get_settings(c)
        if not user:
            return {
                "id": None, "name": "Visitante", "plan": "FREE", "premiumActive": False, "premiumCycle": "MONTHLY", "creatorActive": False,
                "roles": [], "profile": {}, "profileComplete": False, "csrfToken": None,
                "quotas": {"free": {"used": 0, "limit": settings["freeDownloadsPerDay"], "remaining": settings["freeDownloadsPerDay"]}, "premium": {"used": 0, "limit": settings["premiumDownloadsPerDay"], "remaining": settings["premiumDownloadsPerDay"]}},
            }
        today = utcnow().date().isoformat()
        free_used = c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND day=? AND source='FREE'", (user["id"], today)).fetchone()["n"]
        prem_used = c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND day=? AND source='PREMIUM'", (user["id"], today)).fetchone()["n"]
        profile = jload(user["profile_json"], {})
        profile.update({"firstName": user["first_name"], "lastName": user["last_name"], "email": user["email"]})
        roles = jload(user["roles_json"], [])
        founder = c.execute("SELECT slot,status,granted_at FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'", (user["id"],)).fetchone()
        return {
            "id": user["id"], "name": (user["first_name"] + " " + user["last_name"]).strip(), "email": user["email"], "status": user["status"],
            "plan": user["plan"], "premiumActive": bool(user["premium_active"]) or bool(founder), "premiumCycle": "LIFETIME" if founder else user["premium_cycle"],
            "premiumLifetime": bool(founder), "founderSlot": int(founder["slot"]) if founder else None, "founderGrantedAt": founder["granted_at"] if founder else None,
            "founderFeaturePolicy": "ALL_CURRENT_AND_FUTURE_PAID_FEATURES" if founder else None,
            "creatorActive": "collaborator" in roles and user["creator_status"] == "ACTIVE", "creatorStatus": user["creator_status"], "roles": roles,
            "profile": profile, "profileComplete": bool(user["profile_complete"]), "csrfToken": csrf,
            "quotas": {
                "free": {"used": free_used, "limit": settings["freeDownloadsPerDay"], "remaining": max(0, settings["freeDownloadsPerDay"] - free_used)},
                "premium": {"used": prem_used, "limit": settings["premiumDownloadsPerDay"], "remaining": max(0, settings["premiumDownloadsPerDay"] - prem_used)},
            },
        }

# ---------------------------------------------------------------------
# Seed demo products with current V0.6.32 visuals
# ---------------------------------------------------------------------

def seed_products():
    """Importa os assets de exemplo apenas uma vez por banco.

    Em bancos vindos da V1, a existência de qualquer produto indica que a carga
    inicial já aconteceu. Isso evita que exemplos removidos/ocultados pelo Admin
    reapareçam depois de reiniciar o servidor.
    """
    seed_file = ROOT / "seed_products.json"
    if not seed_file.exists():
        return
    with db() as c:
        if c.execute("SELECT 1 FROM app_meta WHERE key='demo_seed_imported_v1'").fetchone():
            return
        existing = c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
        if existing:
            c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)", ("demo_seed_imported_v1", "existing-db", iso_now()))
            return
        seeds = json.loads(seed_file.read_text(encoding="utf-8"))
        demo_original = ORIGINALS / "demo-seed.psd"
        create_demo_psd(demo_original)
        for p in seeds:
            c.execute(
                """INSERT INTO products(id,slug,owner_id,title,seller,category,formats_json,description,preview_url,original_path,license,access_tier,
                   individual_purchase_enabled,individual_price_cents,status,tags_json,featured,hidden,sales,premium_downloads,free_downloads,created_by_admin,
                   moderation_json,history_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    p["id"], p["slug"], None, p["title"], p["seller"], p["category"], jdump(p["formats"]), p["description"], p["image"], str(demo_original),
                    p.get("license", "Uso comercial"), p["accessTier"], 1 if p.get("individualPurchaseEnabled") else 0, None, "PUBLISHED", "[]", 0, 0,
                    int(p.get("sales", 0)), int(p.get("premiumDownloads", 0)), int(p.get("freeDownloads", 0)), 0,
                    jdump({"mode": "SEED", "result": "APPROVED"}), jdump([{"at": iso_now(), "actor": "Sistema", "action": "SEED_IMPORT", "status": "PUBLISHED"}]), iso_now(), iso_now(),
                ),
            )
        c.execute("INSERT OR REPLACE INTO app_meta(key,value,updated_at) VALUES(?,?,?)", ("demo_seed_imported_v1", "seeded", iso_now()))

seed_products()

# ---------------------------------------------------------------------
# Public + auth
# ---------------------------------------------------------------------
@app.get("/api/health")
def health():
    return {"ok": True, "build": BUILD_VERSION, "buildId": BUILD_ID, "apiVersion": app.version, "env": APP_ENV, "gateway": "PENDING_INTEGRATION", "db": "sqlite"}


@app.get("/api/runtime-status")
def runtime_status():
    if APP_ENV != "local":
        return {"build": BUILD_VERSION, "apiVersion": app.version}
    with db() as c:
        migrations = {
            key: bool(c.execute("SELECT 1 FROM app_meta WHERE key=?", (key,)).fetchone())
            for key in (
                "v6_plan_premium_max_migrated",
                "v7_social_reviews_support_migrated",
                "v8_stabilization_migrated",
                "v81_final_stabilization_migrated",
                "v82_format_access_migrated",
                "v83_apply_audit_migrated",
                "v84_root_fixes_migrated",
                "v86_final_operational_migrated",
                "v87_creator_experience_migrated",
                "v871_ui_wiring_migrated",
            )
        }
    return {
        "build": BUILD_VERSION,
        "buildId": BUILD_ID,
        "apiVersion": app.version,
        "dataDir": str(DATA),
        "database": str(DB_PATH),
        "frontend": str(FRONTEND / "index.html"),
        "migrations": migrations,
    }


@app.get("/api/config")
def config_api():
    with db() as c:
        s = get_settings(c)
        return {
            "brand": s["brand"], "positioning": s["positioning"],
            "free": {"price": 0, "downloadsPerDay": s["freeDownloadsPerDay"], "downloadsPerMonth": s["freeDownloadsPerMonth"]},
            "premium": {"monthly": money(s["premiumMonthlyCents"]), "annual": money(s["premiumAnnualCents"]), "annualInstallments": s["premiumAnnualInstallments"], "annualInstallment": money(s["premiumAnnualInstallmentCents"]), "downloadsPerDay": s["premiumDownloadsPerDay"], "downloadsPerMonth": s["premiumDownloadsPerMonth"]},
            "individualPurchase": {"price": money(s["individualPriceCents"]), "minimum": money(s["priceRules"]["minimumCents"]), "allowIndividual": bool(s["priceRules"]["allowIndividual"])},
            "resourceTypes": s["resourceTypes"], "withdrawalMinimum": money(s["withdrawalMinimumCents"]),
            "watermark": {"opacity": s["watermarkOpacity"], "scale": s["watermarkScale"], "rotation": s["watermarkRotation"], "mode": s["watermarkMode"]},
            "moderationMode": s["moderationMode"], "appearance": s["appearance"], "branding": s["branding"], "content": s["content"], "general": s["general"],
            "featuredProductIds": s.get("featuredProductIds", []), "creatorHighlights": s.get("creatorHighlights", {}), "aiModeration": {**s["aiModeration"], "providerConfigured": bool(AI_API_KEY or AI_FAKE_SAFE)},
            "gatewayFee": s["gatewayFee"],
            "planContent": s.get("planContent", {}),
            "premiumCreatorCompensation": s.get("premiumCreatorCompensation", {}),
            "founderPremium": founder_campaign_view(c, s),
            "runtime": {"mode": "HOMOLOGATION", "gatewayStatus": "PENDING_INTEGRATION"},
        }


@app.get("/api/categories")
def categories():
    with db() as c:
        return get_settings(c)["resourceTypes"]


@app.get("/api/account")
def account(request: Request):
    sess, user = get_session(request)
    return account_view(user, sess["csrf_token"] if sess else None)


@app.post("/api/auth/register")
async def register(request: Request):
    body = await request.json()
    first = str(body.get("firstName", "")).strip()
    last = str(body.get("lastName", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    role = str(body.get("role", "client"))
    if not first or not last:
        raise HTTPException(400, "Informe nome e sobrenome.")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Informe um e-mail válido.")
    if len(password) < 8:
        raise HTTPException(400, "A senha deve ter pelo menos 8 caracteres.")
    if role not in ("client", "collaborator"):
        raise HTTPException(400, "Perfil de cadastro inválido.")
    register_ip = request.client.host if request.client else "x"
    # Protege abuso sem impedir vários designers legítimos na mesma rede.
    rate_limit(f"register-ip:{register_ip}", 60, 3600)
    rate_limit(f"register-email:{register_ip}:{email}", 6, 3600)
    salt, ph = hash_secret(password)
    user_id = str(uuid.uuid4())
    roles = ["client"] + (["collaborator"] if role == "collaborator" else [])
    now = iso_now()
    try:
        with db() as c:
            c.execute(
                "INSERT INTO users(id,email,password_salt,password_hash,first_name,last_name,profile_json,roles_json,status,plan,premium_active,premium_cycle,creator_status,profile_complete,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (user_id, email, salt, ph, first, last, "{}", jdump(roles), "ACTIVE", "FREE", 0, "MONTHLY", "ACTIVE", 0, now, now),
            )
            c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (user_id,))
            if role == "collaborator":
                maybe_grant_founder_premium(c, user_id)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Já existe uma conta com este e-mail.")
    token, csrf = create_user_session(user_id)
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    response = JSONResponse({"ok": True, "account": account_view(user, csrf)})
    attach_user_cookie(response, token)
    audit(email, "USER_REGISTERED", {"userId": user_id, "role": role})
    return response


def has_explicit_client_block(c: sqlite3.Connection, user_id: str) -> bool:
    rows = c.execute(
        "SELECT details_json FROM audit_logs WHERE action='CLIENT_STATUS_CHANGED' ORDER BY created_at DESC LIMIT 500"
    ).fetchall()
    for row in rows:
        details = jload(row["details_json"], {})
        if str(details.get("userId")) == str(user_id):
            return str(details.get("after", "")).upper() in ("BLOCKED", "DISABLED")
    return False


@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    ip = request.client.host if request.client else "x"
    rate_limit(f"login:{ip}:{email}", 8, 900)
    with db() as c:
        user = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not user or not verify_secret(password, user["password_salt"], user["password_hash"]):
        raise HTTPException(401, "E-mail ou senha incorretos.")
    if user["status"] != "ACTIVE":
        roles = jload(user["roles_json"], [])
        legacy_creator_block = "collaborator" in roles and user["creator_status"] == "SUSPENDED"
        with db() as c:
            explicit_client_block = has_explicit_client_block(c, user["id"])
            if legacy_creator_block and not explicit_client_block:
                c.execute("UPDATE users SET status='ACTIVE',updated_at=? WHERE id=?", (iso_now(), user["id"]))
                user = c.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            else:
                raise HTTPException(403, "Esta conta está bloqueada ou desativada.")
    token, csrf = create_user_session(user["id"])
    response = JSONResponse({"ok": True, "account": account_view(user, csrf)})
    attach_user_cookie(response, token)
    audit(email, "USER_LOGIN", {"userId": user["id"]})
    return response


@app.post("/api/auth/logout")
def logout(request: Request):
    sess, user = get_session(request)
    if sess:
        if request.headers.get("x-csrf-token") != sess["csrf_token"]:
            raise HTTPException(403, "Token de segurança inválido.")
        with db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (request.cookies.get(USER_COOKIE),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(USER_COOKIE, path="/")
    return response


@app.patch("/api/account/profile")
async def update_profile(request: Request):
    user = require_user(request, csrf=True)
    body = await request.json()
    first = str(body.get("firstName", user["first_name"])).strip()
    last = str(body.get("lastName", user["last_name"])).strip()
    email = str(body.get("email", user["email"])).strip().lower()
    if not first or not last or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Dados de perfil inválidos.")
    old_profile = jload(user["profile_json"], {})
    profile = {**old_profile}
    for k in ("bio", "site", "instagram", "youtube", "document", "termsAccepted", "photo"):
        if k in body:
            profile[k] = body[k]
    if isinstance(profile.get("photo"), str) and len(profile["photo"]) > 180_000:
        raise HTTPException(400, "Foto de perfil muito grande.")
    complete = bool(body.get("profileComplete", user["profile_complete"]))
    try:
        with db() as c:
            c.execute("UPDATE users SET first_name=?,last_name=?,email=?,profile_json=?,profile_complete=?,updated_at=? WHERE id=?", (first, last, email, jdump(profile), 1 if complete else 0, iso_now(), user["id"]))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Este e-mail já está em uso.")
    audit(user["email"], "PROFILE_UPDATED", {"userId": user["id"]})
    sess, updated = get_session(request)
    return account_view(updated, sess["csrf_token"] if sess else None)


@app.post("/api/account/activate-collaborator")
async def activate_collaborator(request: Request):
    user = require_user(request, csrf=True)
    roles = jload(user["roles_json"], [])
    if "collaborator" not in roles:
        roles.append("collaborator")
    with db() as c:
        c.execute("UPDATE users SET roles_json=?,creator_status='ACTIVE',updated_at=? WHERE id=?", (jdump(roles), iso_now(), user["id"]))
        c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (user["id"],))
        slot = maybe_grant_founder_premium(c, user["id"])
    audit(user["email"], "COLLABORATOR_ROLE_ACTIVATED", {"userId": user["id"], "founderSlot": slot})
    sess, updated = get_session(request)
    return account_view(updated, sess["csrf_token"] if sess else None)


@app.post("/api/account/simulate-plan")
async def simulate_plan(request: Request):
    user = require_user(request, csrf=True)
    body = await request.json()
    plan = str(body.get("plan", "FREE")).upper()
    cycle = "ANNUAL" if body.get("cycle") == "ANNUAL" else "MONTHLY"
    if plan not in ("FREE", "PREMIUM"):
        raise HTTPException(400, "Plano inválido.")
    with db() as c:
        lifetime = c.execute("SELECT 1 FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'", (user["id"],)).fetchone()
        if lifetime:
            if plan != "PREMIUM":
                raise HTTPException(409, "Esta conta possui Premium vitalício e não pode ser rebaixada.")
            c.execute("UPDATE users SET plan='PREMIUM',premium_active=1,premium_cycle='LIFETIME',updated_at=? WHERE id=?", (iso_now(), user["id"]))
        else:
            c.execute("UPDATE users SET plan=?,premium_active=?,premium_cycle=?,updated_at=? WHERE id=?", (plan, 1 if plan == "PREMIUM" else 0, cycle, iso_now(), user["id"]))
    audit(user["email"], "PLAN_SIMULATED", {"plan": plan, "cycle": cycle})
    sess, updated = get_session(request)
    return account_view(updated, sess["csrf_token"] if sess else None)


@app.get("/api/account/library")
def get_library(request: Request):
    user = require_user(request)
    with db() as c:
        favorites = [r["product_id"] for r in c.execute("SELECT product_id FROM favorites WHERE user_id=?", (user["id"],)).fetchall()]
        collection = []
        if is_premium_user(c,user):
            collection=[r["product_id"] for r in c.execute("SELECT DISTINCT ci.product_id FROM collection_items ci JOIN collection_folders cf ON cf.id=ci.collection_id WHERE cf.user_id=?",(user["id"],)).fetchall()]
        following = [r["seller"] for r in c.execute("SELECT seller FROM follows WHERE user_id=?", (user["id"],)).fetchall()]
        cart = [r["product_id"] for r in c.execute("SELECT c.product_id FROM cart c JOIN products p ON p.id=c.product_id WHERE c.user_id=? AND p.access_tier!='PREMIUM_MAX' AND p.individual_purchase_enabled=1", (user["id"],)).fetchall()]
    return {"favorites": favorites, "collection": collection, "following": following, "cart": cart}


@app.patch("/api/account/library")
async def patch_library(request: Request):
    user = require_user(request, csrf=True)
    body = await request.json()
    mapping = {"favorites": ("favorites", "product_id"), "following": ("follows", "seller"), "cart": ("cart", "product_id")}
    with db() as c:
        for key, (table, col) in mapping.items():
            if key not in body:
                continue
            vals = list(dict.fromkeys(str(x) for x in (body.get(key) or [])))[:300]
            c.execute(f"DELETE FROM {table} WHERE user_id=?", (user["id"],))
            for val in vals:
                if col == "product_id":
                    product=c.execute("SELECT access_tier,individual_purchase_enabled FROM products WHERE id=?", (val,)).fetchone()
                    if not product:
                        continue
                    if key=="cart" and (product["access_tier"]=="PREMIUM_MAX" or not product["individual_purchase_enabled"]):
                        continue
                c.execute(f"INSERT OR IGNORE INTO {table}(user_id,{col}) VALUES(?,?)", (user["id"], val))
    return get_library(request)

@app.get("/api/account/collections")
def account_collections(request: Request):
    user=require_user(request)
    with db() as c:
        premium=is_premium_user(c,user)
        if not premium:
            return {"premium":False,"collections":[],"itemIds":[]}
        folders=c.execute("SELECT * FROM collection_folders WHERE user_id=? ORDER BY updated_at DESC,name",(user["id"],)).fetchall()
        out=[];all_ids=[]
        for f in folders:
            ids=[r["product_id"] for r in c.execute("SELECT ci.product_id FROM collection_items ci JOIN products p ON p.id=ci.product_id WHERE ci.collection_id=? AND p.admin_removed=0 ORDER BY ci.created_at DESC",(f["id"],)).fetchall()]
            all_ids.extend(ids);out.append({"id":f["id"],"name":f["name"],"productIds":ids,"count":len(ids),"updatedAt":f["updated_at"]})
        return {"premium":True,"collections":out,"itemIds":list(dict.fromkeys(all_ids))}


@app.post("/api/account/collections")
async def create_collection(request: Request):
    user=require_user(request,csrf=True);body=await request.json();name=str(body.get("name","")).strip()
    if not name or len(name)>40: raise HTTPException(400,"Use um nome de coleção entre 1 e 40 caracteres.")
    with db() as c:
        if not is_premium_user(c,user): raise HTTPException(403,"Coleções são exclusivas do Premium.")
        if c.execute("SELECT COUNT(*) n FROM collection_folders WHERE user_id=?",(user["id"],)).fetchone()["n"]>=30: raise HTTPException(400,"Limite de 30 coleções atingido.")
        fid=str(uuid.uuid4());now=iso_now()
        try:c.execute("INSERT INTO collection_folders(id,user_id,name,created_at,updated_at) VALUES(?,?,?,?,?)",(fid,user["id"],name,now,now))
        except sqlite3.IntegrityError:raise HTTPException(409,"Você já possui uma coleção com este nome.")
    return {"ok":True,"id":fid,"name":name}


@app.patch("/api/account/collections/{collection_id}")
async def rename_collection(collection_id: str, request: Request):
    user=require_user(request,csrf=True);body=await request.json();name=str(body.get("name","")).strip()
    if not name or len(name)>40: raise HTTPException(400,"Nome inválido.")
    with db() as c:
        if not is_premium_user(c,user): raise HTTPException(403,"Coleções são exclusivas do Premium.")
        row=c.execute("SELECT 1 FROM collection_folders WHERE id=? AND user_id=?",(collection_id,user["id"])).fetchone()
        if not row: raise HTTPException(404,"Coleção não encontrada.")
        try:c.execute("UPDATE collection_folders SET name=?,updated_at=? WHERE id=?",(name,iso_now(),collection_id))
        except sqlite3.IntegrityError:raise HTTPException(409,"Você já possui uma coleção com este nome.")
    return {"ok":True}


@app.delete("/api/account/collections/{collection_id}")
def delete_collection(collection_id: str, request: Request):
    user=require_user(request,csrf=True)
    with db() as c:
        if not is_premium_user(c,user): raise HTTPException(403,"Coleções são exclusivas do Premium.")
        row=c.execute("DELETE FROM collection_folders WHERE id=? AND user_id=? RETURNING id",(collection_id,user["id"])).fetchone()
        if not row: raise HTTPException(404,"Coleção não encontrada.")
    return {"ok":True}


@app.post("/api/account/collections/{collection_id}/items")
async def add_collection_item(collection_id: str, request: Request):
    user=require_user(request,csrf=True);body=await request.json();pid=str(body.get("productId","")).strip()
    with db() as c:
        if not is_premium_user(c,user): raise HTTPException(403,"Coleções são exclusivas do Premium.")
        if not c.execute("SELECT 1 FROM collection_folders WHERE id=? AND user_id=?",(collection_id,user["id"])).fetchone(): raise HTTPException(404,"Coleção não encontrada.")
        if not c.execute("SELECT 1 FROM products WHERE id=? AND admin_removed=0",(pid,)).fetchone(): raise HTTPException(404,"Recurso não encontrado.")
        if c.execute("SELECT COUNT(*) n FROM collection_items WHERE collection_id=?",(collection_id,)).fetchone()["n"]>=300: raise HTTPException(400,"Esta coleção atingiu 300 recursos.")
        c.execute("INSERT OR IGNORE INTO collection_items(collection_id,product_id,created_at) VALUES(?,?,?)",(collection_id,pid,iso_now()))
        c.execute("UPDATE collection_folders SET updated_at=? WHERE id=?",(iso_now(),collection_id))
    return {"ok":True}


@app.delete("/api/account/collections/{collection_id}/items/{product_id}")
def remove_collection_item(collection_id: str, product_id: str, request: Request):
    user=require_user(request,csrf=True)
    with db() as c:
        if not is_premium_user(c,user): raise HTTPException(403,"Coleções são exclusivas do Premium.")
        if not c.execute("SELECT 1 FROM collection_folders WHERE id=? AND user_id=?",(collection_id,user["id"])).fetchone(): raise HTTPException(404,"Coleção não encontrada.")
        c.execute("DELETE FROM collection_items WHERE collection_id=? AND product_id=?",(collection_id,product_id));c.execute("UPDATE collection_folders SET updated_at=? WHERE id=?",(iso_now(),collection_id))
    return {"ok":True}


@app.post("/api/account/cancel-subscription")
def cancel_subscription(request: Request):
    user=require_user(request,csrf=True)
    with db() as c:
        if founder_feature_access(c,user["id"],"PREMIUM"):
            raise HTTPException(409,"Premium vitalício de Fundador não possui assinatura recorrente para cancelar.")
        if not user["premium_active"]:
            raise HTTPException(409,"Esta conta não possui assinatura Premium ativa.")
        c.execute("UPDATE users SET plan='FREE',premium_active=0,premium_cycle='MONTHLY',updated_at=? WHERE id=?",(iso_now(),user["id"]))
    audit(user["email"],"SUBSCRIPTION_CANCELLED_HOMOLOGATION",{"mode":"HOMOLOGATION"})
    sess,updated=get_session(request);return account_view(updated,sess["csrf_token"] if sess else None)


@app.get("/api/notifications")
def user_notifications(request: Request):
    user=require_user(request)
    with db() as c:
        rows=c.execute("SELECT * FROM user_notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 30",(user["id"],)).fetchall()
        unread=sum(1 for r in rows if not r["read_at"])
        return {"unreadCount":unread,"items":[{"id":r["id"],"type":r["type"],"title":r["title"],"message":r["message"],"action":r["action"],"productId":r["product_id"],"readAt":r["read_at"],"createdAt":r["created_at"]} for r in rows]}


@app.post("/api/notifications/read")
async def read_notifications(request: Request):
    user=require_user(request,csrf=True)
    body=await request.json()
    ids=[str(x) for x in (body.get("ids") or []) if str(x)]
    with db() as c:
        now=iso_now()
        if ids:
            marks=','.join('?' for _ in ids)
            c.execute(f"UPDATE user_notifications SET read_at=COALESCE(read_at,?) WHERE user_id=? AND id IN ({marks})",(now,user["id"],*ids))
        else:
            c.execute("UPDATE user_notifications SET read_at=COALESCE(read_at,?) WHERE user_id=?",(now,user["id"]))
    return {"ok":True}


# ---------------------------------------------------------------------
# Products, purchase, downloads
# ---------------------------------------------------------------------
@app.get("/api/catalog/revision")
def public_catalog_revision():
    with db() as c:
        return {"revision": catalog_revision(c)}


@app.get("/api/products")
def products(request: Request):
    _, user = get_session(request)
    with db() as c:
        s = get_settings(c)
        rows = c.execute("SELECT * FROM products WHERE status='PUBLISHED' AND hidden=0 AND admin_removed=0 ORDER BY featured DESC, created_at DESC").fetchall()
        return [enrich_product_view(c, product_view(r, user, s), r) for r in rows]


@app.get("/api/products/{key}")
def product(key: str, request: Request):
    _, user = get_session(request)
    with db() as c:
        s = get_settings(c)
        row = c.execute("SELECT * FROM products WHERE (id=? OR slug=?) AND status='PUBLISHED' AND hidden=0 AND admin_removed=0", (key, key)).fetchone()
        if not row:
            raise HTTPException(404, "Recurso não encontrado.")
        return enrich_product_view(c, product_view(row, user, s), row)


@app.get("/api/highlighted-collaborators")
def public_highlighted_collaborators():
    with db() as c:
        return highlighted_collaborators(c)


@app.get("/api/collaborators/{user_id}")
def public_collaborator(user_id: str):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE id=? AND roles_json LIKE '%collaborator%'", (user_id,)).fetchone()
        if not u or u["creator_status"] != "ACTIVE":
            raise HTTPException(404, "Colaborador não encontrado.")
        profile = jload(u["profile_json"], {})
        founder = c.execute("SELECT slot FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'", (user_id,)).fetchone()
        published = c.execute("SELECT COUNT(*) n FROM products WHERE owner_id=? AND status='PUBLISHED' AND hidden=0 AND admin_removed=0", (user_id,)).fetchone()["n"]
        return {
            "id": u["id"],
            "name": f"{u['first_name']} {u['last_name']}".strip(),
            "bio": profile.get("bio", ""),
            "photo": profile.get("photo", ""),
            "site": profile.get("site", ""),
            "instagram": profile.get("instagram", ""),
            "youtube": profile.get("youtube", ""),
            "founder": bool(founder),
            "joinedAt": u["created_at"],
            "publishedAssets": int(published),
        }


def notify_asset_incompatibility(c: sqlite3.Connection, product: sqlite3.Row, action: str="REMOVED") -> None:
    owner_id=product["owner_id"] if product else None
    if not owner_id:
        return
    now=iso_now()
    title="Recurso removido por Incompatibilidade"
    message=f'O recurso “{product["title"]}” foi retirado pela Administração por Incompatibilidade. Para entender o motivo ou solicitar uma revisão, entre em contato pelo chat da ATV.'
    nid=str(uuid.uuid4())
    c.execute(
        """INSERT INTO user_notifications(id,user_id,type,title,message,action,product_id,read_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(user_id,type,product_id) DO UPDATE SET title=excluded.title,message=excluded.message,action=excluded.action,read_at=NULL,created_at=excluded.created_at""",
        (nid,owner_id,"ASSET_INCOMPATIBILITY",title,message,"OPEN_CHAT",product["id"],None,now),
    )


def is_premium_user(c: sqlite3.Connection, user: sqlite3.Row) -> bool:
    return bool(user["premium_active"] or founder_feature_access(c,user["id"],"PREMIUM"))


def validate_canva_url(value: str) -> str:
    raw=str(value or "").strip()
    if not raw:
        raise HTTPException(400,"Informe o link público do Canva.")
    from urllib.parse import urlparse
    try:
        u=urlparse(raw)
    except Exception:
        raise HTTPException(400,"Link do Canva inválido.")
    host=(u.hostname or "").lower()
    if u.scheme != "https" or not (host=="canva.com" or host.endswith(".canva.com")):
        raise HTTPException(400,"Use um link público válido do canva.com.")
    return raw


def creator_notification(c: sqlite3.Connection, owner_id: Optional[str], type_: str, title: str, message: str, action: str="", product_id: Optional[str]=None):
    if not owner_id:
        return
    c.execute("INSERT INTO user_notifications(id,user_id,type,title,message,action,product_id,read_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
              (str(uuid.uuid4()),owner_id,type_,title,message,action,product_id,None,iso_now()))


def register_product_use(c: sqlite3.Connection, user: sqlite3.Row, product: sqlite3.Row, source: str, settings: dict[str,Any]) -> tuple[bool,str]:
    today=utcnow().date().isoformat()
    exists=c.execute("SELECT id FROM download_events WHERE user_id=? AND product_id=? AND day=?",(user["id"],product["id"],today)).fetchone()
    if exists:
        return True,str(exists["id"])
    if source in ("FREE","PREMIUM"):
        limit=int(settings["freeDownloadsPerDay"] if source=="FREE" else settings["premiumDownloadsPerDay"])
        used=int(c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND day=? AND source=?",(user["id"],today,source)).fetchone()["n"])
        if used>=limit:
            raise HTTPException(429,f"Limite diário de {limit} downloads atingido.")
    event_id=str(uuid.uuid4());now=iso_now()
    c.execute("INSERT INTO download_events(id,user_id,product_id,source,day,created_at) VALUES(?,?,?,?,?,?)",(event_id,user["id"],product["id"],source,today,now))
    record_download_compensation(c,event_id,product,settings,source)
    if product["access_tier"]=="FREE":
        c.execute("UPDATE products SET free_downloads=free_downloads+1 WHERE id=?",(product["id"],))
    if product["access_tier"] in ("PREMIUM","PREMIUM_MAX") and source=="PREMIUM":
        c.execute("UPDATE products SET premium_downloads=premium_downloads+1 WHERE id=?",(product["id"],))
    if product["owner_id"]:
        if source=="PREMIUM":
            msg=f'O recurso “{product["title"]}” foi utilizado por um assinante Premium. A utilização foi registrada na contabilização Premium.'
        elif source=="FREE":
            msg=f'O recurso “{product["title"]}” recebeu um download Free elegível. A utilização foi registrada no histórico.'
        else:
            msg=f'Um cliente acessou novamente “{product["title"]}”, que já pertence à biblioteca dele. Nenhuma nova venda foi criada.'
        creator_notification(c,product["owner_id"],f"ASSET_DOWNLOAD:{event_id}","Seu recurso foi utilizado",msg,"OPEN_SALES",product["id"])
    bump_catalog(c,"download")
    return False,event_id


def commission_for(gross: int, s: dict) -> tuple[int, int, int]:
    pc = s.get("platformCommission", {})
    platform = 0
    if pc.get("type") in ("FIXED", "HYBRID"):
        platform += int(pc.get("fixedCents", 0))
    if pc.get("type") in ("PERCENT", "HYBRID"):
        platform += round(gross * float(pc.get("percent", 0)) / 100)
    gf = s.get("gatewayFee", {})
    gateway = round(gross * float(gf.get("percent", 0)) / 100) + int(gf.get("fixedCents", 0))
    creator = gross - platform - gateway
    if creator < 0:
        raise HTTPException(500, "Configuração financeira inválida.")
    return platform, gateway, creator


@app.post("/api/checkout/simulate")
async def checkout_simulate(request: Request):
    user = require_user(request, csrf=True)
    body = await request.json()
    requested = list(dict.fromkeys(str(x) for x in body.get("items", [])))
    if not requested:
        raise HTTPException(400, "Carrinho vazio.")
    with db() as c:
        s = get_settings(c)
        rows = c.execute(f"SELECT * FROM products WHERE id IN ({','.join('?' for _ in requested)}) AND status='PUBLISHED' AND hidden=0 AND admin_removed=0", requested).fetchall()
        eligible = []
        for r in rows:
            if r["access_tier"] == "PREMIUM_MAX":
                continue
            if not r["individual_purchase_enabled"]:
                continue
            if user["premium_active"] and r["access_tier"] in ("PREMIUM", "PREMIUM_MAX"):
                continue
            if c.execute("SELECT 1 FROM entitlements WHERE user_id=? AND product_id=?", (user["id"], r["id"])).fetchone():
                continue
            eligible.append(r)
        if not eligible:
            raise HTTPException(400, "Carrinho sem recursos elegíveis ou recursos já liberados.")
        prices = [(r, product_price_cents(r, s)) for r in eligible]
        total = sum(p for _, p in prices)
        order_id = "ORD-" + uuid.uuid4().hex[:12].upper()
        now = iso_now()
        c.execute("INSERT INTO orders(id,user_id,total_cents,status,gateway_mode,created_at) VALUES(?,?,?,?,?,?)", (order_id, user["id"], total, "APPROVED_TEST", "HOMOLOGATION_SIMULATION", now))
        breakdown = []
        for r, price in prices:
            order_item_id=str(uuid.uuid4())
            c.execute("INSERT INTO order_items(id,order_id,product_id,price_cents) VALUES(?,?,?,?)", (order_item_id, order_id, r["id"], price))
            c.execute("INSERT OR IGNORE INTO entitlements(id,user_id,product_id,order_id,source,created_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), user["id"], r["id"], order_id, "PURCHASE", now))
            c.execute("UPDATE products SET sales=sales+1,updated_at=? WHERE id=?", (now, r["id"]))
            platform, gateway, creator = commission_for(price, s)
            if r["owner_id"]:
                c.execute("INSERT OR IGNORE INTO wallets(user_id) VALUES(?)", (r["owner_id"],))
                c.execute("UPDATE wallets SET pending_cents=pending_cents+?,total_earned_cents=total_earned_cents+? WHERE user_id=?", (creator, creator, r["owner_id"]))
                creator_notification(c,r["owner_id"],f"ASSET_SALE:{order_item_id}","Você realizou uma venda",f'O recurso “{r["title"]}” foi adquirido. Repasse atual: {money(creator)}. O valor permanece pendente até o processamento financeiro.',"OPEN_WALLET",r["id"])
            breakdown.append({"productId": r["id"], "gross": money(price), "gateway": money(gateway), "platform": money(platform), "creator": money(creator)})
        c.execute("DELETE FROM cart WHERE user_id=?", (user["id"],))
    audit(user["email"], "CHECKOUT_SIMULATED", {"orderId": order_id, "items": [r["id"] for r in eligible], "totalCents": total})
    return {"order": {"id": order_id, "items": [r["id"] for r in eligible], "total": money(total), "createdAt": now, "status": "APPROVED_TEST"}, "itemsBreakdown": breakdown}


@app.post("/api/downloads/consume")
async def download_consume(request: Request):
    user = require_user(request, csrf=True)
    body = await request.json(); product_id=str(body.get("productId","")); requested_source=str(body.get("source","")).upper()
    with db() as c:
        s=get_settings(c); p=c.execute("SELECT * FROM products WHERE id=? AND status='PUBLISHED' AND hidden=0 AND admin_removed=0",(product_id,)).fetchone()
        if not p: raise HTTPException(404,"Recurso não encontrado.")
        purchased=bool(c.execute("SELECT 1 FROM entitlements WHERE user_id=? AND product_id=?",(user["id"],product_id)).fetchone())
        premium_active=bool(user["premium_active"] or founder_feature_access(c,user["id"],"PREMIUM"))
        if p["access_tier"]=="FREE":
            source="PREMIUM" if premium_active else "FREE"
        elif premium_active:
            source="PREMIUM"
        elif purchased:
            source="PURCHASE"
        else: raise HTTPException(403,"Este recurso exige Premium ou compra individual liberada.")
        if requested_source=="PURCHASE" and purchased and not premium_active: source="PURCHASE"
        today=utcnow().date().isoformat(); already=bool(c.execute("SELECT 1 FROM download_events WHERE user_id=? AND product_id=? AND day=?",(user["id"],product_id,today)).fetchone())
        if not already and source in ("FREE","PREMIUM"):
            limit=int(s["freeDownloadsPerDay"] if source=="FREE" else s["premiumDownloadsPerDay"])
            used=int(c.execute("SELECT COUNT(*) n FROM download_events WHERE user_id=? AND day=? AND source=?",(user["id"],today,source)).fetchone()["n"])
            if used>=limit: raise HTTPException(429,f"Limite diário de {limit} downloads atingido.")
        # A autorização não consome cota. O evento é criado somente quando o servidor começa a entregar o arquivo.
        token=secrets.token_urlsafe(32)
        c.execute("INSERT INTO download_tokens(token,user_id,product_id,expires_at,used,source) VALUES(?,?,?,?,0,?)",(token,user["id"],p["id"],(utcnow()+timedelta(minutes=3)).isoformat(),source))
    return {"ok":True,"message":"Download liberado.","downloadUrl":f"/api/downloads/file/{token}","alreadyCountedToday":already}


@app.get("/api/downloads/file/{token}")
def download_file(token: str, request: Request):
    user=require_user(request)
    zip_path=None; response_path=None; response_name=None; media="application/octet-stream"
    with db() as c:
        row=c.execute("SELECT dt.*,p.original_path,p.title,p.slug,p.formats_json,p.access_tier,p.owner_id FROM download_tokens dt JOIN products p ON p.id=dt.product_id WHERE dt.token=? AND dt.user_id=?",(token,user["id"])).fetchone()
        if not row or datetime.fromisoformat(row["expires_at"])<=utcnow(): raise HTTPException(410,"Link de download expirado.")
        if row["access_tier"]=="PREMIUM_MAX":
            files=c.execute("SELECT original_path,extension,filename FROM product_files WHERE product_id=? ORDER BY created_at,id",(row["product_id"],)).fetchall()
            if not files: raise HTTPException(404,"Pacote Premium Max sem arquivos.")
            paths=[]
            for f in files:
                fp=resolve_private_file(f["original_path"],ORIGINALS)
                if not fp:
                    raise HTTPException(404,f"Arquivo do pacote não encontrado: {f['filename']}")
                paths.append((fp,f["filename"],f["extension"]))
            zip_path=PACKAGES/f"{slugify(row['slug'] or row['title'])}-{uuid.uuid4().hex[:8]}.zip"; used_names=set()
            with zipfile.ZipFile(zip_path,"w",compression=zipfile.ZIP_DEFLATED) as zf:
                for idx,(fp,name,ext) in enumerate(paths,1):
                    arc=clean_package_filename(name,f"arquivo-{idx}.{str(ext).lower()}"); stem,suffix=Path(arc).stem,Path(arc).suffix; candidate=arc;n=2
                    while candidate.lower() in used_names: candidate=f"{stem}-{n}{suffix}";n+=1
                    used_names.add(candidate.lower());zf.write(fp,arcname=candidate)
            response_path=zip_path;response_name=f"{slugify(row['slug'] or row['title'])}.zip";media="application/zip"
        else:
            response_path=resolve_private_file(row["original_path"],ORIGINALS)
            if not response_path: raise HTTPException(404,"Arquivo original não encontrado na pasta data/originals.")
            response_name=slugify(row["title"])+(response_path.suffix or ".bin")
            suffix=response_path.suffix.lower()
            media={".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".psd":"image/vnd.adobe.photoshop"}.get(suffix,"application/octet-stream")
        # Conta somente quando o arquivo já está validado e o endpoint iniciou a entrega.
        today=utcnow().date().isoformat(); source=str(row["source"] or "FREE")
        p=c.execute("SELECT * FROM products WHERE id=?",(row["product_id"],)).fetchone();s=get_settings(c)
        try:
            register_product_use(c,user,p,source,s)
        except HTTPException:
            if zip_path: zip_path.unlink(missing_ok=True)
            raise
        c.execute("UPDATE download_tokens SET used=1 WHERE token=?",(token,))
    bg=BackgroundTask(lambda p=zip_path:p.unlink(missing_ok=True)) if zip_path else None
    return FileResponse(str(response_path),filename=response_name,media_type=media,background=bg,headers={"Cache-Control":"no-store","Accept-Ranges":"bytes"})


@app.post("/api/products/{product_id}/external-access")
async def external_product_access(product_id: str, request: Request):
    user=require_user(request,csrf=True)
    with db() as c:
        p=c.execute("SELECT * FROM products WHERE id=? AND status='PUBLISHED' AND hidden=0 AND admin_removed=0",(product_id,)).fetchone()
        if not p: raise HTTPException(404,"Recurso não encontrado.")
        if str(p["delivery_mode"] or "FILE")!="CANVA": raise HTTPException(400,"Este recurso não utiliza acesso externo.")
        purchased=bool(c.execute("SELECT 1 FROM entitlements WHERE user_id=? AND product_id=?",(user["id"],product_id)).fetchone())
        premium=is_premium_user(c,user)
        if p["access_tier"]=="FREE": source="PREMIUM" if premium else "FREE"
        elif premium: source="PREMIUM"
        elif purchased: source="PURCHASE"
        else: raise HTTPException(403,"Este recurso exige Premium ou compra individual liberada.")
        s=get_settings(c);already,_=register_product_use(c,user,p,source,s);url=validate_canva_url(p["external_url"])
    return {"ok":True,"url":url,"alreadyCountedToday":already,"message":"Acesso ao Canva liberado."}


@app.get("/api/downloads")
def downloads(request: Request):
    user = require_user(request)
    with db() as c:
        s = get_settings(c)
        ent = c.execute("SELECT e.created_at,e.source,p.* FROM entitlements e JOIN products p ON p.id=e.product_id WHERE e.user_id=? ORDER BY e.created_at DESC", (user["id"],)).fetchall()
        rec = c.execute("SELECT d.created_at,d.source,p.* FROM download_events d JOIN products p ON p.id=d.product_id WHERE d.user_id=? ORDER BY d.created_at DESC LIMIT 20", (user["id"],)).fetchall()
        return {
            "purchased": [{"source": r["source"], "createdAt": r["created_at"], "product": product_view(r, user, s)} for r in ent],
            "recent": [{"source": r["source"], "createdAt": r["created_at"], "product": product_view(r, user, s)} for r in rec],
        }

@app.get("/api/reviews/me/{product_id}")
def my_review(product_id: str, request: Request):
    user=require_user(request)
    with db() as c:
        r=c.execute("SELECT rating,updated_at FROM product_reviews WHERE user_id=? AND product_id=?",(user["id"],product_id)).fetchone()
        eligible=bool(c.execute("SELECT 1 FROM download_events WHERE user_id=? AND product_id=?",(user["id"],product_id)).fetchone())
        return {"eligible":eligible,"rating":int(r["rating"]) if r else None,"updatedAt":r["updated_at"] if r else None}


@app.post("/api/reviews")
async def save_review(request: Request):
    user=require_user(request,csrf=True); body=await request.json(); product_id=str(body.get("productId","")); rating=int(body.get("rating") or 0)
    if rating<1 or rating>5: raise HTTPException(400,"A avaliação deve ter entre 1 e 5 estrelas.")
    with db() as c:
        if not c.execute("SELECT 1 FROM products WHERE id=?",(product_id,)).fetchone(): raise HTTPException(404,"Recurso não encontrado.")
        if not c.execute("SELECT 1 FROM download_events WHERE user_id=? AND product_id=?",(user["id"],product_id)).fetchone(): raise HTTPException(403,"Faça o download do recurso antes de avaliá-lo.")
        now=iso_now(); existing=c.execute("SELECT id FROM product_reviews WHERE user_id=? AND product_id=?",(user["id"],product_id)).fetchone()
        if existing:c.execute("UPDATE product_reviews SET rating=?,updated_at=? WHERE id=?",(rating,now,existing["id"]))
        else:c.execute("INSERT INTO product_reviews(id,user_id,product_id,rating,created_at,updated_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),user["id"],product_id,rating,now,now))
        avg,count=product_rating(c,product_id);bump_catalog(c,"review")
    return {"ok":True,"rating":rating,"average":avg,"count":count}


# ---------------------------------------------------------------------
# Uploads / seller
# ---------------------------------------------------------------------
def validate_magic(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    if ext not in ("psd", "png", "jpg"):
        raise HTTPException(400, "Formato não aceito. Envie PSD, PNG ou JPG/JPEG.")
    if ext == "psd" and not data.startswith(b"8BPS"):
        raise HTTPException(400, "O conteúdo não corresponde a um PSD válido.")
    if ext == "png" and not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(400, "O conteúdo não corresponde a um PNG válido.")
    if ext == "jpg" and not data.startswith(b"\xff\xd8\xff"):
        raise HTTPException(400, "O conteúdo não corresponde a um JPG/JPEG válido.")
    return ext


def validate_preview(data: bytes, content_type: str) -> str:
    if len(data) > MAX_PREVIEW_BYTES:
        raise HTTPException(400, "Preview muito grande.")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "webp"
    raise HTTPException(400, "Preview deve ser PNG, JPG/JPEG ou WebP.")


async def store_upload(request: Request, file: UploadFile, preview: UploadFile, metadata: str, preview_method: str, admin_mode: bool = False):
    if admin_mode:
        admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN"})
        user = None
    else:
        user = require_user(request, csrf=True, collaborator=True)
        admin = None
    original_data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(original_data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Arquivo excede {MAX_UPLOAD_BYTES//1024//1024} MB.")
    ext = validate_magic(file.filename or "", original_data)
    preview_data = await preview.read(MAX_PREVIEW_BYTES + 1)
    pext = validate_preview(preview_data, preview.content_type or "")
    upload_id = str(uuid.uuid4())
    original_path = ORIGINALS / f"{upload_id}.{ext}"
    preview_path = PREVIEWS / f"{upload_id}.{pext}"
    original_path.write_bytes(original_data)
    preview_path.write_bytes(preview_data)
    meta = jload(metadata, {}) if metadata else {}
    meta["_originalFilename"] = clean_package_filename(file.filename or "", f"arquivo.{ext}")
    with db() as c:
        c.execute("INSERT INTO uploads(id,user_id,admin_id,original_path,preview_path,extension,metadata_json,preview_method,consumed,created_at) VALUES(?,?,?,?,?,?,?,?,0,?)",
                  (upload_id, user["id"] if user else None, admin["id"] if admin else None, str(original_path), str(preview_path), ext.upper(), jdump(meta), preview_method or "UNKNOWN", iso_now()))
    return {"ok": True, "uploadId": upload_id, "extension": ext.upper(), "previewUrl": f"/media/previews/{preview_path.name}", "metadata": meta, "previewMethod": preview_method}


@app.post("/api/uploads/resource")
async def upload_resource(request: Request, file: UploadFile = File(...), preview: UploadFile = File(...), metadata: str = Form("{}"), previewMethod: str = Form("UNKNOWN")):
    return await store_upload(request, file, preview, metadata, previewMethod, False)


@app.post("/api/admin/uploads/resource")
async def admin_upload_resource(request: Request, file: UploadFile = File(...), preview: UploadFile = File(...), metadata: str = Form("{}"), previewMethod: str = Form("ADMIN_UPLOAD")):
    return await store_upload(request, file, preview, metadata, previewMethod, True)


async def append_package_files(upload_id: str, request: Request, files: list[UploadFile], admin_mode: bool = False):
    if admin_mode:
        admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN"})
        user = None
    else:
        user = require_user(request, csrf=True, collaborator=True)
        admin = None
    if not files:
        return {"ok": True, "uploadId": upload_id, "files": []}
    if len(files) > 30:
        raise HTTPException(400, "Premium Max aceita no máximo 30 arquivos adicionais por recurso nesta homologação.")
    with db() as c:
        up = c.execute("SELECT * FROM uploads WHERE id=? AND consumed=0", (upload_id,)).fetchone()
        if not up:
            raise HTTPException(404, "Upload pendente não encontrado.")
        if user and up["user_id"] != user["id"]:
            raise HTTPException(403, "Este upload não pertence à sua conta.")
        if admin and up["admin_id"] != admin["id"]:
            raise HTTPException(403, "Este upload não pertence a esta sessão administrativa.")
        meta = jload(up["metadata_json"], {})
        package = list(meta.get("packageFiles", []))
        if len(package) + len(files) > 30:
            raise HTTPException(400, "O pacote excede o limite de 30 arquivos adicionais.")
        created = []
        for f in files:
            data = await f.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Arquivo {f.filename or ''} excede {MAX_UPLOAD_BYTES//1024//1024} MB.")
            ext = validate_magic(f.filename or "", data)
            stored = ORIGINALS / f"{upload_id}-part-{uuid.uuid4().hex[:10]}.{ext}"
            stored.write_bytes(data)
            item = {"originalPath": str(stored), "extension": ext.upper(), "filename": clean_package_filename(f.filename or '', f"arquivo.{ext}")}
            package.append(item); created.append({"extension": item["extension"], "filename": item["filename"]})
        meta["packageFiles"] = package
        c.execute("UPDATE uploads SET metadata_json=? WHERE id=?", (jdump(meta), upload_id))
    return {"ok": True, "uploadId": upload_id, "files": created, "totalAdditionalFiles": len(package)}


@app.post("/api/uploads/{upload_id}/files")
async def upload_package_files(upload_id: str, request: Request, files: list[UploadFile] = File(...)):
    return await append_package_files(upload_id, request, files, False)


@app.post("/api/admin/uploads/{upload_id}/files")
async def admin_upload_package_files(upload_id: str, request: Request, files: list[UploadFile] = File(...)):
    return await append_package_files(upload_id, request, files, True)


@app.post("/api/admin/uploads/{upload_id}/preview")
async def admin_replace_pending_preview(upload_id: str, request: Request, preview: UploadFile = File(...), previewMethod: str = Form("ADMIN_MANUAL_OVERRIDE")):
    """Substitui somente a miniatura de um upload ainda não consumido."""
    admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN"})
    data = await preview.read(MAX_PREVIEW_BYTES + 1)
    ext = validate_preview(data, preview.content_type or "")
    with db() as c:
        up = c.execute("SELECT * FROM uploads WHERE id=? AND consumed=0", (upload_id,)).fetchone()
        if not up:
            raise HTTPException(404, "Upload pendente não encontrado ou já utilizado.")
        if up["admin_id"] != admin["id"]:
            raise HTTPException(403, "Este upload não pertence a esta sessão administrativa.")
        old_path = Path(up["preview_path"])
        new_path = PREVIEWS / f"{upload_id}.{ext}"
        new_path.write_bytes(data)
        c.execute("UPDATE uploads SET preview_path=?,preview_method=? WHERE id=?", (str(new_path), previewMethod or "ADMIN_MANUAL_OVERRIDE", upload_id))
        if old_path != new_path and old_path.exists():
            try: old_path.unlink()
            except OSError: pass
    return {"ok": True, "uploadId": upload_id, "previewUrl": f"/media/previews/{new_path.name}", "previewMethod": previewMethod}


def consume_upload(c: sqlite3.Connection, upload_id: str, user_id: Optional[str] = None, admin_id: Optional[str] = None) -> sqlite3.Row:
    up = c.execute("SELECT * FROM uploads WHERE id=? AND consumed=0", (upload_id,)).fetchone()
    if not up:
        raise HTTPException(400, "Upload inválido ou já utilizado.")
    if user_id and up["user_id"] != user_id:
        raise HTTPException(403, "Este upload não pertence à sua conta.")
    if admin_id and up["admin_id"] != admin_id:
        raise HTTPException(403, "Este upload não pertence a esta sessão administrativa.")
    c.execute("UPDATE uploads SET consumed=1 WHERE id=?", (upload_id,))
    return up


@app.get("/api/creator/simulation")
def creator_simulation(request: Request, sales: int = 1):
    user = require_user(request, collaborator=True)
    n = max(1, min(int(sales), 100000))
    with db() as c:
        s = get_settings(c)
        gross = int(s["individualPriceCents"])
        platform, gateway, creator = commission_for(gross, s)
    return {"sales": n, "perSale": {"gross": money(gross), "gateway": money(gateway), "platform": money(platform), "creator": money(creator)}, "totals": {"gross": money(gross*n), "gateway": money(gateway*n), "platform": money(platform*n), "creator": money(creator*n)}, "gatewayConfigured": bool(s["gatewayFee"].get("configured")), "premiumCompensationModel": s.get("premiumCreatorCompensation",{}).get("model","PER_VALID_DOWNLOAD_PREPARED")}


@app.get("/api/seller/summary")
def seller_summary(request: Request):
    user = require_user(request, collaborator=True)
    with db() as c:
        s = get_settings(c)
        rows = c.execute("SELECT * FROM products WHERE owner_id=? ORDER BY created_at DESC", (user["id"],)).fetchall()
        visible_rows=[r for r in rows if not r["admin_removed"] and r["status"]!="SUSPENDED"]
        wallet = c.execute("SELECT * FROM wallets WHERE user_id=?", (user["id"],)).fetchone()
        gross = int(s["individualPriceCents"]); platform, gateway, creator = commission_for(gross, s)
        return {
            "sales": sum(r["sales"] for r in rows), "premiumDownloads": sum(r["premium_downloads"] for r in rows), "freeDownloads": sum(r["free_downloads"] for r in rows), "currentNetPerSale": money(creator),
            "premiumCreatorCompensation": s["premiumCreatorCompensation"],
            "wallet": {"available": money(wallet["available_cents"]), "pending": money(wallet["pending_cents"]), "blocked": money(wallet["blocked_cents"]), "withdrawn": money(wallet["withdrawn_cents"]), "totalEarned": money(wallet["total_earned_cents"]), "withdrawalMinimum": money(s["withdrawalMinimumCents"]), "canWithdraw": wallet["available_cents"] >= s["withdrawalMinimumCents"], "missingForWithdrawal": money(max(0, s["withdrawalMinimumCents"] - wallet["available_cents"]))},
            "rating": (lambda vals: round(sum(v[0]*v[1] for v in vals if v[0] is not None)/max(1,sum(v[1] for v in vals)),1) if sum(v[1] for v in vals) else None)([product_rating(c,r["id"]) for r in rows]),
            "ratingCount": sum(product_rating(c,r["id"])[1] for r in rows),
            "products": [enrich_product_view(c, product_view(r, user, s), r) for r in visible_rows]
        }


@app.get("/api/seller/activity")
def seller_activity(request: Request):
    user = require_user(request, collaborator=True)
    with db() as c:
        sales_rows = c.execute(
            """SELECT o.id order_id,o.created_at,o.status,oi.price_cents,p.id product_id,p.title,u.email buyer_email
               FROM order_items oi
               JOIN orders o ON o.id=oi.order_id
               JOIN products p ON p.id=oi.product_id
               LEFT JOIN users u ON u.id=o.user_id
               WHERE p.owner_id=?
               ORDER BY o.created_at DESC LIMIT 100""",
            (user["id"],),
        ).fetchall()
        download_rows = c.execute(
            """SELECT d.id,d.created_at,d.source,p.id product_id,p.title,p.access_tier
               FROM download_events d
               JOIN products p ON p.id=d.product_id
               WHERE p.owner_id=?
               ORDER BY d.created_at DESC LIMIT 150""",
            (user["id"],),
        ).fetchall()
        return {
            "sales": [
                {"orderId":r["order_id"],"productId":r["product_id"],"title":r["title"],"buyer":r["buyer_email"] or "Cliente ATV",
                 "price":money(r["price_cents"]),"status":r["status"],"createdAt":r["created_at"]}
                for r in sales_rows
            ],
            "downloads": [
                {"id":r["id"],"productId":r["product_id"],"title":r["title"],"source":r["source"],
                 "accessTier":r["access_tier"],"createdAt":r["created_at"]}
                for r in download_rows
            ],
        }


@app.get("/api/seller/reviews")
def seller_reviews(request: Request):
    user = require_user(request, collaborator=True)
    with db() as c:
        rows = c.execute(
            """SELECT pr.rating,pr.updated_at,p.id product_id,p.title,
                      COALESCE(NULLIF(TRIM(u.first_name||' '||u.last_name),''),'Cliente ATV') reviewer
               FROM product_reviews pr
               JOIN products p ON p.id=pr.product_id
               LEFT JOIN users u ON u.id=pr.user_id
               WHERE p.owner_id=?
               ORDER BY pr.updated_at DESC LIMIT 200""",
            (user["id"],),
        ).fetchall()
        count=len(rows); avg=round(sum(int(r["rating"]) for r in rows)/count,1) if count else None
        return {
            "average":avg,"count":count,
            "reviews":[{"productId":r["product_id"],"title":r["title"],"rating":int(r["rating"]),"reviewer":r["reviewer"],"updatedAt":r["updated_at"]} for r in rows]
        }


@app.get("/api/seller/products")
def seller_products(request: Request):
    user = require_user(request, collaborator=True)
    with db() as c:
        s = get_settings(c)
        rows = c.execute("SELECT * FROM products WHERE owner_id=? AND admin_removed=0 AND status!='SUSPENDED' ORDER BY created_at DESC", (user["id"],)).fetchall()
        out=[]
        for r in rows:
            v=enrich_product_view(c,product_view(r,user,s),r)
            v["canvaUrl"]=r["external_url"] if str(r["delivery_mode"] or "FILE")=="CANVA" else None
            out.append(v)
        return out


@app.get("/api/seller/products/{key}")
def seller_product_detail(key: str, request: Request):
    user = require_user(request, collaborator=True)
    with db() as c:
        s = get_settings(c)
        row = c.execute(
            "SELECT * FROM products WHERE owner_id=? AND admin_removed=0 AND status!='SUSPENDED' AND (id=? OR slug=?)",
            (user["id"], key, key),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Recurso do colaborador não encontrado.")
        v=enrich_product_view(c,product_view(row,user,s),row)
        v["canvaUrl"]=row["external_url"] if str(row["delivery_mode"] or "FILE")=="CANVA" else None
        return v


@app.patch("/api/seller/products/{product_id}")
async def update_seller_product(product_id: str, request: Request):
    user = require_user(request, csrf=True, collaborator=True)
    body = await request.json()
    with db() as c:
        s=get_settings(c)
        row=c.execute("SELECT * FROM products WHERE id=? AND owner_id=?",(product_id,user["id"])).fetchone()
        if not row:
            raise HTTPException(404,"Recurso não encontrado.")
        title=str(body.get("title",row["title"])).strip()
        description=str(body.get("description",row["description"])).strip()
        category=str(body.get("category",row["category"])).strip(); category="CANVA" if category.upper()=="CANVA" else category
        tier=str(body.get("accessTier",row["access_tier"])).upper()
        canva_url=str(body.get("canvaUrl",row["external_url"] or "")).strip()
        delivery_mode="CANVA" if category=="CANVA" else "FILE"
        if delivery_mode=="CANVA":
            if tier=="PREMIUM_MAX": raise HTTPException(400,"Canva não utiliza Premium Max. Use Free ou Premium.")
            canva_url=validate_canva_url(canva_url)
        else:
            canva_url=""
        if not title:
            raise HTTPException(400,"Título obrigatório.")
        if category not in s["resourceTypes"]:
            raise HTTPException(400,"Categoria inválida.")
        if tier not in ("FREE","PREMIUM","PREMIUM_MAX"):
            raise HTTPException(400,"Tipo de acesso inválido.")
        if tier=="PREMIUM_MAX" and row["access_tier"]!="PREMIUM_MAX":
            package_count=c.execute("SELECT COUNT(*) n FROM product_files WHERE product_id=?",(product_id,)).fetchone()["n"]
            if int(package_count)<2:
                raise HTTPException(409,"Para transformar este recurso em Premium Max, envie um novo pacote com múltiplos arquivos.")
        changed=title!=row["title"] or description!=row["description"] or category!=row["category"] or tier!=row["access_tier"] or canva_url!=(row["external_url"] or "") or delivery_mode!=str(row["delivery_mode"] or "FILE")
        if not changed:
            return enrich_product_view(c,product_view(row,user,s),row)
        now=iso_now()
        old_status=row["status"]
        next_status="PENDING_REVIEW" if old_status=="PUBLISHED" else old_status
        if next_status in ("REJECTED","SUSPENDED","CHANGES_REQUESTED"):
            next_status="PENDING_REVIEW"
        hidden=1 if next_status!="PUBLISHED" else int(row["hidden"])
        hist=jload(row["history_json"],[])
        hist.append({"at":now,"actor":user["email"],"action":"COLLABORATOR_EDIT","status":next_status,
                     "reason":"Alterações do Colaborador enviadas para nova revisão" if old_status=="PUBLISHED" else "Recurso atualizado"})
        moderation=jload(row["moderation_json"],{})
        if next_status=="PENDING_REVIEW":
            moderation.update({"result":"PENDING","reviewedAt":None,"approvedAt":None,"humanDecision":None,"reason":"COLLABORATOR_EDIT"})
        c.execute(
            """UPDATE products SET title=?,description=?,category=?,access_tier=?,
               individual_purchase_enabled=?,status=?,hidden=?,featured=0,moderation_json=?,history_json=?,external_url=?,delivery_mode=?,updated_at=?
               WHERE id=?""",
            (title,description,category,tier,1 if tier == "PREMIUM" else 0,next_status,hidden,
             jdump(moderation),jdump(hist),canva_url or None,delivery_mode,now,product_id),
        )
        if old_status=="PUBLISHED":
            s["featuredProductIds"]=[x for x in s.get("featuredProductIds",[]) if x!=product_id]
            save_settings(c,s)
        updated=c.execute("SELECT * FROM products WHERE id=?",(product_id,)).fetchone()
        bump_catalog(c,"seller-edit")
        view=enrich_product_view(c,product_view(updated,user,s),updated)
    audit(user["email"],"PRODUCT_EDITED_BY_COLLABORATOR",{"productId":product_id,"beforeStatus":old_status,"afterStatus":next_status,"accessTier":tier,"category":category})
    return view


@app.delete("/api/seller/products/{product_id}")
def delete_seller_product(product_id: str, request: Request):
    user = require_user(request, csrf=True, collaborator=True)
    with db() as c:
        s = get_settings(c)
        p = c.execute("SELECT * FROM products WHERE id=? AND owner_id=?", (product_id, user["id"])).fetchone()
        if not p:
            raise HTTPException(404, "Recurso não encontrado ou não pertence a este Colaborador.")

        commercial = (
            c.execute("SELECT 1 FROM order_items WHERE product_id=? LIMIT 1", (product_id,)).fetchone()
            or c.execute("SELECT 1 FROM entitlements WHERE product_id=? LIMIT 1", (product_id,)).fetchone()
            or c.execute("SELECT 1 FROM download_events WHERE product_id=? LIMIT 1", (product_id,)).fetchone()
        )
        s["featuredProductIds"] = [x for x in s.get("featuredProductIds", []) if x != product_id]
        save_settings(c, s)
        now = iso_now()

        if commercial:
            # O Colaborador pode retirar o próprio asset, mas histórico de venda/download
            # não pode ser apagado. `admin_removed` aqui representa remoção do catálogo ativo.
            hist = jload(p["history_json"], [])
            hist.append({
                "at": now,
                "actor": user["email"],
                "action": "COLLABORATOR_REMOVE_FROM_STOREFRONT",
                "status": "SUSPENDED",
                "reason": "Recurso removido pelo próprio Colaborador; histórico comercial preservado",
            })
            c.execute(
                "UPDATE products SET status='SUSPENDED',hidden=1,featured=0,admin_removed=1,history_json=?,updated_at=? WHERE id=?",
                (jdump(hist), now, product_id),
            )
            mode = "ARCHIVED"
            message = "Recurso removido do catálogo. O histórico de vendas e downloads foi preservado."
        else:
            preview_url = str(p["preview_url"] or "")
            preview_path = (PREVIEWS / Path(preview_url).name) if preview_url.startswith("/media/previews/") else None
            original = resolve_private_file(p["original_path"]) if p["original_path"] else None
            package_paths = [resolve_private_file(r["original_path"]) for r in c.execute("SELECT original_path FROM product_files WHERE product_id=?", (product_id,)).fetchall()]
            c.execute("DELETE FROM download_tokens WHERE product_id=?", (product_id,))
            c.execute("DELETE FROM products WHERE id=? AND owner_id=?", (product_id, user["id"]))
            for fp in [preview_path, original, *package_paths]:
                if fp and "demo-seed" not in fp.name:
                    try:
                        if fp.exists():
                            fp.unlink()
                    except OSError:
                        pass
            mode = "DELETED"
            message = "Recurso excluído definitivamente."

        bump_catalog(c, "seller-delete")

    audit(user["email"], "ASSET_REMOVED_BY_COLLABORATOR", {
        "productId": product_id,
        "title": p["title"],
        "mode": mode,
    })
    return {"ok": True, "mode": mode, "message": message}


@app.post("/api/seller/products")
async def create_seller_product(request: Request):
    user = require_user(request, csrf=True, collaborator=True)
    body = await request.json()
    title = str(body.get("title", "")).strip()
    category = str(body.get("category", "")).strip(); category = "CANVA" if category.upper()=="CANVA" else category
    tier = str(body.get("accessTier", "")).upper()
    upload_id = str(body.get("uploadId", ""))
    canva_url = str(body.get("canvaUrl", "")).strip()
    if not title:
        raise HTTPException(400, "Título obrigatório.")
    with db() as c:
        s = get_settings(c)
        if category not in s["resourceTypes"]:
            raise HTTPException(400, "Categoria inválida.")
        if tier not in ("FREE", "PREMIUM", "PREMIUM_MAX"):
            raise HTTPException(400, "Selecione ATV Free, Premium ou Premium Max.")
        delivery_mode="CANVA" if category=="CANVA" else "FILE"
        if delivery_mode=="CANVA":
            if tier=="PREMIUM_MAX": raise HTTPException(400,"Canva não utiliza Premium Max. Use Free ou Premium.")
            canva_url=validate_canva_url(canva_url)
        else:
            canva_url=""
        up = consume_upload(c, upload_id, user_id=user["id"])
        profile = jload(user["profile_json"], {})
        seller = (user["first_name"] + " " + user["last_name"]).strip() or "Colaborador ATV"
        id_ = "p-" + uuid.uuid4().hex[:12]
        base = slugify(title); slug = base
        if c.execute("SELECT 1 FROM products WHERE slug=?", (slug,)).fetchone(): slug = f"{base}-{id_[-5:]}"
        now=iso_now(); submission_number=int(c.execute("SELECT COUNT(*) n FROM products WHERE owner_id=?",(user["id"],)).fetchone()["n"])+1
        direct_auto=s["moderationMode"]=="AUTO_APPROVE_TEST"
        ai_cfg={**default_settings()["aiModeration"],**s.get("aiModeration",{})}; ai_eligible=bool(ai_cfg.get("enabled")) and submission_number>int(ai_cfg.get("startAfterUploads",10))
        ai_result=None; auto=direct_auto
        if ai_eligible and not direct_auto:
            ai_result=ai_moderate_preview(title,str(body.get("description","")),Path(up["preview_path"]),s); auto=ai_result.get("decision")=="AUTO_APPROVE"
        status="PUBLISHED" if auto else "PENDING_REVIEW"
        moderation={"mode":"AI_AUTO_AFTER_TRUST" if ai_eligible else s["moderationMode"],"result":"APPROVED" if auto else "PENDING","reviewedAt":now if auto else None,"approvedAt":now if auto else None,"submissionNumber":submission_number,"ai":ai_result}
        history = [{"at": now, "actor": seller, "action": "SUBMIT", "status": status}]
        meta = jload(up["metadata_json"], {})
        extra_files = list(meta.get("packageFiles", [])) if tier == "PREMIUM_MAX" else []
        formats = list(dict.fromkeys([str(up["extension"]).upper()] + [str(f.get("extension", "")).upper() for f in extra_files if f.get("extension")]))
        c.execute("""INSERT INTO products(id,slug,owner_id,title,seller,category,formats_json,description,preview_url,original_path,license,access_tier,individual_purchase_enabled,individual_price_cents,status,tags_json,featured,hidden,sales,premium_downloads,free_downloads,created_by_admin,moderation_json,history_json,external_url,delivery_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (id_,slug,user["id"],title,seller,category,jdump(["CANVA"] if delivery_mode=="CANVA" else formats),str(body.get("description", "")),f"/media/previews/{Path(up['preview_path']).name}",up["original_path"],"Uso comercial",tier,1 if tier == "PREMIUM" else 0,None,status,jdump(body.get("tags", [])),0,0,0,0,0,0,jdump(moderation),jdump(history),canva_url or None,delivery_mode,now,now))
        if tier == "PREMIUM_MAX":
            primary_name = clean_package_filename(str(meta.get("_originalFilename") or ""), f"arquivo.{str(up['extension']).lower()}")
            c.execute("INSERT INTO product_files(id,product_id,original_path,extension,filename,created_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), id_, up["original_path"], str(up["extension"]).upper(), primary_name, now))
            for f in extra_files:
                fp = Path(str(f.get("originalPath", "")))
                if not fp.exists():
                    raise HTTPException(400, "Um arquivo adicional do Premium Max não está mais disponível.")
                c.execute("INSERT INTO product_files(id,product_id,original_path,extension,filename,created_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), id_, str(fp), str(f.get("extension", fp.suffix.lstrip('.'))).upper(), clean_package_filename(str(f.get("filename", fp.name)), fp.name), now))
        bump_catalog(c, "seller-create")
    audit(user["email"], "PRODUCT_SUBMITTED", {"productId": id_, "status": status})
    with db() as c:
        s=get_settings(c); row=c.execute("SELECT * FROM products WHERE id=?",(id_,)).fetchone()
        view=enrich_product_view(c, product_view(row,user,s), row)
    return {**view, "autoApproved": auto, "aiEligible": ai_eligible, "submissionNumber": submission_number, "aiModeration": ai_result}

# ---------------------------------------------------------------------
# Support / reports
# ---------------------------------------------------------------------
@app.post("/api/support/tickets")
async def support_ticket(request: Request):
    body=await request.json();_,user=get_session(request);name,email,subject,message=[str(body.get(k,"")).strip() for k in ("name","email","subject","message")]
    if not all((name,email,subject,message)) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$",email): raise HTTPException(400,"Preencha corretamente todos os campos.")
    id_="SUP-"+uuid.uuid4().hex[:8].upper(); public_token=secrets.token_urlsafe(24);now=iso_now()
    with db() as c:
        c.execute("INSERT INTO support_tickets(id,user_id,name,email,subject,message,status,public_token,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(id_,user["id"] if user else None,name,email,subject,message,"OPEN",public_token,now,now))
        c.execute("INSERT INTO support_messages(id,ticket_id,sender_type,sender_id,message,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),id_,"USER",user["id"] if user else None,message,now))
    return JSONResponse({"ok":True,"protocol":id_,"status":"OPEN","accessToken":public_token},status_code=201)


def support_ticket_authorized(c: sqlite3.Connection, ticket_id: str, request: Request, access_token: str="") -> tuple[sqlite3.Row,Optional[sqlite3.Row]]:
    _,user=get_session(request); t=c.execute("SELECT * FROM support_tickets WHERE id=?",(ticket_id,)).fetchone()
    if not t: raise HTTPException(404,"Atendimento não encontrado.")
    if user and t["user_id"]==user["id"]: return t,user
    if access_token and t["public_token"] and hmac.compare_digest(str(t["public_token"]),str(access_token)): return t,user
    raise HTTPException(403,"Acesso ao atendimento não autorizado.")


@app.get("/api/support/tickets/{ticket_id}/messages")
def support_messages(ticket_id: str, request: Request, token: str=""):
    with db() as c:
        t,_=support_ticket_authorized(c,ticket_id,request,token); rows=c.execute("SELECT sender_type,message,created_at FROM support_messages WHERE ticket_id=? ORDER BY created_at",(ticket_id,)).fetchall()
        return {"ticket":{"id":t["id"],"subject":t["subject"],"status":t["status"]},"messages":[{"sender":r["sender_type"],"message":r["message"],"createdAt":r["created_at"]} for r in rows]}


@app.post("/api/support/tickets/{ticket_id}/messages")
async def support_send_message(ticket_id: str, request: Request, token: str=""):
    body=await request.json();message=str(body.get("message","")).strip()
    if not message: raise HTTPException(400,"Digite uma mensagem.")
    with db() as c:
        t,user=support_ticket_authorized(c,ticket_id,request,token);now=iso_now();c.execute("INSERT INTO support_messages(id,ticket_id,sender_type,sender_id,message,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),ticket_id,"USER",user["id"] if user else None,message,now));c.execute("UPDATE support_tickets SET status='OPEN',updated_at=? WHERE id=?",(now,ticket_id))
    return {"ok":True}


@app.post("/api/reports")
async def report_content(request: Request):
    body = await request.json()
    _, user = get_session(request)
    vals = {k: str(body.get(k, "")).strip() for k in ("name", "email", "resource", "reason", "description")}
    if not all(vals.values()) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", vals["email"]):
        raise HTTPException(400, "Preencha corretamente todos os campos obrigatórios.")
    id_ = "DEN-" + uuid.uuid4().hex[:8].upper()
    with db() as c:
        c.execute("INSERT INTO reports(id,user_id,name,email,resource,reason,description,evidence_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (id_, user["id"] if user else None, vals["name"], vals["email"], vals["resource"], vals["reason"], vals["description"], jdump(body.get("evidence")), "OPEN", iso_now()))
    audit(vals["email"], "REPORT_CREATED", {"reportId": id_})
    return JSONResponse({"ok": True, "protocol": id_, "status": "OPEN"}, status_code=201)

@app.get("/api/admin/support/summary")
def admin_support_summary(request: Request):
    require_admin(request)
    with db() as c:
        open_count=int(c.execute("SELECT COUNT(*) n FROM support_tickets WHERE status='OPEN'").fetchone()["n"])
        latest=c.execute("SELECT id,subject,name,updated_at FROM support_tickets WHERE status='OPEN' ORDER BY COALESCE(updated_at,created_at) DESC LIMIT 1").fetchone()
        return {"open":open_count,"latest":dict(latest) if latest else None}


@app.get("/api/admin/support")
def admin_support_list(request: Request):
    require_admin(request)
    with db() as c:
        rows=c.execute("SELECT * FROM support_tickets ORDER BY COALESCE(updated_at,created_at) DESC").fetchall()
        return [{"id":r["id"],"name":r["name"],"email":r["email"],"subject":r["subject"],"status":r["status"],"createdAt":r["created_at"],"updatedAt":r["updated_at"] or r["created_at"]} for r in rows]


@app.get("/api/admin/support/{ticket_id}")
def admin_support_detail(ticket_id: str, request: Request):
    require_admin(request)
    with db() as c:
        t=c.execute("SELECT * FROM support_tickets WHERE id=?",(ticket_id,)).fetchone()
        if not t: raise HTTPException(404,"Atendimento não encontrado.")
        rows=c.execute("SELECT sender_type,message,created_at FROM support_messages WHERE ticket_id=? ORDER BY created_at",(ticket_id,)).fetchall()
        return {"ticket":{"id":t["id"],"name":t["name"],"email":t["email"],"subject":t["subject"],"status":t["status"]},"messages":[{"sender":r["sender_type"],"message":r["message"],"createdAt":r["created_at"]} for r in rows]}


@app.post("/api/admin/support/{ticket_id}/reply")
async def admin_support_reply(ticket_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={"SUPER_ADMIN","ADMIN","MODERATOR"});body=await request.json();message=str(body.get("message","")).strip()
    if not message: raise HTTPException(400,"Digite uma resposta.")
    with db() as c:
        if not c.execute("SELECT 1 FROM support_tickets WHERE id=?",(ticket_id,)).fetchone():raise HTTPException(404,"Atendimento não encontrado.")
        now=iso_now();c.execute("INSERT INTO support_messages(id,ticket_id,sender_type,sender_id,message,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),ticket_id,"ADMIN",admin["id"],message,now));c.execute("UPDATE support_tickets SET status='ANSWERED',updated_at=? WHERE id=?",(now,ticket_id))
    audit(admin["name"],"SUPPORT_REPLIED",{"ticketId":ticket_id});return {"ok":True}


@app.patch("/api/admin/support/{ticket_id}/status")
async def admin_support_status(ticket_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={"SUPER_ADMIN","ADMIN","MODERATOR"});body=await request.json();status=str(body.get("status","")).upper()
    if status not in ("OPEN","ANSWERED","CLOSED"):raise HTTPException(400,"Status inválido.")
    with db() as c:c.execute("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?",(status,iso_now(),ticket_id))
    audit(admin["name"],"SUPPORT_STATUS_CHANGED",{"ticketId":ticket_id,"status":status});return {"ok":True,"status":status}


# ---------------------------------------------------------------------
# Admin 2-step auth
# ---------------------------------------------------------------------
@app.get("/api/admin/auth/status")
def admin_status(request: Request):
    sess, admin = get_admin_session(request)
    return {"authenticated": bool(admin), "role": admin["role"] if admin else None, "csrfToken": sess["csrf_token"] if sess else None}


@app.post("/api/admin/auth/step1")
async def admin_step1(request: Request):
    body = await request.json()
    pin = str(body.get("pin", ""))
    ip = request.client.host if request.client else "x"
    rate_limit(f"admin1:{ip}", 8, 900)
    with db() as c:
        admin = c.execute("SELECT * FROM admins WHERE id='adm-main' AND status='ACTIVE'").fetchone()
        if not admin or not verify_secret(pin, admin["pin_salt"], admin["pin_hash"]):
            raise HTTPException(401, "Código numérico incorreto.")
        challenge = secrets.token_urlsafe(28)
        c.execute("DELETE FROM admin_challenges WHERE expires_at <= ?", (iso_now(),))
        c.execute("INSERT INTO admin_challenges(id,admin_id,expires_at,attempts,created_at) VALUES(?,?,?,?,?)", (challenge, admin["id"], (utcnow()+timedelta(minutes=5)).isoformat(), 0, iso_now()))
    return {"ok": True, "challenge": challenge, "next": "SECURITY_KEY"}


@app.post("/api/admin/auth/step2")
async def admin_step2(request: Request):
    body = await request.json()
    challenge = str(body.get("challenge", "")); key = str(body.get("key", ""))
    if not re.match(r"^[A-Za-z]{2}[^A-Za-z0-9]{2}$", key):
        raise HTTPException(400, "A chave deve conter duas letras e dois símbolos.")
    ip = request.client.host if request.client else "x"
    rate_limit(f"admin2:{ip}", 8, 900)
    with db() as c:
        ch = c.execute("SELECT * FROM admin_challenges WHERE id=?", (challenge,)).fetchone()
        if not ch or datetime.fromisoformat(ch["expires_at"]) <= utcnow():
            raise HTTPException(401, "Desafio administrativo expirado. Recomece o acesso.")
        admin = c.execute("SELECT * FROM admins WHERE id=? AND status='ACTIVE'", (ch["admin_id"],)).fetchone()
        if not admin or not verify_secret(key, admin["key_salt"], admin["key_hash"]):
            c.execute("UPDATE admin_challenges SET attempts=attempts+1 WHERE id=?", (challenge,))
            if ch["attempts"] + 1 >= 5:
                c.execute("DELETE FROM admin_challenges WHERE id=?", (challenge,))
            raise HTTPException(401, "Chave administrativa incorreta.")
        c.execute("DELETE FROM admin_challenges WHERE id=?", (challenge,))
    token, csrf = create_admin_session(admin["id"])
    response = JSONResponse({"ok": True, "role": admin["role"], "csrfToken": csrf})
    attach_admin_cookie(response, token)
    audit(admin["name"], "ADMIN_LOGIN_2FA_SUCCESS", {"adminId": admin["id"]})
    return response


@app.post("/api/admin/auth/logout")
def admin_logout(request: Request):
    sess, admin = get_admin_session(request)
    if sess and request.headers.get("x-admin-csrf-token") != sess["csrf_token"]:
        raise HTTPException(403, "Token administrativo inválido.")
    if sess:
        with db() as c:
            c.execute("DELETE FROM admin_sessions WHERE token=?", (request.cookies.get(ADMIN_COOKIE),))
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response

# ---------------------------------------------------------------------
# Admin data / settings
# ---------------------------------------------------------------------
@app.get("/api/admin/summary")
def admin_summary(request: Request):
    require_admin(request)
    with db() as c:
        s = get_settings(c); today = utcnow().date().isoformat()
        clients = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        collabs = c.execute("SELECT COUNT(*) n FROM users WHERE roles_json LIKE '%collaborator%' AND creator_status='ACTIVE'").fetchone()["n"]
        admins = c.execute("SELECT COUNT(*) n FROM admins WHERE status='ACTIVE'").fetchone()["n"]
        total_assets = c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"]
        published = c.execute("SELECT COUNT(*) n FROM products WHERE status='PUBLISHED' AND hidden=0 AND admin_removed=0").fetchone()["n"]
        pending = c.execute("SELECT COUNT(*) n FROM products WHERE status IN ('PENDING_REVIEW','IN_REVIEW','CHANGES_REQUESTED')").fetchone()["n"]
        rejected = c.execute("SELECT COUNT(*) n FROM products WHERE status='REJECTED'").fetchone()["n"]
        suspended = c.execute("SELECT COUNT(*) n FROM products WHERE status='SUSPENDED'").fetchone()["n"]
        today_orders = c.execute("SELECT * FROM orders WHERE substr(created_at,1,10)=?", (today,)).fetchall()
        active_subs = c.execute("SELECT COUNT(*) n FROM users WHERE premium_active=1").fetchone()["n"]
        reports = c.execute("SELECT COUNT(*) n FROM reports WHERE status!='CLOSED'").fetchone()["n"]
        open_support = c.execute("SELECT COUNT(*) n FROM support_tickets WHERE status='OPEN'").fetchone()["n"]
        top = c.execute("SELECT * FROM products ORDER BY sales DESC LIMIT 5").fetchall()
        founder_campaign = founder_campaign_view(c, s)
        return {"clients":clients,"collaborators":collabs,"admins":admins,"founderPremium":founder_campaign,"totalAssets":total_assets,"publishedAssets":published,"pendingProducts":pending,"rejectedAssets":rejected,"suspendedAssets":suspended,"todaySales":len(today_orders),"todayVolume":money(sum(r['total_cents'] for r in today_orders)),"todayPlatform":0,"totalCatalogSales":sum(r['sales'] for r in c.execute('SELECT sales FROM products').fetchall()),"activeSubscriptions":active_subs,"cancelledSubscriptions":0,"reports":reports,"pendingWithdrawals":0,"paymentErrors":0,"openSupportTickets":int(open_support),"freeDownloadsPerMonth":s['freeDownloadsPerMonth'],"premiumDownloadsPerMonth":s['premiumDownloadsPerMonth'],"premiumMonthly":money(s['premiumMonthlyCents']),"premiumAnnual":money(s['premiumAnnualCents']),"topAssets":[product_view(r,None,s) for r in top],"recentOrders":[]}


@app.get("/api/admin/settings")
def admin_settings(request: Request):
    require_admin(request)
    with db() as c: return get_settings(c)


@app.patch("/api/admin/settings")
async def patch_admin_settings(request: Request):
    admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN"})
    body = await request.json()
    allowed_top = {"brand","positioning","freeDownloadsPerDay","premiumDownloadsPerDay","freeDownloadsPerMonth","premiumDownloadsPerMonth","premiumMonthlyCents","premiumAnnualCents","premiumAnnualInstallments","premiumAnnualInstallmentCents","individualPriceCents","withdrawalMinimumCents","releaseDays","platformCommission","gatewayFee","premiumCreatorCompensation","moderationMode","resourceTypes","watermarkOpacity","watermarkScale","watermarkRotation","watermarkMode","appearance","branding","content","general","featuredProductIds","priceRules","aiModeration","founderPremiumCampaign","planContent","creatorHighlights"}
    payload = {k:v for k,v in body.items() if k in allowed_top}
    with db() as c:
        old=get_settings(c); new={**old,**payload}
        for k in ("appearance","branding","content","general","priceRules","aiModeration","gatewayFee","platformCommission","founderPremiumCampaign","premiumCreatorCompensation","creatorHighlights"):
            if k in payload: new[k]={**old.get(k,{}),**payload[k]}
        if 'planContent' in payload:
            new['planContent']={**old.get('planContent',{})}
            for plan_key,plan_value in payload.get('planContent',{}).items():
                if plan_key in ('FREE','PREMIUM') and isinstance(plan_value,dict):
                    new['planContent'][plan_key]={**old.get('planContent',{}).get(plan_key,{}),**plan_value}
        comp=new.get('premiumCreatorCompensation',{})
        premium_comp=int(comp.get('premiumCents',60)); max_comp=int(comp.get('premiumMaxCents',70))
        if premium_comp < 0:
            raise HTTPException(400,'Remuneração Premium inválida.')
        if not (int(comp.get('premiumMaxMinCents',70)) <= max_comp <= int(comp.get('premiumMaxMaxCents',80))):
            raise HTTPException(400,'Premium Max deve ficar entre R$ 0,70 e R$ 0,80 por download válido.')
        comp['paymentsEnabled']=False
        new['premiumCreatorCompensation']=comp
        if int(new['premiumAnnualInstallments'])*int(new['premiumAnnualInstallmentCents']) != int(new['premiumAnnualCents']):
            raise HTTPException(400, "As parcelas do plano anual precisam totalizar o preço anual.")
        if int(new['individualPriceCents']) < int(new.get('priceRules',{}).get('minimumCents',0)):
            raise HTTPException(400, "O preço padrão não pode ser menor que o preço mínimo.")
        if not all(re.match(r"^#[0-9A-Fa-f]{6}$", str(v)) for v in new.get('appearance',{}).values() if str(v).startswith('#')):
            raise HTTPException(400, "Uma das cores informadas é inválida.")
        if len(new.get('resourceTypes', [])) > 50 or any(len(str(x)) > 48 for x in new.get('resourceTypes', [])):
            raise HTTPException(400, "A lista de categorias excede o limite seguro.")
        if any(len(str(new.get('content',{}).get(k,''))) > lim for k,lim in [('heroKicker',120),('heroTitle',160),('heroSubtitle',700),('footerDescription',700)]):
            raise HTTPException(400, "Um dos textos institucionais excede o limite permitido.")
        for key,val in new.get('branding',{}).items():
            if isinstance(val,str) and len(val) > 700_000:
                raise HTTPException(400, f"Arquivo de identidade '{key}' excede o limite permitido.")
        if not (0 <= int(new['freeDownloadsPerDay']) <= 500 and 0 <= int(new['premiumDownloadsPerDay']) <= 5000):
            raise HTTPException(400, "Limite diário de downloads fora da faixa permitida.")
        save_settings(c,new)
        bump_catalog(c, "settings")
    audit(admin['name'],'ADMIN_SETTINGS_UPDATED',{'changed':list(payload.keys())})
    return new


@app.get("/api/admin/founders")
def admin_founders(request: Request):
    require_admin(request, roles={"SUPER_ADMIN", "ADMIN"})
    with db() as c:
        s = get_settings(c)
        campaign = founder_campaign_view(c, s)
        rows = c.execute(
            """SELECT g.slot,g.status,g.granted_at,u.id user_id,u.email,u.first_name,u.last_name,u.creator_status
               FROM founder_premium_grants g
               JOIN users u ON u.id=g.user_id
               ORDER BY g.slot"""
        ).fetchall()
        return {
            "campaign": campaign,
            "recipients": [
                {
                    "slot": int(r["slot"]),
                    "userId": r["user_id"],
                    "name": f"{r['first_name']} {r['last_name']}".strip(),
                    "email": r["email"],
                    "status": r["status"],
                    "creatorStatus": r["creator_status"],
                    "grantedAt": r["granted_at"],
                }
                for r in rows
            ],
        }


@app.patch("/api/admin/founders")
async def admin_founders_settings(request: Request):
    admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN"})
    body = await request.json()
    with db() as c:
        s = get_settings(c)
        cfg = {**default_settings()["founderPremiumCampaign"], **s.get("founderPremiumCampaign", {})}
        claimed = c.execute("SELECT COUNT(*) n FROM founder_premium_grants WHERE status='ACTIVE'").fetchone()["n"]

        if "limit" in body:
            limit = int(body["limit"])
            if limit < 1 or limit > 100:
                raise HTTPException(400, "O limite da campanha deve ficar entre 1 e 100.")
            if limit < claimed:
                raise HTTPException(409, "O limite não pode ser menor que a quantidade de benefícios já concedidos.")
            cfg["limit"] = limit

        if "autoClose" in body:
            cfg["autoClose"] = bool(body["autoClose"])

        if "enabled" in body:
            enabled = bool(body["enabled"])
            if enabled and claimed >= int(cfg.get("limit", 10)):
                raise HTTPException(409, "Todas as vagas Premium vitalícias já foram preenchidas.")
            cfg["enabled"] = enabled
            cfg["closedReason"] = None if enabled else str(body.get("closedReason") or "PAUSED_BY_ADMIN")

        s["founderPremiumCampaign"] = cfg
        save_settings(c, s)
        c.execute(
            "INSERT INTO audit_logs(id,actor,action,details_json,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), admin["name"], "FOUNDER_CAMPAIGN_UPDATED", jdump({"campaign": cfg, "claimed": claimed}), iso_now()),
        )
    return admin_founders(request)


@app.get("/api/admin/clients")
def admin_clients(request: Request):
    require_admin(request)
    with db() as c:
        out=[]
        for u in c.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall():
            out.append({"id":u['id'],"name":f"{u['first_name']} {u['last_name']}","email":u['email'],"plan":u['plan'],"status":u['status'],"createdAt":u['created_at'],"purchases":c.execute('SELECT COUNT(*) n FROM entitlements WHERE user_id=?',(u['id'],)).fetchone()['n'],"favorites":c.execute('SELECT COUNT(*) n FROM favorites WHERE user_id=?',(u['id'],)).fetchone()['n']})
        return out


@app.patch("/api/admin/clients/{user_id}/status")
async def admin_client_status(user_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={'SUPER_ADMIN','ADMIN'}); body=await request.json(); status=str(body.get('status',''))
    if status not in ('ACTIVE','BLOCKED','DISABLED'): raise HTTPException(400,'Status inválido.')
    with db() as c:
        old=c.execute('SELECT status FROM users WHERE id=?',(user_id,)).fetchone()
        if not old: raise HTTPException(404,'Cliente não encontrado.')
        c.execute('UPDATE users SET status=?,updated_at=? WHERE id=?',(status,iso_now(),user_id))
        if status!='ACTIVE': c.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
    audit(admin['name'],'CLIENT_STATUS_CHANGED',{'userId':user_id,'before':old['status'],'after':status}); return {'ok':True,'status':status}


@app.get("/api/admin/highlighted-collaborators")
def admin_highlighted_collaborators(request: Request):
    require_admin(request)
    with db() as c:
        s=get_settings(c); current=highlighted_collaborators(c,s); users=[]
        for u in c.execute("SELECT * FROM users WHERE creator_status='ACTIVE' AND roles_json LIKE '%collaborator%' ORDER BY created_at DESC").fetchall():
            profile=jload(u["profile_json"],{});users.append({"id":u["id"],"name":f"{u['first_name']} {u['last_name']}".strip(),"email":u["email"],"photo":profile.get("photo","") or ""})
        return {"config":s.get("creatorHighlights",{}),"current":current,"candidates":users}


@app.get("/api/admin/collaborators")
def admin_collaborators(request: Request):
    require_admin(request)
    with db() as c:
        out=[]
        for u in c.execute("SELECT * FROM users WHERE roles_json LIKE '%collaborator%' ORDER BY created_at DESC").fetchall():
            own=c.execute('SELECT * FROM products WHERE owner_id=?',(u['id'],)).fetchall()
            profile=jload(u['profile_json'],{})
            founder=c.execute("SELECT slot FROM founder_premium_grants WHERE user_id=? AND status='ACTIVE'",(u['id'],)).fetchone()
            tiers={k:sum(1 for p in own if p['access_tier']==k) for k in ('FREE','PREMIUM','PREMIUM_MAX')}
            out.append({'id':u['id'],'userId':u['id'],'name':f"{u['first_name']} {u['last_name']}",'email':u['email'],'status':u['creator_status'],'assets':len(own),'pending':sum(1 for p in own if p['status'] in ('PENDING_REVIEW','IN_REVIEW','CHANGES_REQUESTED')),'sales':sum(p['sales'] for p in own),'photo':profile.get('photo',''),'site':profile.get('site',''),'founder':bool(founder),'founderSlot':int(founder['slot']) if founder else None,'tiers':tiers})
        return out


@app.get("/api/admin/collaborators/{user_id}")
def admin_collaborator_detail(user_id: str, request: Request):
    require_admin(request)
    with db() as c:
        u=c.execute("SELECT * FROM users WHERE id=? AND roles_json LIKE '%collaborator%'",(user_id,)).fetchone()
        if not u: raise HTTPException(404,"Colaborador não encontrado.")
        profile=jload(u["profile_json"],{});s=get_settings(c);rows=c.execute("SELECT * FROM products WHERE owner_id=? ORDER BY created_at DESC",(user_id,)).fetchall()
        return {"id":u["id"],"name":f"{u['first_name']} {u['last_name']}".strip(),"email":u["email"],"status":u["creator_status"],"photo":profile.get("photo","") or "","bio":profile.get("bio","") or "","site":profile.get("site","") or "","instagram":profile.get("instagram","") or "","youtube":profile.get("youtube","") or "","joinedAt":u["created_at"],"assets":[enrich_product_view(c,product_view(r,None,s),r) for r in rows],"published":sum(1 for r in rows if r["status"]=="PUBLISHED" and not r["admin_removed"]),"suspended":sum(1 for r in rows if r["status"]=="SUSPENDED" or r["admin_removed"]),"sales":sum(int(r["sales"]) for r in rows)}


@app.patch("/api/admin/collaborators/{user_id}/status")
async def admin_collab_status(user_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={'SUPER_ADMIN','ADMIN'}); body=await request.json(); status=str(body.get('status',''))
    if status not in ('ACTIVE','PENDING','SUSPENDED'): raise HTTPException(400,'Status inválido.')
    with db() as c:
        old=c.execute('SELECT creator_status,status FROM users WHERE id=?',(user_id,)).fetchone()
        if not old: raise HTTPException(404,'Colaborador não encontrado.')
        c.execute('UPDATE users SET creator_status=?,updated_at=? WHERE id=?',(status,iso_now(),user_id))
        if status=='ACTIVE' and old['status']!='ACTIVE' and not has_explicit_client_block(c,user_id):
            c.execute("UPDATE users SET status='ACTIVE',updated_at=? WHERE id=?",(iso_now(),user_id))
    audit(admin['name'],'COLLABORATOR_STATUS_CHANGED',{'userId':user_id,'before':old['creator_status'],'after':status}); return {'ok':True,'status':status}


@app.get("/api/admin/products")
def admin_products(request: Request):
    require_admin(request)
    with db() as c:
        s=get_settings(c); include_removed=request.query_params.get("includeRemoved")=="1"; rows=c.execute("SELECT * FROM products"+("" if include_removed else " WHERE admin_removed=0")+" ORDER BY created_at DESC").fetchall();out=[]
        for r in rows:
            v=enrich_product_view(c,product_view(r,None,s),r);v["canvaUrl"]=r["external_url"] if str(r["delivery_mode"] or "FILE")=="CANVA" else None;out.append(v)
        return out


@app.post("/api/admin/products")
async def admin_create_product(request: Request):
    admin=require_admin(request,csrf=True,roles={'SUPER_ADMIN','ADMIN'}); body=await request.json()
    title=str(body.get('title','')).strip(); category=str(body.get('category','')).strip(); category='CANVA' if category.upper()=='CANVA' else category; tier=str(body.get('accessTier','PREMIUM')).upper(); upload_id=str(body.get('uploadId','')); canva_url=str(body.get('canvaUrl','')).strip()
    if not title: raise HTTPException(400,'Informe o título do asset.')
    with db() as c:
        s=get_settings(c)
        if category not in s['resourceTypes']: raise HTTPException(400,'Categoria inválida.')
        if tier not in ('FREE','PREMIUM','PREMIUM_MAX'): raise HTTPException(400,'Tipo de acesso inválido.')
        delivery_mode='CANVA' if category=='CANVA' else 'FILE'
        if delivery_mode=='CANVA':
            if tier=='PREMIUM_MAX': raise HTTPException(400,'Canva não utiliza Premium Max. Use Free ou Premium.')
            canva_url=validate_canva_url(canva_url)
        else: canva_url=''
        up=consume_upload(c,upload_id,admin_id=admin['id'])
        requested=int(body.get('individualPriceCents') or s['individualPriceCents']); minimum=int(s['priceRules']['minimumCents'])
        if requested<minimum: raise HTTPException(400,'Preço abaixo do mínimo configurado.')
        id_='adm-'+uuid.uuid4().hex[:12]; base=slugify(title); slug=base if not c.execute('SELECT 1 FROM products WHERE slug=?',(base,)).fetchone() else f'{base}-{id_[-4:]}'
        now=iso_now(); status=str(body.get('status','PUBLISHED')); status=status if status in ('DRAFT','PENDING_REVIEW','IN_REVIEW','PUBLISHED','REJECTED','SUSPENDED','CHANGES_REQUESTED') else 'PUBLISHED'
        featured=bool(body.get('featured'))
        moderation={'mode':'ADMIN_DIRECT','result':'APPROVED','reviewedAt':now,'approvedAt':now,'humanDecision':True}
        history=[{'at':now,'actor':admin['name'],'action':'CREATE','status':status,'reason':'Asset adicionado pelo Painel Admin'}]
        meta= jload(up['metadata_json'], {})
        extra_files=list(meta.get('packageFiles', [])) if tier=='PREMIUM_MAX' else []
        formats=list(dict.fromkeys([str(up['extension']).upper()]+[str(f.get('extension','')).upper() for f in extra_files if f.get('extension')]))
        c.execute("""INSERT INTO products(id,slug,owner_id,title,seller,category,formats_json,description,preview_url,original_path,license,access_tier,individual_purchase_enabled,individual_price_cents,status,tags_json,featured,hidden,sales,premium_downloads,free_downloads,created_by_admin,moderation_json,history_json,external_url,delivery_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (id_,slug,None,title,str(body.get('seller','ATV Admin')).strip() or 'ATV Admin',category,jdump(["CANVA"] if delivery_mode=="CANVA" else formats),str(body.get('description','')).strip() or 'Recurso digital publicado pela administração da ATV.',f"/media/previews/{Path(up['preview_path']).name}",up['original_path'],str(body.get('license','Uso comercial')),tier,1 if tier == 'PREMIUM' else 0,requested,status,jdump(body.get('tags',[])),1 if featured else 0,0,0,0,0,1,jdump(moderation),jdump(history),canva_url or None,delivery_mode,now,now))
        if tier=='PREMIUM_MAX':
            c.execute("INSERT INTO product_files(id,product_id,original_path,extension,filename,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),id_,up['original_path'],str(up['extension']).upper(),clean_package_filename(str(meta.get('_originalFilename') or ''),f"arquivo.{str(up['extension']).lower()}"),now))
            for f in extra_files:
                fp=Path(str(f.get('originalPath','')))
                if not fp.exists(): raise HTTPException(400,'Um arquivo adicional do Premium Max não está mais disponível.')
                c.execute("INSERT INTO product_files(id,product_id,original_path,extension,filename,created_at) VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),id_,str(fp),str(f.get('extension',fp.suffix.lstrip('.'))).upper(),clean_package_filename(str(f.get('filename',fp.name)),fp.name),now))
        if featured:
            s['featuredProductIds']=list(dict.fromkeys([id_]+s.get('featuredProductIds',[])))[:8]; save_settings(c,s)
        row=c.execute('SELECT * FROM products WHERE id=?',(id_,)).fetchone()
        bump_catalog(c, "admin-create")
    audit(admin['name'],'ASSET_CREATED_BY_ADMIN',{'productId':id_,'title':title});
    with db() as c:
        row=c.execute('SELECT * FROM products WHERE id=?',(id_,)).fetchone(); s=get_settings(c); view=enrich_product_view(c,product_view(row,None,s),row)
    return JSONResponse(view,status_code=201)


@app.post("/api/admin/products/demo-visibility")
async def admin_demo_visibility(request: Request):
    admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN"})
    body = await request.json()
    visible = bool(body.get("visible", False))
    with db() as c:
        s = get_settings(c)
        rows = c.execute("SELECT id FROM products WHERE owner_id IS NULL AND created_by_admin=0 AND moderation_json LIKE '%SEED%'").fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute("UPDATE products SET hidden=?,featured=0,updated_at=? WHERE owner_id IS NULL AND created_by_admin=0 AND moderation_json LIKE '%SEED%'", (0 if visible else 1, iso_now()))
            if not visible:
                s["featuredProductIds"] = [x for x in s.get("featuredProductIds", []) if x not in ids]
                save_settings(c, s)
            bump_catalog(c, "demo-visibility")
    audit(admin["name"], "DEMO_ASSETS_SHOWN" if visible else "DEMO_ASSETS_HIDDEN", {"count": len(ids)})
    return {"ok": True, "visible": visible, "count": len(ids)}


@app.post("/api/admin/products/{product_id}/preview")
async def admin_replace_product_preview(product_id: str, request: Request, preview: UploadFile = File(...), previewMethod: str = Form("ADMIN_PRODUCT_PREVIEW_OVERRIDE")):
    admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN", "MODERATOR"})
    data = await preview.read(MAX_PREVIEW_BYTES + 1)
    ext = validate_preview(data, preview.content_type or "")
    with db() as c:
        p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Asset não encontrado.")
        filename = f"product-{product_id}-{uuid.uuid4().hex[:8]}.{ext}"
        new_path = PREVIEWS / filename
        new_path.write_bytes(data)
        old_url = p["preview_url"] or ""
        c.execute("UPDATE products SET preview_url=?,updated_at=? WHERE id=?", (f"/media/previews/{filename}", iso_now(), product_id))
        if old_url.startswith("/media/previews/"):
            old_path = PREVIEWS / Path(old_url).name
            if old_path != new_path and old_path.exists():
                try: old_path.unlink()
                except OSError: pass
        row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        s = get_settings(c)
        bump_catalog(c, "preview-replaced")
    audit(admin["name"], "ASSET_PREVIEW_REPLACED", {"productId": product_id, "previewMethod": previewMethod})
    return product_view(row, None, s)


@app.patch("/api/admin/products/{product_id}")
async def admin_patch_product(product_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={'SUPER_ADMIN','ADMIN','MODERATOR'}); body=await request.json()
    with db() as c:
        s=get_settings(c); p=c.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
        if not p: raise HTTPException(404,'Asset não encontrado.')
        values={k:p[k] for k in p.keys()}; before={'title':p['title'],'category':p['category'],'accessTier':p['access_tier'],'status':p['status'],'featured':bool(p['featured']),'hidden':bool(p['hidden']),'individualPriceCents':p['individual_price_cents']}
        if 'title' in body and str(body['title']).strip(): values['title']=str(body['title']).strip()
        if 'category' in body:
            category_value=str(body['category']).strip(); category_value='CANVA' if category_value.upper()=='CANVA' else category_value
            if category_value not in s['resourceTypes']: raise HTTPException(400,'Categoria inválida.')
            values['category']=category_value
        if 'canvaUrl' in body: values['external_url']=str(body.get('canvaUrl') or '').strip() or None
        values['delivery_mode']='CANVA' if values['category']=='CANVA' else 'FILE'
        if values['delivery_mode']=='CANVA':
            if values['access_tier']=='PREMIUM_MAX': raise HTTPException(400,'Canva não utiliza Premium Max.')
            values['external_url']=validate_canva_url(values['external_url'])
        else: values['external_url']=None
        if 'accessTier' in body:
            tier=str(body['accessTier']).upper()
            if tier not in ('FREE','PREMIUM','PREMIUM_MAX'): raise HTTPException(400,'Tipo de acesso inválido.')
            values['access_tier']=tier
            values['individual_purchase_enabled']=1 if tier == 'PREMIUM' else 0
            if tier=='PREMIUM_MAX' and not c.execute('SELECT 1 FROM product_files WHERE product_id=?',(product_id,)).fetchone() and p['original_path']:
                c.execute('INSERT INTO product_files(id,product_id,original_path,extension,filename,created_at) VALUES(?,?,?,?,?,?)',(str(uuid.uuid4()),product_id,p['original_path'],(jload(p['formats_json'],[]) or [Path(p['original_path']).suffix.lstrip('.')])[0],Path(p['original_path']).name,iso_now()))
        if values['delivery_mode']=='CANVA' and values['access_tier']=='PREMIUM_MAX':
            raise HTTPException(400,'Canva não utiliza Premium Max.')
        if 'status' in body:
            if body['status'] not in ('DRAFT','PENDING_REVIEW','IN_REVIEW','PUBLISHED','REJECTED','SUSPENDED','CHANGES_REQUESTED'): raise HTTPException(400,'Status inválido.')
            values['status']=body['status']
        if 'featured' in body: values['featured']=1 if body['featured'] else 0
        if 'hidden' in body: values['hidden']=1 if body['hidden'] else 0
        if values['status'] != 'PUBLISHED':
            values['hidden']=1
            values['featured']=0
        if 'individualPriceCents' in body:
            v=int(body['individualPriceCents'])
            if v<int(s['priceRules']['minimumCents']): raise HTTPException(400,'Preço abaixo do mínimo configurado.')
            values['individual_price_cents']=v
        c.execute('UPDATE products SET title=?,category=?,access_tier=?,individual_purchase_enabled=?,status=?,featured=?,hidden=?,individual_price_cents=?,external_url=?,delivery_mode=?,updated_at=? WHERE id=?',(values['title'],values['category'],values['access_tier'],values['individual_purchase_enabled'],values['status'],values['featured'],values['hidden'],values['individual_price_cents'],values['external_url'],values['delivery_mode'],iso_now(),product_id))
        if values['status']=='SUSPENDED' and p['status']!='SUSPENDED':
            notify_asset_incompatibility(c,p,'SUSPENDED')
        if values['featured']:
            s['featuredProductIds']=list(dict.fromkeys(s.get('featuredProductIds',[])+[product_id]))[:8]
        else: s['featuredProductIds']=[x for x in s.get('featuredProductIds',[]) if x!=product_id]
        save_settings(c,s); row=c.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
        view=enrich_product_view(c,product_view(row,None,s),row)
        bump_catalog(c, "admin-patch")
    audit(admin['name'],'ASSET_UPDATED',{'productId':product_id,'before':before,'after':{'title':row['title'],'category':row['category'],'accessTier':row['access_tier'],'status':row['status'],'featured':bool(row['featured']),'hidden':bool(row['hidden']),'individualPriceCents':row['individual_price_cents']}}); return view


@app.delete("/api/admin/products/{product_id}")
def admin_delete_product(product_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={'SUPER_ADMIN','ADMIN'})
    with db() as c:
        s=get_settings(c); p=c.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
        if not p: raise HTTPException(404,'Asset não encontrado.')
        commercial=(c.execute('SELECT 1 FROM order_items WHERE product_id=? LIMIT 1',(product_id,)).fetchone()
                    or c.execute('SELECT 1 FROM entitlements WHERE product_id=? LIMIT 1',(product_id,)).fetchone()
                    or c.execute('SELECT 1 FROM download_events WHERE product_id=? LIMIT 1',(product_id,)).fetchone())
        s['featuredProductIds']=[x for x in s.get('featuredProductIds',[]) if x!=product_id]; save_settings(c,s)
        notify_asset_incompatibility(c,p,'REMOVED')
        # O contador `sales` dos assets de exemplo é demonstrativo.
        # Pedidos, entitlements ou downloads válidos obrigam a preservação do histórico.
        if commercial:
            hist=jload(p['history_json'],[]); hist.append({'at':iso_now(),'actor':admin['name'],'action':'REMOVE_FROM_STOREFRONT','status':'SUSPENDED','reason':'Incompatibilidade'})
            c.execute("UPDATE products SET status='SUSPENDED',hidden=1,featured=0,admin_removed=1,history_json=?,updated_at=? WHERE id=?",(jdump(hist),iso_now(),product_id)); mode='ARCHIVED'; msg='Asset removido do catálogo ativo e histórico comercial preservado.' 
        else:
            preview_url=str(p['preview_url'] or '')
            preview_path=(PREVIEWS / Path(preview_url).name) if preview_url.startswith('/media/previews/') else None
            original=Path(p['original_path']) if p['original_path'] else None
            package_paths=[Path(r['original_path']) for r in c.execute('SELECT original_path FROM product_files WHERE product_id=?',(product_id,)).fetchall()]
            c.execute('DELETE FROM download_tokens WHERE product_id=?',(product_id,))
            c.execute('DELETE FROM products WHERE id=?',(product_id,)); mode='DELETED'; msg='Asset removido definitivamente.'
            for fp in [preview_path,original,*package_paths]:
                if fp and 'demo-seed' not in fp.name:
                    try:
                        if fp.exists(): fp.unlink()
                    except OSError:
                        pass
        bump_catalog(c, "admin-delete")
    audit(admin['name'],'ASSET_REMOVED_FROM_STOREFRONT' if mode=='ARCHIVED' else 'ASSET_DELETED_BY_ADMIN',{'productId':product_id,'title':p['title'],'mode':mode}); return {'ok':True,'mode':mode,'message':msg,'previewKey':None}


@app.post("/api/admin/products/{product_id}/action")
async def admin_product_action(product_id: str, request: Request):
    admin=require_admin(request,csrf=True,roles={'SUPER_ADMIN','ADMIN','MODERATOR'}); body=await request.json(); action=str(body.get('action','')).upper(); reason=str(body.get('reason','')).strip()
    mapping={'APPROVE':'PUBLISHED','REJECT':'REJECTED','REQUEST_CHANGES':'CHANGES_REQUESTED','SUSPEND':'SUSPENDED','REVIEW':'IN_REVIEW'}
    if action not in mapping: raise HTTPException(400,'Ação administrativa inválida.')
    if action in ('REJECT','REQUEST_CHANGES') and not reason: raise HTTPException(400,'Informe o motivo da decisão.')
    with db() as c:
        s=get_settings(c); p=c.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
        if not p: raise HTTPException(404,'Asset não encontrado.')
        hist=jload(p['history_json'],[]); hist.append({'at':iso_now(),'actor':admin['name'],'action':action,'status':mapping[action],'reason':reason or None})
        mod=jload(p['moderation_json'],{}); mod.update({'result':mapping[action],'reviewedAt':iso_now(),'humanDecision':True,'reason':reason or None})
        next_status=mapping[action]
        next_hidden=0 if next_status=='PUBLISHED' else 1
        c.execute('UPDATE products SET status=?,hidden=?,featured=CASE WHEN ?=1 THEN 0 ELSE featured END,moderation_json=?,history_json=?,updated_at=? WHERE id=?',(next_status,next_hidden,next_hidden,jdump(mod),jdump(hist),iso_now(),product_id))
        if action=='SUSPEND':
            notify_asset_incompatibility(c,p,'SUSPENDED')
        if next_hidden:
            s['featuredProductIds']=[x for x in s.get('featuredProductIds',[]) if x!=product_id]
            save_settings(c,s)
        row=c.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
        bump_catalog(c, "admin-action")
    audit(admin['name'],'ASSET_'+action,{'productId':product_id,'after':mapping[action],'reason':reason}); return product_view(row,None,s)


@app.get("/api/admin/products/{product_id}/detail")
def admin_product_detail(product_id: str, request: Request):
    require_admin(request)
    with db() as c:
        s = get_settings(c)
        row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Asset não encontrado.")
        return enrich_product_view(c, product_view(row, None, s), row)


@app.post("/api/admin/products/{product_id}/approve")
def admin_approve_product(product_id: str, request: Request):
    admin = require_admin(request, csrf=True, roles={"SUPER_ADMIN", "ADMIN", "MODERATOR"})
    with db() as c:
        s = get_settings(c)
        p = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Asset não encontrado.")
        now = iso_now()
        hist = jload(p["history_json"], [])
        hist.append({"at": now, "actor": admin["name"], "action": "APPROVE", "status": "PUBLISHED", "reason": None})
        mod = jload(p["moderation_json"], {})
        mod.update({"result": "APPROVED", "reviewedAt": now, "approvedAt": now, "humanDecision": True, "reason": None})
        c.execute(
            """UPDATE products
               SET status='PUBLISHED',hidden=0,moderation_json=?,history_json=?,updated_at=?
               WHERE id=?""",
            (jdump(mod), jdump(hist), now, product_id),
        )
        row = c.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row or row["status"] != "PUBLISHED" or int(row["hidden"]) != 0:
            raise HTTPException(500, "A aprovação não pôde ser confirmada no banco.")
        bump_catalog(c, "admin-approve")
        c.execute(
            "INSERT INTO audit_logs(id,actor,action,details_json,created_at) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), admin["name"], "ASSET_APPROVED_CONFIRMED", jdump({"productId": product_id}), now),
        )
        return enrich_product_view(c, product_view(row, None, s), row)


@app.get("/api/admin/orders")
def admin_orders(request: Request):
    require_admin(request)
    with db() as c:
        out=[]
        for o in c.execute('SELECT * FROM orders ORDER BY created_at DESC').fetchall():
            items=c.execute('SELECT product_id,price_cents FROM order_items WHERE order_id=?',(o['id'],)).fetchall()
            user=c.execute('SELECT email FROM users WHERE id=?',(o['user_id'],)).fetchone()
            out.append({'id':o['id'],'items':[i['product_id'] for i in items],'total':money(o['total_cents']),'status':o['status'],'gatewayMode':o['gateway_mode'],'buyer':user['email'] if user else None,'createdAt':o['created_at']})
        return out


@app.get("/api/admin/admins")
def admin_admins(request: Request):
    require_admin(request,roles={'SUPER_ADMIN'})
    with db() as c: return [{'id':r['id'],'name':r['name'],'role':r['role'],'status':r['status']} for r in c.execute('SELECT * FROM admins ORDER BY created_at').fetchall()]


@app.patch("/api/admin/admins/{admin_id}")
async def patch_admin_user(admin_id: str, request: Request):
    actor=require_admin(request,csrf=True,roles={'SUPER_ADMIN'}); body=await request.json(); role=str(body.get('role','')); status=str(body.get('status',''))
    if role not in ('SUPER_ADMIN','ADMIN','MODERATOR') or status not in ('ACTIVE','DISABLED'): raise HTTPException(400,'Nível ou status inválido.')
    with db() as c:
        adm=c.execute('SELECT * FROM admins WHERE id=?',(admin_id,)).fetchone()
        if not adm: raise HTTPException(404,'Administrador não encontrado.')
        c.execute('UPDATE admins SET role=?,status=? WHERE id=?',(role,status,admin_id))
        if status!='ACTIVE': c.execute('DELETE FROM admin_sessions WHERE admin_id=?',(admin_id,))
    audit(actor['name'],'ADMIN_PERMISSION_CHANGED',{'adminId':admin_id,'before':{'role':adm['role'],'status':adm['status']},'after':{'role':role,'status':status}}); return {'id':admin_id,'name':adm['name'],'role':role,'status':status}


@app.get("/api/admin/audit")
def admin_audit(request: Request):
    require_admin(request)
    with db() as c:
        return [{'id':r['id'],'actor':r['actor'],'action':r['action'],'details':jload(r['details_json'],{}),'at':r['created_at']} for r in c.execute('SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 500').fetchall()]

# Compatibility endpoint used by older UI pieces
@app.get("/api/admin/pending-products")
def admin_pending(request: Request):
    require_admin(request)
    with db() as c:
        s=get_settings(c); rows=c.execute("SELECT * FROM products WHERE status IN ('PENDING_REVIEW','IN_REVIEW','CHANGES_REQUESTED') ORDER BY created_at").fetchall(); return [enrich_product_view(c,product_view(r,None,s),r) for r in rows]

# ---------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------
def frontend_html_response() -> HTMLResponse:
    p = FRONTEND / "index.html"
    if not p.exists():
        return HTMLResponse("ATV frontend ainda não foi gerado.", status_code=503)
    return HTMLResponse(
        p.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
            "X-ATV-Frontend-Build": BUILD_VERSION,
        },
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return frontend_html_response()


@app.get("/{path:path}")
def spa_fallback(path: str):
    if path.startswith("api/") or path.startswith("media/"):
        raise HTTPException(404)
    return frontend_html_response()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
