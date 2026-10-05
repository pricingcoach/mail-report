import email
from email.utils import parseaddr
from datetime import datetime, timedelta
import imaplib
import logging
import os
import xml.etree.ElementTree as ET

import pandas as pd

from hotel_analysis.email_base import EmailBase

logger = logging.getLogger(__name__)


class EmailIntegration(EmailBase):
    """Email integration for reservation_code, extending shared EmailBase.

    Inherits: _connect, _disconnect, normalize, _get_date_filter
    """

    def save_attachment(self, email_configs):
        """Download email attachments based on config rules.

        Args:
            email_configs: list of dicts with keys: mail, subject, reply_to_mail, file, file_name
        """
        mail = self._connect()
        _today = datetime.today().date()
        since_str = _today.strftime("%d-%b-%Y")
        before_str = (_today + timedelta(days=1)).strftime("%d-%b-%Y")

        for row in email_configs:
            subject = self.normalize(row['subject'])
            sender = self.normalize(row['mail'])
            expected_reply_to = self.normalize(row.get('reply_to_mail') or '')
            download_folder = row['file']
            file_name_filter = (row.get('file_name') or '').lower()
            os.makedirs(download_folder, exist_ok=True)

            search_criteria = (
                f'FROM "{sender}" SUBJECT "{subject}" SINCE "{since_str}" BEFORE "{before_str}"'
            )
            try:
                result, data = mail.uid('SEARCH', None, search_criteria)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ConnectionResetError, OSError) as e:
                logger.warning("IMAP search hatasi, yeniden baglaniliyor: %s", e)
                try:
                    mail = self._reconnect()
                    result, data = mail.uid('SEARCH', None, search_criteria)
                except Exception as e2:
                    logger.error("Yeniden baglanti sonrasi arama basarisiz (%s): %s", subject, e2)
                    continue

            logger.info("Arama sorgusu: %s", search_criteria)

            if result != 'OK':
                logger.warning("IMAP SEARCH basarisiz (result=%s): %s", result, search_criteria)
                continue

            uids = data[0].split()
            logger.info("Bulunan %d eslesen e-posta (subject=%s)", len(uids), subject)
            if not uids:
                continue

            # reply_to filtresi varsa: tüm uid'leri header ile tara, eşleşeni bul.
            # reply_to yoksa: sadece en son email yeterli.
            if expected_reply_to:
                matched_uid = None
                for uid in reversed(uids):  # en yeniden eskiye
                    try:
                        r, hdr_data = mail.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (REPLY-TO)])')
                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ConnectionResetError, OSError) as e:
                        logger.warning("Header fetch hatasi (uid=%s): %s", uid, e)
                        try:
                            mail = self._reconnect()
                            r, hdr_data = mail.uid('FETCH', uid, '(BODY.PEEK[HEADER.FIELDS (REPLY-TO)])')
                        except Exception as e2:
                            logger.error("Yeniden baglanti sonrasi header fetch basarisiz (uid=%s): %s", uid, e2)
                            continue
                    if r != 'OK' or not hdr_data or not isinstance(hdr_data[0], tuple):
                        continue
                    hdr_msg = email.message_from_bytes(hdr_data[0][1])
                    rt = hdr_msg.get("Reply-To")
                    rt_clean = parseaddr(rt)[1].lower() if rt else None
                    if rt_clean == expected_reply_to.lower():
                        matched_uid = uid
                        break
                if matched_uid is None:
                    if len(uids) == 1:
                        logger.warning("reply_to bulunamadi ama tek email var, kullaniliyor: subject=%s reply_to=%s", subject, expected_reply_to)
                        target_uids = uids
                    else:
                        logger.warning("reply_to eslesmesi bulunamadi, atlaniyor: subject=%s reply_to=%s", subject, expected_reply_to)
                        continue
                else:
                    target_uids = [matched_uid]
            else:
                target_uids = uids[-1:]  # reply_to filtresi yoksa sadece en son

            for uid in target_uids:
                try:
                    result, msg_data = mail.uid('FETCH', uid, '(RFC822)')
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ConnectionResetError, OSError) as e:
                    logger.warning("IMAP fetch hatasi, yeniden baglaniliyor: %s", e)
                    try:
                        mail = self._reconnect()
                        result, msg_data = mail.uid('FETCH', uid, '(RFC822)')
                    except Exception as e2:
                        logger.error("Yeniden baglanti sonrasi fetch basarisiz (uid=%s): %s", uid, e2)
                        continue
                if result != 'OK':
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                for part in msg.walk():
                    if part.get_content_maintype() == 'multipart' or part.get('Content-Disposition') is None:
                        continue

                    filename = part.get_filename()
                    if not filename:
                        continue

                    if file_name_filter and file_name_filter not in filename.lower():
                        continue

                    filepath = os.path.join(download_folder, filename)
                    with open(filepath, 'wb') as f:
                        f.write(part.get_payload(decode=True))
                        logger.info("Downloaded: %s to %s", filename, download_folder)

                    try:
                        base_name = os.path.splitext(filename)[0]
                        xlsx_out = os.path.join(download_folder, f"{base_name}.xlsx")

                        if "p1432" in filename.lower() or "blockenteredonby" in filename.lower():
                            # P1432 XML'i donusturme — oldugu gibi birak, HotelUpdate isle
                            logger.info("P1432 XML indirildi, donusturme atlanir: %s", filename)
                            continue
                        elif "grppickup" in filename.lower():
                            # grppickup XML'i donusturme — oldugu gibi birak, HotelUpdate isle
                            logger.info("grppickup XML indirildi, donusturme atlanir: %s", filename)
                            continue
                        elif filename.lower().endswith('.txt'):
                            self.parse_txt_to_excel(filepath, xlsx_out)
                        elif "can" in filename.lower():
                            self.parse_cancel_report(filepath).to_excel(xlsx_out)
                        elif "indgrp" in filename.lower():
                            self.parse_xml_to_excel(filepath, xlsx_out)
                        elif "resenteredon" in filename.lower():
                            self.parse_resenteredon_xml_to_excel(filepath, xlsx_out)
                        else:
                            self.convert_xml_to_dataframe(filepath).to_excel(xlsx_out)
                        os.remove(filepath)
                    except Exception as e:
                        logger.error("XML donusturme hatasi (%s): %s", filename, e)

        logger.info("Tum satirlar islendi.")
        self._disconnect()

    def save_royan_attachment(self, df):
        mail = self._connect()
        date_filter = self._get_date_filter()

        for _, row in df.iterrows():
            subject = self.normalize(row['subject'])
            sender = self.normalize(row['mail'])
            download_folder = row['file']
            os.makedirs(download_folder, exist_ok=True)

            room_codes = set(self.normalize(code).lower() for code in row['room_code'])

            search_criteria = f'X-GM-RAW "subject:\\\"{subject}\\\" from:{sender} {date_filter}"'
            try:
                result, data = mail.uid('SEARCH', None, search_criteria)
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ConnectionResetError, OSError) as e:
                logger.warning("IMAP search hatasi, yeniden baglaniliyor: %s", e)
                try:
                    mail = self._reconnect()
                    result, data = mail.uid('SEARCH', None, search_criteria)
                except Exception as e2:
                    logger.error("Yeniden baglanti sonrasi arama basarisiz (%s): %s", subject, e2)
                    continue

            if result != 'OK':
                continue

            uids = data[0].split()
            for uid in uids:
                try:
                    result, msg_data = mail.uid('FETCH', uid, '(RFC822)')
                except (imaplib.IMAP4.abort, imaplib.IMAP4.error, ConnectionResetError, OSError) as e:
                    logger.warning("IMAP fetch hatasi, yeniden baglaniliyor: %s", e)
                    try:
                        mail = self._reconnect()
                        result, msg_data = mail.uid('FETCH', uid, '(RFC822)')
                    except Exception as e2:
                        logger.error("Yeniden baglanti sonrasi fetch basarisiz (uid=%s): %s", uid, e2)
                        continue
                if result != 'OK':
                    continue

                msg = email.message_from_bytes(msg_data[0][1])

                for part in msg.walk():
                    content_disposition = part.get_content_disposition()
                    filename = part.get_filename()

                    if content_disposition not in ['attachment', 'inline'] or not filename:
                        continue

                    if not filename.lower().endswith(('.xlsx', '.xls')):
                        continue

                    filename_lower = filename.lower()
                    if not room_codes or any(code.lower() in filename_lower for code in room_codes):
                        filepath = os.path.join(download_folder, filename)
                        if not os.path.exists(filepath):
                            with open(filepath, 'wb') as f:
                                f.write(part.get_payload(decode=True))
                            logger.info("Downloaded Royan: %s", filename)

            logger.info("Search: %s", search_criteria)
            logger.info("Found %d emails.", len(uids))

        self._disconnect()

    def convert_xml_to_dataframe(self, file_path):
        tree = ET.parse(file_path)
        root = tree.getroot()

        data = []
        tag_dict = {
            "PS2600_LSGRD_RESV": 'LIST_G_1/G_1',
            "P2617_RESENTEREDON_INDGRP": './/G_NO_OF_ROOMS',
            "P2910_MARKETSEGMENT": './/G_DETAIL',
            "RESENTEREDON": './/G_ROOM',
            "HISTORY_FORECAST": './/G_CONSIDERED_DATE',
            "RESCANCEL": './/G_FULL_NAME'
        }

        for _key, item in tag_dict.items():
            for detail in root.findall(item):
                record = {elem.tag: elem.text for elem in detail}
                data.append(record)

        return pd.DataFrame(data)

    def parse_txt_to_excel(self, txt_file_path, output_excel_path):
        """Tab-delimited Opera TXT raporunu Excel'e donustur."""
        with open(txt_file_path, 'r', encoding='utf-8') as f:
            raw_lines = f.readlines()

        # Header satirini dinamik bul (ORDER_GROUP veya FULL_NAME iceren satir)
        header_idx = 3  # default: 4. satir (Sheraton vb.)
        for i, line in enumerate(raw_lines[:10]):
            if 'ORDER_GROUP' in line or 'FULL_NAME' in line:
                header_idx = i
                break

        header_line = raw_lines[header_idx].rstrip('\n')
        headers = header_line.split('\t')
        expected_tabs = len(headers) - 1

        # Data satirlarini birlestir (newline iceren alanlar icin)
        data_lines = []
        current = ''
        for line in raw_lines[header_idx + 1:]:
            line = line.rstrip('\n')
            if not line.strip():
                continue
            if current:
                current = current + ' ' + line
            else:
                current = line
            if current.count('\t') >= expected_tabs:
                data_lines.append(current)
                current = ''
        if current.strip():
            data_lines.append(current)

        # DataFrame olustur
        rows = [line.split('\t') for line in data_lines]
        df = pd.DataFrame(rows, columns=headers)

        # Footer/summary satirini kaldir (RESV_NAME_ID bos olan)
        if 'RESV_NAME_ID' in df.columns:
            df = df[df['RESV_NAME_ID'].notna() & (df['RESV_NAME_ID'] != '')].reset_index(drop=True)

        # Numerik donusumler (XML parse ile ayni tipler)
        float_cols = ['REVENUE', 'ROOM_REVENUE', 'SHARE_AMOUNT', 'CF_ADR']
        int_cols = ['NO_OF_ROOMS', 'ADULTS', 'CHILDREN', 'CHILDREN1',
                    'CHILDREN2', 'CHILDREN3', 'PERSON', 'NIGHTS']
        for col in float_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        for col in int_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')

        df.to_excel(output_excel_path, sheet_name='Reservations', index=False)
        logger.info("TXT -> Excel donusturuldu: %s (%d rezervasyon)", output_excel_path, len(df))
        return df

    def parse_resenteredon_xml_to_excel(self, xml_file_path, output_excel_path):
        """Opera Cloud RESENTEREDON XML raporunu Excel'e donustur."""
        tree = ET.parse(xml_file_path)
        root = tree.getroot()

        # Opera Cloud yeni formatta G_NO_OF_ROOMS, eski formatta G_ROOM kullanir
        nodes = root.findall('.//G_NO_OF_ROOMS') or root.findall('.//G_ROOM')
        data = []
        for g_room in nodes:
            record = {elem.tag: elem.text for elem in g_room}
            data.append(record)

        df = pd.DataFrame(data)

        # Numerik donusumler
        float_cols = ['SHARE_AMOUNT', 'SHARE_AMOUNT_PER_STAY']
        int_cols = ['NO_OF_ROOMS', 'PERSONS', 'NIGHTS']
        for col in float_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        for col in int_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce').astype('Int64')

        df.to_excel(output_excel_path, sheet_name='Reservations', index=False)
        logger.info("RESENTEREDON XML -> Excel: %s (%d rezervasyon)", output_excel_path, len(df))
        return df

    def parse_cancel_report(self, file_path):
        tree = ET.parse(file_path)
        root = tree.getroot()
        rows = []

        for arrival in root.findall('.//G_ARRIVAL'):
            rows.append({
                'RESV_NAME_ID': arrival.findtext('RESV_NAME_ID', default=""),
                'CANCELLATION_DATE': arrival.findtext('CANCELLATION_DATE', default="")
            })

        for node in root.findall('.//G_FULL_NAME'):
            rows.append({
                'RESV_NAME_ID': node.findtext('RESV_NAME_ID2', default=""),
                'CANCELLATION_DATE': node.findtext('CANCELLATION_DATE', default="")
            })

        return pd.DataFrame(rows)

    def parse_xml_to_excel(self, xml_file_path, output_excel_path):
        try:
            tree = ET.parse(xml_file_path)
            root = tree.getroot()
        except ET.ParseError as e:
            logger.warning("XML bozuk, lxml recovery deneniyor: %s — %s",
                           os.path.basename(xml_file_path), e)
            from lxml import etree as lxml_et
            parser = lxml_et.XMLParser(recover=True)
            tree = lxml_et.parse(xml_file_path, parser)
            root = tree.getroot()
            if root is None:
                raise ValueError(f"lxml recovery de basarisiz: {xml_file_path}")
            logger.warning("lxml recovery basarili — bozuk kayit(lar) atlanmis olabilir: %s",
                           os.path.basename(xml_file_path))

        all_reservations = []

        for report_group in root.findall('.//G_REPORT_GROUP'):
            group_name = report_group.find('REPORT_GROUP')
            group_name_text = group_name.text if group_name is not None else ""

            for report_group2 in report_group.findall('.//G_REPORT_GROUP2'):
                for currency_group in report_group2.findall('.//G_CURRENCY_CODE'):
                    currency_code_element = currency_group.find('CURRENCY_CODE')
                    currency_code = currency_code_element.text if currency_code_element is not None else ""

                    for reservation in currency_group.findall('.//G_NO_OF_ROOMS'):
                        reservation_data = {
                            'CURRENCY_CODE': currency_code,
                            'GROUP_NAME': group_name_text,
                        }

                        for field in reservation:
                            if field.text is not None:
                                try:
                                    if field.tag in ['REVENUE', 'ROOM_REVENUE', 'SHARE_AMOUNT', 'CF_ADR']:
                                        reservation_data[field.tag] = float(field.text)
                                    elif field.tag in ['NO_OF_ROOMS', 'ADULTS', 'CHILDREN', 'CHILDREN1',
                                                        'CHILDREN2', 'CHILDREN3', 'PERSON', 'NIGHTS']:
                                        reservation_data[field.tag] = int(field.text)
                                    else:
                                        reservation_data[field.tag] = field.text
                                except (ValueError, TypeError):
                                    reservation_data[field.tag] = field.text
                            else:
                                reservation_data[field.tag] = ""

                        all_reservations.append(reservation_data)

        df = pd.DataFrame(all_reservations)

        columns = ['CURRENCY_CODE', 'GROUP_NAME'] + [col for col in df.columns if col not in ['CURRENCY_CODE', 'GROUP_NAME']]
        df = df.reindex(columns=columns)

        _summary_cols = {'NO_OF_ROOMS', 'ADULTS', 'CHILDREN', 'REVENUE', 'SHARE_AMOUNT', 'NIGHTS'}

        with pd.ExcelWriter(output_excel_path, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Reservations', index=False)

            if df.empty or not _summary_cols.issubset(df.columns):
                logger.warning("parse_xml_to_excel: Beklenen kolonlar eksik veya df bos, ozet atlaniyor.")
            else:
                currency_summary = df.groupby('CURRENCY_CODE').agg({
                    'NO_OF_ROOMS': 'sum',
                    'ADULTS': 'sum',
                    'CHILDREN': 'sum',
                    'REVENUE': 'sum',
                    'SHARE_AMOUNT': 'sum',
                    'NIGHTS': 'sum'
                }).reset_index()
                currency_summary.to_excel(writer, sheet_name='Currency_Summary', index=False)

                group_summary = df.groupby(['GROUP_NAME', 'CURRENCY_CODE']).agg({
                    'NO_OF_ROOMS': 'sum',
                    'ADULTS': 'sum',
                    'CHILDREN': 'sum',
                    'REVENUE': 'sum',
                    'SHARE_AMOUNT': 'sum',
                    'NIGHTS': 'sum'
                }).reset_index()
                group_summary.to_excel(writer, sheet_name='Group_Summary', index=False)

        logger.info("Veriler %s dosyasina kaydedildi. Toplam %d rezervasyon.", output_excel_path, len(df))

        return df
