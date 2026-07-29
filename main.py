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
    # 他に見たいサイトがあれば追加できます
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
    core.http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
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
# 強力な「リダイレクト・ポップアップ阻止スクリプト」の注入
# -------------------------------------------------------
ANTI_REDIRECT_SCRIPT = """
<script>
(function() {
    'use strict';

    // 1. window.open (ポップアップ・別タブ) を完全停止
    window.open = function(url, target, features) {
        console.warn('[Anti-Ad] Blocked window.open:', url);
        return null;
    };

    // 2. target="_blank" (別タブ遷移) をすべて削除
    function removeTargetBlank() {
        document.querySelectorAll('a[target]').forEach(a => {
            if (a.getAttribute('target') === '_blank') {
                a.removeAttribute('target');
            }
        });
    }

    // 3. 画面全体を覆う「透明な広告要素 (クリックジャック)」を全自動削除
    function removeClickjackOverlays() {
        const elements = document.querySelectorAll('div, a, span, section');
        const screenWidth = window.innerWidth;
        const screenHeight = window.innerHeight;

        elements.forEach(el => {
            const style = window.getComputedStyle(el);
            const isFixedOrAbs = style.position === 'fixed' || style.position === 'absolute';
            const highZIndex = parseInt(style.zIndex) > 10 || style.zIndex === '9999' || style.zIndex === '2147483647';
            
            // 画面の広範囲（70%以上）を覆っている要素を判定
            const rect = el.getBoundingClientRect();
            const isFullCover = rect.width >= screenWidth * 0.7 && rect.height >= screenHeight * 0.7;

            if (isFixedOrAbs && highZIndex && isFullCover) {
                // 画像が含まれていない（透明な罠）場合は物理削除
                if (!el.querySelector('img') && !el.querySelector('canvas')) {
                    console.warn('[Anti-Ad] Removed clickjack overlay:', el);
                    el.remove();
                }
            }
        });
    }

    // 4. クリックイベントの横取り（ポップアンダー・勝手なページ移動）をブロック
    document.addEventListener('click', function(e) {
        let target = e.target;
        while (target && target !== document.body) {
            if (target.tagName === 'A') {
                const href = target.getAttribute('href') || '';
                // onclick 属性に悪質な処理が入っている場合は無効化
                if (target.getAttribute('onclick')) {
                    target.removeAttribute('onclick');
                }
                break;
            }
            target = target.parentElement;
        }
    }, true); // キャプチャフェーズで最優先実行

    // 定期監視して後から生成される広告トラップも消去
    document.addEventListener('DOMContentLoaded', () => {
        removeTargetBlank();
        removeClickjackOverlays();
        setInterval(() => {
            removeTargetBlank();
            removeClickjackOverlays();
        }, 800);
    });
})();
</script>
"""

# -------------------------------------------------------
# 画像 URL を /imgproxy/ 経由に書き換える
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

    # 2. onclick 属性に入っているリダイレクト命令（window.open等）を強力削除
    html = re.sub(r'onclick=["\'][^"\']*(?:window\.open|location\.href)[^"\']*["\']', '', html, flags=re.IGNORECASE)

    # 3. リダイレクト阻止スクリプトを <head> 直後に注入
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
# /imgproxy/{image_url}  — 画像リバースプロキシ
# -------------------------------------------------------
@app.get("/imgproxy/{image_url:path}")
async def imgproxy(image_url: str, request: Request):
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
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
        "Accept": request.headers.get("accept", "image/webp,image/*,*/*"),
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
    except Exception as e:
        print(f"imgproxy error: {e}")
        return Response(status_code=502, content=b"imgproxy failed")


# -------------------------------------------------------
# /{raw_path}  — 汎用リバースプロキシ
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
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
        "Accept": request.headers.get("accept", "*/*"),
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
        print(f"Proxy error: {e}")
        return Response(status_code=502, content=b"Worker connection failed")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
