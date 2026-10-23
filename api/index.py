
import os
import re
import json
import hmac
import hashlib
import logging
import html
from urllib.parse import urlparse, urljoin

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# =========================
# إعدادات Meta
# =========================


PAGE_ACCESS_TOKEN = "EAATLbkq5LgwBS75x08zMQMhuyOQZCvQ2KVK8ciKcZBJfjlkuZC7Uo9V9R3I4r17tIFvRL6aHW2GWZAVTWQYaidkHXAanZCopUcVQZCenBq6FRfpm4WyGiDNmdRA3TXuGtZBSSvESUHqXcTZAYmGCup7IAEYXO5CAcUD5S2S1jbyHGYk5KrZAyVQvxZAaPyiblXxes784z7cAZDZD"
VERIFY_TOKEN = "ABCD1234"
APP_SECRET = "e3fa1ca2e5e3ad34dd6624bc3d3f1979"

GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v23.0")

GRAPH_MESSAGES_URL = (
    f"https://graph.facebook.com/{GRAPH_VERSION}/me/messages"
)

# =========================
# Headers ديال Facebook
# =========================

FB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; DevHilal Build/HUAWEIDRA-Hilal; "
        "wv) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Version/4.0 Chrome/88.0.4324.93 Mobile Safari/537.36 "
        "[FBAN/EMA;FBLC/ar_AR;FB_WAB/368.0.0.5.95]"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
}

session = requests.Session()
session.headers.update(FB_HEADERS)

# =========================
# التحقق من الدومينات
# =========================

FACEBOOK_HOSTS = (
    "facebook.com",
    "fb.watch",
)

def host_is(host, allowed_domains):
    host = (host or "").lower().rstrip(".")

    return any(
        host == domain or host.endswith("." + domain)
        for domain in allowed_domains
    )


def is_facebook_url(url):
    try:
        parsed = urlparse(url)

        return (
            parsed.scheme in ("http", "https")
            and host_is(parsed.hostname, FACEBOOK_HOSTS)
        )
    except Exception:
        return False


def is_video_cdn_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()

        return (
            parsed.scheme in ("http", "https")
            and (
                host_is(host, ("fbcdn.net",))
                or host.startswith("video.")
                or host.startswith("video-")
                or host.startswith("z-m-video.")
                or host.startswith("z-m-video-")
            )
            and (
                path.endswith(".mp4")
                or "video" in host
                or "z-m-video" in host
            )
        )
    except Exception:
        return False


# =========================
# تبديل صيغة رابط الفيديو
# =========================

def transform_video_url(url):
    if not isinstance(url, str):
        return url

    url = url.strip()

    # video.example.com  -> z-m-video.example.com
    # video-iad6-1.xx...  -> z-m-video-iad6-1.xx...
    # كيحافظ على المسار و query parameters

    return re.sub(
        r"^https?://video([.-])",
        r"https://z-m-video\1",
        url,
        count=1,
        flags=re.IGNORECASE,
    )


# =========================
# إرسال رسالة إلى Messenger
# =========================

def send_text(sender_id, text):
    if not sender_id:
        return False

    payload = {
        "recipient": {
            "id": str(sender_id)
        },
        "messaging_type": "RESPONSE",
        "message": {
            "text": str(text)[:2000]
        }
    }

    try:
        response = requests.post(
            GRAPH_MESSAGES_URL,
            params={"access_token": PAGE_ACCESS_TOKEN},
            json=payload,
            timeout=20,
        )

        if not response.ok:
            logging.error(
                "Messenger API error: %s",
                response.text[:1000],
            )
            return False

        return True

    except requests.RequestException:
        logging.exception("Failed to send Messenger message")
        return False


# =========================
# استخراج الروابط من payload
# =========================

def find_urls_in_payload(data):
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():

                key_lower = str(key).lower()

                if (
                    isinstance(value, str)
                    and (
                        "url" in key_lower
                        or "href" in key_lower
                        or "link" in key_lower
                    )
                ):
                    candidate = value.strip()

                    if candidate.startswith(("https://", "http://")):
                        found.append(candidate)

                walk(value)

        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)

    # حذف التكرار مع الحفاظ على الترتيب
    return list(dict.fromkeys(found))


def extract_urls_from_text(text_value):
    if not isinstance(text_value, str):
        return []

    urls = re.findall(r"https?://[^\s<>\"']+", text_value)

    cleaned = []

    for url in urls:
        url = url.rstrip(".,;!?)\u3002")

        if url:
            cleaned.append(url)

    return list(dict.fromkeys(cleaned))


# =========================
# جلب صفحة Facebook بأمان
# =========================

def fetch_facebook_page(url):
    current_url = url

    for _ in range(6):
        if not is_facebook_url(current_url):
            logging.warning("Rejected non-Facebook URL")
            return None

        response = None

        try:
            response = session.get(
                current_url,
                allow_redirects=False,
                stream=True,
                timeout=(10, 20),
            )

            # تتبع التحويلات يدويا مع التحقق من الدومين
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")

                if not location:
                    return None

                current_url = urljoin(current_url, location)
                continue

            if response.status_code != 200:
                logging.warning(
                    "Facebook returned status %s",
                    response.status_code,
                )
                return None

            content_type = response.headers.get(
                "Content-Type", ""
            ).lower()

            if not any(
                kind in content_type
                for kind in (
                    "text/html",
                    "application/json",
                    "text/plain",
                    "application/xhtml+xml",
                )
            ):
                return None

            # حد أقصى 5MB لتفادي تحميل صفحات ضخمة
            chunks = []
            total_size = 0
            max_size = 5 * 1024 * 1024

            for chunk in response.iter_content(65536):
                if not chunk:
                    continue

                total_size += len(chunk)

                if total_size > max_size:
                    logging.warning("Facebook response too large")
                    return None

                chunks.append(chunk)

            encoding = response.encoding or "utf-8"

            return b"".join(chunks).decode(
                encoding,
                errors="replace",
            )

        except requests.RequestException:
            logging.exception("Failed to fetch Facebook page")
            return None

        finally:
            if response is not None:
                response.close()

    return None


# =========================
# البحث عن browser_native_hd_url
# =========================

def find_hd_url_in_json(data):
    if isinstance(data, dict):
        for key, value in data.items():
            if (
                key == "browser_native_hd_url"
                and isinstance(value, str)
                and value.startswith(("https://", "http://"))
            ):
                return value

        for value in data.values():
            result = find_hd_url_in_json(value)

            if result:
                return result

    elif isinstance(data, list):
        for item in data:
            result = find_hd_url_in_json(item)

            if result:
                return result

    return None


def extract_hd_url_from_page(page_text):
    # أولا: محاولة قراءة JSON مباشرة
    try:
        data = json.loads(page_text)
        result = find_hd_url_in_json(data)

        if result:
            return html.unescape(result)
    except (ValueError, TypeError):
        pass

    # ثانيا: البحث داخل HTML أو JSON مضمّن
    pattern = (
        r'"browser_native_hd_url"\s*:\s*'
        r'"((?:\\.|[^"\\])*)"'
    )

    for match in re.finditer(pattern, page_text):
        raw_value = match.group(1)

        try:
            decoded = json.loads('"' + raw_value + '"')
        except (ValueError, TypeError):
            decoded = raw_value.replace(r"\/", "/")

        decoded = html.unescape(decoded)

        if decoded.startswith(("https://", "http://")):
            return decoded

    return None


def extract_video_url(facebook_url):
    if not is_facebook_url(facebook_url):
        return None

    page_text = fetch_facebook_page(facebook_url)

    if not page_text:
        return None

    video_url = extract_hd_url_from_page(page_text)

    if video_url:
        return transform_video_url(video_url)

    return None


# =========================
# معالجة الرسالة والمرفقات
# =========================

def handle_message(sender_id, message):
    if not isinstance(message, dict):
        return

    candidates = []
    attachment_candidates = []

    # 1. استخراج الروابط من نص الرسالة
    text_value = message.get("text", "") or ""

    candidates.extend(extract_urls_from_text(text_value))

    # 2. البحث في attachments و payload بالكامل
    attachments = message.get("attachments", []) or []

    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue

        payload = attachment.get("payload", {}) or {}
        attachment_type = str(
            attachment.get("type", "")
        ).lower()

        payload_urls = find_urls_in_payload(payload)

        # روابط المرفقات نعطيها الأولوية إذا كان المرفق فيديو
        if attachment_type == "video":
            attachment_candidates.extend(payload_urls)
        else:
            candidates.extend(payload_urls)

    # 3. حذف الروابط المكررة
    attachment_candidates = list(
        dict.fromkeys(attachment_candidates)
    )

    candidates = list(
        dict.fromkeys(attachment_candidates + candidates)
    )

    if not candidates:
        send_text(
            sender_id,
            "ما لقيتش رابط داخل الرسالة أو المرفق. "
            "تأكد أن Messenger صيفط رابط المنشور أو الفيديو."
        )
        return

    # 4. فحص الروابط
    for candidate in candidates:

        # رابط فيديو مباشر من CDN
        if is_video_cdn_url(candidate):
            result = transform_video_url(candidate)
            send_text(sender_id, result)
            return

        # رابط منشور أو فيديو من Facebook
        if is_facebook_url(candidate):
            result = extract_video_url(candidate)

            if result:
                send_text(sender_id, result)
                return

    send_text(
        sender_id,
        "لقيت رابط، ولكن ما قدرتش نستخرج رابط MP4 مباشر. "
        "ممكن Facebook ما أتاحش رابط الفيديو أو الصفحة كتحتاج تسجيل الدخول."
    )


def process_messaging_event(event):
    sender_id = (
        event.get("sender", {}).get("id")
        if isinstance(event.get("sender"), dict)
        else None
    )

    message = event.get("message")

    if not sender_id or not isinstance(message, dict):
        return

    # تجاهل الرسائل اللي الصفحة رسلاتها بنفسها
    if message.get("is_echo"):
        return

    handle_message(sender_id, message)


# =========================
# التحقق من توقيع Meta
# =========================

def verify_signature(raw_body):
    signature_256 = request.headers.get(
        "X-Hub-Signature-256", ""
    )

    if signature_256.startswith("sha256="):
        expected = hmac.new(
            APP_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        received = signature_256.split("=", 1)[1]

        return hmac.compare_digest(expected, received)

    # توافق مع توقيع SHA1 القديم
    signature_1 = request.headers.get(
        "X-Hub-Signature", ""
    )

    if signature_1.startswith("sha1="):
        expected = hmac.new(
            APP_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha1,
        ).hexdigest()

        received = signature_1.split("=", 1)[1]

        return hmac.compare_digest(expected, received)

    return False


# =========================
# Routes ديال Vercel
# =========================

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "running",
        "service": "Facebook Messenger Video Link Bot",
    })


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode", "")
    token = request.args.get("hub.verify_token", "")
    challenge = request.args.get("hub.challenge", "")

    if mode == "subscribe" and hmac.compare_digest(
        token, VERIFY_TOKEN
    ):
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data(cache=True)

    if not verify_signature(raw_body):
        logging.warning("Webhook signature verification failed")
        return "Invalid signature", 403

    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "Invalid JSON", 400

    if data.get("object") != "page":
        return "Unsupported object", 404

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            try:
                process_messaging_event(event)
            except Exception:
                logging.exception("Failed to process Messenger event")

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
