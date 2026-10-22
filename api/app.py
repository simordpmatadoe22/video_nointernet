
import os
import re
import hmac
import hashlib
import requests

from flask import Flask, request
from urllib.parse import urlparse, urljoin

app = Flask(__name__)

# =====================================
# CONFIGURATION
# =====================================

PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]
APP_SECRET = os.environ["APP_SECRET"]

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")

GRAPH_URL = (
    f"https://graph.facebook.com/"
    f"{GRAPH_VERSION}/me/messages"
)

# =====================================
# HTTP SESSION
# =====================================

session = requests.Session()

session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; "
        "DevHilal Build/HUAWEIDRA-Hilal; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Version/4.0 Chrome/88.0.4324.93 "
        "Mobile Safari/537.36 "
        "[FBAN/EMA;FBLC/ar_AR;FB_WAB/368.0.0.5.95]"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    )
})

# =====================================
# VERIFY META SIGNATURE
# =====================================

def verify_signature(body, signature):
    if not signature or not signature.startswith("sha256="):
        return False

    expected = hmac.new(
        APP_SECRET.encode("utf-8"),
        body,
        hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(
        expected,
        signature.split("=", 1)[1]
    )


# =====================================
# SEND MESSENGER MESSAGE
# =====================================

def send_text(psid, text):
    try:
        response = requests.post(
            GRAPH_URL,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": psid},
                "messaging_type": "RESPONSE",
                "message": {"text": text}
            },
            timeout=15
        )

        if not response.ok:
            print(
                "Messenger API error:",
                response.status_code,
                response.text[:300]
            )
            return False

        return True

    except requests.RequestException as exc:
        print("Send error:", str(exc))
        return False


# =====================================
# FACEBOOK URL VALIDATION
# =====================================

ALLOWED_HOSTS = {
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "web.facebook.com",
    "fb.com",
    "www.fb.com",
    "fb.watch",
    "www.fb.watch"
}


def is_facebook_url(url):
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme == "https"
            and parsed.hostname in ALLOWED_HOSTS
            and parsed.username is None
            and parsed.password is None
        )

    except (ValueError, AttributeError):
        return False


def extract_facebook_link(text):
    if not text:
        return None

    pattern = (
        r'https?://(?:www\.|m\.|web\.)?facebook\.com/[^\s<>"\']+'
        r'|https?://(?:www\.)?fb\.watch/[^\s<>"\']+'
        r'|https?://(?:www\.)?fb\.com/[^\s<>"\']+'
    )

    match = re.search(pattern, text, re.IGNORECASE)

    if not match:
        return None

    url = match.group(0).rstrip(".,!?) ]}")

    if not is_facebook_url(url):
        return None

    return url


# =====================================
# TRANSFORM VIDEO URL
# =====================================

def transform_video_url(url):
    return (
        url.replace(
            "https://video.",
            "https://z-m-video."
        ).replace(
            "http://video.",
            "https://z-m-video."
        )
    )


# =====================================
# FIND browser_native_hd_url IN JSON
# =====================================

def find_browser_native_hd_url(data):
    if isinstance(data, dict):

        value = data.get("browser_native_hd_url")

        if isinstance(value, str):
            value = value.replace("\\/", "/")

            if value.startswith(("https://", "http://")):
                return value

        for child in data.values():
            result = find_browser_native_hd_url(child)

            if result:
                return result

    elif isinstance(data, list):

        for child in data:
            result = find_browser_native_hd_url(child)

            if result:
                return result

    return None


# =====================================
# GET FACEBOOK PAGE SAFELY
# =====================================

def get_facebook_page(url):
    current_url = url

    # Follow only a limited number of Facebook redirects.
    for _ in range(6):

        if not is_facebook_url(current_url):
            raise ValueError("Redirect خارج نطاق Facebook")

        response = session.get(
            current_url,
            timeout=15,
            allow_redirects=False,
            stream=True
        )

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            response.close()

            if not location:
                raise ValueError("Redirect بلا Location")

            current_url = urljoin(current_url, location)
            continue

        try:
            response.raise_for_status()

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if not (
                "text/html" in content_type
                or "json" in content_type
                or "text/plain" in content_type
            ):
                raise ValueError("نوع الاستجابة غير مدعوم")

            # Prevent downloading an unexpectedly large page.
            chunks = []
            total = 0
            limit = 5 * 1024 * 1024

            for chunk in response.iter_content(16384):
                if not chunk:
                    continue

                total += len(chunk)

                if total > limit:
                    raise ValueError("استجابة Facebook كبيرة بزاف")

                chunks.append(chunk)

            body = b"".join(chunks)

            return (
                body.decode("utf-8", errors="replace"),
                content_type
            )

        finally:
            response.close()

    raise ValueError("عدد التحويلات تجاوز الحد")


# =====================================
# EXTRACT DIRECT VIDEO URL
# =====================================

def extract_video_url(facebook_url):
    try:
        html, content_type = get_facebook_page(facebook_url)

        # Try JSON first.
        if "json" in content_type:
            try:
                result = find_browser_native_hd_url(
                    requests.models.complexjson.loads(html)
                )

                if result:
                    return transform_video_url(result)

            except (ValueError, TypeError):
                pass

        # Search the HTML / embedded JSON.
        patterns = [
            r'"browser_native_hd_url"\s*:\s*"((?:\\.|[^"\\])*)"',
            r'\\?"browser_native_hd_url\\?"\s*:\s*\\?"([^"\\]+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, html)

            if not match:
                continue

            video_url = match.group(1)

            video_url = (
                video_url
                .replace("\\/", "/")
                .replace("\\u0025", "%")
                .replace("\\u0026", "&")
                .replace("&amp;", "&")
            )

            if video_url.startswith(("https://", "http://")):
                return transform_video_url(video_url)

        print("browser_native_hd_url not found")
        return None

    except (
        requests.RequestException,
        ValueError
    ) as exc:
        print("Extraction error:", str(exc))
        return None


# =====================================
# PROCESS INCOMING MESSAGE
# =====================================

def process_message(event):
    sender = event.get("sender", {}).get("id")
    message = event.get("message", {})

    if not sender or message.get("is_echo"):
        return

    # Ignore messages without text or attachments.
    text = message.get("text", "")

    # 1. Normal Facebook link sent in Messenger.
    facebook_url = extract_facebook_link(text)

    if facebook_url:
        send_text(
            sender,
            "⏳ توصلت بالرابط، كنقلب على رابط الفيديو..."
        )

        direct_url = extract_video_url(facebook_url)

        if direct_url:
            send_text(
                sender,
                "✅ رابط الفيديو:\n\n" + direct_url
            )
        else:
            send_text(
                sender,
                "❌ ما لقيتش browser_native_hd_url.\n\n"
                "ممكن المنشور خاص أو Facebook رجع "
                "صفحة تسجيل الدخول أو ما كاينش رابط HD متاح."
            )

        return

    # 2. Video attachment.
    for attachment in message.get("attachments", []):
        if attachment.get("type") == "video":
            payload = attachment.get("payload", {})
            media_url = payload.get("url", "")

            if media_url.startswith("https://"):
                send_text(
                    sender,
                    "🎬 توصلت بمرفق فيديو.\n\n"
                    "رابط المرفق:\n" + media_url
                    + "\n\nهاد الرابط ماشي بالضرورة رابط "
                    "MP4 الأصلي ديال المنشور."
                )
            else:
                send_text(
                    sender,
                    "توصلت بالفيديو، ولكن Meta ما وفرتش "
                    "رابط مرفق قابل للاستعمال."
                )

            return

    # 3. Other messages.
    if text.strip():
        send_text(
            sender,
            "👋 صيفط رابط Facebook أو شارك فيديو مع الصفحة."
        )


# =====================================
# VERCEL ROUTES
# =====================================

@app.route("/", methods=["GET"])
def home():
    return "Facebook Video Bot is running", 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "", 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.get_data()

    signature = request.headers.get(
        "X-Hub-Signature-256", ""
    )

    if not verify_signature(body, signature):
        return "Invalid signature", 403

    data = request.get_json(silent=True) or {}

    if data.get("object") != "page":
        return "Ignored", 200

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            try:
                process_message(event)
            except Exception as exc:
                print("Message processing error:", str(exc))

    return "EVENT_RECEIVED", 200
