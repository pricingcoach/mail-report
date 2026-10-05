import email
from email.utils import parseaddr
import logging
import os

import pandas as pd
import xml.etree.ElementTree as ET

from hotel_analysis.email_base import EmailBase

logger = logging.getLogger(__name__)


class EmailIntegration(EmailBase):
    """Europrotel email integration extending shared EmailBase."""

    def save_attachment(self, email_configs):
        """Download attachments from Gmail based on email config rules.

        Args:
            email_configs: list of dicts with keys: mail, subject, reply_to_mail, file, file_name
        """
        mail = self._connect()
        date_filter = self._get_date_filter()

        for row in email_configs:
            subject = self.normalize(row['subject'])
            sender = self.normalize(row['mail'])
            expected_reply_to = self.normalize(row.get('reply_to_mail') or '')
            download_folder = row['file']
            file_name_filter = (row.get('file_name') or '').lower()
            os.makedirs(download_folder, exist_ok=True)

            subject_for_search = subject.replace('_', ' ')
            search_criteria = f'X-GM-RAW "subject:\\"{subject_for_search}\\" from:{sender} {date_filter}"'

            result, data = mail.uid('SEARCH', None, search_criteria)

            if result != 'OK':
                continue

            for uid in data[0].split():
                result, msg_data = mail.uid('FETCH', uid, '(RFC822)')
                if result != 'OK':
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                if expected_reply_to:
                    reply_to = msg.get("Reply-To")
                    reply_to_clean = parseaddr(reply_to)[1] if reply_to else None
                    if reply_to_clean != expected_reply_to:
                        continue

                for part in msg.walk():
                    if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition') is None:
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue

                    # file_name filtresi: sadece beklenen dosya adini iceren ekleri indir
                    if file_name_filter and file_name_filter not in filename.lower():
                        continue

                    filepath = os.path.join(download_folder, filename)
                    with open(filepath, 'wb') as f:
                        f.write(part.get_payload(decode=True))
                        logger.info("Downloaded: %s to %s", filename, download_folder)
                    try:
                        self.convert_xml_to_dataframe(filepath).to_excel(
                            os.path.join(download_folder, f"{filename.split('.xml')[0]}.xlsx")
                        )
                        os.remove(filepath)
                    except Exception as e:
                        logger.error("XML donusum hatasi: %s", e)

            logger.info("Arama sorgusu: %s", search_criteria)
            logger.info("Bulunan %d eslesen e-posta", len(data[0].split()))

        logger.info("Tum satirlar islendi.")
        self._disconnect()

    def convert_xml_to_dataframe(self, file_path):
        """Parse namespace-based XML files to DataFrame."""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            namespace = root.tag.split("}")[0].strip("{")
            ns = {"ns": namespace}

            data = []
            for detail in root.findall("ns:Details", ns):
                data.append(detail.attrib)

            return pd.DataFrame(data)
        except Exception as e:
            logger.error("XML parse hatasi: %s", e)
            return pd.DataFrame()
