import re
import urllib.parse
from pyodide.http import pyfetch
from js import Response, Headers, Uint8Array
import time

# -------------------------------------------------------
# 【キャッシュ設定】（Workerのオンメモリキャッシュ）
# -------------------------------------------------------
_CACHE = {}
CACHE_TTL = 3600  # 1時間

# -------------------------------------------------------
# 【マルチサイト設定】
# -------------------------------------------------------
SITES = {
    "mangarw": "https://mangarw.com",
    "soraraw": "https://soraraw.com",
}
DEFAULT_SITE = "mangarw"

# -------------------------------------------------------
# 広告・リダイレクト用ドメインリスト
# -------------------------------------------------------
AD_DOMAINS = [
    "universityshocksooner.com",
    "adexchangerapid.com",
    "platform.pubadx.one",
    "preferencenail.com",
    "gomuraw.js",
    "vntsm.com",
]

_SKIP_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "content-security-policy",
}

# -------------------------------------------------------
# 【超強化版】リダイレクト・ポップアップ完全抹殺スクリプト
# -------------------------------------------------------
ANTI_REDIRECT_SCRIPT = """
<style>
[class*="ad-"], [class*="ad_"], [id*="ad-"], [id*="ad_"],
[class*="banner"], [id*="banner"], [class*="pop-"], [class*="popup"],
div[style*="z-index: 2147483647"], div[style*="z-index: 99999"],
iframe[src*="about:blank"], iframe:not([src]) {
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
}
</style>
<script>
(function() {
    'use strict';
    window.open = function() { return null; };
    try {
        let isUserAction = false;
        document.addEventListener('click', (e) => {
            const a = e.target.closest('a');
            if (a && a.href && !a.href.includes('javascript:')) isUserAction = true;
        }, true);
    } catch(e) {}

    const originalCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName, options) {
        const el = originalCreateElement(tagName, options);
        if (tagName.toLowerCase() === 'script') {
            const originalSetAttribute = el.setAttribute.bind(el);
            el.setAttribute = function(name, value) {
                if (name.toLowerCase() === 'src' && value.includes('http') && !value.includes(location.host)) {
                    return;
                }
                return originalSetAttribute(name, value);
            };
        }
        return el;
    };

    function sanitizeElement(el) {
        if (!el) return;
        if (el.getAttribute && el.getAttribute('onclick')) {
            const onclickVal = el.getAttribute('onclick');
            if (onclickVal.includes('window.open') || onclickVal.includes('location') || onclickVal.includes('http')) {
                el.removeAttribute('onclick');
            }
        }
        if (el.tagName === 'A') el.removeAttribute('target');
    }

    ['click', 'touchstart', 'touchend', 'mousedown', 'mouseup'].forEach(eventType => {
        document.addEventListener(eventType, function(e) {
            let target = e.target;
            while (target && target !== document.body) {
                sanitizeElement(target);
                const style = window.getComputedStyle(target);
                if ((style.position === 'fixed' || style.position === 'absolute') &&
                    (parseInt(style.zIndex) > 500) &&
                    !target.querySelector('img') && !target.querySelector('video') &&
                    target.tagName !== 'A' && target.tagName !== 'BUTTON') {
                    if (target.offsetWidth > window.innerWidth * 0.8 && target.offsetHeight > window.innerHeight * 0.8) {
                        e.stopPropagation();
                        e.preventDefault();
                        target.remove();
                        return false;
                    }
                }
                target = target.parentElement;
            }
        }, true);
    });

    function cleanUp() {
        document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
        document.querySelectorAll('[onclick]').forEach(el => sanitizeElement(el));
    }
    document.addEventListener('DOMContentLoaded', () => {
        cleanUp();
        setInterval(cleanUp, 300);
    });
})();
</script>
"""

# -------------------------------------------------------
# URL・リンク書き換え処理
# -------------------------------------------------------
_SRCSET_ENTRY_RE = re.compile(r"(https?://[^\s,]+)", re.IGNORECASE)
_CSS_URL_RE = re.compile(
    r'url\(\s*(["\']?)(https?://[^\s"\')\]>]+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\')\]>]*)\1\s*\)',
    re.IGNORECASE,
)

def _to_imgproxy(url: str) -> str:
    if "/imgproxy/" in url:
        return url
    for ad_domain in AD_DOMAINS:
        if ad_domain in url:
            return "about:blank"
    stripped = re.sub(r"^https?://", "", url)
    return f"/imgproxy/{stripped}"

def _rewrite_srcset(srcset: str) -> str:
    def replace_url(m: re.Match) -> str:
        return _to_imgproxy(m.group(1))
    return _SRCSET_ENTRY_RE.sub(replace_url, srcset)

def remove_ads(html: str) -> str:
    html = re.sub(r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*>', '', html, flags=re.IGNORECASE)
    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
        html = re.sub(r"<script[^>]*" + escaped + r"[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<iframe[^>]*" + escaped + r"[^>]*>.*?</iframe>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<a[^>]*" + escaped + r"[^>]*>.*?</a>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'onclick=["\'][^"\']*(?:window\.open|location\.href|location\.assign)[^"\']*["\']', '', html, flags=re.IGNORECASE)
    
    if "<head>" in html:
        html = html.replace("<head>", f"<head>\n{ANTI_REDIRECT_SCRIPT}", 1)
    elif "<HEAD>" in html:
        html = html.replace("<HEAD>", f"<HEAD>\n{ANTI_REDIRECT_SCRIPT}", 1)
    else:
        html = ANTI_REDIRECT_SCRIPT + html
    return html

def rewrite_site_links(html: str, site_key: str, origin: str) -> str:
    escaped_origin = re.escape(origin)
    html = re.sub(escaped_origin, f"/{site_key}", html, flags=re.IGNORECASE)
    def rewrite_href(m: re.Match) -> str:
        quote, path = m.group(1), m.group(2)
        if any(path.startswith(prefix) for prefix in ["//", "http:", "https:", "javascript:", "data:", "#"]):
            return m.group(0)
        if path.startswith(f"/{site_key}/") or path == f"/{site_key}":
            return m.group(0)
        return f"href={quote}/{site_key}{path}{quote}"
    html = re.sub(r'href=(["\'])(/[^"\']*)\1', rewrite_href, html, flags=re.IGNORECASE)
    return html

def rewrite_img_urls(html: str) -> str:
    def rewrite_src(m: re.Match) -> str:
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        if url.startswith("data:") or "/imgproxy/" in url:
            return m.group(0)
        return f"{attr}={quote}{_to_imgproxy(url)}{quote}"
    html = re.sub(r'(src)=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2', rewrite_src, html, flags=re.IGNORECASE)
    html = re.sub(r'(data-(?:src|lazy-src|original|bg))=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2', rewrite_src, html, flags=re.IGNORECASE)
    def rewrite_srcset_attr(m: re.Match) -> str:
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        return f"{attr}={quote}{_rewrite_srcset(val)}{quote}"
    html = re.sub(r'((?:data-)?srcset)=(["\'])([^"\']+)\2', rewrite_srcset_attr, html, flags=re.IGNORECASE)
    def rewrite_css_url(m: re.Match) -> str:
        quote, url = m.group(1), m.group(2)
        if url.startswith("data:") or "/imgproxy/" in url:
            return m.group(0)
        return f"url({quote}{_to_imgproxy(url)}{quote})"
    html = _CSS_URL_RE.sub(rewrite_css_url, html)
    return html

# -------------------------------------------------------
# /imgproxy/{image_url} (Google公式プロキシへのリダイレクト)
# -------------------------------------------------------
async def handle_imgproxy(image_url: str, request):
    for ad_domain in AD_DOMAINS:
        if ad_domain in image_url:
            return Response.new("Blocked Ad Image", status=404)

    image_url = image_url.replace("https%3A//", "https://").replace("http%3A//", "http://")
    if not image_url.startswith("http"):
        image_url = "https://" + image_url

    # ★通信量ゼロ・負荷ゼロのGoogleプロキシサーバーへ案内（MDM回避）
    encoded_url = urllib.parse.quote(image_url, safe='')
    google_proxy_url = f"https://images1-focus-opensocial.googleusercontent.com/gadgets/proxy?container=focus&refresh=31536000&url={encoded_url}"

    out_headers = Headers.new()
    out_headers.set("Location", google_proxy_url)
    out_headers.set("Access-Control-Allow-Origin", "*")
    out_headers.set("Cache-Control", "public, max-age=3600")
    
    return Response.new("", status=302, headers=out_headers)

# -------------------------------------------------------
# メインエントリーポイント (Fetchイベントハンドラ)
# -------------------------------------------------------
async def on_fetch(request, env):
    url_str = request.url
    parsed = urllib.parse.urlparse(url_str)
    path = parsed.path

    # ルートパス
    if path == "/":
        out_headers = Headers.new()
        out_headers.set("Location", f"/{DEFAULT_SITE}/")
        return Response.new("", status=302, headers=out_headers)

    # 画像プロキシ
    if path.startswith("/imgproxy/"):
        image_target = path[len("/imgproxy/"):]
        return await handle_imgproxy(image_target, request)

    # パスの解析
    raw_path = path.lstrip("/")
    segments = raw_path.split("/", 1)
    first_seg = segments[0]

    if first_seg in SITES:
        site_key = first_seg
        target_path = "/" + segments[1] if len(segments) > 1 else "/"
    else:
        site_key = DEFAULT_SITE
        client_referer = request.headers.get("referer") or ""
        for key in SITES:
            if f"/{key}/" in client_referer or client_referer.endswith(f"/{key}"):
                site_key = key
                break
        target_path = "/" + raw_path if raw_path else "/"

    origin = SITES[site_key]
    target_url = f"{origin}{target_path}"
    if parsed.query:
        target_url += f"?{parsed.query}"

    # キャッシュチェック
    cache_key = f"{request.method}:{target_url}"
    if request.method == "GET" and cache_key in _CACHE:
        timestamp, c_content, c_status, c_headers = _CACHE[cache_key]
        if time.time() - timestamp < CACHE_TTL:
            out_headers = Headers.new()
            for k, v in c_headers.items():
                out_headers.set(k, v)
            return Response.new(c_content, status=c_status, headers=out_headers)

    # Refererの偽装
    client_referer = request.headers.get("referer") or ""
    client_host = request.headers.get("host") or ""
    if client_referer:
        fake_referer = (
            client_referer.replace(f"https://{client_host}/{site_key}", origin)
            .replace(f"http://{client_host}/{site_key}", origin)
            .replace(f"https://{client_host}", origin)
            .replace(f"http://{client_host}", origin)
        )
    else:
        fake_referer = f"{origin}/"

    # プロキシヘッダーの構築
    headers_dict = {
        "Host": origin.replace("https://", "").replace("http://", ""),
        "X-Forwarded-Host": client_host,
        "X-Forwarded-Proto": "https",
        "User-Agent": request.headers.get("user-agent") or "Mozilla/5.0",
        "Accept": request.headers.get("accept") or "*/*",
        "Accept-Language": request.headers.get("accept-language") or "ja,en-US;q=0.9,en;q=0.8",
        "Cookie": request.headers.get("cookie") or "",
        "Referer": fake_referer,
        "Origin": origin,
        "Accept-Encoding": "gzip, deflate", # ★Brotliをブロックして解凍エラーを防ぐ
    }

    for header_name in ["content-type", "x-requested-with", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"]:
        val = request.headers.get(header_name)
        if val:
            headers_dict[header_name.title()] = val

    method = request.method
    body = None
    if method not in ("GET", "HEAD"):
        body = await request.arrayBuffer()

    try:
        # ターゲットサイトへのリクエスト
        res = await pyfetch(target_url, method=method, headers=headers_dict, body=body)
        
        # レスポンスヘッダーの構築
        out_headers = Headers.new()
        cached_headers = {}
        for k, v in res.headers.entries():
            if k.lower() not in _SKIP_HEADERS:
                out_headers.set(k, v)
                cached_headers[k] = v
        out_headers.set("Access-Control-Allow-Origin", "*")
        out_headers.set("Access-Control-Allow-Credentials", "true")
        out_headers.set("Cache-Control", "public, max-age=3600")
        cached_headers["Access-Control-Allow-Origin"] = "*"
        cached_headers["Cache-Control"] = "public, max-age=3600"

        # リダイレクトの処理
        if res.status in (301, 302, 303, 307, 308):
            loc = res.headers.get("location") or ""
            if loc:
                if any(ad in loc for ad in AD_DOMAINS):
                    out_headers.set("Location", f"/{site_key}/")
                    return Response.new("", status=302, headers=out_headers)
                if loc.startswith("http"):
                    loc = loc.replace(origin, f"/{site_key}")
                if loc.startswith("/"):
                    if not loc.startswith(f"/{site_key}/"):
                        loc = f"/{site_key}{loc}"
                out_headers.set("Location", loc)
                return Response.new("", status=res.status, headers=out_headers)

        content_type = res.headers.get("content-type") or ""

        # HTMLの処理（広告除去、リンク書き換え）
        if "text/html" in content_type.lower():
            text = await res.text()
            html = remove_ads(text)
            html = rewrite_site_links(html, site_key, origin)
            html = rewrite_img_urls(html)
            encoded_html = html.encode("utf-8")
            
            if method == "GET" and res.status == 200:
                _CACHE[cache_key] = (time.time(), encoded_html, res.status, cached_headers)
                
            return Response.new(encoded_html, status=res.status, headers=out_headers)
        
        # HTML以外の処理（バイナリそのまま）
        else:
            data = await res.arrayBuffer()
            if method == "GET" and res.status == 200:
                _CACHE[cache_key] = (time.time(), data, res.status, cached_headers)
            return Response.new(data, status=res.status, headers=out_headers)

    except Exception as e:
        return Response.new(f"Worker Proxy Error: {str(e)}", status=502)
