import re
from pathlib import Path

from django.http import FileResponse, Http404, HttpResponse

from .models import Job


def subtitles_response(job):
    if job.status == Job.Status.CANCELLED:
        raise Http404
    return _file_response(job.subtitles, True, f'{Path(job.original_name).stem}.srt', 'application/x-subrip')


def video_response(job, *, attachment, range_header=None):
    if job.status != Job.Status.SUCCEEDED:
        raise Http404
    response = _file_response(job.result, attachment, f'{Path(job.original_name).stem}_subtitled.mp4', 'video/mp4')
    response['Accept-Ranges'] = 'bytes'
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', range_header or '', flags=re.IGNORECASE)
    if not match or not any(match.groups()):
        return response

    size = int(response['Content-Length'])
    first, last = match.groups()
    try:
        if first:
            start = int(first)
            end = min(int(last), size - 1) if last else size - 1
        else:
            start = max(size - int(last), 0)
            end = size - 1
    except ValueError:
        return response

    if start > end:
        response.close()
        return HttpResponse(status=416, headers={
            'Accept-Ranges': 'bytes',
            'Content-Range': f'bytes */{size}',
            'Content-Length': '0',
        })

    file = response.file_to_stream
    file.seek(start)
    length = end - start + 1
    # Replacing the iterator disables WSGI's whole-file wrapper and keeps the file closer.
    response.streaming_content = _read_range(file, length, response.block_size)
    response.status_code = 206
    response['Content-Range'] = f'bytes {start}-{end}/{size}'
    response['Content-Length'] = str(length)
    return response


def _read_range(file, remaining, block_size):
    while remaining and (chunk := file.read(min(block_size, remaining))):
        yield chunk
        remaining -= len(chunk)


def _file_response(field, attachment, filename, content_type):
    if not field:
        raise Http404
    try:
        file = field.open('rb')
    except (FileNotFoundError, ValueError) as exc:
        raise Http404 from exc
    return FileResponse(file, as_attachment=attachment, filename=filename, content_type=content_type)
