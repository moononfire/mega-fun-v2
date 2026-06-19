"""
Campaign sender — designed to be run by cron every minute.
Usage: python scripts/send_campaign.py

Each invocation:
1. Finds the current active campaign (oldest first)
2. Sends one email per active mailbox
3. If campaign is done, marks it 'done' and promotes next queued campaign to 'active'
4. Exits

Cron entry example:  * * * * *  cd /path/to/mega-fun && python scripts/send_campaign.py
"""

import sys
import os
import smtplib
import time
import random
import uuid
from pathlib import Path
from email.mime.text import MIMEText

# VPS Worker: client context
CLIENT_SLUG = sys.argv[1] if len(sys.argv) > 1 else "default"
BASE_DIR    = Path(f"/home/deploy/clients/{CLIENT_SLUG}")
INPUT_DIR   = BASE_DIR / "input"
OUTPUT_DIR  = BASE_DIR / "output"
params_kv   = dict(arg.split("=", 1) for arg in sys.argv[2:] if "=" in arg)
from email.mime.multipart import MIMEMultipart

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
from config import DATABASE
from app.crypto import decrypt
from init_db import init_db

init_db()


def get_db():
    conn = sqlite3.connect(DATABASE, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    return conn


def get_tracking_base_url(db):
    """Return tracking base URL from settings, or empty string if not set."""
    row = db.execute("SELECT value FROM settings WHERE key = 'tracking_base_url'").fetchone()
    if row and row["value"]:
        return row["value"].rstrip("/")
    return ""


def send_email(smtp_server, smtp_port, sender_email, sender_password, to_email, subject, body, tracking_url=None):
    """Send a single email via SMTP. Returns (success, error)."""
    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = sender_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if tracking_url:
            html_body = body.replace("\n", "<br>")
            html = (
                f"<html><body><p>{html_body}</p>"
                f'<img src="{tracking_url}" width="1" height="1" style="display:none" alt="" />'
                f"</body></html>"
            )
            msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, to_email, msg.as_string())

        return True, None
    except Exception as e:
        return False, str(e)


def promote_next_queued(db):
    """Promote the oldest queued campaign to active."""
    queued = db.execute(
        "SELECT id FROM campaigns WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if queued:
        db.execute("UPDATE campaigns SET status = 'active' WHERE id = ?", (queued["id"],))
        db.commit()
        print(f"Promoted campaign {queued['id']} to active")


def get_daily_limit(db):
    """Return global daily email limit (0 = unlimited)."""
    row = db.execute("SELECT value FROM settings WHERE key = 'daily_email_limit'").fetchone()
    if row and row["value"]:
        try:
            return int(row["value"])
        except ValueError:
            pass
    return 0


def get_total_sent_today(db):
    """Return total emails sent today across all mailboxes."""
    row = db.execute(
        "SELECT COALESCE(SUM(daily_sent), 0) FROM mailboxes"
    ).fetchone()
    return row[0]


def main():
    db = get_db()
    tracking_base_url = get_tracking_base_url(db)

    # Check global daily email limit
    daily_limit = get_daily_limit(db)
    if daily_limit > 0:
        total_sent = get_total_sent_today(db)
        if total_sent >= daily_limit:
            db.close()
            print(f"Daily email limit reached ({total_sent}/{daily_limit}). Skipping.")
            return

    # Find the active campaign (oldest first)
    campaign = db.execute(
        "SELECT * FROM campaigns WHERE status = 'active' ORDER BY created_at ASC LIMIT 1"
    ).fetchone()

    if not campaign:
        db.close()
        return  # Nothing to do

    campaign_id = campaign["id"]
    subject = campaign["subject"]
    body = campaign["body_template"]

    # Get all active mailboxes
    mailboxes = db.execute("SELECT * FROM mailboxes WHERE active = 1").fetchall()

    if not mailboxes:
        db.close()
        print("No active mailboxes")
        return

    sent_count = 0
    for mb in mailboxes:
        # Re-check global daily limit before each send
        if daily_limit > 0 and get_total_sent_today(db) >= daily_limit:
            print(f"Daily email limit reached ({daily_limit}). Stopping this round.")
            break

        # Check per-mailbox daily limit
        if mb["daily_limit"] > 0 and mb["daily_sent"] >= mb["daily_limit"]:
            print(f"Mailbox {mb['email']} reached daily limit ({mb['daily_sent']}/{mb['daily_limit']}). Skipping.")
            continue

        # Pick one pending email for this campaign
        pending = db.execute(
            """SELECT ce.id AS ce_id, e.email AS recipient
               FROM campaign_emails ce
               JOIN emails e ON ce.email_id = e.id
               WHERE ce.campaign_id = ? AND ce.status = 'pending'
               LIMIT 1""",
            (campaign_id,),
        ).fetchone()

        if not pending:
            # No more pending — campaign is done
            db.execute("UPDATE campaigns SET status = 'done' WHERE id = ?", (campaign_id,))
            db.commit()
            promote_next_queued(db)
            db.close()
            print(f"Campaign {campaign_id} done")
            return

        ce_id = pending["ce_id"]
        recipient = pending["recipient"]

        # Generate tracking token
        token = uuid.uuid4().hex

        # Mark as sending to avoid double-pick
        db.execute(
            "UPDATE campaign_emails SET status = 'sending', mailbox_id = ?, open_token = ? WHERE id = ?",
            (mb["id"], token, ce_id),
        )
        db.commit()

        tracking_url = f"{tracking_base_url}/track/{token}" if tracking_base_url else None

        success, error = send_email(
            mb["smtp_server"], mb["smtp_port"],
            mb["email"], decrypt(mb["password"]),
            recipient, subject, body, tracking_url,
        )

        if success:
            db.execute(
                "UPDATE campaign_emails SET status = 'sent', sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ce_id,),
            )
            db.execute(
                "UPDATE mailboxes SET daily_sent = daily_sent + 1, total_sent = total_sent + 1, last_sent_at = CURRENT_TIMESTAMP WHERE id = ?",
                (mb["id"],),
            )
            sent_count += 1
            print(f"Sent to {recipient} from {mb['email']}")
        else:
            db.execute(
                "UPDATE campaign_emails SET status = 'failed', error = ? WHERE id = ?",
                (error, ce_id),
            )
            print(f"Failed {recipient} from {mb['email']}: {error}")

        db.commit()
        time.sleep(random.uniform(1, 2))

    # Check if there are still pending emails after this round
    remaining = db.execute(
        "SELECT COUNT(*) FROM campaign_emails WHERE campaign_id = ? AND status = 'pending'",
        (campaign_id,),
    ).fetchone()[0]

    if remaining == 0:
        db.execute("UPDATE campaigns SET status = 'done' WHERE id = ?", (campaign_id,))
        db.commit()
        promote_next_queued(db)
        print(f"Campaign {campaign_id} done")

    db.close()
    print(f"Round complete: sent {sent_count} emails, {remaining} remaining")


if __name__ == "__main__":
    main()
