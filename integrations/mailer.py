"""
integrations/mailer.py — completion notifications.

Sent on **both** success and failure. A batch that dies silently is the worst
outcome: nobody finds out until someone opens Drive expecting 300 videos.

Transport is SMTP against Google Workspace with an app password. At ~7 sends a
day that is the whole feature — the Gmail API alternative needs domain-wide
delegation, which means a downloaded key file plus Admin Console client-ID and
scope authorisation for no practical gain here. `_send_message()` is the only
transport-aware function, so swapping in the Gmail API or a transactional
provider later means rewriting one function.

Nothing here raises to the caller. Mail is a notification about work that has
already finished; a broken SMTP password must not turn a successful 300-video
batch into a failed job.
"""

from __future__ import annotations

import logging
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Optional

import config
from jobs import store

logger = logging.getLogger(__name__)


def _recipients(job: Optional[dict] = None) -> list[str]:
    """Job-specific address wins; otherwise the configured default list."""
    if job:
        raw = (job.get("notify_email") or "").strip()
        if raw:
            return [a.strip() for a in raw.split(",") if a.strip()]
    return list(config.MAIL_TO)


def _send_message(msg: EmailMessage) -> bool:
    """The only transport-aware code in this module."""
    try:
        if config.SMTP_STARTTLS:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30,
                                  context=ssl.create_default_context()) as smtp:
                smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
                smtp.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP login rejected for %s. With Google Workspace this is almost "
            "always an app password problem: the account needs 2-Step "
            "Verification on, and BVG_SMTP_PASSWORD must be the 16-character "
            "app password, not the account password.", config.SMTP_USER)
    except Exception:  # noqa: BLE001 — never propagate to the job
        logger.exception("Sending mail failed")
    return False


def send(subject: str, body: str, to: Optional[Iterable[str]] = None,
         attachments: Iterable[Path] = ()) -> bool:
    """Send one plain-text email with optional attachments."""
    if not config.mail_configured():
        logger.warning(
            "Email not configured — skipping notification %r. Set BVG_SMTP_USER, "
            "BVG_SMTP_PASSWORD and BVG_MAIL_TO to enable it.", subject)
        return False

    recipients = list(to) if to is not None else list(config.MAIL_TO)
    if not recipients:
        logger.warning("No recipients for notification %r", subject)
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.MAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    for path in attachments:
        path = Path(path)
        if not path.is_file():
            continue
        if path.stat().st_size > config.MAIL_MAX_ATTACHMENT_BYTES:
            logger.info("Skipping attachment %s — over the size limit", path.name)
            continue
        ctype, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (ctype or "application/octet-stream").partition("/")
        msg.add_attachment(path.read_bytes(), maintype=maintype,
                           subtype=subtype, filename=path.name)

    ok = _send_message(msg)
    if ok:
        logger.info("Sent %r to %s", subject, ", ".join(recipients))
    return ok


# --------------------------------------------------------------------------- job mail

def _duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60:02d}s"


def _render_body(job: dict, status: str, result: dict) -> str:
    total = result.get("total") or 0
    rendered = result.get("rendered") or 0
    failed = result.get("failed") or 0
    uploaded = result.get("uploaded")

    lines = [
        f"Batch: {job.get('label') or job['id']}",
        f"Status: {status}",
        "",
        f"{rendered:,} of {total:,} rendered" + (f", {failed:,} failed" if failed else ""),
    ]

    batches = result.get("batches") or 1
    if batches > 1:
        lines.append(
            f"  {result.get('rows', 0):,} sheet rows x {batches} batches, "
            f"using {result.get('promo_videos', 1)} promo video(s), "
            f"mixed across {result.get('folders', batches)} folders")

    if uploaded is not None and result.get("upload_mode") == "zip":
        zips = result.get("drive_zips") or result.get("drive_files") or 0
        size_gb = (result.get("zip_bytes") or 0) / 1024 ** 3
        published = result.get("upload_platforms") or ["yt", "tk"]
        lines.append(
            f"{uploaded:,} of {rendered:,} uploaded to Google Drive as "
            f"{zips:,} ZIP(s), {size_gb:,.1f} GB — every output folder holds "
            + " and ".join(f"{p}.zip" for p in published))
        pending = result.get("platforms_pending") or []
        if pending:
            # The single most useful thing this email can say when a night was
            # split to stay inside Drive's daily allowance: the job is not
            # finished, the videos are still on the VM, and here is what to do.
            lines.append(
                f"  {', '.join(p + '.zip' for p in pending)} NOT published yet "
                f"— {result.get('videos_kept', 0):,} video(s) are still on the "
                f"VM. Requeue this batch tomorrow with those platform(s) "
                f"selected; nothing already uploaded is re-sent.")
    elif uploaded is not None:
        drive_files = result.get("drive_files", uploaded * 2)
        lines.append(
            f"{uploaded:,} of {rendered:,} uploaded to Google Drive "
            f"({drive_files:,} files — each video is published twice, once "
            f"with a single hashtag and once with all of them)")
    lines.append(f"Elapsed: {_duration(result.get('elapsed'))}")

    mix = result.get("mix") or {}
    if len(mix) > 1:
        lines.append("")
        lines.append("Folder contents (videos per source batch):")
        for folder in sorted(mix)[:12]:
            spread = mix[folder].get("from_batch") or {}
            detail = ", ".join(f"b{b}:{n}" for b, n in sorted(spread.items()))
            lines.append(f"  batch_{int(folder):02d}: "
                         f"{mix[folder].get('total', 0):,} videos ({detail})")

    drive_link = result.get("drive_link")
    if drive_link:
        lines += ["", "Google Drive folder:", drive_link]
    else:
        lines += [
            "",
            "No Drive link — uploading is not configured, so the videos are "
            "still on the VM at:",
            result.get("videos_dir") or "(unknown)",
        ]

    failures = result.get("failures") or []
    if failures:
        lines += ["", f"Failed rows ({failed}):"]
        lines += [f"  {line}" for line in failures]

    warnings = result.get("batch_warnings") or []
    if warnings:
        lines += ["", "Batch warnings:"]
        lines += [f"  {w}" for w in warnings]

    upload_failures = result.get("upload_failures") or []
    if upload_failures:
        lines += ["", f"Drive uploads that failed ({len(upload_failures)}):"]
        lines += [f"  {line}" for line in upload_failures]

    return "\n".join(lines)


def _scrape_body(job: dict, status: str, result: dict) -> str:
    lines = [
        f"Scrape: {job.get('label') or job['id']}",
        f"Status: {status}",
        "",
        f"Account: {result.get('account') or 'unknown'}",
        # A profile advertising "1000 posts" may hold far fewer actual videos —
        # report the real count so a short batch isn't a mystery.
        f"Videos found: {result.get('videos_found', 0)}"
        f" (photo posts skipped: {result.get('photos_skipped', 0)})",
        f"Downloaded: {result.get('downloaded', 0)}"
        f", trimmed: {result.get('trimmed', 0)}"
        f", already had: {result.get('duplicates', 0)}",
        f"Batches built: {result.get('batches', 0)}",
        f"Elapsed: {_duration(result.get('elapsed'))}",
    ]
    drive_link = result.get("drive_link")
    if drive_link:
        lines += ["", "Google Drive folder:", drive_link]
    failures = result.get("failures") or []
    if failures:
        lines += ["", f"Clips that failed ({len(failures)}):"]
        lines += [f"  {line}" for line in failures]
    return "\n".join(lines)


def _captions_body(job: dict, status: str, result: dict) -> str:
    return "\n".join([
        f"Status: {status}",
        "",
        f"Theme: {result.get('theme')}",
        f"Captions: {result.get('captions', 0):,}",
        f"Hashtag sets: {result.get('hashtag_sets', 0):,}",
        f"Unique combinations: {result.get('combinations', 0):,}",
        f"Model: {result.get('model')}",
        f"Elapsed: {_duration(result.get('elapsed'))}",
        "",
        "This pool is now active — new batches draw from it automatically, and "
        "no two videos will ever get the same caption + hashtag pair.",
    ])


def send_job_notification(job: dict, status: str, result: Optional[dict],
                          error: Optional[str]) -> bool:
    """Completion email for any job kind."""
    result = result or {}
    label = job.get("label") or job["id"]
    kind = job.get("kind")

    if status == store.STATUS_SUCCEEDED:
        if kind in (store.KIND_RENDER, store.KIND_PIPELINE):
            failed = result.get("failed") or 0
            subject = (f"Videos ready: {result.get('rendered', 0)} of "
                       f"{result.get('total', 0)} — {label}")
            if failed:
                subject += f" ({failed} failed)"
        elif kind == store.KIND_CAPTIONS:
            subject = (f"Caption pool ready: "
                       f"{result.get('combinations', 0):,} combinations")
        else:
            subject = f"Scrape complete: {result.get('downloaded', 0)} clips — {label}"
    else:
        subject = f"FAILED: {label}"

    if status == store.STATUS_SUCCEEDED:
        if kind in (store.KIND_RENDER, store.KIND_PIPELINE):
            body = _render_body(job, status, result)
        elif kind == store.KIND_CAPTIONS:
            body = _captions_body(job, status, result)
        else:
            body = _scrape_body(job, status, result)
    else:
        body = "\n".join([
            f"Job: {label}",
            f"Kind: {kind}",
            "Status: FAILED",
            "",
            "Error:",
            error or "(no error recorded)",
            "",
            "Anything already finished is kept — resubmitting resumes from "
            "where this stopped rather than starting over.",
        ])
        # Partial progress is the most useful thing in a failure mail.
        try:
            counts = store.item_counts(job["id"])
            if counts.get("total"):
                body += (f"\n\nProgress when it stopped: {counts['rendered']} "
                         f"rendered, {counts['render_failed']} failed, "
                         f"{counts['render_pending']} not started "
                         f"(of {counts['total']}).")
        except Exception:  # noqa: BLE001
            pass

    attachments = []
    sheet = result.get("sheet_path")
    if sheet and Path(sheet).is_file():
        attachments.append(Path(sheet))

    return send(subject, body, to=_recipients(job), attachments=attachments)


def send_test_email(to: Optional[str] = None) -> tuple[bool, str]:
    """Used by the setup check in the UI. Returns (ok, message)."""
    if not config.mail_configured():
        missing = [name for name, value in (
            ("BVG_SMTP_HOST", config.SMTP_HOST),
            ("BVG_SMTP_USER", config.SMTP_USER),
            ("BVG_SMTP_PASSWORD", config.SMTP_PASSWORD),
            ("BVG_MAIL_FROM", config.MAIL_FROM),
        ) if not value]
        return False, "Email is not configured. Missing: " + ", ".join(missing)

    recipients = [a.strip() for a in (to or "").split(",") if a.strip()] or list(config.MAIL_TO)
    if not recipients:
        return False, ("No recipient. Type an address above, or set BVG_MAIL_TO "
                       "to make one the default for every batch.")
    ok = send(
        "Bulk Video Generator — test email",
        "If you are reading this, job notifications are working.\n\n"
        f"Sent from {config.MAIL_FROM} via {config.SMTP_HOST}:{config.SMTP_PORT}.",
        to=recipients,
    )
    if ok:
        return True, f"Sent to {', '.join(recipients)}."
    return False, "Sending failed — check the worker log for the SMTP error."
