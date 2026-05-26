from datetime import datetime, timezone
import json
import os
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import bcrypt
from flask import Blueprint, jsonify, redirect, request, send_file

from infrastructure.db_conn.mysql_config import get_connection
from infrastructure.db_conn.mongo_config import mirror_document
from infrastructure.entrypoints.full_fixture import FULL_FIXTURE
from infrastructure.storage.r2_storage import read_image, upload_image


mvp_api = Blueprint("mvp_api", __name__, url_prefix="/api")


FRONTEND_URL = os.getenv("FRONTEND_URL", "https://betmundial.vercel.app").rstrip("/")


def rows_to_json(rows):
    for row in rows:
        for key, value in list(row.items()):
            if isinstance(value, datetime):
                row[key] = value.isoformat()
    return rows


def one_to_json(row):
    if not row:
        return None
    return rows_to_json([row])[0]


def mirror_safe(collection, document, key_fields=None):
    try:
        mirror_document(collection, document, key_fields)
    except Exception:
        pass


def public_account(account):
    account = one_to_json(account)
    if not account:
        return None
    account.pop("password_hash", None)
    role_access = {
        "fan": {
            "access_path": "/fan",
            "label": "Fan app",
            "permissions": ["fixture:read", "prediction:create", "voucher:read", "promotion:read"],
        },
        "merchant": {
            "access_path": "/merchant",
            "label": "Business console",
            "permissions": ["voucher:verify", "reward:create_pending", "promotion:create_pending"],
        },
        "admin": {
            "access_path": "/admin",
            "label": "Admin console",
            "permissions": ["campaign:review", "result:publish", "outreach:manage", "promotion:publish"],
        },
    }
    token = f"demo-{account['role']}-{account['id']}-{uuid.uuid4().hex[:10]}"
    return {**account, **role_access.get(account["role"], {}), "token": token}


def api_post_json(url, payload, headers=None):
    data = urlencode(payload).encode("utf-8")
    req = Request(url, data=data, headers=headers or {})
    with urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def api_get_json(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def oauth_callback_html(account):
    payload = json.dumps(public_account(account))
    params = urlencode({"oauth": "success", "oauth_account": payload})
    return redirect(f"{FRONTEND_URL}/fan?{params}")


def find_or_create_oauth_fan(email, name):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM gp_accounts WHERE email=%s", (email,))
            account = cursor.fetchone()
            if not account:
                password_hash = bcrypt.hashpw(uuid.uuid4().hex.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                cursor.execute(
                    """
                    INSERT INTO gp_accounts (role, name, email, password_hash, city, account_status)
                    VALUES ('fan',%s,%s,%s,%s,'active')
                    """,
                    (name or email, email, password_hash, ""),
                )
                cursor.execute("SELECT * FROM gp_accounts WHERE id=%s", (cursor.lastrowid,))
                account = cursor.fetchone()
            else:
                cursor.execute("UPDATE gp_accounts SET last_login_at=NOW() WHERE id=%s", (account["id"],))
        connection.commit()
    return account


def kickoff_has_started(match):
    if not match:
        return True
    if match.get("status") == "final" or match.get("home_score") is not None or match.get("away_score") is not None:
        return True
    raw = match.get("kickoff_utc")
    if not raw:
        return False
    try:
        kickoff = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return False
    return kickoff <= datetime.now(timezone.utc)


def ensure_schema():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS gp_fans (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(160) NOT NULL,
            handle VARCHAR(40) NOT NULL UNIQUE,
            contact VARCHAR(180) NOT NULL,
            channel ENUM('whatsapp','email') NOT NULL DEFAULT 'whatsapp',
            device_id VARCHAR(80),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_matches (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            external_id VARCHAR(80),
            group_name VARCHAR(80),
            phase VARCHAR(80),
            match_date DATETIME NULL,
            city VARCHAR(120),
            venue VARCHAR(160),
            home_team VARCHAR(120) NOT NULL,
            away_team VARCHAR(120) NOT NULL,
            home_code VARCHAR(12),
            away_code VARCHAR(12),
            home_score INT NULL,
            away_score INT NULL,
            first_half_home_score INT NULL,
            first_half_away_score INT NULL,
            status ENUM('scheduled','live','final') DEFAULT 'scheduled',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_predictions (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            fan_id BIGINT UNSIGNED NOT NULL,
            match_id BIGINT UNSIGNED NOT NULL,
            home_score INT NOT NULL,
            away_score INT NOT NULL,
            first_half_home_score INT NULL,
            first_half_away_score INT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_fan_match (fan_id, match_id),
            FOREIGN KEY (fan_id) REFERENCES gp_fans(id) ON DELETE CASCADE,
            FOREIGN KEY (match_id) REFERENCES gp_matches(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_merchants (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(180) NOT NULL,
            city VARCHAR(120),
            zone VARCHAR(180),
            address TEXT,
            link VARCHAR(500),
            instagram_url VARCHAR(500),
            facebook_url VARCHAR(500),
            tiktok_url VARCHAR(500),
            whatsapp_url VARCHAR(500),
            image_url VARCHAR(500),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_merchant_rewards (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            merchant_id BIGINT UNSIGNED NOT NULL,
            title VARCHAR(180) NOT NULL,
            prize TEXT NOT NULL,
            rule ENUM('participate','winner','exact','goal_diff','home_goals','away_goals','first_half_goals') NOT NULL,
            quantity INT NOT NULL DEFAULT 0,
            city VARCHAR(120),
            expires_at VARCHAR(80),
            image_url VARCHAR(500),
            active TINYINT(1) DEFAULT 1,
            review_status VARCHAR(40) DEFAULT 'approved',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (merchant_id) REFERENCES gp_merchants(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_merchant_promotions (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            merchant_id BIGINT UNSIGNED NOT NULL,
            title VARCHAR(180) NOT NULL,
            description TEXT NOT NULL,
            image_url VARCHAR(500),
            city VARCHAR(120),
            address TEXT,
            link VARCHAR(500),
            instagram_url VARCHAR(500),
            facebook_url VARCHAR(500),
            tiktok_url VARCHAR(500),
            whatsapp_url VARCHAR(500),
            expires_at VARCHAR(80),
            active TINYINT(1) DEFAULT 1,
            review_status VARCHAR(40) DEFAULT 'approved',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (merchant_id) REFERENCES gp_merchants(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_vouchers (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            code VARCHAR(120) NOT NULL UNIQUE,
            fan_id BIGINT UNSIGNED NOT NULL,
            match_id BIGINT UNSIGNED NOT NULL,
            prediction_id BIGINT UNSIGNED NOT NULL,
            merchant_id BIGINT UNSIGNED NOT NULL,
            reward_id BIGINT UNSIGNED NOT NULL,
            status ENUM('valid','used','expired','void') DEFAULT 'valid',
            issued_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            redeemed_at DATETIME NULL,
            FOREIGN KEY (fan_id) REFERENCES gp_fans(id) ON DELETE CASCADE,
            FOREIGN KEY (match_id) REFERENCES gp_matches(id) ON DELETE CASCADE,
            FOREIGN KEY (prediction_id) REFERENCES gp_predictions(id) ON DELETE CASCADE,
            FOREIGN KEY (merchant_id) REFERENCES gp_merchants(id) ON DELETE CASCADE,
            FOREIGN KEY (reward_id) REFERENCES gp_merchant_rewards(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_accounts (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            role ENUM('fan','merchant','admin') NOT NULL,
            name VARCHAR(180) NOT NULL,
            email VARCHAR(180) NOT NULL UNIQUE,
            password_hash VARCHAR(120) NOT NULL,
            city VARCHAR(120),
            merchant_id BIGINT UNSIGNED NULL,
            account_status VARCHAR(40) DEFAULT 'active',
            last_login_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (merchant_id) REFERENCES gp_merchants(id) ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_role_access (
            role_name VARCHAR(40) PRIMARY KEY,
            label VARCHAR(120) NOT NULL,
            access_path VARCHAR(120) NOT NULL,
            permissions TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS gp_outreach_leads (
            id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            city VARCHAR(120) NOT NULL,
            source_app VARCHAR(120),
            business_name VARCHAR(180) NOT NULL,
            category VARCHAR(120),
            contact_name VARCHAR(160),
            contact_channel ENUM('whatsapp','email','instagram','facebook','tiktok','manual') DEFAULT 'manual',
            contact_value VARCHAR(500),
            invite_token VARCHAR(80) NOT NULL UNIQUE,
            invite_url VARCHAR(700),
            status ENUM('new','invited','opened','registered','declined') DEFAULT 'new',
            notes TEXT,
            sent_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ]
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            for statement in [
                "ALTER TABLE gp_matches ADD COLUMN kickoff_utc VARCHAR(40) NULL AFTER match_date",
                "ALTER TABLE gp_matches ADD COLUMN match_timezone VARCHAR(80) NULL AFTER kickoff_utc",
                "ALTER TABLE gp_merchants ADD COLUMN city VARCHAR(120) NULL AFTER name",
                "ALTER TABLE gp_merchants ADD COLUMN instagram_url VARCHAR(500) NULL AFTER link",
                "ALTER TABLE gp_merchants ADD COLUMN facebook_url VARCHAR(500) NULL AFTER instagram_url",
                "ALTER TABLE gp_merchants ADD COLUMN tiktok_url VARCHAR(500) NULL AFTER facebook_url",
                "ALTER TABLE gp_merchants ADD COLUMN whatsapp_url VARCHAR(500) NULL AFTER tiktok_url",
                "ALTER TABLE gp_merchant_rewards ADD COLUMN city VARCHAR(120) NULL AFTER quantity",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN city VARCHAR(120) NULL AFTER image_url",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN address TEXT NULL AFTER city",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN instagram_url VARCHAR(500) NULL AFTER link",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN facebook_url VARCHAR(500) NULL AFTER instagram_url",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN tiktok_url VARCHAR(500) NULL AFTER facebook_url",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN whatsapp_url VARCHAR(500) NULL AFTER tiktok_url",
                "ALTER TABLE gp_merchant_rewards ADD COLUMN review_status VARCHAR(40) DEFAULT 'approved' AFTER active",
                "ALTER TABLE gp_merchant_promotions ADD COLUMN review_status VARCHAR(40) DEFAULT 'approved' AFTER active",
                "ALTER TABLE gp_accounts ADD COLUMN account_status VARCHAR(40) DEFAULT 'active' AFTER merchant_id",
                "ALTER TABLE gp_accounts ADD COLUMN last_login_at DATETIME NULL AFTER account_status",
            ]:
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    if "Duplicate column" not in str(exc):
                        raise
            cursor.execute("UPDATE gp_merchant_rewards SET review_status='approved', active=1 WHERE review_status IS NULL")
            cursor.execute("UPDATE gp_merchant_promotions SET review_status='approved', active=1 WHERE review_status IS NULL")
            cursor.executemany(
                """
                INSERT INTO gp_role_access (role_name, label, access_path, permissions)
                VALUES (%s,%s,%s,%s)
                ON DUPLICATE KEY UPDATE label=VALUES(label), access_path=VALUES(access_path), permissions=VALUES(permissions)
                """,
                [
                    ("fan", "Fan app", "/fan", "fixture:read,prediction:create,voucher:read,promotion:read"),
                    ("merchant", "Business console", "/merchant", "voucher:verify,reward:create_pending,promotion:create_pending"),
                    ("admin", "Admin console", "/admin", "campaign:review,result:publish,outreach:manage,promotion:publish"),
                ],
            )
            cursor.execute("SELECT COUNT(*) AS total FROM gp_matches")
            if cursor.fetchone()["total"] == 0:
                cursor.execute(
                    """
                    INSERT INTO gp_matches
                    (group_name, phase, match_date, kickoff_utc, match_timezone, city, venue, home_team, away_team, home_code, away_code, home_score, away_score, status)
                    VALUES
                    ('Grupo A', 'Grupos', '2026-06-11 13:00:00', '2026-06-11T19:00:00Z', 'America/Mexico_City', 'Mexico City', 'Mexico City Stadium', 'Mexico', 'South Africa', 'MEX', 'RSA', NULL, NULL, 'scheduled'),
                    ('Grupo A', 'Grupos', '2026-06-11 20:00:00', '2026-06-12T02:00:00Z', 'America/Mexico_City', 'Guadalajara', 'Guadalajara Stadium', 'Korea Republic', 'Czechia', 'KOR', 'CZE', NULL, NULL, 'scheduled'),
                    ('Grupo B', 'Grupos', '2026-06-12 15:00:00', '2026-06-12T19:00:00Z', 'America/Toronto', 'Toronto', 'Toronto Stadium', 'Canada', 'Bosnia & Herzegovina', 'CAN', 'BIH', NULL, NULL, 'scheduled'),
                    ('Grupo D', 'Grupos', '2026-06-12 18:00:00', '2026-06-13T01:00:00Z', 'America/Los_Angeles', 'Los Angeles', 'Los Angeles Stadium', 'United States', 'Paraguay', 'USA', 'PAR', NULL, NULL, 'scheduled'),
                    ('Grupo C', 'Grupos', '2026-06-13 18:00:00', '2026-06-13T22:00:00Z', 'America/New_York', 'New York New Jersey', 'New York New Jersey Stadium', 'Brazil', 'Morocco', 'BRA', 'MAR', NULL, NULL, 'scheduled'),
                    ('Grupo J', 'Grupos', '2026-06-16 21:00:00', '2026-06-17T01:00:00Z', 'America/New_York', 'Miami', 'Miami Stadium', 'Argentina', 'Algeria', 'ARG', 'ALG', NULL, NULL, 'scheduled')
                    """
                )
            cursor.execute(
                """
                UPDATE gp_matches
                SET group_name = CASE id
                    WHEN 1 THEN 'Grupo A'
                    WHEN 2 THEN 'Grupo A'
                    WHEN 3 THEN 'Grupo B'
                    WHEN 4 THEN 'Grupo D'
                    WHEN 5 THEN 'Grupo C'
                    WHEN 6 THEN 'Grupo J'
                    ELSE group_name
                END,
                match_date = CASE id
                    WHEN 1 THEN '2026-06-11 13:00:00'
                    WHEN 2 THEN '2026-06-11 20:00:00'
                    WHEN 3 THEN '2026-06-12 15:00:00'
                    WHEN 4 THEN '2026-06-12 18:00:00'
                    WHEN 5 THEN '2026-06-13 18:00:00'
                    WHEN 6 THEN '2026-06-16 21:00:00'
                    ELSE match_date
                END,
                kickoff_utc = CASE id
                    WHEN 1 THEN '2026-06-11T19:00:00Z'
                    WHEN 2 THEN '2026-06-12T02:00:00Z'
                    WHEN 3 THEN '2026-06-12T19:00:00Z'
                    WHEN 4 THEN '2026-06-13T01:00:00Z'
                    WHEN 5 THEN '2026-06-13T22:00:00Z'
                    WHEN 6 THEN '2026-06-17T01:00:00Z'
                    ELSE kickoff_utc
                END,
                match_timezone = CASE id
                    WHEN 1 THEN 'America/Mexico_City'
                    WHEN 2 THEN 'America/Mexico_City'
                    WHEN 3 THEN 'America/Toronto'
                    WHEN 4 THEN 'America/Los_Angeles'
                    WHEN 5 THEN 'America/New_York'
                    WHEN 6 THEN 'America/New_York'
                    ELSE COALESCE(match_timezone, 'UTC')
                END,
                city = CASE id
                    WHEN 1 THEN 'Mexico City'
                    WHEN 2 THEN 'Guadalajara'
                    WHEN 3 THEN 'Toronto'
                    WHEN 4 THEN 'Los Angeles'
                    WHEN 5 THEN 'New York New Jersey'
                    WHEN 6 THEN 'Miami'
                    ELSE city
                END,
                venue = CASE id
                    WHEN 1 THEN 'Mexico City Stadium'
                    WHEN 2 THEN 'Guadalajara Stadium'
                    WHEN 3 THEN 'Toronto Stadium'
                    WHEN 4 THEN 'Los Angeles Stadium'
                    WHEN 5 THEN 'New York New Jersey Stadium'
                    WHEN 6 THEN 'Miami Stadium'
                    ELSE venue
                END,
                home_team = CASE id
                    WHEN 1 THEN 'Mexico'
                    WHEN 2 THEN 'Korea Republic'
                    WHEN 3 THEN 'Canada'
                    WHEN 4 THEN 'United States'
                    WHEN 5 THEN 'Brazil'
                    WHEN 6 THEN 'Argentina'
                    ELSE home_team
                END,
                away_team = CASE id
                    WHEN 1 THEN 'South Africa'
                    WHEN 2 THEN 'Czechia'
                    WHEN 3 THEN 'Bosnia & Herzegovina'
                    WHEN 4 THEN 'Paraguay'
                    WHEN 5 THEN 'Morocco'
                    WHEN 6 THEN 'Algeria'
                    ELSE away_team
                END,
                home_code = CASE id
                    WHEN 1 THEN 'MEX'
                    WHEN 2 THEN 'KOR'
                    WHEN 3 THEN 'CAN'
                    WHEN 4 THEN 'USA'
                    WHEN 5 THEN 'BRA'
                    WHEN 6 THEN 'ARG'
                    ELSE home_code
                END,
                away_code = CASE id
                    WHEN 1 THEN 'RSA'
                    WHEN 2 THEN 'CZE'
                    WHEN 3 THEN 'BIH'
                    WHEN 4 THEN 'PAR'
                    WHEN 5 THEN 'MAR'
                    WHEN 6 THEN 'ALG'
                    ELSE away_code
                END,
                home_score = NULL,
                away_score = NULL,
                first_half_home_score = NULL,
                first_half_away_score = NULL,
                status = 'scheduled'
                WHERE id BETWEEN 1 AND 6
                """
            )
            cursor.execute(
                """
                INSERT IGNORE INTO gp_matches
                (id, group_name, phase, match_date, kickoff_utc, match_timezone, city, venue, home_team, away_team, home_code, away_code, status)
                VALUES
                (5, 'Grupo C', 'Grupos', '2026-06-13 18:00:00', '2026-06-13T22:00:00Z', 'America/New_York', 'New York New Jersey', 'New York New Jersey Stadium', 'Brazil', 'Morocco', 'BRA', 'MAR', 'scheduled'),
                (6, 'Grupo J', 'Grupos', '2026-06-16 21:00:00', '2026-06-17T01:00:00Z', 'America/New_York', 'Miami', 'Miami Stadium', 'Argentina', 'Algeria', 'ARG', 'ALG', 'scheduled')
                """
            )
            cursor.execute(
                """
                UPDATE gp_matches
                SET kickoff_utc = CASE id
                    WHEN 1 THEN '2026-06-12T01:00:00Z'
                    WHEN 2 THEN '2026-06-12T04:00:00Z'
                    WHEN 3 THEN '2026-06-13T00:00:00Z'
                    WHEN 4 THEN '2026-06-14T01:00:00Z'
                    ELSE kickoff_utc
                END,
                match_timezone = CASE id
                    WHEN 1 THEN 'America/Mexico_City'
                    WHEN 2 THEN 'America/Mexico_City'
                    WHEN 3 THEN 'America/Toronto'
                    WHEN 4 THEN 'America/New_York'
                    ELSE COALESCE(match_timezone, 'UTC')
                END
                WHERE kickoff_utc IS NULL OR match_timezone IS NULL
                """
            )
            cursor.executemany(
                """
                INSERT INTO gp_matches
                (id, group_name, phase, match_date, kickoff_utc, match_timezone, city, venue,
                 home_team, away_team, home_code, away_code, home_score, away_score, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,NULL,'scheduled')
                ON DUPLICATE KEY UPDATE
                group_name=VALUES(group_name),
                phase=VALUES(phase),
                match_date=VALUES(match_date),
                kickoff_utc=VALUES(kickoff_utc),
                match_timezone=VALUES(match_timezone),
                city=VALUES(city),
                venue=VALUES(venue),
                home_team=VALUES(home_team),
                away_team=VALUES(away_team),
                home_code=VALUES(home_code),
                away_code=VALUES(away_code)
                """,
                FULL_FIXTURE,
            )
            cursor.execute("SELECT COUNT(*) AS total FROM gp_merchants")
            if cursor.fetchone()["total"] == 0:
                cursor.execute(
                    """
                    INSERT INTO gp_merchants (name, zone, address, link, image_url)
                    VALUES
                    ('Boliche La Final', 'Tlalpan / Azteca', 'Calz. de Tlalpan 3465, Santa Ursula Coapa', 'https://maps.google.com/?q=Estadio+Azteca', '/world-cup-abstract-bg.png'),
                    ('Terraza Gol Norte', 'Guadalajara', 'Av. Circuito JVC 2800, Zapopan', 'https://maps.google.com/?q=Estadio+Akron', '/world-cup-abstract-bg.png')
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO gp_merchant_rewards (merchant_id, title, prize, rule, quantity, expires_at, image_url)
                    VALUES
                    (1, 'Exact score night', '2x1 en entrada antes de medianoche', 'exact', 80, '12 Jun 23:59', '/world-cup-abstract-bg.png'),
                    (1, 'Winner pick promo', 'Shot de bienvenida para mesa mundialista', 'winner', 120, '30 Jun 23:59', '/world-cup-abstract-bg.png'),
                    (2, 'Promo por participar', 'Bucket 3x2 para mesa mundialista', 'participate', 200, '30 Jun 23:59', '/world-cup-abstract-bg.png')
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO gp_merchant_promotions (merchant_id, title, description, image_url, link, expires_at)
                    VALUES
                    (1, 'Happy hour mundialista', '10% off mostrando la app en barra.', '/world-cup-abstract-bg.png', 'https://maps.google.com/?q=Estadio+Azteca', 'Durante partidos')
                    """
                )
        connection.commit()


def side(home, away):
    if home == away:
        return "draw"
    return "home" if home > away else "away"


def qualifies(prediction, match, reward):
    if match["home_score"] is None or match["away_score"] is None:
        return False
    rule = reward["rule"]
    if rule == "participate":
        return True
    if rule == "exact":
        return prediction["home_score"] == match["home_score"] and prediction["away_score"] == match["away_score"]
    if rule == "winner":
        return side(prediction["home_score"], prediction["away_score"]) == side(match["home_score"], match["away_score"])
    if rule == "goal_diff":
        return prediction["home_score"] - prediction["away_score"] == match["home_score"] - match["away_score"]
    if rule == "home_goals":
        return prediction["home_score"] == match["home_score"]
    if rule == "away_goals":
        return prediction["away_score"] == match["away_score"]
    if rule == "first_half_goals":
        required = [
            prediction.get("first_half_home_score"),
            prediction.get("first_half_away_score"),
            match.get("first_half_home_score"),
            match.get("first_half_away_score"),
        ]
        if any(value is None for value in required):
            return False
        return (
            prediction["first_half_home_score"] + prediction["first_half_away_score"]
            == match["first_half_home_score"] + match["first_half_away_score"]
        )
    return False


def group_letter(group_name):
    if not group_name:
        return None
    return str(group_name).strip().split()[-1].upper()


def build_standings(connection):
    standings = {}
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM gp_matches
            WHERE phase='Grupos'
            ORDER BY group_name, match_date, id
            """
        )
        matches = cursor.fetchall()
    for match in matches:
        letter = group_letter(match.get("group_name"))
        if not letter:
            continue
        table = standings.setdefault(letter, {})
        for side_name, code_key, score_key, against_key in [
            ("home_team", "home_code", "home_score", "away_score"),
            ("away_team", "away_code", "away_score", "home_score"),
        ]:
            code = match.get(code_key)
            table.setdefault(
                code,
                {
                    "team": match.get(side_name),
                    "code": code,
                    "played": 0,
                    "points": 0,
                    "gd": 0,
                    "gf": 0,
                },
            )
            if match.get("status") != "final" or match.get(score_key) is None or match.get(against_key) is None:
                continue
            item = table[code]
            item["played"] += 1
            item["gf"] += match[score_key]
            item["gd"] += match[score_key] - match[against_key]
            if match[score_key] > match[against_key]:
                item["points"] += 3
            elif match[score_key] == match[against_key]:
                item["points"] += 1
    resolved = {}
    for letter, table in standings.items():
        ordered = sorted(table.values(), key=lambda item: (item["points"], item["gd"], item["gf"]), reverse=True)
        if len(ordered) >= 2 and all(item["played"] >= 3 for item in ordered[:4]):
            resolved[f"1{letter}"] = ordered[0]
            resolved[f"2{letter}"] = ordered[1]
    return standings, resolved


def resolve_knockout_slots(connection):
    _standings, resolved = build_standings(connection)
    if not resolved:
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT id, home_team, away_team, home_code, away_code
            FROM gp_matches
            WHERE phase <> 'Grupos'
            """
        )
        matches = cursor.fetchall()
        for match in matches:
            home = resolved.get(match["home_code"]) or resolved.get(match["home_team"])
            away = resolved.get(match["away_code"]) or resolved.get(match["away_team"])
            if not home and not away:
                continue
            cursor.execute(
                """
                UPDATE gp_matches
                SET home_team=COALESCE(%s, home_team),
                    home_code=COALESCE(%s, home_code),
                    away_team=COALESCE(%s, away_team),
                    away_code=COALESCE(%s, away_code)
                WHERE id=%s
                """,
                (
                    home["team"] if home else None,
                    home["code"] if home else None,
                    away["team"] if away else None,
                    away["code"] if away else None,
                    match["id"],
                ),
            )


def issue_vouchers(connection, match_id=None, prediction_id=None):
    with connection.cursor() as cursor:
        where = []
        params = []
        if match_id:
            where.append("p.match_id = %s")
            params.append(match_id)
        if prediction_id:
            where.append("p.id = %s")
            params.append(prediction_id)
        clause = "WHERE " + " AND ".join(where) if where else ""
        cursor.execute(
            f"""
            SELECT p.*, f.handle
            FROM gp_predictions p
            JOIN gp_fans f ON f.id = p.fan_id
            {clause}
            """,
            params,
        )
        predictions = cursor.fetchall()
        cursor.execute("SELECT * FROM gp_merchant_rewards WHERE active = 1 AND review_status = 'approved'")
        rewards = cursor.fetchall()
        for prediction in predictions:
            cursor.execute("SELECT * FROM gp_matches WHERE id = %s", (prediction["match_id"],))
            match = cursor.fetchone()
            if not match:
                continue
            for reward in rewards:
                if not qualifies(prediction, match, reward):
                    continue
                code = f"{reward['id']}-{prediction['handle']}-{prediction['id']}"
                cursor.execute(
                    """
                    INSERT IGNORE INTO gp_vouchers
                    (code, fan_id, match_id, prediction_id, merchant_id, reward_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (code, prediction["fan_id"], prediction["match_id"], prediction["id"], reward["merchant_id"], reward["id"]),
                )
                if cursor.rowcount:
                    mirror_safe(
                        "vouchers",
                        {
                            "code": code,
                            "fan_id": prediction["fan_id"],
                            "fan_handle": prediction["handle"],
                            "match_id": prediction["match_id"],
                            "prediction_id": prediction["id"],
                            "merchant_id": reward["merchant_id"],
                            "reward_id": reward["id"],
                            "reward_title": reward.get("title"),
                            "reward_rule": reward.get("rule"),
                            "status": "valid",
                        },
                        ["code"],
                    )


@mvp_api.before_app_request
def init_schema_once():
    if not getattr(mvp_api, "_schema_ready", False):
        ensure_schema()
        mvp_api._schema_ready = True


@mvp_api.route("/matches", methods=["GET"])
def list_matches():
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM gp_matches ORDER BY match_date ASC, id ASC")
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/matches/<int:match_id>/result", methods=["PUT"])
def update_match_result(match_id):
    payload = request.get_json(force=True) or {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE gp_matches
                SET home_score=%s, away_score=%s, first_half_home_score=%s, first_half_away_score=%s, status='final'
                WHERE id=%s
                """,
                (
                    payload.get("home_score"),
                    payload.get("away_score"),
                    payload.get("first_half_home_score"),
                    payload.get("first_half_away_score"),
                    match_id,
                ),
            )
            issue_vouchers(connection, match_id=match_id)
            resolve_knockout_slots(connection)
        connection.commit()
    return jsonify({"status": "ok"})


@mvp_api.route("/standings", methods=["GET"])
def standings():
    with get_connection() as connection:
        raw, resolved = build_standings(connection)
    return jsonify({"groups": raw, "resolved_slots": resolved})


@mvp_api.route("/knockout/resolve", methods=["POST"])
def resolve_knockout():
    with get_connection() as connection:
        resolve_knockout_slots(connection)
        connection.commit()
    return jsonify({"status": "ok"})


@mvp_api.route("/fans", methods=["POST"])
def create_fan():
    payload = request.get_json(force=True) or {}
    name = payload.get("name") or "Fan"
    handle = payload.get("handle") or f"{name[:3].upper()}-{int(datetime.utcnow().timestamp()) % 1000}"
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO gp_fans (name, handle, contact, channel, device_id)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (name, handle, payload.get("contact") or "", payload.get("channel") or "whatsapp", payload.get("device_id")),
            )
            fan_id = cursor.lastrowid
            cursor.execute("SELECT * FROM gp_fans WHERE id=%s", (fan_id,))
            fan = cursor.fetchone()
        connection.commit()
    return jsonify(one_to_json(fan)), 201


@mvp_api.route("/auth/register", methods=["POST"])
def auth_register():
    payload = request.get_json(force=True) or {}
    role = payload.get("role") or "fan"
    if role not in {"fan", "merchant", "admin"}:
        return jsonify({"error": "invalid role"}), 400
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    if not email or len(password) < 6:
        return jsonify({"error": "email and password with 6+ chars are required"}), 400

    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT id FROM gp_accounts WHERE email=%s", (email,))
            if cursor.fetchone():
                return jsonify({"error": "account already exists", "next": "login"}), 409
            merchant_id = payload.get("merchant_id")
            if role == "merchant" and not merchant_id and payload.get("merchant_name"):
                cursor.execute(
                    """
                    INSERT INTO gp_merchants
                    (name, city, zone, address, link, instagram_url, facebook_url, tiktok_url, whatsapp_url, image_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        payload.get("merchant_name"),
                        payload.get("city"),
                        payload.get("zone"),
                        payload.get("address"),
                        payload.get("link"),
                        payload.get("instagram_url"),
                        payload.get("facebook_url"),
                        payload.get("tiktok_url"),
                        payload.get("whatsapp_url"),
                        payload.get("image_url"),
                    ),
                )
                merchant_id = cursor.lastrowid
            cursor.execute(
                """
                INSERT INTO gp_accounts (role, name, email, password_hash, city, merchant_id, account_status)
                VALUES (%s,%s,%s,%s,%s,%s,'active')
                """,
                (role, payload.get("name") or email, email, password_hash, payload.get("city"), merchant_id),
            )
            account_id = cursor.lastrowid
            cursor.execute("SELECT * FROM gp_accounts WHERE id=%s", (account_id,))
            account = cursor.fetchone()
        connection.commit()
    mirror_safe("accounts", public_account(account), ["id"])
    return jsonify(public_account(account)), 201


@mvp_api.route("/auth/login", methods=["POST"])
def auth_login():
    payload = request.get_json(force=True) or {}
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM gp_accounts WHERE email=%s", (email,))
            account = cursor.fetchone()
    if not account or not bcrypt.checkpw(password.encode("utf-8"), account["password_hash"].encode("utf-8")):
        return jsonify({"error": "invalid credentials"}), 401
    if account.get("account_status") and account["account_status"] != "active":
        return jsonify({"error": "account inactive"}), 403
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("UPDATE gp_accounts SET last_login_at=NOW() WHERE id=%s", (account["id"],))
        connection.commit()
    return jsonify(public_account(account))


@mvp_api.route("/auth/roles", methods=["GET"])
def auth_roles():
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM gp_role_access ORDER BY FIELD(role_name, 'fan', 'merchant', 'admin')")
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/auth/oauth/status", methods=["GET"])
def oauth_status():
    return jsonify(
        {
            "google": bool(os.getenv("GOOGLE_OAUTH_CLIENT_ID") and os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")),
            "facebook": bool(
                (os.getenv("FACEBOOK_OAUTH_CLIENT_ID") or os.getenv("FACEBOOK_APP_ID"))
                and (os.getenv("FACEBOOK_OAUTH_CLIENT_SECRET") or os.getenv("FACEBOOK_APP_SECRET"))
            ),
        }
    )


@mvp_api.route("/auth/oauth/<provider>/start", methods=["GET"])
def oauth_start(provider):
    provider = provider.lower()
    if provider == "google":
        client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or f"{request.url_root.rstrip('/')}/api/auth/oauth/google/callback"
        if not client_id:
            return redirect(f"{FRONTEND_URL}/fan?auth_error=google_oauth_not_configured")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "access_type": "offline",
            "prompt": "select_account",
            "state": uuid.uuid4().hex,
        }
        return redirect(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")
    if provider == "facebook":
        client_id = os.getenv("FACEBOOK_OAUTH_CLIENT_ID") or os.getenv("FACEBOOK_APP_ID")
        redirect_uri = os.getenv("FACEBOOK_OAUTH_REDIRECT_URI") or f"{request.url_root.rstrip('/')}/api/auth/oauth/facebook/callback"
        if not client_id:
            return redirect(f"{FRONTEND_URL}/fan?auth_error=facebook_oauth_not_configured")
        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "public_profile",
            "state": uuid.uuid4().hex,
        }
        return redirect(f"https://www.facebook.com/v20.0/dialog/oauth?{urlencode(params)}")
    return jsonify({"error": "unsupported oauth provider"}), 404


@mvp_api.route("/auth/oauth/google/callback", methods=["GET"])
def google_oauth_callback():
    code = request.args.get("code")
    if not code:
        return redirect(f"{FRONTEND_URL}/fan?auth_error=google_no_code")
    redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI") or f"{request.url_root.rstrip('/')}/api/auth/oauth/google/callback"
    try:
        token = api_post_json(
            "https://oauth2.googleapis.com/token",
            {
                "code": code,
                "client_id": os.getenv("GOOGLE_OAUTH_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_OAUTH_CLIENT_SECRET"),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        profile = api_get_json("https://openidconnect.googleapis.com/v1/userinfo", token.get("access_token"))
        email = (profile.get("email") or "").strip().lower()
        if not email:
            return redirect(f"{FRONTEND_URL}/fan?auth_error=google_email_missing")
        account = find_or_create_oauth_fan(email, profile.get("name") or email)
        return oauth_callback_html(account)
    except Exception:
        return redirect(f"{FRONTEND_URL}/fan?auth_error=google_oauth_failed")


@mvp_api.route("/auth/oauth/facebook/callback", methods=["GET"])
def facebook_oauth_callback():
    code = request.args.get("code")
    if not code:
        return redirect(f"{FRONTEND_URL}/fan?auth_error=facebook_no_code")
    redirect_uri = os.getenv("FACEBOOK_OAUTH_REDIRECT_URI") or f"{request.url_root.rstrip('/')}/api/auth/oauth/facebook/callback"
    client_id = os.getenv("FACEBOOK_OAUTH_CLIENT_ID") or os.getenv("FACEBOOK_APP_ID")
    client_secret = os.getenv("FACEBOOK_OAUTH_CLIENT_SECRET") or os.getenv("FACEBOOK_APP_SECRET")
    try:
        token = api_get_json(
            f"https://graph.facebook.com/v20.0/oauth/access_token?{urlencode({'client_id': client_id, 'client_secret': client_secret, 'redirect_uri': redirect_uri, 'code': code})}"
        )
        profile = api_get_json(
            f"https://graph.facebook.com/me?{urlencode({'fields': 'id,name', 'access_token': token.get('access_token')})}"
        )
        facebook_id = str(profile.get("id") or "").strip()
        email = (profile.get("email") or "").strip().lower()
        if not email and facebook_id:
            email = f"facebook-{facebook_id}@goalpromo.local"
        if not email:
            return redirect(f"{FRONTEND_URL}/fan?auth_error=facebook_email_missing")
        account = find_or_create_oauth_fan(email, profile.get("name") or email)
        return oauth_callback_html(account)
    except Exception:
        return redirect(f"{FRONTEND_URL}/fan?auth_error=facebook_oauth_failed")


@mvp_api.route("/outreach/leads", methods=["GET", "POST"])
def outreach_leads():
    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        token = uuid.uuid4().hex[:16]
        frontend_url = (payload.get("frontend_url") or "https://betmundial.vercel.app").rstrip("/")
        invite_url = f"{frontend_url}/?invite={token}&role=merchant"
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO gp_outreach_leads
                    (city, source_app, business_name, category, contact_name, contact_channel, contact_value, invite_token, invite_url, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        payload.get("city") or "Global",
                        payload.get("source_app"),
                        payload.get("business_name") or "Local aliado",
                        payload.get("category"),
                        payload.get("contact_name"),
                        payload.get("contact_channel") or "manual",
                        payload.get("contact_value"),
                        token,
                        invite_url,
                        payload.get("notes"),
                    ),
                )
                lead_id = cursor.lastrowid
                cursor.execute("SELECT * FROM gp_outreach_leads WHERE id=%s", (lead_id,))
                lead = cursor.fetchone()
            connection.commit()
        mirror_safe("outreach_leads", one_to_json(lead), ["id"])
        return jsonify(one_to_json(lead)), 201

    city = request.args.get("city")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            if city:
                cursor.execute(
                    "SELECT * FROM gp_outreach_leads WHERE city LIKE %s ORDER BY id DESC LIMIT 200",
                    (f"%{city}%",),
                )
            else:
                cursor.execute("SELECT * FROM gp_outreach_leads ORDER BY id DESC LIMIT 200")
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/outreach/leads/bulk", methods=["POST"])
def outreach_leads_bulk():
    payload = request.get_json(force=True) or {}
    leads = payload.get("leads") or []
    frontend_url = (payload.get("frontend_url") or "https://betmundial.vercel.app").rstrip("/")
    created = []
    with get_connection() as connection:
        with connection.cursor() as cursor:
            for lead in leads[:200]:
                token = uuid.uuid4().hex[:16]
                invite_url = f"{frontend_url}/?invite={token}&role=merchant"
                cursor.execute(
                    """
                    INSERT INTO gp_outreach_leads
                    (city, source_app, business_name, category, contact_name, contact_channel, contact_value, invite_token, invite_url, notes)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        lead.get("city") or payload.get("city") or "Global",
                        lead.get("source_app") or payload.get("source_app") or "Google Maps",
                        lead.get("business_name") or "Local aliado",
                        lead.get("category"),
                        lead.get("contact_name"),
                        lead.get("contact_channel") or "manual",
                        lead.get("contact_value"),
                        token,
                        invite_url,
                        lead.get("notes"),
                    ),
                )
                created.append(cursor.lastrowid)
            if created:
                placeholders = ",".join(["%s"] * len(created))
                cursor.execute(f"SELECT * FROM gp_outreach_leads WHERE id IN ({placeholders}) ORDER BY id DESC", created)
                rows = cursor.fetchall()
            else:
                rows = []
        connection.commit()
    for row in rows:
        mirror_safe("outreach_leads", one_to_json(row), ["id"])
    return jsonify(rows_to_json(rows)), 201


@mvp_api.route("/outreach/leads/<int:lead_id>/sent", methods=["POST"])
def mark_outreach_sent(lead_id):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE gp_outreach_leads SET status='invited', sent_at=NOW() WHERE id=%s",
                (lead_id,),
            )
            cursor.execute("SELECT * FROM gp_outreach_leads WHERE id=%s", (lead_id,))
            lead = cursor.fetchone()
        connection.commit()
    if not lead:
        return jsonify({"error": "lead not found"}), 404
    mirror_safe("outreach_leads", one_to_json(lead), ["id"])
    return jsonify(one_to_json(lead))


@mvp_api.route("/fans", methods=["GET"])
def list_fans():
    device_id = request.args.get("device_id")
    with get_connection() as connection:
        with connection.cursor() as cursor:
            if device_id:
                cursor.execute("SELECT * FROM gp_fans WHERE device_id=%s ORDER BY id DESC", (device_id,))
            else:
                cursor.execute("SELECT * FROM gp_fans ORDER BY id DESC LIMIT 50")
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/predictions", methods=["POST"])
def create_prediction():
    payload = request.get_json(force=True) or {}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM gp_matches WHERE id=%s", (payload["match_id"],))
            match = cursor.fetchone()
            if kickoff_has_started(match):
                return jsonify({"error": "predictions are closed for this match"}), 409
            cursor.execute(
                """
                INSERT INTO gp_predictions
                (fan_id, match_id, home_score, away_score, first_half_home_score, first_half_away_score)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                home_score=VALUES(home_score),
                away_score=VALUES(away_score),
                first_half_home_score=VALUES(first_half_home_score),
                first_half_away_score=VALUES(first_half_away_score)
                """,
                (
                    payload["fan_id"],
                    payload["match_id"],
                    payload["home_score"],
                    payload["away_score"],
                    payload.get("first_half_home_score"),
                    payload.get("first_half_away_score"),
                ),
            )
            cursor.execute(
                "SELECT * FROM gp_predictions WHERE fan_id=%s AND match_id=%s",
                (payload["fan_id"], payload["match_id"]),
            )
            prediction = cursor.fetchone()
            issue_vouchers(connection, prediction_id=prediction["id"])
        connection.commit()
    return jsonify(one_to_json(prediction)), 201


@mvp_api.route("/fans/<int:fan_id>/predictions", methods=["GET"])
def fan_predictions(fan_id):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.*, m.home_team, m.away_team, m.home_code, m.away_code, m.group_name, m.match_date
                FROM gp_predictions p
                JOIN gp_matches m ON m.id = p.match_id
                WHERE p.fan_id=%s
                ORDER BY p.created_at DESC
                """,
                (fan_id,),
            )
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/fans/<int:fan_id>/vouchers", methods=["GET"])
def fan_vouchers(fan_id):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.*, mr.title, mr.prize, mr.rule, mr.expires_at, mr.image_url,
                       me.name AS merchant_name, me.zone, me.link,
                       m.home_team, m.away_team, m.home_code, m.away_code
                FROM gp_vouchers v
                JOIN gp_merchant_rewards mr ON mr.id = v.reward_id
                JOIN gp_merchants me ON me.id = v.merchant_id
                JOIN gp_matches m ON m.id = v.match_id
                WHERE v.fan_id=%s
                ORDER BY v.issued_at DESC
                """,
                (fan_id,),
            )
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/uploads/images", methods=["POST"])
def upload_promotion_image():
    image = request.files.get("image")
    if not image:
        return jsonify({"error": "image file is required"}), 400
    folder = request.form.get("folder") or "promotions"
    try:
        result = upload_image(image, folder=folder)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if result.get("storage") == "r2" and result.get("key"):
        result["url"] = f"{request.url_root.rstrip('/')}/api/uploads/files/{result['key']}"
    mirror_safe("uploads", result, ["key"])
    return jsonify(result), 201


@mvp_api.route("/uploads/files/<path:key>", methods=["GET"])
def uploaded_file(key):
    stored = read_image(key)
    if not stored:
        return jsonify({"error": "file not found"}), 404
    return send_file(stored["body"], mimetype=stored["content_type"])


@mvp_api.route("/merchants", methods=["GET", "POST"])
def merchants():
    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO gp_merchants
                    (name, city, zone, address, link, instagram_url, facebook_url, tiktok_url, whatsapp_url, image_url)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        payload.get("name"),
                        payload.get("city"),
                        payload.get("zone"),
                        payload.get("address"),
                        payload.get("link"),
                        payload.get("instagram_url"),
                        payload.get("facebook_url"),
                        payload.get("tiktok_url"),
                        payload.get("whatsapp_url"),
                        payload.get("image_url"),
                    ),
                )
                merchant_id = cursor.lastrowid
                cursor.execute("SELECT * FROM gp_merchants WHERE id=%s", (merchant_id,))
                merchant = cursor.fetchone()
            connection.commit()
        mirror_safe("merchants", one_to_json(merchant), ["id"])
        return jsonify(one_to_json(merchant)), 201
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM gp_merchants ORDER BY id DESC")
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/merchant-rewards", methods=["GET", "POST"])
def merchant_rewards():
    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        review_status = payload.get("review_status") or "pending_review"
        if review_status not in {"pending_review", "approved", "rejected", "paused"}:
            return jsonify({"error": "invalid review status"}), 400
        active = 1 if review_status == "approved" else 0
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO gp_merchant_rewards
                    (merchant_id, title, prize, rule, quantity, city, expires_at, image_url, active, review_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        payload["merchant_id"],
                        payload.get("title"),
                        payload.get("prize"),
                        payload.get("rule"),
                        payload.get("quantity") or 0,
                        payload.get("city"),
                        payload.get("expires_at"),
                        payload.get("image_url"),
                        active,
                        review_status,
                    ),
                )
                reward_id = cursor.lastrowid
                cursor.execute(
                    """
                    SELECT r.*, COALESCE(r.city, m.city) AS campaign_city,
                           m.name AS merchant_name, m.city AS merchant_city, m.zone, m.link,
                           m.instagram_url, m.facebook_url, m.tiktok_url, m.whatsapp_url, m.address
                    FROM gp_merchant_rewards r
                    JOIN gp_merchants m ON m.id = r.merchant_id
                    WHERE r.id=%s
                    """,
                    (reward_id,),
                )
                reward = cursor.fetchone()
            connection.commit()
        mirror_safe("merchant_rewards", one_to_json(reward), ["id"])
        return jsonify(one_to_json(reward)), 201
    include_all = request.args.get("include_all") in {"1", "true", "yes"}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            status_clause = "" if include_all else "WHERE r.active=1 AND r.review_status='approved'"
            cursor.execute(
                f"""
                SELECT r.*, COALESCE(r.city, m.city) AS campaign_city,
                       m.name AS merchant_name, m.city AS merchant_city, m.zone, m.link,
                       m.instagram_url, m.facebook_url, m.tiktok_url, m.whatsapp_url, m.address
                FROM gp_merchant_rewards r
                JOIN gp_merchants m ON m.id = r.merchant_id
                {status_clause}
                ORDER BY r.id DESC
                """
            )
            return jsonify(rows_to_json(cursor.fetchall()))


@mvp_api.route("/merchant-promotions", methods=["GET", "POST"])
def merchant_promotions():
    if request.method == "POST":
        payload = request.get_json(force=True) or {}
        review_status = payload.get("review_status") or "pending_review"
        if review_status not in {"pending_review", "approved", "rejected", "paused"}:
            return jsonify({"error": "invalid review status"}), 400
        active = 1 if review_status == "approved" else 0
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO gp_merchant_promotions
                    (merchant_id, title, description, image_url, city, address, link, instagram_url, facebook_url, tiktok_url, whatsapp_url, expires_at, active, review_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        payload["merchant_id"],
                        payload.get("title"),
                        payload.get("description"),
                        payload.get("image_url"),
                        payload.get("city"),
                        payload.get("address"),
                        payload.get("link"),
                        payload.get("instagram_url"),
                        payload.get("facebook_url"),
                        payload.get("tiktok_url"),
                        payload.get("whatsapp_url"),
                        payload.get("expires_at"),
                        active,
                        review_status,
                    ),
                )
                promotion_id = cursor.lastrowid
                cursor.execute(
                    """
                    SELECT p.*, COALESCE(p.city, m.city) AS campaign_city,
                           COALESCE(p.address, m.address) AS campaign_address,
                           m.name AS merchant_name, m.city AS merchant_city, m.zone,
                           COALESCE(p.instagram_url, m.instagram_url) AS campaign_instagram_url,
                           COALESCE(p.facebook_url, m.facebook_url) AS campaign_facebook_url,
                           COALESCE(p.tiktok_url, m.tiktok_url) AS campaign_tiktok_url,
                           COALESCE(p.whatsapp_url, m.whatsapp_url) AS campaign_whatsapp_url
                    FROM gp_merchant_promotions p
                    JOIN gp_merchants m ON m.id = p.merchant_id
                    WHERE p.id=%s
                    """,
                    (promotion_id,),
                )
                promotion = cursor.fetchone()
            connection.commit()
        mirror_safe("merchant_promotions", one_to_json(promotion), ["id"])
        return jsonify(one_to_json(promotion)), 201
    include_all = request.args.get("include_all") in {"1", "true", "yes"}
    with get_connection() as connection:
        with connection.cursor() as cursor:
            status_clause = "" if include_all else "WHERE p.active=1 AND p.review_status='approved'"
            cursor.execute(
                f"""
                SELECT p.*, COALESCE(p.city, m.city) AS campaign_city,
                       COALESCE(p.address, m.address) AS campaign_address,
                       m.name AS merchant_name, m.city AS merchant_city, m.zone,
                       COALESCE(p.instagram_url, m.instagram_url) AS campaign_instagram_url,
                       COALESCE(p.facebook_url, m.facebook_url) AS campaign_facebook_url,
                       COALESCE(p.tiktok_url, m.tiktok_url) AS campaign_tiktok_url,
                       COALESCE(p.whatsapp_url, m.whatsapp_url) AS campaign_whatsapp_url
                FROM gp_merchant_promotions p
                JOIN gp_merchants m ON m.id = p.merchant_id
                {status_clause}
                ORDER BY p.id DESC
                """
            )
            return jsonify(rows_to_json(cursor.fetchall()))


def review_campaign(table, campaign_id, collection):
    payload = request.get_json(force=True) or {}
    review_status = payload.get("review_status") or payload.get("status")
    if review_status not in {"pending_review", "approved", "rejected", "paused"}:
        return jsonify({"error": "invalid review status"}), 400
    active = 1 if review_status == "approved" else 0
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {table} SET review_status=%s, active=%s WHERE id=%s",
                (review_status, active, campaign_id),
            )
            cursor.execute(f"SELECT * FROM {table} WHERE id=%s", (campaign_id,))
            campaign = cursor.fetchone()
        connection.commit()
    if not campaign:
        return jsonify({"error": "campaign not found"}), 404
    mirror_safe(collection, one_to_json(campaign), ["id"])
    return jsonify(one_to_json(campaign))


@mvp_api.route("/merchant-rewards/<int:reward_id>/review", methods=["POST"])
def review_merchant_reward(reward_id):
    return review_campaign("gp_merchant_rewards", reward_id, "merchant_rewards")


@mvp_api.route("/merchant-promotions/<int:promotion_id>/review", methods=["POST"])
def review_merchant_promotion(promotion_id):
    return review_campaign("gp_merchant_promotions", promotion_id, "merchant_promotions")


@mvp_api.route("/vouchers/<code>", methods=["GET"])
def verify_voucher(code):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.*, f.name AS fan_name, f.handle, mr.prize, mr.title, me.name AS merchant_name,
                       m.home_team, m.away_team, m.home_code, m.away_code
                FROM gp_vouchers v
                JOIN gp_fans f ON f.id = v.fan_id
                JOIN gp_merchant_rewards mr ON mr.id = v.reward_id
                JOIN gp_merchants me ON me.id = v.merchant_id
                JOIN gp_matches m ON m.id = v.match_id
                WHERE v.code=%s
                """,
                (code,),
            )
            voucher = cursor.fetchone()
            if not voucher:
                return jsonify({"status": "invalid"}), 404
            voucher_json = one_to_json(voucher)
            mirror_safe("voucher_verifications", {**voucher_json, "verification_status": "valid"}, ["code"])
            return jsonify(voucher_json)


@mvp_api.route("/vouchers/<code>/redeem", methods=["POST"])
def redeem_voucher(code):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE gp_vouchers SET status='used', redeemed_at=NOW() WHERE code=%s AND status='valid'",
                (code,),
            )
            updated = cursor.rowcount
        connection.commit()
    if not updated:
        return jsonify({"status": "not_redeemed"}), 409
    mirror_safe("vouchers", {"code": code, "status": "used", "redeemed_at": datetime.utcnow()}, ["code"])
    return jsonify({"status": "used"})
