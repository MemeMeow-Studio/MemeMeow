# 为受控文件提供基于持久化内容版本的 HTTP 条件响应。

from pathlib import Path
from stat import S_ISREG

from anyio import to_thread
from starlette.requests import Request
from starlette.responses import FileResponse, Response
from werkzeug.http import parse_etags, quote_etag


async def revalidated_file_response(
    request: Request, path: Path, *, media_type: str, content_digest: str,
) -> Response:
    """用已保存的内容版本返回文件或 304，供完成授权的媒体读取入口调用。

    输入路径必须来自受控存储，content_digest 必须随文件内容提交。这里只读取文件
    状态，不扫描文件计算哈希；HTTP 条件值由 Werkzeug 解析，支持弱标签和标签列表。
    """
    if not content_digest:
        raise ValueError("media_content_digest_missing")
    file_stat = await to_thread.run_sync(path.stat)
    if not S_ISREG(file_stat.st_mode):
        raise ValueError("media_path_not_regular_file")
    headers = {
        "Cache-Control": "private, no-cache",
        "Vary": "Cookie",
        "ETag": quote_etag(content_digest),
    }
    conditions = ",".join(request.headers.getlist("if-none-match"))
    if request.method in {"GET", "HEAD"} and parse_etags(conditions).contains_weak(content_digest):
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers, stat_result=file_stat)
