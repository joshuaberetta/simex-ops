"""Inject documents: matching a zip of them to injects by the MEL's Supporting documents names.

A zip can be hundreds of MB, so the request only checks it and saves it to a temporary file;
the matching runs in a thread of the web process and records its progress in an
AttachmentImport row, which the Import page shows."""

import logging
import mimetypes
import os
import tempfile
import threading
import zipfile
from pathlib import PurePosixPath

from . import notices
from .importer import LOCKED
from .models import Attachment, AttachmentImport, Inject, audit

log = logging.getLogger("simex.documents")
MAX_ATTACHMENT = 30 * 1024 * 1024
MAX_ZIP_TOTAL = 300 * 1024 * 1024
RUN_INLINE = False  # tests: no background thread


class ZipError(ValueError):
    pass


def doc_matches(doc_line, filename):
    d, f = doc_line.strip().lower(), filename.lower()
    stem = f.rsplit(".", 1)[0]
    return f == d or stem == d or stem == d.rsplit(".", 1)[0] or (len(d) > 8 and f.startswith(d))


def missing_docs(inj, filenames):
    """Lines of the inject's Supporting documents that no attached filename matches."""
    docs = [d for d in (inj.supporting_documents or "").splitlines() if d.strip()]
    return [d for d in docs if not any(doc_matches(d, f) for f in filenames)]


def put_attachment(inj, filename, data):
    a = inj.attachments.filter(filename=filename).first()
    if a:
        a.data, a.size = data, len(data)
        a.save()
        return a
    return Attachment.objects.create(inject=inj, filename=filename, data=data, size=len(data),
                                     content_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")


def _entries(zf):
    return [e for e in zf.infolist() if not e.is_dir() and not PurePosixPath(e.filename).name.startswith((".", "~$"))
            and "__MACOSX" not in e.filename]


def start(upload, user=""):
    """Check the uploaded zip, keep a copy and start matching it. Returns the AttachmentImport."""
    if any(j.active for j in AttachmentImport.objects.filter(status="running")):
        raise ZipError("A zip is already being matched; wait for it to finish.")
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="simex-docs-")
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in upload.chunks():
                out.write(chunk)
        try:
            with zipfile.ZipFile(path) as zf:
                if sum(e.file_size for e in _entries(zf)) > MAX_ZIP_TOTAL:
                    raise ZipError("Zip contents are over 300 MB.")
        except zipfile.BadZipFile:
            raise ZipError("Not a valid zip file.")
    except BaseException:
        os.unlink(path)
        raise
    job = AttachmentImport.objects.create(filename=upload.name, user=str(user or ""))
    if RUN_INLINE:
        run(job.id, path)
    else:
        threading.Thread(target=run, args=(job.id, path), daemon=True, name=f"docs-{job.id}").start()
    return job


def run(job_id, path):
    from django.db import connection
    job = AttachmentImport.objects.get(pk=job_id)
    try:
        with zipfile.ZipFile(path) as zf:
            entries = _entries(zf)
            injects = list(Inject.objects.exclude(supporting_documents=None).exclude(status__in=LOCKED))
            job.total = len(injects)
            job.save(update_fields=["total", "updated_at"])
            used = set()
            for inj in injects:
                for line in inj.supporting_documents.splitlines():
                    for e in entries:
                        name = PurePosixPath(e.filename).name
                        if doc_matches(line, name) and e.file_size <= MAX_ATTACHMENT:
                            put_attachment(inj, name, zf.read(e))
                            used.add(e.filename)
                            job.attached += 1
                            break
                job.done += 1
                job.save(update_fields=["done", "attached", "updated_at"])
            job.unused = [PurePosixPath(e.filename).name for e in entries if e.filename not in used]
            job.status = "done"
            job.save()
        audit("attachments_zip", f"{job.filename}: {job.attached} attached", job.user)
        notices.attachments_done(job)
    except Exception as e:
        log.exception("zip import %s failed", job_id)
        job.status, job.error = "failed", str(e)[:1000]
        job.save()
        audit("attachments_zip_failed", f"{job.filename}: {e}", job.user)
        notices.attachments_done(job)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
        if not RUN_INLINE:
            connection.close()
