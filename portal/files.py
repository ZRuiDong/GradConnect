import re
from pathlib import Path
from urllib.parse import quote
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header


def send_private_file(request, field, content_type, filename, download=False):
    if not field or not field.storage.exists(field.name):
        raise Http404("文件不存在。")
    size = field.size
    if settings.USE_X_ACCEL_REDIRECT:
        response = HttpResponse(content_type=content_type)
        response["X-Accel-Redirect"] = "/_protected/" + quote(field.name, safe="/")
    else:
        range_header = request.headers.get("Range") if content_type == "video/mp4" else None
        if range_header:
            match = re.fullmatch(r"bytes=([0-9]{0,20})-([0-9]{0,20})", range_header)
            if not match or not any(match.groups()):
                return invalid_range(size)
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last) if last else size - 1, size - 1)
            else:
                start, end = max(0, size - int(last)), size - 1
            if start > end or start >= size:
                return invalid_range(size)
            def chunks():
                with field.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        block = stream.read(min(65536, remaining))
                        if not block:
                            break
                        remaining -= len(block)
                        yield block
            response = StreamingHttpResponse(chunks() if request.method != "HEAD" else [], status=206, content_type=content_type)
            response["Content-Range"] = f"bytes {start}-{end}/{size}"
            response["Content-Length"] = end - start + 1
        else:
            response = FileResponse(field.open("rb"), content_type=content_type)
    response["Content-Disposition"] = content_disposition_header(download, Path(filename).name)
    response["X-Content-Type-Options"] = "nosniff"
    if content_type == "video/mp4":
        response["Accept-Ranges"] = "bytes"
    return response


def invalid_range(size):
    response = HttpResponse(status=416)
    response["Content-Range"] = f"bytes */{size}"
    return response
