import imaplib
import email
import os
from datetime import datetime, timedelta
import pandas as pd
import shutil
import re
import imaplib
import email
import time
from datetime import datetime
from email.header import decode_header
import urllib.parse
import unicodedata


def normalize_turkish(text):
    """Türkçe karakterleri normalize eder - karşılaştırma için"""
    if not text:
        return ""
    # Unicode normalization
    text = unicodedata.normalize('NFKC', str(text))
    # Türkçe karakterleri ASCII eşdeğerlerine çevir
    turkish_map = {
        'İ': 'i', 'I': 'i', 'ı': 'i',  # Türkçe I/İ/ı -> i
        'Ğ': 'g', 'ğ': 'g',
        'Ü': 'u', 'ü': 'u',
        'Ş': 's', 'ş': 's',
        'Ö': 'o', 'ö': 'o',
        'Ç': 'c', 'ç': 'c',
    }
    for tr_char, ascii_char in turkish_map.items():
        text = text.replace(tr_char, ascii_char)
    return text.lower()


class FetchEmail():
    connection = None
    error = None

    def __init__(self, mail_server, username, password):
        self.imap_server = mail_server
        self.username = username
        self.password = password
        self._connect()

    def _decode_filename(self, filename):
        """Dosya adını decode eder (quoted-printable, base64, vb.)"""
        if not filename:
            return None

        try:
            # Önce email header decode işlemini dene
            decoded_header = decode_header(filename)
            decoded_filename = ""

            for text, encoding in decoded_header:
                if isinstance(text, bytes):
                    if encoding:
                        decoded_filename += text.decode(encoding)
                    else:
                        decoded_filename += text.decode('utf-8', errors='ignore')
                else:
                    decoded_filename += text

            # Eğer hala quoted-printable formatında ise, URL decode de dene
            if '=' in decoded_filename and any(c in decoded_filename for c in ['=C4', '=B0', '=C5', '=9F']):
                try:
                    # Quoted-printable decode
                    import quopri
                    decoded_filename = quopri.decodestring(decoded_filename.encode()).decode('utf-8', errors='ignore')
                except:
                    pass

            # Bazı özel quoted-printable karakterleri manuel olarak çevir
            replacements = {
                '=C4=B0': 'İ',  # İ harfi
                '=C5=9F': 'ş',  # ş harfi
                '=C4=9E': 'Ğ',  # Ğ harfi
                '=C3=A7': 'ç',  # ç harfi
                '=C3=BC': 'ü',  # ü harfi
                '=C3=B6': 'ö',  # ö harfi
                '=UTF-8Q': '',  # UTF-8Q prefix'ini kaldır
            }

            for old, new in replacements.items():
                decoded_filename = decoded_filename.replace(old, new)

            # Son olarak geçersiz karakterleri temizle
            decoded_filename = re.sub(r'[<>:"/\\|?*]', '', decoded_filename)

            return decoded_filename if decoded_filename else filename

        except Exception as e:
            print(f"Dosya adı decode edilirken hata: {e}")
            # Hata durumunda orijinal dosya adını temizleyerek döndür
            return re.sub(r'[<>:"/\\|?*]', '', filename)

    def _connect(self):
        """IMAP sunucusuna bağlan"""
        try:
            if self.connection:
                try:
                    self.connection.close()
                except:
                    pass

            self.connection = imaplib.IMAP4_SSL(self.imap_server, 993)
            self.connection.sock.settimeout(60)  # 60 saniye timeout
            self.connection.login(self.username, self.password)
            self.connection.select(readonly=False)
            print("IMAP bağlantısı başarıyla kuruldu.")
        except Exception as e:
            print(f"IMAP bağlantısı kurulurken hata: {e}")
            raise

    def _reconnect(self):
        """Bağlantıyı yeniden kur"""
        print("Bağlantı yeniden kuruluyor...")
        time.sleep(2)  # Kısa bir bekleme
        self._connect()

    def _is_connection_alive(self):
        """Bağlantının aktif olup olmadığını kontrol et"""
        try:
            self.connection.noop()
            return True
        except:
            return False

    def close_connection(self):
        """Bağlantıyı güvenli bir şekilde kapat"""
        try:
            if self.connection:
                self.connection.close()
                self.connection.logout()
                print("IMAP bağlantısı kapatıldı.")
        except Exception as e:
            print(f"Bağlantı kapatılırken hata: {e}")

    def has_attachments(self, msg):
        """Mail'de attachment olup olmadığını hızlıca kontrol et"""
        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':
                continue
            filename = part.get_filename()
            if filename:
                return True
        return False

    def save_attachment(self, msg, download_folder=None, targetHotels=None, subject_check=None, targetHotelsReply=None):
        if download_folder is None:
            download_folder = os.environ.get('TEMP', '.')  # Eğer TEMP yoksa, '.' olarak ata

        att_paths = []
        subject = msg.get('Subject', 'No_Subject')  # Eğer subject yoksa, hata almamak için default değer koyduk

        # Subject'i decode et
        decoded_subject = self._decode_filename(subject) if subject else 'No_Subject'
        cleaned_subject = re.sub(r'[<>:"/\\|?*]', '', decoded_subject)  # Geçersiz karakterleri temizle

        reply_to_header = msg.get("Reply-To")
        reply_to = email.utils.parseaddr(reply_to_header)[1] if reply_to_header else None  # Parse Reply-To like From
        parsed_address = email.utils.parseaddr(msg.get('From', ''))[1]  # Eğer From yoksa, boş string döndür

        # Mail tarihini al (dosya adı için timestamp)
        date_tuple = email.utils.parsedate_tz(msg.get("Date"))
        msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple)) if date_tuple else datetime.now()
        timestamp_suffix = msg_date.strftime("_%Y%m%d_%H%M%S")

        subject_check_value = None
        target_directory = download_folder

        print(f"\n=== DEBUG: Processing email ===")
        print(f"  Email subject: '{cleaned_subject}'")
        print(f"  Email date: {msg_date}")
        print(f"  From: {parsed_address}")
        print(f"  Reply-To (raw): {reply_to_header}")
        print(f"  Reply-To (parsed): {reply_to}")
        print(f"  subject_check is None: {subject_check is None}")

        if subject_check is not None:
            check_row = None
            if reply_to:
                # CRITICAL FIX: Reply-To varsa SADECE reply_to ile eşleştir, From'a bakma (duplicate önleme)
                check_row = subject_check[subject_check["reply_to_mail"] == reply_to]
                print(f"  check_row by reply_to: {len(check_row) if check_row is not None and not check_row.empty else 0} rows")
                if check_row.empty:
                    print(f"  ⚠ Reply-To '{reply_to}' found but not in Excel config. Skipping From check to avoid duplicates.")
            else:
                # Reply-To yoksa From adresine bak
                check_row = subject_check[subject_check["mail"] == parsed_address]
                print(f"  check_row by mail: {len(check_row) if check_row is not None and not check_row.empty else 0} rows")

            if check_row is not None and not check_row.empty:
                print(f"  Found {len(check_row)} matching rows in subject_check")
                if 'subject_check' in check_row.columns:
                    print(f"  subject_check values: {check_row['subject_check'].tolist()}")
                # CRITICAL FIX: Aynı mail için birden fazla company olabilir
                # Tüm satırları kontrol et ve subject'e uyan satırı bul
                matched_row = None

                for idx, row in check_row.iterrows():
                    row_subject_check = row["subject_check"]
                    row_file_name = row.get("file_name", None)
                    print(f"\n  Checking row: subject_check='{row_subject_check}', file_name='{row_file_name}', file_read='{row.get('file_read', 'N/A')}'")

                    # Eğer subject_check True (Boolean) ise, file_name ile eşleştir
                    if row_subject_check == True or str(row_subject_check).lower() == 'true':
                        if row_file_name and pd.notna(row_file_name):
                            # file_name'den subject pattern çıkar (underscore'dan önceki kısım)
                            file_name_pattern = str(row_file_name).split('_')[0] if '_' in str(row_file_name) else str(row_file_name)

                            # Email subject ile karşılaştır (Türkçe karakter desteğiyle)
                            pattern_normalized = normalize_turkish(file_name_pattern)
                            subject_normalized = normalize_turkish(cleaned_subject)
                            subject_match = pattern_normalized in subject_normalized or subject_normalized in pattern_normalized
                            print(f"    Pattern from file_name: '{file_name_pattern}'")
                            print(f"    Match check: '{pattern_normalized}' ↔ '{subject_normalized}' = {subject_match}")

                            if subject_match:
                                matched_row = row
                                subject_check_value = file_name_pattern
                                print(f"    ✓ MATCH! Using this row.")
                                break
                            else:
                                print(f"    ✗ No match, trying next row...")
                        else:
                            print(f"    ✗ subject_check is True but file_name is missing, trying next row...")
                    # Eğer subject_check string ise (eski davranış), o string ile eşleştir
                    elif row_subject_check and pd.notna(row_subject_check):
                        check_normalized = normalize_turkish(str(row_subject_check))
                        subject_normalized = normalize_turkish(cleaned_subject)
                        subject_in_cleaned = check_normalized in subject_normalized
                        print(f"    '{check_normalized}' in '{subject_normalized}' = {subject_in_cleaned}")

                        if subject_in_cleaned:
                            matched_row = row
                            subject_check_value = row_subject_check
                            print(f"    ✓ MATCH! Using this row.")
                            break
                        else:
                            print(f"    ✗ No match, trying next row...")
                    else:
                        # Subject check yoksa, bu satırı kullan (backward compatibility)
                        print(f"    No subject_check value, using this row (backward compatibility)")
                        matched_row = row
                        subject_check_value = None
                        break

                # Hiçbir satır eşleşmediyse, attachment kaydetme
                if matched_row is None:
                    print(f"⚠ No matching subject_check found for subject '{cleaned_subject}'")
                    print(f"  Available subject_check values: {check_row['subject_check'].tolist()}")
                    print(f"  Skipping attachment save.")
                    return []

                # Eşleşen satırın file_read değerini kullan (eğer varsa)
                if 'file_read' in matched_row.index and pd.notna(matched_row['file_read']):
                    target_directory = matched_row['file_read']
                    print(f"  Target directory from matched row: {target_directory}")
                elif reply_to and targetHotelsReply and reply_to in targetHotelsReply:
                    target_directory = targetHotelsReply[reply_to]
                elif targetHotels and parsed_address in targetHotels:
                    target_directory = targetHotels[parsed_address]

        for part in msg.walk():
            if part.get_content_maintype() == 'multipart':  # Eğer multipart ise atla
                continue
            filename = part.get_filename()
            if not filename:  # Eğer dosya ismi yoksa, devam et
                continue

            # Dosya adını decode et
            decoded_filename = self._decode_filename(filename)
            if not decoded_filename:
                continue

            if decoded_filename.lower().endswith(('.xlsx', '.xls', '.xml')):
                filename_with_subject = f"{cleaned_subject}_{decoded_filename}" if subject_check_value else decoded_filename

                # Dosya adına timestamp ekle (aynı isimli dosyalar üzerine yazılmasın)
                file_base, file_ext = os.path.splitext(filename_with_subject)
                filename_with_timestamp = f"{file_base}{timestamp_suffix}{file_ext}"
                
                att_path = os.path.join(target_directory, filename_with_timestamp)
                try:
                    with open(att_path, 'wb') as fp:
                        fp.write(part.get_payload(decode=True))
                    att_paths.append(att_path)
                    print(f"✓ Saved: {filename_with_timestamp}")
                except Exception as e:
                    print(f"✗ Error saving {filename_with_timestamp}: {e}")
                    print(f"Dosya kaydedildi: {att_path}")  # Debugging için eklenen çıktı
                    print(f"Orijinal subject: {subject}")  # Debug için
                    print(f"Decode edilmiş subject: {decoded_subject}")  # Debug için
                    print(f"Orijinal dosya adı: {filename}")  # Debug için
                    print(f"Decode edilmiş dosya adı: {decoded_filename}")  # Debug için
                except Exception as e:
                    print(f"Dosya kaydedilirken hata oluştu: {e}")

        return att_paths


    def fetch_all_messages(self, start_date, end_date=None, from_addresses=None, reply_to_addresses=None, mark_as_seen=False, subject_filters=None):
        """Tüm mailleri çeker (okunmuş/okunmamış fark etmeksizin)

        Args:
            start_date: Başlangıç tarihi (dahil)
            end_date: Bitiş tarihi (hariç) - None ise sadece SINCE kullanılır
            from_addresses: Gönderen adresleri
            reply_to_addresses: Reply-To adresleri (ortak mailbox için)
            mark_as_seen: Okundu işaretle
            subject_filters: Subject'te aranacak kelimeler listesi (örn: ['market', 'segment', 'pazar'])
        """
        emails = []
        formatted_start_date = start_date.strftime('%d-%b-%Y')
        formatted_end_date = end_date.strftime('%d-%b-%Y') if end_date else None
        message_map = {}

        # Tarih kriteri oluştur
        if formatted_end_date:
            date_criteria = f'SINCE "{formatted_start_date}" BEFORE "{formatted_end_date}"'
        else:
            date_criteria = f'SINCE "{formatted_start_date}"'

        # Reply-To adresleri ile arama (öncelikli)
        if reply_to_addresses:
            for address in reply_to_addresses:
                try:
                    # Bağlantı kontrolü
                    if not self._is_connection_alive():
                        self._reconnect()

                    # Reply-To header'ı ile ara + Subject filtreleme IMAP seviyesinde
                    print(f"Searching Reply-To: {address}")

                    # Subject filters varsa, her keyword için ayrı arama yap ve birleştir
                    message_ids = set()
                    if subject_filters:
                        for keyword in subject_filters:
                            try:
                                result, messages = self.connection.search(None,
                                    f'HEADER "Reply-To" "{address}"',
                                    date_criteria,
                                    f'SUBJECT "{keyword}"')
                                if result == "OK" and messages[0]:
                                    message_ids.update(messages[0].split())
                            except Exception as e:
                                print(f"  ⚠ Subject search error for '{keyword}': {e}")
                                continue
                    else:
                        # Subject filter yoksa tüm mailleri al
                        result, messages = self.connection.search(None,
                            f'HEADER "Reply-To" "{address}"',
                            date_criteria)
                        if result == "OK" and messages[0]:
                            message_ids.update(messages[0].split())

                    if not message_ids:
                        print(f"Reply-To {address} için mail bulunamadı.")
                        continue

                    print(f"  Found {len(message_ids)} matching emails")

                    # Bulunan message ID'leri için fetch yap
                    for message in message_ids:
                        try:
                            # Her mesaj işlemi öncesi bağlantı kontrolü
                            if not self._is_connection_alive():
                                self._reconnect()

                            ret, data = self.connection.fetch(message, '(RFC822)')

                            if ret != 'OK' or not data:
                                print(f"Mail ID {message} okunamadı! Fetch sonucu: {ret}, Data uzunluğu: {len(data) if data else 0}")
                                continue

                            msg = email.message_from_bytes(data[0][1])
                            subject = msg["Subject"]
                            sender = email.utils.parseaddr(msg.get('From', ''))[1]

                            date_tuple = email.utils.parsedate_tz(msg["Date"])
                            msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))

                            # CRITICAL FIX: Use sender+subject as key to avoid overwriting emails with same subject
                            unique_key = f"{sender}|{subject}"
                            if unique_key in message_map:
                                if message_map[unique_key]['date'] < msg_date:
                                    message_map[unique_key] = {'msg': msg, 'date': msg_date}
                            else:
                                message_map[unique_key] = {'msg': msg, 'date': msg_date}

                            # Opsiyonel olarak okundu işaretle
                            if mark_as_seen:
                                max_retries = 3
                                for attempt in range(max_retries):
                                    try:
                                        if not self._is_connection_alive():
                                            self._reconnect()

                                        self.connection.store(message, '+FLAGS', '\\Seen')
                                        break
                                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                                        print(f"Store işlemi hatası (deneme {attempt + 1}/{max_retries}): {e}")
                                        if attempt < max_retries - 1:
                                            time.sleep(1)
                                            self._reconnect()
                                        else:
                                            print(f"Mail {message} okundu olarak işaretlenemedi!")

                            print(f"Reply-To {address}: {subject} işlendi.")

                        except Exception as e:
                            print(f"Mail işleme hatası: {e}")
                            continue

                except Exception as e:
                    print(f"Reply-To {address} adresinden mail çekme hatası: {e}")
                    continue

        # From adresleri ile arama
        if from_addresses:
            for address in from_addresses:
                try:
                    # Bağlantı kontrolü
                    if not self._is_connection_alive():
                        self._reconnect()

                    print(f"Searching From: {address}")

                    # Subject filters varsa, her keyword için ayrı arama yap ve birleştir
                    message_ids = set()
                    if subject_filters:
                        for keyword in subject_filters:
                            try:
                                result, messages = self.connection.search(None,
                                    f'FROM "{address}"',
                                    date_criteria,
                                    f'SUBJECT "{keyword}"')
                                if result == "OK" and messages[0]:
                                    message_ids.update(messages[0].split())
                            except Exception as e:
                                print(f"  ⚠ Subject search error for '{keyword}': {e}")
                                continue
                    else:
                        # Subject filter yoksa tüm mailleri al
                        result, messages = self.connection.search(None,
                            f'FROM "{address}"',
                            date_criteria)
                        if result == "OK" and messages[0]:
                            message_ids.update(messages[0].split())

                    if not message_ids:
                        print(f"{address} Mail adresi için mail bulunamadı.")
                        continue

                    print(f"  Found {len(message_ids)} matching emails")

                    # Bulunan message ID'leri için fetch yap
                    for message in message_ids:
                        try:
                            # Her mesaj işlemi öncesi bağlantı kontrolü
                            if not self._is_connection_alive():
                                self._reconnect()

                            ret, data = self.connection.fetch(message, '(RFC822)')

                            if ret != 'OK' or not data:
                                print(f"Mail ID {message} okunamadı! Fetch sonucu: {ret}, Data uzunluğu: {len(data) if data else 0}")
                                continue

                            msg = email.message_from_bytes(data[0][1])
                            subject = msg["Subject"]
                            sender = email.utils.parseaddr(msg.get('From', ''))[1]

                            date_tuple = email.utils.parsedate_tz(msg["Date"])
                            msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))

                            # CRITICAL FIX: Use sender+subject as key to avoid overwriting emails with same subject
                            unique_key = f"{sender}|{subject}"
                            if unique_key in message_map:
                                if message_map[unique_key]['date'] < msg_date:
                                    message_map[unique_key] = {'msg': msg, 'date': msg_date}
                            else:
                                message_map[unique_key] = {'msg': msg, 'date': msg_date}

                            # Opsiyonel olarak okundu işaretle
                            if mark_as_seen:
                                max_retries = 3
                                for attempt in range(max_retries):
                                    try:
                                        if not self._is_connection_alive():
                                            self._reconnect()

                                        self.connection.store(message, '+FLAGS', '\\Seen')
                                        break
                                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                                        print(f"Store işlemi hatası (deneme {attempt + 1}/{max_retries}): {e}")
                                        if attempt < max_retries - 1:
                                            time.sleep(1)
                                            self._reconnect()
                                        else:
                                            print(f"Mail {message} okundu olarak işaretlenemedi!")

                            print(f"From {address}: {subject} işlendi.")

                        except Exception as e:
                            print(f"Mail işleme hatası: {e}")
                            continue

                except Exception as e:
                    print(f"{address} adresinden mail çekme hatası: {e}")
                    continue

        else:
            try:
                if not self._is_connection_alive():
                    self._reconnect()

                print("Searching all emails (no address filter)")

                # Subject filters varsa, her keyword için ayrı arama yap ve birleştir
                message_ids = set()
                if subject_filters:
                    for keyword in subject_filters:
                        try:
                            result, messages = self.connection.search(None,
                                date_criteria,
                                f'SUBJECT "{keyword}"')
                            if result == "OK" and messages[0]:
                                message_ids.update(messages[0].split())
                        except Exception as e:
                            print(f"  ⚠ Subject search error for '{keyword}': {e}")
                            continue
                else:
                    # Subject filter yoksa tüm mailleri al
                    result, messages = self.connection.search(None, date_criteria)
                    if result == "OK" and messages[0]:
                        message_ids.update(messages[0].split())

                if not message_ids:
                    print("Belirtilen tarihten sonra e-posta bulunamadı.")
                else:
                    print(f"  Found {len(message_ids)} matching emails")

                    for message in message_ids:
                        try:
                            if not self._is_connection_alive():
                                self._reconnect()

                            ret, data = self.connection.fetch(message, '(RFC822)')
                            msg = email.message_from_bytes(data[0][1])
                            subject = msg["Subject"]
                            sender = email.utils.parseaddr(msg.get('From', ''))[1]

                            date_tuple = email.utils.parsedate_tz(msg["Date"])
                            msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))

                            # CRITICAL FIX: Use sender+subject as key to avoid overwriting emails with same subject
                            unique_key = f"{sender}|{subject}"
                            if unique_key in message_map:
                                if message_map[unique_key]['date'] < msg_date:
                                    message_map[unique_key] = {'msg': msg, 'date': msg_date}
                            else:
                                message_map[unique_key] = {'msg': msg, 'date': msg_date}

                            # Opsiyonel olarak okundu işaretle
                            if mark_as_seen:
                                max_retries = 3
                                for attempt in range(max_retries):
                                    try:
                                        if not self._is_connection_alive():
                                            self._reconnect()

                                        self.connection.store(message, '+FLAGS', '\\Seen')
                                        break
                                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                                        print(f"Store işlemi hatası (deneme {attempt + 1}/{max_retries}): {e}")
                                        if attempt < max_retries - 1:
                                            time.sleep(1)
                                            self._reconnect()
                                        else:
                                            print(f"Mail {message} okundu olarak işaretlenemedi!")
                        except Exception as e:
                            print(f"Mail işleme hatası: {e}")
                            continue

            except Exception as e:
                print(f"Genel mail çekme hatası: {e}")

        for item in message_map.values():
            emails.append(item['msg'])

        return emails


    def parse_email_address(self, email_address):
        return email.utils.parseaddr(email_address)

    def move_files_with_timestamp(self, df):
        history_folder = df['file_history'].iloc[0]
        os.makedirs(history_folder, exist_ok=True)

        unique_file_reads = df['file_read'].unique()
        for file_read in unique_file_reads:
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

            source_folder = file_read.replace('\\', '/')
            destination_folder = history_folder
            file_hotel = df[df['file_read'] == file_read]['file_hotel'].iloc[0]

            for filename in os.listdir(source_folder):
                source_file_path = os.path.join(source_folder, filename)
                if os.path.isfile(source_file_path):
                    new_filename = f"{os.path.splitext(filename)[0]}_{file_hotel}_{timestamp}{os.path.splitext(filename)[1]}"
                    destination_file_path = os.path.join(destination_folder, new_filename)
                    shutil.move(source_file_path, destination_file_path)


