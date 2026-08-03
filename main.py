import re
import time
import urllib.parse
import json
from contextlib import asynccontextmanager
from typing import Dict, Tuple

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse

_CACHE: Dict[str, Tuple[float, bytes, int, dict, str]] = {}
CACHE_TTL = 259200  # 3日間キャッシュ

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

class Core:
    http_client: httpx.AsyncClient | None = None

core = Core()

@asynccontextmanager
async def lifespan(app: FastAPI):
    core.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    yield
    await core.http_client.aclose()

app = FastAPI(lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_SKIP_HEADERS = {
    "content-encoding",
    "content-length",
    "transfer-encoding",
    "connection",
    "content-security-policy",
}

# -------------------------------------------------------
# 【修正版】必須プログラムは壊さず、ウザい広告だけを消すスクリプト
# -------------------------------------------------------
ANTI_REDIRECT_SCRIPT = """
<style>
/* 広告やポップアップを非表示にする */
[class*="ad-"], [class*="ad_"], [id*="ad-"], [id*="ad_"],
[class*="banner"], [id*="banner"], [class*="pop-"], [class*="popup"],
div[style*="z-index: 2147483647"], div[style*="z-index: 99999"],
iframe[src*="about:blank"], iframe:not([src]) {
    display: none !important;
    visibility: hidden !important;
    opacity: 0 !important;
    pointer-events: none !important;
}

/* 🌟 続きを読むボタンを保護（追加部分） */
#load-more-chapters, .load-more, .read-more {
    display: block !important; 
    visibility: visible !important; 
    opacity: 1 !important;
    background-color: #3b82f6 !important; 
    color: white !important;
    pointer-events: auto !important; /* 確実にクリックできるようにする */
    z-index: 999999 !important; /* 他の要素に埋もれないように最前面へ */
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

    function sanitizeElement(el) {
        if (!el) return;
        
        // ★ 保護したボタンのクリック機能は絶対に消さないように除外
        if (el.id === 'load-more-chapters' || el.classList?.contains('load-more') || el.classList?.contains('read-more')) {
            return; 
        }

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
                // ★ 保護したボタンならJSの強制削除処理をストップ
                if (target.id === 'load-more-chapters' || (target.classList && (target.classList.contains('load-more') || target.classList.contains('read-more')))) {
                    return; 
                }
                
                sanitizeElement(target);
                const style = window.getComputedStyle(target);
                if ((style.position === 'fixed' || style.position === 'absolute') &&
                    (parseInt(style.zIndex) > 500) &&
                    !target.querySelector('img') && !target.querySelector('canvas') &&
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

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
    });
})();
</script>
"""

_SRCSET_ENTRY_RE = re.compile(r"(https?://[^\s,]+)", re.IGNORECASE)
_CSS_URL_RE = re.compile(
    r'url\(\s*(["\']?)(https?://[^\s"\')\]>]+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\')\]>]*)\1\s*\)',
    re.IGNORECASE,
)

# -------------------------------------------------------
# 【強化版】Cloudflareを突破しやすいDuckDuckGoプロキシ
# -------------------------------------------------------
def _to_imgproxy(url: str) -> str:
    if "duckduckgo.com" in url or "googleusercontent.com" in url:
        return url
    for ad_domain in AD_DOMAINS:
        if ad_domain in url:
            return "about:blank"
    
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith("http"):
        url = "https://" + url.lstrip("/")
        
    encoded_url = urllib.parse.quote(url)
    
    # 🌟 直リンク禁止の突破力が高い DuckDuckGo の公式プロキシを使用
    duck_proxy = f"https://external-content.duckduckgo.com/iu/?u={encoded_url}"
    return duck_proxy

def _rewrite_srcset(srcset: str) -> str:
    def replace_url(m: re.Match) -> str:
        return _to_imgproxy(m.group(1))
    return _SRCSET_ENTRY_RE.sub(replace_url, srcset)

def remove_ads(html: str) -> str:
    html = re.sub(r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*>', '', html, flags=re.IGNORECASE)
    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
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
        if url.startswith("data:") or "duckduckgo.com" in url or "googleusercontent.com" in url:
            return m.group(0)
        return f"{attr}={quote}{_to_imgproxy(url)}{quote}"
    html = re.sub(r'(src)=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2', rewrite_src, html, flags=re.IGNORECASE)
    html = re.sub(r'(data-(?:src|lazy-src|original|bg|image))=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2', rewrite_src, html, flags=re.IGNORECASE)
    def rewrite_srcset_attr(m: re.Match) -> str:
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        return f"{attr}={quote}{_rewrite_srcset(val)}{quote}"
    html = re.sub(r'((?:data-)?srcset)=(["\'])([^"\']+)\2', rewrite_srcset_attr, html, flags=re.IGNORECASE)
    def rewrite_css_url(m: re.Match) -> str:
        quote, url = m.group(1), m.group(2)
        if url.startswith("data:") or "duckduckgo.com" in url or "googleusercontent.com" in url:
            return m.group(0)
        return f"url({quote}{_to_imgproxy(url)}{quote})"
    html = _CSS_URL_RE.sub(rewrite_css_url, html)
    return html

@app.get("/imgproxy/{image_url:path}")
async def imgproxy(image_url: str):
    for ad_domain in AD_DOMAINS:
        if ad_domain in image_url:
            return Response(status_code=404, content=b"Blocked Ad Image")

    image_url = image_url.replace("https%3A//", "https://").replace("http%3A//", "http://")
    if not image_url.startswith("http"):
        image_url = "https://" + image_url

    encoded_url = urllib.parse.quote(image_url)
    duck_proxy = f"https://external-content.duckduckgo.com/iu/?u={encoded_url}"
    return RedirectResponse(url=duck_proxy, status_code=302)

@app.get("/")
async def root():
    return RedirectResponse(url=f"/{DEFAULT_SITE}/")

@app.api_route("/{raw_path:path}", methods=["GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE"])
async def proxy(request: Request, raw_path: str):
    segments = raw_path.lstrip("/").split("/", 1)
    first_seg = segments[0]

    if first_seg in SITES:
        site_key = first_seg
        target_path = "/" + segments[1] if len(segments) > 1 else "/"
    else:
        site_key = DEFAULT_SITE
        client_referer = request.headers.get("referer", "")
        for key in SITES:
            if f"/{key}/" in client_referer or client_referer.endswith(f"/{key}"):
                site_key = key
                break
        target_path = "/" + raw_path if raw_path else "/"

    origin = SITES[site_key]
    url = f"{origin}{target_path}"
    if request.url.query:
        url += f"?{request.url.query}"

    cache_key = f"{request.method}:{url}"
    if request.method == "GET" and cache_key in _CACHE:
        timestamp, c_content, c_status, c_headers, c_media = _CACHE[cache_key]
        if time.time() - timestamp < CACHE_TTL:
            return Response(content=c_content, status_code=c_status, headers=c_headers, media_type=c_media)

    client_referer = request.headers.get("referer", "")
    if client_referer:
        client_host = request.headers.get("host", "")
        fake_referer = (
            client_referer.replace(f"https://{client_host}/{site_key}", origin)
            .replace(f"http://{client_host}/{site_key}", origin)
            .replace(f"https://{client_host}", origin)
            .replace(f"http://{client_host}", origin)
        )
    else:
        fake_referer = f"{origin}/"

    proxy_headers = {
        "Host": origin.replace("https://", "").replace("http://", ""),
        "X-Forwarded-Host": request.headers.get("host", ""),
        "X-Forwarded-Proto": "https",
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"),
        "Accept": request.headers.get("accept", "*/*"),
        "Accept-Language": request.headers.get("accept-language", "ja,en-US;q=0.9,en;q=0.8"),
        "Cookie": request.headers.get("cookie", ""),
        "Referer": fake_referer,
        "Origin": origin,
        "Accept-Encoding": "gzip, deflate, br", 
    }

    for header_name in ["x-requested-with", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"]:
        if header_name in request.headers:
            proxy_headers[header_name.title()] = request.headers[header_name]

    body = None
    if request.method not in ("GET", "HEAD"):
        body = await request.body()

    try:
        res = await core.http_client.request(
            method=request.method,
            url=url,
            headers=proxy_headers,
            content=body,
        )

        content_type = res.headers.get("content-type", "")
        res_headers = {k: v for k, v in res.headers.items() if k.lower() not in _SKIP_HEADERS}
        res_headers["Access-Control-Allow-Origin"] = "*"
        res_headers["Access-Control-Allow-Credentials"] = "true"
        res_headers["Cache-Control"] = f"public, max-age={CACHE_TTL}"

        if res.status_code in (301, 302, 303, 307, 308):
            loc = res_headers.get("location", "")
            if loc:
                if any(ad in loc for ad in AD_DOMAINS):
                    return RedirectResponse(url=f"/{site_key}/")
                if loc.startswith("http"):
                    loc = loc.replace(origin, f"/{site_key}")
                if loc.startswith("/"):
                    if not loc.startswith(f"/{site_key}/"):
                        loc = f"/{site_key}{loc}"
                res_headers["location"] = loc

        # -------------------------------------------------------
        # ★【新規追加】JSONデータの画像URLもプロキシに書き換える
        # -------------------------------------------------------
        if "application/json" in content_type and res.content:
            try:
                json_data = json.loads(res.content.decode("utf-8", errors="ignore"))
                def process_json(data):
                    if isinstance(data, dict):
                        return {k: process_json(v) for k, v in data.items()}
                    elif isinstance(data, list):
                        return [process_json(i) for i in data]
                    elif isinstance(data, str):
                        if data.startswith("http") and any(data.lower().endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"]):
                            return _to_imgproxy(data)
                        if "<" in data and "src=" in data:
                            return rewrite_img_urls(data)
                        return data
                    return data

                new_json = process_json(json_data)
                encoded_json = json.dumps(new_json, ensure_ascii=False).encode("utf-8")
                
                if request.method == "GET" and res.status_code == 200:
                    _CACHE[cache_key] = (time.time(), encoded_json, res.status_code, res_headers, content_type)
                return Response(content=encoded_json, status_code=res.status_code, headers=res_headers, media_type=content_type)
            except Exception:
                pass # JSON変換エラー時はそのまま送る

        # HTMLの処理
        if "text/html" in content_type:
            html = res.content.decode("utf-8", errors="ignore")
            html = remove_ads(html)
            html = rewrite_site_links(html, site_key, origin)
            html = rewrite_img_urls(html)
            encoded_html = html.encode("utf-8")
            
            if request.method == "GET" and res.status_code == 200:
                _CACHE[cache_key] = (time.time(), encoded_html, res.status_code, res_headers, content_type)
            return Response(content=encoded_html, status_code=res.status_code, headers=res_headers, media_type=content_type)

        if request.method == "GET" and res.status_code == 200:
            _CACHE[cache_key] = (time.time(), res.content, res.status_code, res_headers, content_type)
        return Response(content=res.content, status_code=res.status_code, headers=res_headers, media_type=content_type)

    except Exception as e:
        print(f"Proxy error: {e}")
        return Response(status_code=502, content=b"Worker connection failed")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
