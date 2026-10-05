from datetime import datetime, timedelta
import imaplib
import logging
import unicodedata

import pandas as pd

from hotel_analysis.config import EMAIL_USER, EMAIL_PASS, IMAP_HOST

logger = logging.getLogger(__name__)


class EmailBase:
    """Base class for email integration with shared IMAP utilities."""

    def __init__(self, email_user=None, email_pass=None):
        self.email_user = email_user or EMAIL_USER
        self.email_pass = email_pass or EMAIL_PASS
        self.imap_host = IMAP_HOST
        self._mail = None

    def _connect(self):
        # Mevcut bağlantı varsa sağlığını kontrol et
        if self._mail is not None:
            try:
                self._mail.noop()
                return self._mail
            except Exception:
                # Bağlantı kopmuş — temizle ve yeniden bağlan
                self._mail = None

        self._mail = imaplib.IMAP4_SSL(self.imap_host)
        self._mail.sock.settimeout(30)
        self._mail._encoding = 'utf-8'
        self._mail.login(self.email_user, self.email_pass)
        self._mail.select("inbox")
        logger.info("IMAP bağlantısı kuruldu: %s", self.imap_host)
        return self._mail

    def _reconnect(self):
        """Bağlantıyı yeniden kur ve bağlantı nesnesini döndür."""
        logger.info("IMAP yeniden bağlanıyor...")
        self._disconnect()
        return self._connect()

    def _disconnect(self):
        if self._mail is not None:
            try:
                self._mail.logout()
            except Exception:
                pass
            self._mail = None

    @staticmethod
    def normalize(text):
        if pd.isna(text):
            return ''
        return ''.join(
            c for c in unicodedata.normalize('NFKD', str(text))
            if not unicodedata.combining(c)
        )

    def _get_date_filter(self):
        today = datetime.today().date()
        tomorrow = today + timedelta(days=1)
        return f'after:{today.strftime("%Y/%m/%d")} before:{tomorrow.strftime("%Y/%m/%d")}'
