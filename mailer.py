"""
Email utility for HEXIS — sends invite codes via SMTP.
Configure SMTP_HOST / SMTP_USER / SMTP_PASSWORD in .env.
"""

import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import config

logger = logging.getLogger("mailer")
def _smtp_enabled() -> bool:
    return bool(config.SMTP_USER and config.SMTP_PASSWORD)
def send_invite_code(to_email: str, code: str) -> bool:
    """Send an invite code email. Returns True on success."""
    if not _smtp_enabled():
        logger.warning("SMTP not configured — invite code NOT sent by email.")
        return False

    subject = "Your HEXIS Access Code"
    register_url = f"{config.HEXIS_BASE_URL}/register"

    html = f"""
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                max-width:480px;margin:40px auto;background:#161b22;
                border:1px solid #30363d;border-radius:14px;padding:32px;color:#e6edf3">
      <h2 style="margin:0 0 6px;font-size:22px;letter-spacing:.04em">HEXIS</h2>
      <p style="color:#8b949e;margin:0 0 28px;font-size:13px">Algorithmic Trading Agent</p>

      <p style="margin:0 0 16px">Your access has been confirmed. Use the code below to create your account:</p>

      <div style="background:#0d1117;border:1px solid #30363d;border-radius:10px;
                  padding:18px 24px;text-align:center;margin:0 0 24px">
        <span style="font-family:monospace;font-size:26px;font-weight:700;
                     letter-spacing:.12em;color:#3fb950">{code}</span>
      </div>

      <a href="{register_url}" style="display:block;text-align:center;padding:12px;
         background:#3fb950;color:#000;font-weight:700;font-size:15px;
         border-radius:8px;text-decoration:none;margin:0 0 24px">
        Create Account →
      </a>

      <p style="font-size:12px;color:#8b949e;margin:0;line-height:1.6">
        Enter this code on the registration page at
        <a href="{register_url}" style="color:#58a6ff">{register_url}</a>.<br>
        This code can only be used once.
      </p>

      <hr style="border:none;border-top:1px solid #30363d;margin:24px 0">
      <p style="font-size:11px;color:#8b949e;margin:0">
        HEXIS is an algorithmic trading tool. Past performance does not guarantee
        future results. Trading involves significant risk of loss.
      </p>
    </div>
    """

    plain = (
        'Your HEXIS invite code: ' + code + chr(10) + chr(10) +
        'Register at: ' + register_url
    )
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = config.SMTP_FROM or config.SMTP_USER
        msg["To"]      = to_email
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html,  "html"))

        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.sendmail(config.SMTP_USER, to_email, msg.as_string())

        logger.info(f"Invite code sent to {to_email}")
        return True
    except Exception as e:
        logger.error(f"Failed to send invite email to {to_email}: {e}")
        return False


def send_password_reset(to_email, reset_url):
    if not _smtp_enabled():
        logger.warning('SMTP not configured.')
        return False
    plain = 'Reset HEXIS password: ' + reset_url + chr(10) + 'Expires 30 min.'
    html  = '<p>Reset HEXIS password: <a href= + reset_url + >Click here</a> (expires 30 min)</p>'
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Reset your HEXIS password'
        msg['From']    = config.SMTP_FROM or config.SMTP_USER
        msg['To']      = to_email
        msg.attach(MIMEText(plain, 'plain'))
        msg.attach(MIMEText(html,  'html'))
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as s:
            s.ehlo(); s.starttls()
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.sendmail(config.SMTP_USER, to_email, msg.as_string())
        logger.info('Password reset sent to ' + to_email)
        return True
    except Exception as e:
        logger.error('Failed to send reset email: ' + str(e))
        return False
