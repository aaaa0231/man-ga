import re
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

# -------------------------------------------------------
# 【マルチサイト設定】見たいサイトをここに追加
# -------------------------------------------------------
SITES = {
    "mangarw": "https://mangarw.com",
    "soraraw": "https://soraraw.com",
    # "site2": "https://example.com", 
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

# -------------------------------------------------------
# アプリ起動 / 終了時に httpx クライアントを管理
# -------------------------------------------------------
class Core:
    http_client: httpx.AsyncClient | None = None

core = Core()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # follow_redirects=True だと無限リダイレクトループの可能性あり
    # redirect_limit を制限して対応
    core.http_client = httpx.AsyncClient(
        timeout=30.0, 
        follow_redirects=True,
        limits=httpx.Limits(max_redirects=10)
    )
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
# 最強版「リダイレクト阻止 ＆ 広告強制非表示スクリプト」
# -------------------------------------------------------
ANTI_REDIRECT_SCRIPT = """
<style>
/* CSSレベルで広告要素・バナー・透明オーバーレイを強制消去 */
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

    // ========== リダイレクト完全阻止 ==========
    
    // 1. location.href / location.replace / window.open の完全無効化
    Object.defineProperty(window.location, 'href', {
        get() { return window.location.toString(); },
        set(value) { 
            console.warn('[Anti-Redirect] Blocked location.href assignment:', value);
            return false;
        }
    });
    
    window.location.replace = function(url) {
        console.warn('[Anti-Redirect] Blocked location.replace:', url);
        return false;
    };
    
    window.location.assign = function(url) {
        console.warn('[Anti-Redirect] Blocked location.assign:', url);
        return false;
    };
    
    window.open = function() { 
        console.warn('[Anti-Redirect] Blocked window.open');
        return null; 
    };

    // 2. メタリフレッシュ <meta http-equiv="refresh"> 削除
    document.querySelectorAll('meta[http-equiv="refresh"]').forEach(el => {
        console.warn('[Anti-Redirect] Removed meta refresh:', el.getAttribute('content'));
        el.remove();
    });

    // 3. 動的な <script> タグの生成を監視
    const originalCreateElement = document.createElement.bind(document);
    document.createElement = function(tagName, options) {
        const el = originalCreateElement(tagName, options);
        if (tagName.toLowerCase() === 'script') {
            const originalSetAttribute = el.setAttribute.bind(el);
            el.setAttribute = function(name, value) {
                if (name.toLowerCase() === 'src') {
                    if (value.includes('http') && !value.includes(location.host)) {
                        console.warn('[Anti-Redirect] Blocked external script:', value);
                        return;
                    }
                }
                return originalSetAttribute(name, value);
            };
        }
        return el;
    };

    // 4. onload / onmouseover などのイベントハンドラからのリダイレクト削除
    document.addEventListener('mouseover', function(e) {
        if (e.target.onmouseover) {
            const code = e.target.onmouseover.toString();
            if (code.includes('location') || code.includes('window.open')) {
                e.target.onmouseover = null;
            }
        }
    }, true);

    // 5. target="_blank" の削除 & 画面を覆うレイヤー削除
    function cleanUp() {
        // リフレッシュメタタグの二重チェック
        document.querySelectorAll('meta[http-equiv="refresh"]').forEach(el => el.remove());
        
        // target="_blank" 削除
        document.querySelectorAll('a[target="_blank"]').forEach(a => a.removeAttribute('target'));
        
        // 画面全体を覆う透明レイヤー削除
        document.querySelectorAll('div, section, a, span').forEach(el => {
            const style = window.getComputedStyle(el);
            if ((style.position === 'fixed' || style.position === 'absolute') &&
                (parseInt(style.zIndex) > 1000) &&
                !el.querySelector('img') && !el.querySelector('video')) {
                el.remove();
            }
        });

        // onclick/onmouseover 属性内のリダイレクト命令を削除
        document.querySelectorAll('[onclick], [onmouseover], [onload], [onmouseenter]').forEach(el => {
            ['onclick', 'onmouseover', 'onload', 'onmouseenter'].forEach(attr => {
                const val = el.getAttribute(attr);
                if (val && (val.includes('location') || val.includes('window.open') || val.includes('redirect'))) {
                    el.removeAttribute(attr);
                    console.warn('[Anti-Redirect] Removed malicious attribute:', attr, val);
                }
            });
        });
    }

    // 初期化 + 定期監視
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', () => {
            cleanUp();
            setInterval(cleanUp, 300);
        });
    } else {
        cleanUp();
        setInterval(cleanUp, 300);
    }
})();
</script>
"""

# -------------------------------------------------------
# 画像 URL を /imgproxy/ 経由に書き換える処理
# -------------------------------------------------------
_IMG_EXT_RE = re.compile(
    r'https?://[^\s"\')\]>]+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)(?:[?#][^\s"\')\]>]*)?',
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
    # 広告ドメインの画像は読み込まずに無効化
    for ad_domain in AD_DOMAINS:
        if ad_domain in url:
            return "about:blank"
    stripped = re.sub(r"^https?://", "", url)
    return f"/imgproxy/{stripped}"

def _rewrite_srcset(srcset: str) -> str:
    def replace_url(m: re.Match) -> str:
        return _to_imgproxy(m.group(1))
    return _SRCSET_ENTRY_RE.sub(replace_url, srcset)

# -------------------------------------------------------
# 広告・リダイレクトコード除去処理
# -------------------------------------------------------
def remove_ads(html: str) -> str:
    # 1. 広告ドメインを含むタグの物理削除
    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
        html = re.sub(r"<script[^>]*" + escaped + r"[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<iframe[^>]*" + escaped + r"[^>]*>.*?</iframe>", "", html, flags=re.IGNORECASE | re.DOTALL)
        html = re.sub(r"<a[^>]*" + escaped + r"[^>]*>.*?</a>", "", html, flags=re.IGNORECASE | re.DOTALL)

    # 2. メタリフレッシュ削除（リダイレクト防止）
    html = re.sub(r'<meta\s+http-equiv=["\']?refresh["\']?[^>]*>', '', html, flags=re.IGNORECASE | re.DOTALL)

    # 3. onclick/onload/onmouseover 属性内のリダイレクト命令削除
    html = re.sub(r'on(?:click|load|mouseover|mouseenter)=["\'][^"\']*(?:location|window\.open|redirect)[^"\']*["\']', '', html, flags=re.IGNORECASE)
    html = re.sub(r'onclick=["\'][^"\']*(?:window\.open|location\.href)[^"\']*["\']', '', html, flags=re.IGNORECASE)

    # 4. リダイレクト系のスクリプトタグ削除
    html = re.sub(r'<script[^>]*>.*?(?:location\.href|window\.location|location\.replace)[^<]*</script>', '', html, flags=re.IGNORECASE | re.DOTALL)

    # 5. 最新版のリダイレクト阻止＆CSS隠蔽スクリプトを注入
    if "<head>" in html:
        html = html.replace("<head>", f"<head>{ANTI_REDIRECT_SCRIPT}", 1)
    elif "<HEAD>" in html:
        html = html.replace("<HEAD>", f"<HEAD>{ANTI_REDIRECT_SCRIPT}", 1)
    else:
        html = ANTI_REDIRECT_SCRIPT + html

    return html

# -------------------------------------------------------
# URL・リンク書き換え処理
# -------------------------------------------------------
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
# /imgproxy/{image_url}  — 画像リバースプロキシ（キャッシュ有効）
# -------------------------------------------------------
@app.get("/imgproxy/{image_url:path}")
async def imgproxy(image_url: str, request: Request):
    # 広告ドメインの画像を弾く
    for ad_domain in AD_DOMAINS:
        if ad_domain in image_url:
            return Response(status_code=404, content=b"Blocked Ad Image")

    image_url = image_url.replace("https%3A//", "https://").replace("http%3A//", "http://")
    if not image_url.startswith("http"):
        image_url = "https://" + image_url

    referer = "https://" + image_url.split("/")[2] + "/"
    for key, origin_url in SITES.items():
        if origin_url.replace("https://", "").replace("http://", "") in image_url:
            referer = origin_url + "/"
            break

    proxy_headers = {
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept": request.headers.get("accept", "image/webp,image/*,*/*"),
        "Referer": referer,
    }

    try:
        res = await core.http_client.get(image_url, headers=proxy_headers)
        res_headers = {k: v for k, v in res.headers.items() if k.lower() not in _SKIP_HEADERS}
        res_headers["Access-Control-Allow-Origin"] = "*"
        # キャッシュ有効化：1時間キャッシュ
        res_headers["Cache-Control"] = "public, max-age=3600"
        return Response(
            content=res.content,
            status_code=res.status_code,
            headers=res_headers,
            media_type=res.headers.get("content-type", "image/webp"),
        )
    except Exception as e:
        print(f"[ERROR] imgproxy error for {image_url}: {e}")
        return Response(status_code=502, content=b"imgproxy failed")

# -------------------------------------------------------
# ルートパスをデフォルトサイトにリダイレクト
# -------------------------------------------------------
@app.get("/")
async def root():
    return RedirectResponse(url=f"/{DEFAULT_SITE}/")

# -------------------------------------------------------
# /{raw_path}  — 汎用リバースプロキシ（キャッシュ有効）
# -------------------------------------------------------
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
    
    print(f"[PROXY] site_key={site_key}, target_path={target_path}, final_url={url}")

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
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept": request.headers.get("accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": request.headers.get("accept-language", "ja,en-US;q=0.9,en;q=0.8"),
        "Cookie": request.headers.get("cookie", ""),
        "Referer": fake_referer,
        "Origin": origin,
    }

    for header_name in ["content-type", "x-requested-with", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site"]:
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
        
        # キャッシュ有効化：HTML は1時間、その他は24時間キャッシュ
        if "text/html" in content_type:
            res_headers["Cache-Control"] = "public, max-age=3600"  # 1時間
        else:
            res_headers["Cache-Control"] = "public, max-age=86400"  # 24時間

        if "text/html" in content_type:
            html = res.text
            html = remove_ads(html)
            html = rewrite_site_links(html, site_key, origin)
            html = rewrite_img_urls(html)
            return Response(
                content=html.encode("utf-8"),
                status_code=res.status_code,
                headers=res_headers,
                media_type=content_type,
            )

        return Response(
            content=res.content,
            status_code=res.status_code,
            headers=res_headers,
            media_type=content_type,
        )

    except Exception as e:
        print(f"[ERROR] Proxy error for {url}: {e}")
        import traceback
        traceback.print_exc()
        error_html = f"""
        <html>
        <head><title>Proxy Error</title></head>
        <body style="font-family: monospace; padding: 20px;">
        <h1>🚨 プロキシエラー</h1>
        <p><strong>URL:</strong> {url}</p>
        <p><strong>エラー:</strong> {str(e)}</p>
        <p><a href="/{DEFAULT_SITE}/">ホームに戻る</a></p>
        </body>
        </html>
        """
        return Response(status_code=502, content=error_html.encode("utf-8"), media_type="text/html")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
