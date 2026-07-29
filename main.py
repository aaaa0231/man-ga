import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

# -------------------------------------------------------
# 【マルチサイト設定】
# -------------------------------------------------------
SITES = {
    "mangarw": "https://mangarw.com",
    "soraraw": "https://soraraw.com",
}

DEFAULT_SITE = "mangarw"

HOST_MAPPINGS = {
    "mangaraw": "mangarw",
    "mangarw": "mangarw",
    "soraraw": "soraraw",
}

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
    "popunder",
]

# -------------------------------------------------------
# アプリ起動 / 終了時に httpx クライアントを管理
# -------------------------------------------------------
class Core:
    http_client: httpx.AsyncClient | None = None

core = Core()

@asynccontextmanager
async def lifespan(app: FastAPI):
    core.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, http2=True)
    yield
    await core.http_client.aclose()

app = FastAPI(lifespan=lifespan)

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
# リダイレクト阻止 ＆ 広告強制非表示スクリプト（最適化版）
# -------------------------------------------------------
ANTI_REDIRECT_SCRIPT = """
<style>
[class*="ad-"], [class*="ad_"], [id*="ad-"], [id*="ad_"],
[class*="banner"], [id*="banner"], iframe[src*="about:blank"] {
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

    // 漫画の動作に必要な外部スクリプト（CDN等）は許可し、明らかな広告ドメインだけを弾く
    const adDomains = ['universityshocksooner', 'adexchangerapid', 'pubadx', 'preferencenail', 'gomuraw', 'vntsm'];
    const originalCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName, options) {
        const el = originalCreateElement(tagName, options);
        if (tagName.toLowerCase() === 'script') {
            const originalSetAttribute = el.setAttribute.bind(el);
            el.setAttribute = function(name, value) {
                if (name.toLowerCase() === 'src') {
                    if (adDomains.some(d => value.includes(d))) {
                        console.warn('[Anti-Ad] Blocked ad script:', value);
                        return;
                    }
                }
                return originalSetAttribute(name, value);
            };
        }
        return el;
    };

    function cleanUp() {
        document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
        
        // 透明なレイヤー広告の削除（漫画のビューアーUIを消さないよう zIndex 9999以上の悪質なものに限定）
        document.querySelectorAll('a, div').forEach(el => {
            const style = window.getComputedStyle(el);
            if ((style.position === 'fixed' || style.position === 'absolute') &&
                (parseInt(style.zIndex) >= 9999) &&
                (el.offsetWidth > window.innerWidth * 0.8)) {
                if (!el.querySelector('img') && !el.querySelector('canvas')) {
                    el.remove();
                }
            }
        });
    }

    document.addEventListener('click', function(e) {
        let target = e.target;
        while (target && target !== document.body) {
            if (target.tagName === 'A' && target.getAttribute('onclick')) {
                const onclickVal = target.getAttribute('onclick').toLowerCase();
                if (onclickVal.includes('window.open') || onclickVal.includes('location.href')) {
                    target.removeAttribute('onclick');
                }
                break;
            }
            target = target.parentElement;
        }
    }, true);

    document.addEventListener('DOMContentLoaded', () => {
        cleanUp();
        setInterval(cleanUp, 1500);
    });
})();
</script>
"""

# -------------------------------------------------------
# URL 書き換え処理
# -------------------------------------------------------
# JSON用にエスケープされたURL ( https:\/\/ ) も正確に捉えられるように正規表現を強化
_IMG_EXT_RE = re.compile(
    r'https?(?:://|:\\/\\/)[^\s"\')\]\\]+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)(?:[?#][^\s"\')\]\\]*)?',
    re.IGNORECASE,
)
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
    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
        html = re.sub(r"<script[^>]*" + escaped + r"[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<iframe[^>]*" + escaped + r"[^>]*>.*?</iframe>", "", html, flags=re.IGNORECASE | re.DOTALL)

    if "<head>" in html:
        html = html.replace("<head>", f"<head>{ANTI_REDIRECT_SCRIPT}", 1)
    elif "<HEAD>" in html:
        html = html.replace("<HEAD>", f"<HEAD>{ANTI_REDIRECT_SCRIPT}", 1)
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
    html = re.sub(r'(data-[a-zA-Z0-9_-]+)=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2', rewrite_src, html, flags=re.IGNORECASE)

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
# 画像プロキシ
# -------------------------------------------------------
@app.get("/imgproxy/{image_url:path}")
async def imgproxy(image_url: str, request: Request):
    for ad_domain in AD_DOMAINS:
        if ad_domain in image_url:
            return Response(status_code=404, content=b"Blocked")

    image_url = image_url.replace("https%3A//", "https://").replace("http%3A//", "http://")
    if not image_url.startswith("http"):
        image_url = "https://" + image_url

    referer = "https://" + image_url.split("/")[2] + "/"
    for key, origin_url in SITES.items():
        if origin_url.replace("https://", "").replace("http://", "") in image_url:
            referer = origin_url + "/"
            break

    proxy_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": referer,
    }

    try:
        res = await core.http_client.get(image_url, headers=proxy_headers)
        res_headers = {k: v for k, v in res.headers.items() if k.lower() not in _SKIP_HEADERS}
        res_headers["Access-Control-Allow-Origin"] = "*"
        res_headers["Cache-Control"] = "public, max-age=86400"
        return Response(
            content=res.content,
            status_code=res.status_code,
            headers=res_headers,
            media_type=res.headers.get("content-type", "image/webp"),
        )
    except Exception:
        return Response(status_code=502, content=b"imgproxy failed")

# -------------------------------------------------------
# メインプロキシ
# -------------------------------------------------------
@app.api_route("/{raw_path:path}", methods=["GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE"])
async def proxy(request: Request, raw_path: str):
    host = request.headers.get("host", "").lower()
    matched_site_key = next((site_k for sub, site_k in HOST_MAPPINGS.items() if sub in host), None)

    segments = raw_path.lstrip("/").split("/", 1)
    first_seg = segments[0] if segments else ""

    if matched_site_key:
        if first_seg in SITES:
            site_key = first_seg
            target_path = "/" + segments[1] if len(segments) > 1 else "/"
        else:
            site_key = matched_site_key
            target_path = "/" + raw_path if raw_path else "/"
    else:
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
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept": request.headers.get("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
        "Accept-Language": request.headers.get("accept-language", "ja,ja-JP;q=0.9,en-US;q=0.8,en;q=0.7"),
        "Cookie": request.headers.get("cookie", ""),
        "Referer": fake_referer,
        "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": request.headers.get("sec-fetch-dest", "document"),
        "Sec-Fetch-Mode": request.headers.get("sec-fetch-mode", "navigate"),
        "Sec-Fetch-Site": request.headers.get("sec-fetch-site", "same-origin"),
        "Upgrade-Insecure-Requests": "1",
    }

    if request.method != "GET":
        proxy_headers["Origin"] = origin
    proxy_headers = {k: v for k, v in proxy_headers.items() if v}

    body = await request.body() if request.method not in ("GET", "HEAD") else None

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

        if "text/html" in content_type:
            html = res.text
            html = remove_ads(html)
            html = rewrite_site_links(html, site_key, origin)
            html = rewrite_img_urls(html)
            return Response(content=html.encode("utf-8"), status_code=res.status_code, headers=res_headers, media_type=content_type)
        
        elif "application/json" in content_type or "text/javascript" in content_type:
            text = res.text
            def replace_json_img(m: re.Match) -> str:
                original = m.group(0)
                clean_url = original.replace("\\/", "/")
                proxy_url = _to_imgproxy(clean_url)
                if "\\/" in original:
                    proxy_url = proxy_url.replace("/", "\\/")
                return proxy_url
                
            text = _IMG_EXT_RE.sub(replace_json_img, text)
            return Response(content=text.encode("utf-8"), status_code=res.status_code, headers=res_headers, media_type=content_type)

        return Response(content=res.content, status_code=res.status_code, headers=res_headers, media_type=content_type)

    except Exception:
        return Response(status_code=502, content=b"Worker connection failed")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
