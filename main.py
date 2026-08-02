from fastapi import FastAPI, Response, Request, HTTPException
import httpx

app = FastAPI()

@app.get("/proxy")
async def image_proxy(url: str, request: Request):
    """
    指定されたURLの画像を取得し、ブラウザにそのまま返すプロキシAPI
    """
    if not url:
        raise HTTPException(status_code=400, detail="URLパラメータが指定されていません")

    # 相手サーバー(mangarw等)に送るヘッダー
    # ※ Hostヘッダーなどをそのまま転送するとエラーになることがあるため、必要なものだけ再構築します
    req_headers = {
        "User-Agent": request.headers.get("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        # 画像を取得する場合、適切なAcceptヘッダーを送信すると成功率が上がります
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        # 必要であればリファラを追加（画像の直リンク対策を回避するため）
        # "Referer": "https://mangarw.com/", 
    }

    # httpxクライアントを作成 (follow_redirects=True でリダイレクトにも対応)
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            # ターゲットURLへGETリクエスト
            res = await client.get(url, headers=req_headers)
            
            # 相手サーバーからのステータスコードがエラー（404等）の場合
            if res.status_code != 200:
                return Response(
                    content=res.content, 
                    status_code=res.status_code
                )

            # 🌟ここが文字化け（バイナリテキスト化）を直す一番重要なポイント🌟
            # ターゲットサーバーが返してきた Content-Type (例: image/jpeg, image/webp) を取得。
            # もし不明な場合は 'application/octet-stream' とする。
            content_type = res.headers.get("Content-Type", "application/octet-stream")

            # .text ではなく .content (バイナリデータ) をそのままブラウザに返す
            return Response(
                content=res.content,
                status_code=res.status_code,
                media_type=content_type
            )

        except httpx.RequestError as e:
            # リクエスト自体が失敗した場合（タイムアウトなど）
            raise HTTPException(status_code=500, detail=f"通信エラーが発生しました: {str(e)}")
