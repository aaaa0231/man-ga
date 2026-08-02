import re
import urllib.parse
import time
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
import httpx
from contextlib import asynccontextmanager

# -------------------------------------------------------
# 【設定】
# -------------------------------------------------------
SITES = {
    "mangarw": "https://mangarw.com",
    "soraraw": "https://soraraw.com",
}
DEFAULT_SITE = "mangarw"

AD_DOMAINS = [
    "universityshocksooner.com",
    "adexchangerapid.com",
    "platform.pubadx.one",
    "preferencenail.com",
    "gomuraw.js",
    "vntsm.com",
]

_SKIP_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", 
    "connection", "content-security-policy", "host"
}

_CACHE = {}
CACHE_TTL = 3600  # 1時間キャッシュ

# -------------------------------------------------------
# 【超強化版】ポップアップ・広告抹殺スクリプト
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
            if (onclickVal.includes('window.open') || onclickVal.includes('location')) {
                el.removeAttribute('onclick');
            }
        }
        if (el.tagName === 'A') el.removeAttribute('target');
    }
    ['click', 'touchstart', 'mousedown'].forEach(eventType => {
        document.addEventListener(eventType, function(e) {
            let target = e.target;
            while (target && target !== document.body) {
                sanitizeElement(target);
                const style = window.getComputedStyle(target);
                if ((style.position === 'fixed' || style.position === 'absolute') && parseInt(style.zIndex) > 500) {
                    if (target.offsetWidth > window.innerWidth * 0.8) {
                        e.stopPropagation(); e.preventDefault();
                        target.remove(); return false;
                    }
                }
                target = target.parentElement;
            }
        }, true);
    });
    setInterval(() => {
        document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
        document.querySelectorAll('[onclick]').forEach(el => sanitizeElement(el));
    }, 500);
})();
</script>
"""

# -------------------------------------------------------
# グローバルHTTPクライアント
# -------------------------------------------------------
client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=50, max_connections=100))

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await client.aclose()

app = FastAPI(lifespan=lifespan)

# -------------------------------------------------------
# URL・HTML書き換え関数
# -------------------------------------------------------
def _to_imgproxy(url: str) -> str:
    if "/imgproxy/" in url:
        return url
    for ad in AD_DOMAINS:
        if ad in url:
            return "about:blank"
    stripped = re.sub(r"^https?://", "", url)
    return f"/imgproxy/{stripped}"

def remove_ads(html: str) -> str:
    html = re.sub(r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*>', '', html, flags=re.IGNORECASE)
    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
        html = re.sub(r"<script[^>]*" + escaped + r"[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<iframe[^>]*" + escaped + r"[^>]*>.*?</iframe>", "", html, flags=re.IGNORECASE | re.DOTALL)
    
    if "<head>" in html.lower():
        html = re.sub(r'(<head>)', rf'\1\n{ANTI_REDIRECT_SCRIPT}', html, count=1, flags=re.IGNORECASE)
    else:
        html = ANTI_REDIRECT_SCRIPT + html
    return html

def rewrite_html(html: str, site_key: str, origin: str) -> str:
    # リンク書き換え
    escaped_origin = re.escape(origin)
    html = re.sub(escaped_origin, f"/{site_key}", html, flags=re.IGNORECASE)
    
    def rewrite_href(m):
        quote, path = m.group(1), m.group(2)
        if any(path.startswith(p) for p in ["//", "http:", "https:", "javascript:", "data:", "#"]): return m.group(0)
        if path.startswith(f"/{site_key}/") or path == f"/{site_key}": return m.group(0)
        return f"href={quote}/{site_key}{path}{quote}"
    html = re.sub(r'href=(["\'])(/[^"\']*)\1', rewrite_href, html, flags=re.IGNORECASE)
    
    # 画像書き換え
    def rewrite_src(m):
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        if url.startswith("data:") or "/imgproxy/" in url: return m.group(0)
        return f"{attr}={quote}{_to_imgproxy(url)}{quote}"
    
    html = re.sub(r'(src|data-src|data-lazy-src)=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|avif)[^\s"\']*)\2', rewrite_src, html, flags=re.IGNORECASE)
    
    def rewrite_srcset(m):
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        new_val = re.sub(r"(https?://[^\s,]+)", lambda x: _to_imgproxy(x.group(1)), val)
        return f"{attr}={quote}{new_val}{quote}"
    html = re.sub(r'((?:data-)?srcset)=(["\'])([^"\']+)\2', rewrite_srcset, html, flags=re.IGNORECASE)
    
    return html

# -------------------------------------------------------
# メインのルーティング
# -------------------------------------------------------
@app.get("/")
async def root():
    return RedirectResponse(f"/{DEFAULT_SITE}/")

@app.get("/imgproxy/{image_url:path}")
async def imgproxy(image_url: str):
    if any(ad in image_url for ad in AD_DOMAINS):
        return Response("Blocked", status_code=404)
        
    image_url = image_url.replace("https%3A//", "https://").replace("http%3A//", "http://")
    if not image_url.startswith("http"):
        image_url = "https://" + image_url

    # ★ ゼロトラフィック・Google公式プロキシ
    encoded_url = urllib.parse.quote(image_url, safe='')
    google_url = f"https://images1-focus-opensocial.googleusercontent.com/gadgets/proxy?container=focus&refresh=31536000&url={encoded_url}"
    
    return RedirectResponse(google_url)

@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy(request: Request, path: str):
    segments = path.split("/", 1)
    first_seg = segments[0]

    if first_seg in SITES:
        site_key = first_seg
        target_path = "/" + segments[1] if len(segments) > 1 else "/"
    else:
        site_key = DEFAULT_SITE
        target_path = "/" + path

    origin = SITES[site_key]
    query = request.url.query
    target_url = f"{origin}{target_path}" + (f"?{query}" if query else "")

    # キャッシュ確認
    cache_key = f"{request.method}:{target_url}"
    if request.method == "GET" and cache_key in _CACHE:
        timestamp, c_content, c_status, c_headers = _CACHE[cache_key]
        if time.time() - timestamp < CACHE_TTL:
            return Response(content=c_content, status_code=c_status, headers=c_headers)

    # リクエストヘッダー構築
    req_headers = {}
    for k, v in request.headers.items():
        if k.lower() not in _SKIP_HEADERS:
            req_headers[k] = v
    req_headers["Host"] = urllib.parse.urlparse(origin).netloc
    req_headers["Referer"] = f"{origin}/"
    req_headers["Accept-Encoding"] = "gzip, deflate" # Brotli除外でパースエラー回避

    body = await request.body() if request.method in ["POST", "PUT"] else None

    try:
        res = await client.request(
            method=request.method,
            url=target_url,
            headers=req_headers,
            content=body,
            follow_redirects=False,
            timeout=15.0
        )
    except Exception as e:
        return Response(f"Proxy Error: {str(e)}", status_code=502)

    # リダイレクト処理
    if res.status_code in (301, 302, 303, 307, 308):
        loc = res.headers.get("location", "")
        if any(ad in loc for ad in AD_DOMAINS):
            return RedirectResponse(f"/{site_key}/")
        if loc.startswith("http"):
            loc = loc.replace(origin, f"/{site_key}")
        elif loc.startswith("/") and not loc.startswith(f"/{site_key}/"):
            loc = f"/{site_key}{loc}"
        return RedirectResponse(loc, status_code=res.status_code)

    # レスポンスヘッダーの整理
    res_headers = {}
    for k, v in res.headers.items():
        if k.lower() not in _SKIP_HEADERS:
            res_headers[k] = v

    content_type = res.headers.get("content-type", "")

    # HTMLの処理
    if "text/html" in content_type.lower():
        html = res.text
        html = remove_ads(html)
        html = rewrite_html(html, site_key, origin)
        encoded_html = html.encode("utf-8")
        
        if request.method == "GET" and res.status_code == 200:
            _CACHE[cache_key] = (time.time(), encoded_html, res.status_code, res_headers)
            
        return Response(content=encoded_html, status_code=res.status_code, headers=res_headers, media_type="text/html")

    # その他のファイル（そのままパス）
    if request.method == "GET" and res.status_code == 200:
        _CACHE[cache_key] = (time.time(), res.content, res.status_code, res_headers)
        
    return Response(content=res.content, status_code=res.status_code, headers=res_headers)
