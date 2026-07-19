from contextlib import asynccontextmanager
import re

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

# -------------------------------------------------------
# 設定
# -------------------------------------------------------
ORIGIN = "https://mangarw.com"

# -------------------------------------------------------
# 広告抹殺パターン設定
# (参考: ねむ様)
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

# -------------------------------------------------------
# 除外するレスポンスヘッダー
# -------------------------------------------------------
_SKIP_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection", "content-security-policy"}

# -------------------------------------------------------
# 画像 URL を /imgproxy/ 経由に書き換える
# -------------------------------------------------------

# 画像拡張子にマッチするパターン（URL に含まれていれば画像とみなす）
_IMG_EXT_RE = re.compile(
    r'https?://[^\s"\')\]>]+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)(?:[?#][^\s"\')\]>]*)?',
    re.IGNORECASE,
)

# srcset の各エントリ: "url [descriptor], url [descriptor], ..."
_SRCSET_ENTRY_RE = re.compile(r'(https?://[^\s,]+)', re.IGNORECASE)

# CSS url(...)
_CSS_URL_RE = re.compile(
    r'url\(\s*(["\']?)(https?://[^\s"\')\]>]+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\')\]>]*)\1\s*\)',
    re.IGNORECASE,
)


def _to_imgproxy(url: str) -> str:
    """絶対 URL を /imgproxy/{host+path} に変換。https?:// は落とす。すでに /imgproxy/ なら変えない。"""
    if "/imgproxy/" in url:
        return url
    stripped = re.sub(r'^https?://', '', url)
    return f"/imgproxy/{stripped}"


def _rewrite_srcset(srcset: str) -> str:
    """srcset 属性値内の各 URL を /imgproxy/ 経由に書き換える。"""
    def replace_url(m: re.Match) -> str:
        return _to_imgproxy(m.group(1))
    return _SRCSET_ENTRY_RE.sub(replace_url, srcset)


def remove_ads(html: str) -> str:
    """
    HTML 文字列中の広告コードを物理削除・無効化する。
    """
    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
        html = re.sub(
            r'<script[^>]*' + escaped + r'[^>]*>.*?</script>',
            '',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

    html = re.sub(
        r'<a[^>]*adexchangerapid\.com[^>]*>.*?</a>',
        '',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for domain in AD_DOMAINS:
        escaped = re.escape(domain)
        html = re.sub(escaped, 'localhost', html, flags=re.IGNORECASE)

    return html


def rewrite_img_urls(html: str) -> str:
    """
    HTML 文字列中の画像 URL をすべて /imgproxy/ 経由に書き換える。
    """
    def rewrite_src(m: re.Match) -> str:
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        if url.startswith("data:") or "/imgproxy/" in url:
            return m.group(0)
        return f'{attr}={quote}{_to_imgproxy(url)}{quote}'

    html = re.sub(
        r'(src)=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2',
        rewrite_src,
        html,
        flags=re.IGNORECASE,
    )

    html = re.sub(
        r'(data-(?:src|lazy-src|original|bg))=(["\'])(https?://[^\s"\']+\.(?:webp|jpe?g|png|gif|svg|avif|bmp|ico)[^\s"\']*)\2',
        rewrite_src,
        html,
        flags=re.IGNORECASE,
    )

    def rewrite_srcset_attr(m: re.Match) -> str:
        attr, quote, val = m.group(1), m.group(2), m.group(3)
        return f'{attr}={quote}{_rewrite_srcset(val)}{quote}'

    html = re.sub(
        r'((?:data-)?srcset)=(["\'])([^"\']+)\2',
        rewrite_srcset_attr,
        html,
        flags=re.IGNORECASE,
    )

    def rewrite_css_url(m: re.Match) -> str:
        quote, url = m.group(1), m.group(2)
        if url.startswith("data:") or "/imgproxy/" in url:
            return m.group(0)
        return f'url({quote}{_to_imgproxy(url)}{quote})'

    html = _CSS_URL_RE.sub(rewrite_css_url, html)

    return html


# -------------------------------------------------------
# /imgproxy/{image_url}  — 画像リバースプロキシ
# -------------------------------------------------------
@app.get("/imgproxy/{image_url:path}")
async def imgproxy(image_url: str, request: Request):
    image_url = image_url.replace("https%3A//", "https://").replace("http%3A//", "http://")
    if not image_url.startswith("http"):
        image_url = "https://" + image_url

    proxy_headers = {
        "User-Agent": request.headers.get("user-agent", "Mozilla/5.0"),
        "Accept":     request.headers.get("accept", "image/webp,image/*,*/*"),
        "Referer":    ORIGIN + "/",
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
# /{full_path}  — 汎用リバースプロキシ
#   HTML レスポンスの場合は画像 URL を /imgproxy/ 経由に書き換える。
# -------------------------------------------------------
@app.api_route("/{full_path:path}", methods=["GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE"])
async def proxy(request: Request, full_path: str):
    raw_path = request.scope.get("raw_path", b"").decode("utf-8", errors="replace")
    url = f"{ORIGIN}{raw_path}"
    if request.url.query:
        url += f"?{request.url.query}"

    # 1. ブラウザから届いたアクセス元 (Referer) のドメインを ORIGIN に書き換えて偽装する
    client_referer = request.headers.get("referer", "")
    if client_referer:
        client_host = request.headers.get("host", "")
        fake_referer = client_referer.replace(f"https://{client_host}", ORIGIN).replace(f"http://{client_host}", ORIGIN)
    else:
        fake_referer = f"{ORIGIN}/"

    # 2. 相手サーバー（WAFやボット対策）をすり抜けるためのヘッダーを構築
    proxy_headers = {
        "Host":              ORIGIN.replace("https://", "").replace("http://", ""),
        "X-Forwarded-Host":  request.headers.get("host", ""),
        "X-Forwarded-Proto": "https",
        "User-Agent":        request.headers.get("user-agent", "Mozilla/5.0"),
        "Accept":            request.headers.get("accept", "*/*"),
        "Accept-Language":   request.headers.get("accept-language", "ja,en-US;q=0.9,en;q=0.8"),
        "Cookie":            request.headers.get("cookie", ""),
        "Referer":           fake_referer,
        "Origin":            ORIGIN,
    }

    # 3. POST通信やAjax(非同期通信)に必要なヘッダーがあればそのまま転送する
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

        # HTML レスポンスのみ広告削除 + 画像 URL を書き換える
        if "text/html" in content_type:
            html = res.text
            html = remove_ads(html)
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


# -------------------------------------------------------
# 直接実行
# -------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080, reload=True)
