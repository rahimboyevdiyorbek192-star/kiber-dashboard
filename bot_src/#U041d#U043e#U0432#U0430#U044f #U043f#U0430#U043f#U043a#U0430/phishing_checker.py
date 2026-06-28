# phishing_checker.py — Fishing/Scam havola tekshiruv moduli
import os
import ssl
import socket
import sqlite3
import base64
import unicodedata
import urllib.parse
import datetime
import asyncio
from concurrent.futures import ThreadPoolExecutor

import re as _re
import requests

executor = ThreadPoolExecutor(max_workers=10)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phishing_stats.db")

SUSPICIOUS_TLDS = {
    ".xyz", ".top", ".click", ".tk", ".ml", ".ga", ".cf", ".gq",
    ".pw", ".cc", ".su", ".icu", ".live", ".online", ".site", ".fun", ".space"
}
SUSPICIOUS_COUNTRIES = {"Russia", "China", "North Korea", "Iran", "Belarus"}
SHORT_URL_DOMAINS = {
    "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "short.link",
    "rebrand.ly", "cutt.ly", "clck.ru", "vk.cc"
}
SUSPICIOUS_KEYWORDS = {
    "login", "verify", "secure", "account", "update", "confirm", "bank",
    "payment", "signin", "password", "credential", "recover", "wallet",
    "billing", "kirish", "tasdiqlash", "xavfsiz", "hisob", "karta"
}
SCAM_BUTTON_WORDS = {
    "bonus", "sovg'a", "yutdi", "prize", "olish", "win", "gift",
    "бонус", "приз", "получить", "награда", "free", "tekin", "bepul",
    "yutuq", "lotereya", "lottery", "jackpot", "cash", "money"
}
TELEGRAM_DOMAINS = {"t.me", "telegram.me", "telegram.dog"}
UZBEK_BRANDS = {
    "payme": "payme.uz", "click": "click.uz", "uzcard": "uzcard.uz",
    "humo": "humo.uz", "kapitalbank": "kapitalbank.uz",
    "hamkorbank": "hamkorbank.uz", "myuzcard": "myuzcard.uz",
    "davrbank": "davrbank.uz", "aloqabank": "aloqabank.uz", "nbu": "nbu.uz",
}
GLOBAL_BRANDS = {
    "google": "google.com", "facebook": "facebook.com",
    "instagram": "instagram.com", "telegram": "telegram.org",
    "paypal": "paypal.com", "apple": "apple.com",
    "microsoft": "microsoft.com", "amazon": "amazon.com",
    "netflix": "netflix.com",
}
ALL_BRANDS = {**UZBEK_BRANDS, **GLOBAL_BRANDS}


# ── Ma'lumotlar bazasi ──────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS phishing_stats (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            url     TEXT,
            risk    INTEGER,
            checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_stat(url: str, risk: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO phishing_stats (url, risk) VALUES (?, ?)", (url, risk))
        conn.commit()
        conn.close()
    except Exception:
        pass

def get_stats() -> tuple:
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN risk >= 50 THEN 1 ELSE 0 END) FROM phishing_stats"
        ).fetchone()
        conn.close()
        return (row[0] or 0), (row[1] or 0)
    except Exception:
        return 0, 0


# ── Tarmoq tekshiruvlari ────────────────────────────────────────────────────

def get_redirect_chain(url: str) -> list:
    chain = []
    current = url
    try:
        for _ in range(10):
            resp = requests.get(current, allow_redirects=False, timeout=5, stream=True)
            chain.append(current)
            if resp.is_redirect and resp.headers.get("Location"):
                nxt = resp.headers["Location"]
                if not nxt.startswith("http"):
                    p = urllib.parse.urlparse(current)
                    nxt = f"{p.scheme}://{p.netloc}{nxt}"
                current = nxt
            else:
                break
    except Exception:
        pass
    return chain if chain else [url]

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Fishing sahifalarda ko'p uchraydigan input nomlari
_PHISH_INPUT_NAMES = {
    "password", "passwd", "pass", "pwd", "pin", "parol",
    "card", "cardnumber", "card_number", "ccnumber", "cc_num",
    "cvv", "cvc", "cvv2", "expiry", "exp_date", "expdate",
    "otp", "code", "sms_code", "verify_code",
    "login", "username", "email", "phone", "mobile",
    "account", "bank_account", "iban",
}
_PHISH_ACTION_WORDS = {"login", "signin", "verify", "confirm", "auth", "secure", "account", "payment"}
_JS_REDIRECT_RE = _re.compile(
    r'(?:window\.location|location\.href|location\.replace)\s*[=(]\s*["\']([^"\']+)["\']',
    _re.IGNORECASE
)
_META_REFRESH_RE = _re.compile(
    r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^;]+;\s*url=([^"\'>\s]+)',
    _re.IGNORECASE
)


def browse_url(url: str) -> dict:
    """
    Saytga brauzer kabi kiradi, HTML tahlil qiladi.
    Qaytaradi: {title, final_url, forms, inputs, js_redirects,
                suspicious_keywords, page_text_snippet, error}
    """
    result = {
        "title": "", "final_url": url, "forms": [],
        "inputs": [], "js_redirects": [], "suspicious_keywords": [],
        "page_text_snippet": "", "hidden_iframes": 0, "error": None,
    }
    try:
        from bs4 import BeautifulSoup
        sess = requests.Session()
        sess.headers.update(_BROWSER_HEADERS)
        resp = sess.get(url, timeout=10, allow_redirects=True)
        result["final_url"] = resp.url

        ct = resp.headers.get("Content-Type", "")
        if "text/html" not in ct and "application/xhtml" not in ct:
            result["error"] = f"HTML emas ({ct[:40]})"
            return result

        html = resp.text
        soup = BeautifulSoup(html, "lxml")

        # Title
        t = soup.find("title")
        result["title"] = t.get_text(strip=True)[:120] if t else ""

        # Formalar
        for form in soup.find_all("form"):
            action = form.get("action", "").lower()
            method = form.get("method", "get").upper()
            inp_names = []
            for inp in form.find_all(["input", "select"]):
                nm = (inp.get("name", "") or inp.get("id", "") or "").lower()
                tp = (inp.get("type", "text") or "text").lower()
                if tp not in ("hidden", "submit", "button", "image", "reset"):
                    inp_names.append(nm or tp)
                if nm in _PHISH_INPUT_NAMES or tp == "password":
                    result["inputs"].append(nm or tp)
            suspicious_action = any(w in action for w in _PHISH_ACTION_WORDS)
            result["forms"].append({
                "action": action[:80], "method": method,
                "fields": inp_names[:10], "suspicious": suspicious_action,
            })

        # Yashirin iframlar
        result["hidden_iframes"] = sum(
            1 for f in soup.find_all("iframe")
            if "display:none" in (f.get("style", "") or "") or f.get("hidden") is not None
        )

        # JS redirect
        for m in _JS_REDIRECT_RE.finditer(html[:50000]):
            rurl = m.group(1)
            if rurl.startswith("http") and rurl != resp.url:
                result["js_redirects"].append(rurl[:120])

        # Meta refresh
        for m in _META_REFRESH_RE.finditer(html[:10000]):
            result["js_redirects"].append(m.group(1)[:120])

        # Sahifa matni (qisqa)
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        result["page_text_snippet"] = text[:600]

        # Shubhali kalit so'zlar
        tl = text.lower()
        found = []
        for kw in ["kirish", "parol", "login", "password", "verify", "bank",
                   "karta", "card", "cvv", "otp", "tasdiqlang", "bonus",
                   "prize", "yutdi", "sovg'a", "bepul", "tekin", "click"]:
            if kw in tl and kw not in found:
                found.append(kw)
        result["suspicious_keywords"] = found[:10]

    except Exception as e:
        result["error"] = str(e)[:100]
    return result


def check_ssl(domain: str) -> dict:
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as s:
            s.settimeout(5)
            s.connect((domain, 443))
            cert = s.getpeercert()
        expire = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        days_left = (expire.replace(tzinfo=datetime.timezone.utc) - datetime.datetime.now(datetime.timezone.utc)).days
        issuer = dict(x[0] for x in cert.get("issuer", []))
        return {"valid": True, "days_left": days_left, "issuer": issuer.get("organizationName", "Noma'lum")}
    except Exception:
        return {"valid": False, "days_left": 0, "issuer": "Noma'lum"}

def check_whois(domain: str) -> dict:
    try:
        import whois
        w = whois.whois(domain)
        created = w.creation_date
        if isinstance(created, list):
            created = created[0]
        if created:
            if isinstance(created, str):
                created = datetime.datetime.fromisoformat(created)
            if created.tzinfo is None:
                created = created.replace(tzinfo=datetime.timezone.utc)
            age = (datetime.datetime.now(datetime.timezone.utc) - created).days
            return {"age_days": age, "registrar": w.registrar or "Noma'lum"}
    except Exception:
        pass
    return {"age_days": None, "registrar": "Noma'lum"}

def _get_geo(ip: str) -> dict:
    try:
        return requests.get(f"http://ip-api.com/json/{ip}", timeout=5).json()
    except Exception:
        return {}

def check_virustotal(url: str) -> dict:
    vt_key = os.getenv("VIRUSTOTAL_API_KEY", "").strip()
    if not vt_key:
        return {"available": False}
    try:
        url_id = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        headers = {"x-apikey": vt_key}
        resp = requests.get(f"https://www.virustotal.com/api/v3/urls/{url_id}", headers=headers, timeout=10)
        if resp.status_code == 200:
            stats = resp.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            if stats:
                return {"available": True, "malicious": stats.get("malicious", 0),
                        "suspicious": stats.get("suspicious", 0), "total": sum(stats.values())}
        sub = requests.post("https://www.virustotal.com/api/v3/urls", headers=headers, data={"url": url}, timeout=10)
        if sub.status_code == 200:
            return {"available": True, "malicious": 0, "suspicious": 0, "total": 0, "pending": True}
    except Exception:
        pass
    return {"available": False}

def check_urlhaus(url: str) -> dict:
    try:
        resp = requests.post("https://urlhaus-api.abuse.ch/v1/url/", data={"url": url}, timeout=10).json()
        status = resp.get("query_status", "")
        if status == "is_available":
            return {"available": True, "found": True,
                    "threat": resp.get("threat", "malware"), "tags": resp.get("tags") or []}
        return {"available": True, "found": False, "threat": "", "tags": []}
    except Exception:
        return {"available": False}

def check_abuseipdb(ip: str) -> dict:
    abuse_key = os.getenv("ABUSEIPDB_API_KEY", "").strip()
    if not abuse_key or ip == "Noma'lum":
        return {"available": False}
    try:
        resp = requests.get(
            "https://api.abuseipdb.com/api/v2/check",
            params={"ipAddress": ip, "maxAgeInDays": 90},
            headers={"Key": abuse_key, "Accept": "application/json"}, timeout=8
        ).json()
        data = resp.get("data", {})
        return {"available": True, "abuse_score": data.get("abuseConfidenceScore", 0),
                "total_reports": data.get("totalReports", 0)}
    except Exception:
        return {"available": False}


# ── Telegram tekshiruvlari ─────────────────────────────────────────────────

async def probe_telegram_bot(userbot, username: str) -> dict:
    """Userbot orqali shubhali botga /start yuborib yashirin havolalarni topadi."""
    if not userbot:
        return {"available": False}
    try:
        entity = await userbot.get_entity(username)
        await userbot.send_message(entity, "/start")
        await asyncio.sleep(5)
        messages = await userbot.get_messages(entity, limit=8)

        urls = set()
        texts = []
        clicked_buttons = []
        last_msg_id = 0

        def _extract(msg):
            if msg.entities:
                for ent in msg.entities:
                    if hasattr(ent, "url") and ent.url:
                        urls.add(ent.url)
            if msg.reply_markup and hasattr(msg.reply_markup, 'rows'):
                for row in msg.reply_markup.rows:
                    for btn in row.buttons:
                        if hasattr(btn, "url") and btn.url:
                            urls.add(btn.url)

        for msg in messages:
            if msg.out:
                continue
            if msg.text:
                texts.append(msg.text[:300])
            _extract(msg)
            if msg.id > last_msg_id:
                last_msg_id = msg.id

        # Inline tugmalarni bosish (URL tugmalar emas)
        for msg in messages:
            if msg.out or not msg.reply_markup:
                continue
            if not hasattr(msg.reply_markup, 'rows'):
                continue
            for row in msg.reply_markup.rows:
                for btn in row.buttons:
                    if hasattr(btn, "url") and btn.url:
                        continue
                    btn_text = getattr(btn, "text", "") or ""
                    if len(clicked_buttons) >= 4:
                        break
                    try:
                        await btn.click()
                        clicked_buttons.append(btn_text)
                        await asyncio.sleep(3)
                        new_msgs = await userbot.get_messages(entity, limit=6)
                        for nm in new_msgs:
                            if nm.out or nm.id <= last_msg_id:
                                continue
                            if nm.text:
                                texts.append(f"[{btn_text[:30]}] {nm.text[:200]}")
                            _extract(nm)
                            last_msg_id = max(last_msg_id, nm.id)
                    except Exception:
                        pass

        return {"available": True, "urls": list(urls),
                "msg_count": len([m for m in messages if not m.out]),
                "sample_text": texts[0][:200] if texts else "",
                "clicked_buttons": clicked_buttons}
    except Exception as e:
        return {"available": False, "error": str(e)[:150]}

async def check_telegram_channel_info(bot_client, url: str) -> dict:
    """Bot API orqali Telegram kanal/bot ma'lumotlarini oladi."""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in TELEGRAM_DOMAINS:
        return {}
    path = parsed.path.strip("/")
    if not path:
        return {}
    if path.startswith("+"):
        return {"is_private_invite": True}
    username = path.split("?")[0].split("/")[0]
    if not username:
        return {}
    try:
        from telethon.tl.functions.contacts import ResolveUsernameRequest
        result = await bot_client(ResolveUsernameRequest(username))
        chat = result.chats[0] if result.chats else (result.users[0] if result.users else None)
        if not chat:
            return {"found": False, "username": username, "deleted": True}
        title = getattr(chat, 'title', '') or getattr(chat, 'first_name', '') or ''
        title_lower = title.lower().replace(" ", "").replace("_", "").replace("-", "")
        title_homograph = any(
            any(s in unicodedata.name(ch, "") for s in ("CYRILLIC", "GREEK"))
            for ch in title if ch.isalpha()
        )
        brand_in_title = []
        for brand in ALL_BRANDS:
            if brand.replace("_", "").replace("-", "") in title_lower:
                brand_in_title.append(brand)
        verified = getattr(chat, 'verified', False)
        members = getattr(chat, 'participants_count', None)
        un = getattr(chat, 'username', username)
        return {"found": True, "username": un, "title": title,
                "verified": verified, "member_count": members,
                "brand_in_title": brand_in_title, "title_has_homograph": title_homograph}
    except Exception:
        return {"found": False, "username": username, "deleted": True}

async def check_with_browser(url: str) -> dict:
    # Playwright urinish
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ))
            page = await ctx.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=15000)
            except Exception:
                pass
            final_url = page.url
            title = await page.title()
            has_password   = await page.locator("input[type='password']").count() > 0
            has_card_input = await page.locator(
                "input[name*='card'], input[placeholder*='card'], input[placeholder*='karta'], "
                "input[name*='pan'], input[maxlength='16'], input[maxlength='19']"
            ).count() > 0
            has_form = await page.locator("form").count() > 0
            await browser.close()
            return {"available": True, "final_url": final_url, "title": title[:120],
                    "redirected": final_url.rstrip("/") != url.rstrip("/"),
                    "has_password": has_password, "has_card_input": has_card_input,
                    "has_form": has_form}
    except Exception:
        pass

    # Playwright yo'q yoki ishlamadi — requests + BeautifulSoup bilan
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(executor, browse_url, url)
        if data.get("error"):
            return {"available": False}
        inputs = data.get("inputs", [])
        forms  = data.get("forms", [])
        has_password   = any(i in ("password", "passwd", "pass", "pwd", "pin", "parol") for i in inputs)
        has_card_input = any(i in ("card", "cardnumber", "card_number", "ccnumber",
                                   "cvv", "cvc", "cvv2", "pan") for i in inputs)
        has_form       = bool(forms)
        final_url      = data.get("final_url", url)
        return {
            "available":    True,
            "final_url":    final_url,
            "title":        data.get("title", "")[:120],
            "redirected":   final_url.rstrip("/") != url.rstrip("/"),
            "has_password": has_password,
            "has_card_input": has_card_input,
            "has_form":     has_form,
            "js_redirects": data.get("js_redirects", []),
            "suspicious_keywords": data.get("suspicious_keywords", []),
            "hidden_iframes":      data.get("hidden_iframes", 0),
            "page_snippet":        data.get("page_text_snippet", "")[:300],
        }
    except Exception:
        return {"available": False}


# ── Heuristik tekshiruvlar ──────────────────────────────────────────────────

def check_url_patterns(url: str, domain: str) -> list:
    warnings = []
    try:
        socket.inet_aton(domain)
        warnings.append("⚠️ Domain o'rniga IP manzil ishlatilgan")
    except socket.error:
        pass
    if "@" in url:
        warnings.append("⚠️ URL da `@` belgisi — asl manzil yashirilgan bo'lishi mumkin")
    found = [kw for kw in SUSPICIOUS_KEYWORDS if kw in url.lower()]
    if found:
        warnings.append(f"⚠️ Shubhali so'zlar URL da: `{', '.join(found[:4])}`")
    if len(domain.split(".")) > 4:
        warnings.append(f"⚠️ Ko'p subdomen: {len(domain.split('.')) - 2} ta")
    if len(url) > 150:
        warnings.append(f"⚠️ URL juda uzun: {len(url)} ta belgi")
    if domain.count("-") >= 2:
        warnings.append("⚠️ Domenda ko'p `-` — phishing belgisi")
    return warnings

def check_brand_impersonation(domain: str) -> list:
    warnings = []
    dl = domain.lower()
    for brand, official in ALL_BRANDS.items():
        if brand in dl and dl != official and not dl.endswith("." + official):
            tag = "🇺🇿" if brand in UZBEK_BRANDS else "🌐"
            warnings.append(f"⚠️ {tag} `{brand}` brendini taqlid qilishi mumkin! Rasmiy: `{official}`")
    return warnings

def check_homograph(domain: str) -> list:
    found = []
    for ch in domain:
        if ch in ".-0123456789":
            continue
        name = unicodedata.name(ch, "")
        if any(s in name for s in ("CYRILLIC", "GREEK", "ARABIC", "ARMENIAN")):
            found.append(ch)
    if found:
        return [f"⚠️ Homograf hujum! Unicode harflar: `{''.join(set(found))}`"]
    return []

def check_telegram_url(url: str) -> list:
    warnings = []
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc not in TELEGRAM_DOMAINS:
        return warnings
    path = parsed.path.strip("/").lower().replace("_", "").replace("-", "")
    if not path or path.startswith("+"):
        if path.startswith("+"):
            warnings.append("⚠️ Yopiq Telegram kanal taklifi — ehtiyot bo'ling")
        return warnings
    for brand in ALL_BRANDS:
        if brand.replace("_", "").replace("-", "") in path:
            tag = "🇺🇿" if brand in UZBEK_BRANDS else "🌐"
            warnings.append(f"⚠️ {tag} Telegram username `{brand}` brendini taqlid qilishi mumkin")
    return warnings

def check_message_context_telethon(message) -> list:
    """Telethon xabar tuzilmasidan fishing belgilarini topadi."""
    warnings = []
    fwd = getattr(message, 'fwd_from', None)
    if fwd:
        from_name = getattr(fwd, 'from_name', '') or ''
        if from_name:
            nl = from_name.lower()
            if "postbot" in nl or "post bot" in nl:
                warnings.append("⚠️ @PostBot orqali forward — asl manba ataylab yashirilgan")
            for brand in ALL_BRANDS:
                if brand in nl:
                    warnings.append(f"⚠️ Forward yuboruvchi nomi `{brand}` brendini taqlid qilishi mumkin")
                    break
        if not getattr(fwd, 'channel_id', None) and not from_name:
            warnings.append("⚠️ Forward manba — nomi yo'q yashirin kanal")
    # Inline tugmalar
    markup = getattr(message, 'reply_markup', None)
    if markup and hasattr(markup, 'rows'):
        for row in markup.rows:
            for btn in row.buttons:
                btn_text = getattr(btn, 'text', '') or ''
                if btn_text:
                    found = [kw for kw in SCAM_BUTTON_WORDS if kw in btn_text.lower()]
                    if found:
                        warnings.append(f"⚠️ Tugma matni ijtimoiy muhandislik: '{btn_text}'")
                if hasattr(btn, 'url') and btn.url and 'web' in type(btn).__name__.lower():
                    warnings.append(f"⚠️ WebApp tugmasi — havola yashiringan: `{btn.url[:80]}`")
    return warnings


# ── Xavf hisoblash ──────────────────────────────────────────────────────────

def calculate_risk(domain, country, ssl_info, whois_info, redirected,
                   vt, urlhaus, abuse, pattern_w, brand_w, homograph_w,
                   tg_url_w=None, context_w=None) -> tuple:
    score = 0
    reasons = []

    if vt.get("available") and not vt.get("pending") and vt.get("malicious", 0) > 0:
        score += min(vt["malicious"] * 5, 40)
        reasons.append(f"🔴 VirusTotal: {vt['malicious']}/{vt['total']} engine xavfli dedi")
    if urlhaus.get("found"):
        score += 40
        reasons.append(f"🔴 URLhaus: bazada topildi — `{urlhaus.get('threat', 'malware')}`")
    if abuse.get("available") and abuse.get("abuse_score", 0) > 25:
        score += min(abuse["abuse_score"] // 4, 20)
        reasons.append(f"🔴 AbuseIPDB: {abuse['abuse_score']}/100 ({abuse.get('total_reports', 0)} shikoyat)")
    if homograph_w:
        score += 35
        reasons.extend(homograph_w)
    if brand_w:
        score += 25
        reasons.extend(brand_w)
    if tg_url_w:
        deleted = any("O'CHIRILGAN" in w for w in tg_url_w)
        score += 45 if deleted else 30
        reasons.extend(tg_url_w)
    if context_w:
        score += min(len(context_w) * 15, 35)
        reasons.extend(context_w)

    tld = "." + domain.split(".")[-1].lower()
    if tld in SUSPICIOUS_TLDS:
        score += 15
        reasons.append(f"⚠️ Shubhali kengaytma: `{tld}`")
    if country in SUSPICIOUS_COUNTRIES:
        score += 20
        reasons.append(f"⚠️ Server {country}da joylashgan")
    if not ssl_info["valid"]:
        score += 25
        reasons.append("⚠️ SSL sertifikat yo'q yoki yaroqsiz")
    elif ssl_info["days_left"] < 30:
        score += 10
        reasons.append(f"⚠️ SSL {ssl_info['days_left']} kun ichida tugaydi")

    age = whois_info.get("age_days")
    if age is not None:
        if age < 180:
            score += 30
            reasons.append(f"⚠️ Domen {age} kun oldin ro'yxatdan o'tgan — juda yangi")
        elif age < 365:
            score += 15
            reasons.append(f"⚠️ Domen nisbatan yangi ({age} kun)")

    if redirected:
        score += 10
        reasons.append("⚠️ Yashirin yo'naltirish aniqlandi")
    if pattern_w:
        score += min(len(pattern_w) * 8, 25)
        reasons.extend(pattern_w)

    return min(score, 100), reasons

def risk_label(score: int) -> str:
    if score <= 25:
        return "🟢 Xavfsiz"
    elif score <= 50:
        return "🟡 Shubhali"
    elif score <= 75:
        return "🟠 Xavfli"
    else:
        return "🔴 JUDA XAVFLI"

def format_age(days) -> str:
    if days is None:
        return "Noma'lum"
    if days >= 365:
        return f"{days // 365} yil {(days % 365) // 30} oy"
    return f"{days} kun"


# ── Asosiy tahlil ──────────────────────────────────────────────────────────

async def analyze_url(url: str, userbot=None, bot_client=None, context_w=None) -> tuple:
    """
    URL ni to'liq tahlil qiladi.
    Qaytaradi: (report_text, score, probe_urls)
    """
    loop = asyncio.get_event_loop()
    try:
        chain = await loop.run_in_executor(executor, get_redirect_chain, url)
        final_url = chain[-1] if chain else url
        redirected = len(chain) > 1

        parsed = urllib.parse.urlparse(final_url)
        domain = (parsed.netloc or parsed.path).split(":")[0]
        if "@" in domain:
            domain = domain.split("@")[-1]
        if not domain:
            return None, 0, []

        pattern_w   = check_url_patterns(final_url, domain)
        brand_w     = check_brand_impersonation(domain)
        homograph_w = check_homograph(domain)
        tg_url_w    = check_telegram_url(final_url)

        is_tg = domain in TELEGRAM_DOMAINS
        tg_channel = {}
        if is_tg and bot_client:
            tg_channel = await check_telegram_channel_info(bot_client, final_url)

        tg_channel_w = []
        if tg_channel.get("found"):
            if tg_channel.get("brand_in_title") and not tg_channel.get("verified"):
                for brand in tg_channel["brand_in_title"]:
                    tag = "🇺🇿" if brand in UZBEK_BRANDS else "🌐"
                    tg_channel_w.append(f"🔴 {tag} Kanal `{brand}` brendini taqlid qiladi lekin TASDIQLANMAGAN!")
            if tg_channel.get("title_has_homograph"):
                tg_channel_w.append(f"🔴 Kanal nomida unicode harflar: `{tg_channel.get('title', '')}`")
        elif tg_channel.get("deleted"):
            uname = tg_channel.get("username", "")
            tg_channel_w.append(f"🔴 @{uname} — O'CHIRILGAN! Spam yuborib ketishgan — klassik fishing belgisi")
        elif tg_channel.get("is_private_invite"):
            tg_channel_w.append("⚠️ Yopiq Telegram kanal taklifi — kim ekanligini ko'rib bo'lmaydi")

        probe = {"available": False}
        probe_urls = []
        if is_tg and userbot and not tg_channel.get("is_private_invite") and not tg_channel.get("deleted"):
            tg_path = urllib.parse.urlparse(final_url).path.strip("/").split("?")[0]
            if tg_path and not tg_path.startswith("+"):
                probe = await probe_telegram_bot(userbot, tg_path)
                probe_urls = probe.get("urls", [])

        try:
            ip = await loop.run_in_executor(executor, socket.gethostbyname, domain)
        except Exception:
            ip = "Noma'lum"

        results = await asyncio.gather(
            loop.run_in_executor(executor, _get_geo, ip),
            loop.run_in_executor(executor, check_ssl, domain),
            loop.run_in_executor(executor, check_whois, domain),
            loop.run_in_executor(executor, check_virustotal, final_url),
            loop.run_in_executor(executor, check_urlhaus, final_url),
            loop.run_in_executor(executor, check_abuseipdb, ip),
            check_with_browser(final_url),
            return_exceptions=True,
        )

        def _safe(r, default):
            return r if not isinstance(r, Exception) else default

        geo        = _safe(results[0], {})
        ssl_info   = _safe(results[1], {"valid": False, "days_left": 0, "issuer": "Noma'lum"})
        whois_info = _safe(results[2], {"age_days": None, "registrar": "Noma'lum"})
        vt         = _safe(results[3], {"available": False})
        urlhaus    = _safe(results[4], {"available": False})
        abuse      = _safe(results[5], {"available": False})
        browser    = _safe(results[6], {"available": False})

        country = geo.get("country", "Noma'lum")
        isp     = geo.get("isp", "Noma'lum")

        browser_w = []
        if browser.get("available"):
            if browser.get("has_card_input"):
                browser_w.append("🔴 Sahifada karta raqami kiritish shakli topildi!")
            if browser.get("has_password"):
                browser_w.append("🔴 Sahifada parol kiritish shakli topildi!")
            if browser.get("redirected"):
                browser_w.append(f"🔀 Brauzer haqiqiy manzilga o'tkazdi: `{browser.get('final_url', '')[:80]}`")
            for jr in browser.get("js_redirects", [])[:2]:
                browser_w.append(f"⚠️ JavaScript redirect: `{jr[:80]}`")
            if browser.get("hidden_iframes", 0) > 0:
                browser_w.append(f"🔴 Yashirin iframe topildi ({browser['hidden_iframes']} ta) — phishing belgisi!")
            skw = browser.get("suspicious_keywords", [])
            if skw:
                browser_w.append(f"⚠️ Sahifada shubhali so'zlar: `{', '.join(skw[:6])}`")

        all_tg_w = tg_url_w + tg_channel_w
        score, reasons = calculate_risk(
            domain, country, ssl_info, whois_info, redirected,
            vt, urlhaus, abuse, pattern_w, brand_w, homograph_w,
            tg_url_w=all_tg_w,
            context_w=(context_w or []) + browser_w,
        )
        label = risk_label(score)

        ssl_str = (f"✅ Ha ({ssl_info['days_left']} kun · {ssl_info['issuer']})"
                   if ssl_info["valid"] else "❌ Yo'q")

        if redirected and len(chain) > 1:
            chain_lines = "\n🔗 *Yo'naltirish zanjiri:*\n"
            for i, hop in enumerate(chain[:6]):
                prefix = "└→" if i == len(chain) - 1 else "├→"
                chain_lines += f"  {prefix} `{hop[:80]}`\n"
        else:
            chain_lines = "🔗 *Yo'naltirish:* Yo'q"

        if tg_channel.get("found"):
            verified_str = "✅ Tasdiqlangan" if tg_channel.get("verified") else "❌ Tasdiqlanmagan"
            members = tg_channel.get("member_count")
            members_str = f"{members:,}" if members else "Noma'lum"
            tg_block = (f"\n📱 *Telegram kanal:*\n"
                        f"📛 *Nomi:* {tg_channel.get('title', '—')}\n"
                        f"🔖 *Username:* @{tg_channel.get('username', '—')}\n"
                        f"✅ *Tasdiqlangan:* {verified_str}\n"
                        f"👥 *A'zolar:* {members_str}\n")
        elif tg_channel.get("deleted"):
            tg_block = (f"\n📱 *Telegram kanal:*\n"
                        f"🔖 *Username:* @{tg_channel.get('username', '')}\n"
                        f"🗑 *Holat:* O'CHIRILGAN — spam yuborib o'chirib ketishgan\n")
        elif tg_channel.get("is_private_invite"):
            tg_block = "\n📱 *Telegram:* Yopiq kanal taklifi\n"
        elif is_tg:
            tg_block = f"\n📱 *Telegram:* `{urllib.parse.urlparse(final_url).path.strip('/')}` — ma'lumot olishning imkoni yo'q\n"
        else:
            tg_block = ""

        probe_block = ""
        if probe.get("available"):
            msg_count = probe.get("msg_count", 0)
            sample = probe.get("sample_text", "")
            clicked = probe.get("clicked_buttons", [])
            clicked_str = ", ".join(f"'{b}'" for b in clicked[:4]) if clicked else "Yo'q"
            if probe_urls:
                urls_list = "\n".join(f"  🔗 `{u[:80]}`" for u in probe_urls[:5])
                probe_block = (f"\n🤖 *Userbot tekshiruvi ({msg_count} ta javob):*\n"
                               f"🖱 *Bosgan tugmalar:* {clicked_str}\n"
                               f"📨 *Bot matni:* {sample[:100]}\n"
                               f"🔗 *Topilgan havolalar:*\n{urls_list}\n")
            else:
                probe_block = (f"\n🤖 *Userbot tekshiruvi:* {msg_count} ta javob\n"
                               f"🖱 *Bosgan tugmalar:* {clicked_str}\n"
                               f"📨 *Bot matni:* {sample[:100] if sample else '—'}\n"
                               f"🔗 *Havolalar:* Topilmadi\n")

        if vt.get("available"):
            vt_str = ("⏳ Yangi havola — tahlil topshirildi" if vt.get("pending")
                      else f"{'🔴' if vt['malicious'] > 0 else '🟢'} {vt['malicious']}/{vt['total']} engine")
        else:
            vt_str = "⚪ API kalit yo'q (.env da VIRUSTOTAL_API_KEY)"

        uh_str = ("🔴 Bazada topildi!" if urlhaus.get("found")
                  else "🟢 URLhaus bazasida yo'q" if urlhaus.get("available")
                  else "⚪ Tekshirib bo'lmadi")

        if abuse.get("available"):
            s = abuse.get("abuse_score", 0)
            icon = "🔴" if s > 50 else "🟡" if s > 25 else "🟢"
            abuse_str = f"{icon} {s}/100 ({abuse.get('total_reports', 0)} shikoyat)"
        else:
            abuse_str = "⚪ API kalit yo'q (.env da ABUSEIPDB_API_KEY)"

        if browser.get("available"):
            b_title  = browser.get("title", "—") or "—"
            b_card   = "🔴 BOR" if browser.get("has_card_input") else "🟢 Yo'q"
            b_pass   = "🔴 BOR" if browser.get("has_password")   else "🟢 Yo'q"
            b_form   = "🔴 BOR" if browser.get("has_form")       else "🟢 Yo'q"
            b_iframe = f"🔴 {browser['hidden_iframes']} ta" if browser.get("hidden_iframes") else "🟢 Yo'q"
            b_snip   = browser.get("page_snippet", "")
            snip_str = f"\n💬 *Sahifa matni:* _{b_snip[:150]}_" if b_snip else ""
            browser_block = (
                f"\n🌐 *Brauzer tekshiruvi:*\n"
                f"📄 *Sahifa nomi:* {b_title}\n"
                f"💳 *Karta shakli:* {b_card}\n"
                f"🔑 *Parol shakli:* {b_pass}\n"
                f"📝 *Forma:* {b_form} · 🖼 *Yashirin iframe:* {b_iframe}"
                f"{snip_str}\n"
            )
        else:
            browser_block = ""

        report = (
            f"🔍 *Havola tahlili:*\n"
            f"🌐 *Domen:* `{domain}`\n"
            f"📌 *IP:* `{ip}` · {country}\n"
            f"🏢 *Hosting:* {isp}\n"
            f"🛡 *SSL:* {ssl_str}\n"
            f"📅 *Domen yoshi:* {format_age(whois_info.get('age_days'))}\n"
            f"{chain_lines}"
            f"{tg_block}"
            f"{probe_block}"
            f"{browser_block}\n"
            f"*🔬 Threat Intelligence:*\n"
            f"🦠 *VirusTotal:* {vt_str}\n"
            f"☣️ *URLhaus:* {uh_str}\n"
            f"🚨 *AbuseIPDB:* {abuse_str}\n\n"
            f"📊 *Xavf darajasi: {score}/100 — {label}*"
        )

        if reasons:
            reasons_block = "\n\n*⚠️ Topilgan muammolar:*\n" + "\n".join(reasons[:8])
            if len(report) + len(reasons_block) < 4000:
                report += reasons_block

        return report, score, probe_urls

    except Exception:
        return f"🌐 *Havola:* `{url}`\n❌ Tahlil qilib bo'lmadi.", 0, []


def extract_urls_from_telethon_msg(message) -> set:
    """Telethon xabaridan barcha havolalarni topib oladi."""
    urls = set()
    text = message.text or message.message or ""
    entities = message.entities or []
    for ent in entities:
        etype = type(ent).__name__
        if etype == "MessageEntityUrl":
            url = text[ent.offset: ent.offset + ent.length]
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            urls.add(url)
        elif etype == "MessageEntityTextUrl":
            if ent.url:
                urls.add(ent.url)
    # Inline tugmalar
    markup = getattr(message, 'reply_markup', None)
    if markup and hasattr(markup, 'rows'):
        for row in markup.rows:
            for btn in row.buttons:
                if hasattr(btn, 'url') and btn.url:
                    urls.add(btn.url)
    # Oddiy matndan URL ni grep qilish
    import re
    for m in re.finditer(r'https?://[^\s\]>)"\']+', text):
        urls.add(m.group())
    return urls


# ══════════════════════════════════════════════════════════════════════
# 📱 APK TAHLIL
# ══════════════════════════════════════════════════════════════════════

import zipfile
import hashlib
import struct
import re as _re

# Xavfli Android ruxsatlari → (emoji+nom, xavf bali)
DANGEROUS_PERMISSIONS = {
    "READ_SMS":                  ("🔴 SMS o'qish",                        30),
    "SEND_SMS":                  ("🔴 SMS yuborish",                       30),
    "RECEIVE_SMS":               ("🔴 SMS qabul qilish",                   25),
    "RECEIVE_MMS":               ("🟠 MMS qabul qilish",                   20),
    "READ_CONTACTS":             ("🟠 Kontaktlarni o'qish",                20),
    "WRITE_CONTACTS":            ("🟠 Kontaktlarga yozish",                15),
    "READ_CALL_LOG":             ("🟠 Qo'ng'iroq tarixini o'qish",         20),
    "PROCESS_OUTGOING_CALLS":    ("🔴 Chiquvchi qo'ng'iroqlarni kuzatish", 25),
    "RECORD_AUDIO":              ("🔴 Mikrofon — ovoz yozish",             30),
    "CAMERA":                    ("🟠 Kamera",                             15),
    "ACCESS_FINE_LOCATION":      ("🟠 Aniq GPS joylashuv",                 20),
    "ACCESS_BACKGROUND_LOCATION":("🔴 Fonda GPS kuzatish",                 35),
    "READ_EXTERNAL_STORAGE":     ("🟡 Xotirani o'qish",                    10),
    "WRITE_EXTERNAL_STORAGE":    ("🟡 Xotiraga yozish",                    10),
    "MANAGE_EXTERNAL_STORAGE":   ("🔴 Barcha xotiraga kirish",             30),
    "RECEIVE_BOOT_COMPLETED":    ("🟠 Qurilma yonishi bilan autostart",     15),
    "FOREGROUND_SERVICE":        ("🟡 Fon xizmati",                        10),
    "REQUEST_INSTALL_PACKAGES":  ("🔴 Boshqa APK o'rnatish",               35),
    "BIND_ACCESSIBILITY_SERVICE":("🔴 Accessibility — klaviatura/ekran o'qish", 40),
    "SYSTEM_ALERT_WINDOW":       ("🔴 Ekran ustida oyna (overlay)",        30),
    "DISABLE_KEYGUARD":          ("🔴 Ekran qulfini o'chirish",            25),
    "READ_PHONE_STATE":          ("🟡 Telefon IMEI/SIM holati",            10),
    "GET_ACCOUNTS":              ("🟠 Google/boshqa hisoblarni o'qish",    15),
    "USE_CREDENTIALS":           ("🟠 Hisob ma'lumotlariga kirish",        20),
    "CHANGE_NETWORK_STATE":      ("🟡 Tarmoq sozlamalarini o'zgartirish",  10),
    "CHANGE_WIFI_STATE":         ("🟡 Wi-Fi sozlamalarini o'zgartirish",   10),
    "BLUETOOTH_ADMIN":           ("🟡 Bluetooth boshqaruvi",               10),
}

# Ma'lumot uzatish uchun ishlatiladigan shubhali URL patternlar
DATA_EXFIL_PATTERNS = [
    (_re.compile(r'https?://\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'),
     "🔴 To'g'ridan IP manziliga ma'lumot uzatish"),
    (_re.compile(r'api\.telegram\.org/bot[\w:]+/send'),
     "🔴 Telegram bot API — ma'lumotlar Telegram botga yuborilmoqda!"),
    (_re.compile(r'https?://[^/\s]+\.(ru|cn|ir|by|kp)/'),
     "🟠 Shubhali mamlakatdagi server (RU/CN/IR/BY/KP)"),
    (_re.compile(r'https?://[^/\s]+\.onion'),
     "🔴 Tor tarmoqiga ma'lumot uzatish"),
    (_re.compile(r'(?:webhook|exfil|upload|collect|steal|gate|panel|log\.php)'),
     "🔴 C2/phishing panel endpoint aniqlandi"),
    (_re.compile(r'https?://ngrok\.io|\.ngrok\.app'),
     "🟠 ngrok tunnel — vaqtinchalik yashirin server"),
    (_re.compile(r'https?://[^/\s]+pastebin\.com'),
     "🟡 Pastebin — konfiguratsiya yashiringan bo'lishi mumkin"),
]


def _apk_hash(path: str) -> dict:
    """MD5, SHA1, SHA256 hisoblaydi."""
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            md5.update(chunk)
            sha1.update(chunk)
            sha256.update(chunk)
    return {"md5": md5.hexdigest(), "sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}


def _check_vt_hash(sha256: str) -> dict:
    """VirusTotal da hash orqali tekshirish (fayl yuklamaydi)."""
    vt_key = os.getenv("VIRUSTOTAL_API_KEY", "").strip()
    if not vt_key:
        return {"available": False}
    try:
        resp = requests.get(
            f"https://www.virustotal.com/api/v3/files/{sha256}",
            headers={"x-apikey": vt_key}, timeout=10
        )
        if resp.status_code == 200:
            stats = resp.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            name  = resp.json().get("data", {}).get("attributes", {}).get("meaningful_name", "")
            return {"available": True,
                    "malicious": stats.get("malicious", 0),
                    "suspicious": stats.get("suspicious", 0),
                    "total": sum(stats.values()),
                    "name": name}
        if resp.status_code == 404:
            return {"available": True, "not_found": True}
    except Exception:
        pass
    return {"available": False}


def _extract_strings_from_binary(data: bytes, min_len: int = 5) -> list:
    """Binary ma'lumotdan o'qilishi mumkin bo'lgan satrlarni ajratib oladi."""
    # ASCII satrlar
    ascii_strings = _re.findall(rb'[\x20-\x7e]{%d,}' % min_len, data)
    results = set()
    for s in ascii_strings:
        try:
            results.add(s.decode('ascii'))
        except Exception:
            pass
    # UTF-16LE satrlar (Android binary XML da ishlatiladi)
    utf16_strings = _re.findall(rb'(?:[\x20-\x7e]\x00){%d,}' % min_len, data)
    for s in utf16_strings:
        try:
            decoded = s.decode('utf-16-le').strip('\x00').strip()
            if len(decoded) >= min_len:
                results.add(decoded)
        except Exception:
            pass
    return list(results)


def _find_permissions(strings: list) -> list:
    """Satrlar ichidan Android ruxsatlarini topadi."""
    found = []
    for s in strings:
        # android.permission.READ_SMS → READ_SMS
        if 'android.permission.' in s:
            perm = s.split('android.permission.')[-1].split('\x00')[0].strip()
            if perm and perm.upper() == perm and len(perm) > 2:
                found.append(perm)
        # com.google.android.c2dm.permission.RECEIVE kabi
        elif '.permission.' in s:
            perm = s.split('.permission.')[-1].split('\x00')[0].strip()
            if perm and len(perm) > 2:
                found.append(perm)
    return list(set(found))


def _find_network_endpoints(strings: list) -> list:
    """Satrlar ichidan URL va IP manzillarni topadi."""
    endpoints = set()
    url_pat = _re.compile(r'https?://[^\s\'"<>]{6,}')
    ip_pat  = _re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
    for s in strings:
        for m in url_pat.finditer(s):
            u = m.group().rstrip('.,);\'\"')
            if len(u) > 10:
                endpoints.add(u)
        for m in ip_pat.finditer(s):
            ip = m.group()
            if not ip.startswith(('127.', '10.', '192.168.', '0.', '255.')):
                endpoints.add(ip)
    return list(endpoints)


COMMON_APK_PASSWORDS = [
    b"infected", b"virus", b"malware", b"password", b"1234", b"12345",
    b"123456", b"admin", b"test", b"sample", b"apk", b"android",
    b"0000", b"9999", b"qwerty", b"abc123", b"infected1", b"virus123",
    b"mal", b"pack", b"crack", b"patch", b"mod", b"hack", b"evil",
    b"trojan", b"spyware", b"ransomware", b"keylogger", b"backdoor",
    b"pass", b"secret", b"private", b"hidden", b"secure", b"lock",
    b"cracked", b"modded", b"cheat", b"premium", b"pro", b"vip",
    b"", b"1", b"0", b"a", b"x", b"z",
]


def _find_zip_password(file_path: str) -> bytes | None:
    """Common parollar yordamida shifrlangan ZIP ni ochishga urinadi."""
    try:
        zf = zipfile.ZipFile(file_path, 'r')
        test_entry = None
        for name in zf.namelist():
            try:
                info = zf.getinfo(name)
                if info.flag_bits & 0x1:
                    test_entry = name
                    break
            except Exception:
                continue
        if not test_entry:
            zf.close()
            return None
        for pwd in COMMON_APK_PASSWORDS:
            try:
                zf.read(test_entry, pwd=pwd)
                zf.close()
                return pwd
            except (RuntimeError, Exception):
                continue
        zf.close()
    except Exception:
        pass
    return None


def _apk_risk_score(permissions: list, endpoints: list, vt: dict, file_size_mb: float) -> tuple:
    score = 0
    reasons = []

    # VirusTotal
    if vt.get("available") and not vt.get("not_found"):
        mal = vt.get("malicious", 0)
        if mal > 0:
            score += min(mal * 6, 50)
            reasons.append(f"🔴 VirusTotal: {mal}/{vt.get('total', 0)} engine xavfli dedi")
    elif vt.get("not_found"):
        score += 10
        reasons.append("🟡 VirusTotal bazasida topilmadi — yangi/noma'lum APK")

    # Ruxsatlar
    perm_score = 0
    found_dangerous = []
    for perm in permissions:
        info = DANGEROUS_PERMISSIONS.get(perm.upper())
        if info:
            label, pts = info
            if pts >= 25:
                perm_score += pts
                found_dangerous.append(f"{label} (`{perm}`)")
    score += min(perm_score, 60)
    reasons.extend(found_dangerous[:10])

    # Tarmoq endpointlari
    exfil_found = []
    for ep in endpoints:
        for pat, msg in DATA_EXFIL_PATTERNS:
            if pat.search(ep):
                short_ep = ep[:80]
                item = f"{msg}: `{short_ep}`"
                if item not in exfil_found:
                    exfil_found.append(item)
                    score += 20
                break
    reasons.extend(exfil_found[:8])

    # Shifrlangan APK — kuchli shubha belgisi
    for p in permissions:
        if "SHIFRLANGAN" in p:
            score += 25
            reasons.append("🔴 APK fayl shifrlangan — zararli dasturlar ko'pincha shunday qilinadi")
            break

    # Fayl hajmi shubhali bo'lsa
    if file_size_mb > 50:
        score += 5
        reasons.append(f"🟡 Fayl hajmi katta: {file_size_mb:.1f} MB")

    return min(score, 100), reasons


async def analyze_apk(file_path: str) -> tuple:
    """
    APK faylni tahlil qiladi.
    Qaytaradi: (report_text, risk_score)
    """
    loop = asyncio.get_event_loop()
    try:
        file_size = os.path.getsize(file_path)
        file_size_mb = file_size / (1024 * 1024)

        # ZIP sifatida ochish
        try:
            zf = zipfile.ZipFile(file_path, 'r')
            names = zf.namelist()
        except zipfile.BadZipFile:
            return "❌ Bu haqiqiy APK fayl emas (ZIP ochib bo'lmadi).", 80

        has_manifest = 'AndroidManifest.xml' in names
        has_dex      = any(n.endswith('.dex') for n in names)
        has_classes  = 'classes.dex' in names

        if not has_manifest or not has_dex:
            zf.close()
            return "❌ AndroidManifest.xml yoki classes.dex topilmadi — haqiqiy APK emas.", 70

        # Shifrlangan ekanligini aniqlash va parol topishga urinish
        _is_encrypted = False
        try:
            _mi = zf.getinfo('AndroidManifest.xml')
            _is_encrypted = bool(_mi.flag_bits & 0x1)
        except Exception:
            pass
        cracked_pwd = None
        if _is_encrypted:
            cracked_pwd = await loop.run_in_executor(executor, _find_zip_password, file_path)
        _pwd = {'pwd': cracked_pwd} if cracked_pwd is not None else {}

        # Hash hisoblash va VirusTotal parallel
        hashes = await loop.run_in_executor(executor, _apk_hash, file_path)
        vt = await loop.run_in_executor(executor, _check_vt_hash, hashes['sha256'])

        # AndroidManifest.xml dan ruxsatlarni ajratish
        permissions = []
        manifest_strings = []
        pkg_name = ""
        try:
            manifest_data = zf.read('AndroidManifest.xml', **_pwd)
            manifest_strings = await loop.run_in_executor(
                executor, _extract_strings_from_binary, manifest_data, 4
            )
            permissions = _find_permissions(manifest_strings)
            for s in manifest_strings:
                if s.count('.') >= 2 and s.replace('.', '').replace('_', '').isalnum() and len(s) > 8:
                    pkg_name = s
                    break
            if _is_encrypted and cracked_pwd is not None:
                pwd_str = cracked_pwd.decode('utf-8', errors='replace') or '(bo\'sh)'
                permissions.append(f"⚠️ SHIFRLANGAN APK — parol topildi: `{pwd_str}`")
        except RuntimeError:
            if _is_encrypted and cracked_pwd is None:
                permissions = ["⚠️ AndroidManifest.xml SHIFRLANGAN — parol topilmadi (kuchli shifrlash)"]
            else:
                permissions = ["⚠️ AndroidManifest.xml shifrlangan va ochib bo'lmadi"]

        # classes.dex dan URL va IP topish
        dex_names = [n for n in names if n.endswith('.dex')]
        all_dex_strings = []
        for dex_name in dex_names[:3]:
            try:
                dex_data = zf.read(dex_name, **_pwd)
                dex_strings = await loop.run_in_executor(
                    executor, _extract_strings_from_binary, dex_data, 6
                )
                all_dex_strings.extend(dex_strings)
            except RuntimeError:
                pass  # Shifrlangan DEX — o'tkazib yuborish

        zf.close()

        # Tarmoq endpointlari
        endpoints = _find_network_endpoints(all_dex_strings + manifest_strings)

        # Xavf hisoblash
        score, reasons = _apk_risk_score(permissions, endpoints, vt, file_size_mb)
        label = risk_label(score)

        # Ruxsatlar ro'yxati
        dangerous_perms = []
        normal_perms = []
        encrypt_warnings = []
        for p in sorted(set(permissions)):
            if "SHIFRLANGAN" in p:
                encrypt_warnings.append(f"  {p}")
                continue
            info = DANGEROUS_PERMISSIONS.get(p.upper())
            if info and info[1] >= 15:
                dangerous_perms.append(f"  {info[0]}")
            elif info and info[1] > 0:
                normal_perms.append(f"  🟡 {p}")
            elif p.upper() not in DANGEROUS_PERMISSIONS:
                pass  # Noma'lum ruxsatlar

        # Endpointlar (qisqartirilgan)
        ep_lines = ""
        if endpoints:
            ep_show = []
            for ep in endpoints[:10]:
                # Shubhali pattern bor-yo'qligini tekshir
                is_suspicious = any(pat.search(ep) for pat, _ in DATA_EXFIL_PATTERNS)
                icon = "🔴" if is_suspicious else "⚪"
                ep_show.append(f"  {icon} `{ep[:70]}`")
            ep_lines = "\n🌐 *Tarmoq endpointlari:*\n" + "\n".join(ep_show)

        # VirusTotal natijasi
        if vt.get("available") and not vt.get("not_found"):
            mal = vt.get("malicious", 0)
            vt_str = f"{'🔴' if mal > 0 else '🟢'} {mal}/{vt.get('total', 0)} engine xavfli dedi"
        elif vt.get("not_found"):
            vt_str = "🟡 Bazada topilmadi"
        else:
            vt_str = "⚪ API kalit yo'q"

        perms_str = ""
        if encrypt_warnings:
            perms_str += "\n🔐 *Shifrlash:*\n" + "\n".join(encrypt_warnings)
        if dangerous_perms:
            perms_str += "\n🚨 *Xavfli ruxsatlar:*\n" + "\n".join(dangerous_perms[:15])
        if normal_perms:
            perms_str += "\n🟡 *Oddiy ruxsatlar:*\n" + "\n".join(normal_perms[:8])
        if not permissions:
            perms_str = "\n⚪ Ruxsatlar topilmadi (yashirilgan bo'lishi mumkin)"

        _pkg_str      = pkg_name if pkg_name else "Noma'lum"
        _manifest_str = "✅ Manifest bor" if has_manifest else "❌ Manifest yo'q"
        report = (
            f"📱 *APK Tahlil Hisoboti*\n\n"
            f"📦 *Paket nomi:* `{_pkg_str}`\n"
            f"📏 *Hajm:* {file_size_mb:.2f} MB\n"
            f"🗂 *DEX fayl:* {len(dex_names)} ta · {_manifest_str}\n"
            f"🔑 *MD5:* `{hashes['md5']}`\n"
            f"🔑 *SHA256:* `{hashes['sha256'][:32]}...`\n\n"
            f"🦠 *VirusTotal:* {vt_str}\n"
            f"{perms_str}"
            f"{ep_lines}\n\n"
            f"📊 *Xavf darajasi: {score}/100 — {label}*"
        )

        if reasons:
            reasons_block = "\n\n*⚠️ Topilgan muammolar:*\n" + "\n".join(reasons[:10])
            if len(report) + len(reasons_block) < 4000:
                report += reasons_block

        return report, score

    except Exception as e:
        return f"❌ APK tahlil xatosi: {e}", 0


# ══════════════════════════════════════════════════════════════════════
# 🎵 OGG / AUDIO FAYL TAHLIL
# ══════════════════════════════════════════════════════════════════════

OGG_MAGIC    = b'OggS'
VORBIS_MAGIC = b'\x01vorbis'


def _parse_ogg_comments(data: bytes) -> dict:
    """
    Vorbis comment header dan metadata o'qiydi.
    Format: \x03vorbis → vendor string → user comments
    """
    result = {"vendor": "", "comments": [], "suspicious": []}
    try:
        pos = data.find(b'\x03vorbis')
        if pos < 0:
            return result
        pos += 7  # skip \x03vorbis

        vendor_len = struct.unpack_from('<I', data, pos)[0]
        pos += 4
        vendor = data[pos:pos + vendor_len].decode('utf-8', errors='replace')
        result["vendor"] = vendor
        pos += vendor_len

        comment_count = struct.unpack_from('<I', data, pos)[0]
        pos += 4

        for _ in range(min(comment_count, 50)):
            if pos + 4 > len(data):
                break
            clen = struct.unpack_from('<I', data, pos)[0]
            pos += 4
            if clen > 10000 or pos + clen > len(data):
                break
            comment = data[pos:pos + clen].decode('utf-8', errors='replace')
            result["comments"].append(comment)
            pos += clen

            # Shubhali kontent tekshirish
            cl = comment.lower()
            if any(kw in cl for kw in ['http', 'password', 'token', 'key=', 'secret', 'cmd=', 'exec']):
                result["suspicious"].append(comment[:200])
    except Exception:
        pass
    return result


def _ogg_file_info(data: bytes) -> dict:
    """OGG fayl haqida asosiy ma'lumotlar."""
    info = {"pages": 0, "is_valid": False, "has_audio": False}
    pos = 0
    page_count = 0
    while pos + 27 <= len(data):
        if data[pos:pos + 4] != OGG_MAGIC:
            break
        # OGG page header
        if pos + 27 > len(data):
            break
        segments = data[pos + 26]
        seg_table_end = pos + 27 + segments
        if seg_table_end > len(data):
            break
        seg_sizes = list(data[pos + 27: seg_table_end])
        page_size = sum(seg_sizes)
        page_count += 1
        # Vorbis header tekshiruvi
        payload_start = seg_table_end
        if payload_start < len(data) and data[payload_start:payload_start + 7] == VORBIS_MAGIC:
            info["has_audio"] = True
        pos = seg_table_end + page_size
        if page_count > 100:
            break
    info["pages"] = page_count
    info["is_valid"] = page_count > 0
    return info


async def analyze_ogg(file_path: str) -> tuple:
    """
    OGG/audio faylni tahlil qiladi.
    Qaytaradi: (report_text, risk_score)
    """
    loop = asyncio.get_event_loop()
    try:
        file_size = os.path.getsize(file_path)
        file_size_kb = file_size / 1024

        with open(file_path, 'rb') as f:
            data = f.read()

        score = 0
        findings = []

        # 1. Magic bytes tekshiruvi
        is_ogg = data[:4] == OGG_MAGIC
        if not is_ogg:
            score += 60
            magic_hex = data[:4].hex().upper()
            findings.append(f"🔴 Fayl OGG EMAS! Magic bytes: `{magic_hex}` — yashirin fayl bo'lishi mumkin!")
            # Haqiqiy fayl turini aniqlash
            known_magic = {
                b'PK\x03\x04': "ZIP/APK/JAR",
                b'MZ': "Windows EXE",
                b'\x7fELF': "Linux ELF (Linux bajariladigan fayl)",
                b'\xca\xfe\xba\xbe': "Java CLASS fayl",
                b'%PDF': "PDF",
                b'\xff\xd8\xff': "JPEG rasm",
                b'\x89PNG': "PNG rasm",
            }
            for magic, name in known_magic.items():
                if data[:len(magic)] == magic:
                    findings.append(f"🔴 Haqiqiy format: **{name}** — OGG deb niqoblangan!")
                    score += 30
                    break

        # 2. OGG sahifalarini parse qilish
        ogg_info = {}
        comments_data = {}
        if is_ogg:
            ogg_info  = await loop.run_in_executor(executor, _ogg_file_info, data)
            comments_data = await loop.run_in_executor(executor, _parse_ogg_comments, data)

            if not ogg_info.get("is_valid"):
                score += 20
                findings.append("🟠 OGG format noto'g'ri tuzilgan — shubhali")

            if not ogg_info.get("has_audio"):
                score += 25
                findings.append("🔴 OGG da audio ma'lumot topilmadi — niqoblangan fayl bo'lishi mumkin!")

            # 3. Vorbis metadata tekshiruvi
            if comments_data.get("suspicious"):
                score += 40
                for s in comments_data["suspicious"][:3]:
                    findings.append(f"🔴 Metadatada shubhali ma'lumot: `{s[:100]}`")

            # URL yoki kod metadata da bormi
            for comment in comments_data.get("comments", []):
                if _re.search(r'https?://', comment):
                    score += 20
                    findings.append(f"🟠 Metadatada havola: `{comment[:100]}`")
                    break

        # 4. Hajm anomaliyasi (64kbps OGG → ~8KB/s)
        # Agar sahifalar ko'p bo'lsa, lekin hajm g'alati bo'lsa
        if is_ogg and ogg_info.get("pages", 0) > 0:
            pages = ogg_info["pages"]
            expected_max_kb = pages * 10  # taxminan
            if file_size_kb > expected_max_kb * 50:
                score += 15
                findings.append(f"🟡 Fayl hajmi g'alati katta: {file_size_kb:.0f} KB / {pages} sahifa")

        # 5. Hash va VirusTotal
        sha256 = hashlib.sha256(data).hexdigest()
        md5    = hashlib.md5(data).hexdigest()
        vt = await loop.run_in_executor(executor, _check_vt_hash, sha256)

        if vt.get("available") and not vt.get("not_found"):
            mal = vt.get("malicious", 0)
            if mal > 0:
                score += min(mal * 6, 40)
                findings.append(f"🔴 VirusTotal: {mal}/{vt.get('total', 0)} engine xavfli dedi")
            vt_str = f"{'🔴' if mal > 0 else '🟢'} {mal}/{vt.get('total', 0)} engine"
        elif vt.get("not_found"):
            vt_str = "🟡 Bazada topilmadi"
        else:
            vt_str = "⚪ API kalit yo'q"

        score = min(score, 100)
        label = risk_label(score)

        # Metadata bloki
        meta_block = ""
        if comments_data:
            vendor = comments_data.get("vendor", "")
            comments = comments_data.get("comments", [])
            if vendor:
                meta_block += f"🎙 *Encoder:* `{vendor[:80]}`\n"
            if comments:
                clean = [c for c in comments if not any(kw in c.lower()
                         for kw in ['http', 'password', 'token', 'secret'])]
                if clean:
                    meta_block += "🏷 *Metadata:*\n" + "\n".join(f"  `{c[:80]}`" for c in clean[:5]) + "\n"

        report = (
            f"🎵 *OGG/Audio Fayl Tahlil Hisoboti*\n\n"
            f"📏 *Hajm:* {file_size_kb:.1f} KB\n"
            f"{'✅ Haqiqiy OGG' if is_ogg else '❌ OGG EMAS'}"
            + (f" · {ogg_info.get('pages', 0)} sahifa" if is_ogg and ogg_info else "")
            + f"\n"
            f"🔑 *MD5:* `{md5}`\n"
            f"🔑 *SHA256:* `{sha256[:32]}...`\n"
            f"🦠 *VirusTotal:* {vt_str}\n"
            f"{meta_block}\n"
            f"📊 *Xavf darajasi: {score}/100 — {label}*"
        )

        if findings:
            findings_block = "\n\n*⚠️ Topilgan muammolar:*\n" + "\n".join(findings[:10])
            if len(report) + len(findings_block) < 4000:
                report += findings_block

        return report, score

    except Exception as e:
        return f"❌ OGG tahlil xatosi: {e}", 0
