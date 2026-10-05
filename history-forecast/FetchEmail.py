import imaplib
import email
import os
import re
import shutil
import time
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta
from email.header import decode_header
import pandas as pd


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
                except Exception:
                    pass

            self.connection = imaplib.IMAP4_SSL(self.imap_server, 993)
            self.connection.sock.settimeout(30)  # 30 saniye timeout (optimize edildi)
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

    @staticmethod
    def _match_rows_by_filename_parts(rows, subject_lower):
        """file_name parçaları ile subject eşleşmesi — reply_to ve From path'leri paylaşır."""
        return rows[
            rows["file_name"].fillna('').apply(
                lambda fn: any(
                    part in subject_lower
                    for part in fn.lower().replace('_history_forecast', '').split('_')
                    if len(part) >= 3
                )
            )
        ]

    def _is_connection_alive(self):
        """Bağlantının aktif olup olmadığını kontrol et"""
        try:
            self.connection.noop()
            return True
        except Exception:
            return False

    def _mark_seen(self, message_id):
        """Mesajı okundu olarak işaretle (3 deneme)"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.connection.store(message_id, '+FLAGS', '\\Seen')
                return
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error):
                if attempt < max_retries - 1:
                    time.sleep(1)
                    self._reconnect()
                else:
                    print(f"Mail {message_id} okundu olarak işaretlenemedi!")

    def close_connection(self):
        """Bağlantıyı güvenli bir şekilde kapat"""
        try:
            if self.connection:
                self.connection.close()
                self.connection.logout()
                print("IMAP bağlantısı kapatıldı.")
        except Exception as e:
            print(f"Bağlantı kapatılırken hata: {e}")

    def save_attachment(self, msg, download_folder=None, targetHotels=None, subject_check=None):
        if download_folder is None:
            download_folder = os.environ.get('TEMP', '.')  # Eğer TEMP yoksa, '.' olarak ata

        att_paths = []
        subject = msg.get('Subject', 'No_Subject')  # Eğer subject yoksa, hata almamak için default değer koyduk
        
        # Subject'i decode et
        decoded_subject = self._decode_filename(subject) if subject else 'No_Subject'
        cleaned_subject = re.sub(r'[<>:"/\\|?*]', '', decoded_subject)  # Geçersiz karakterleri temizle

        reply_to = msg.get("Reply-To")
        parsed_address = email.utils.parseaddr(msg.get('From', ''))[1]  # Eğer From yoksa, boş string döndür
        
        # Reply-To parse et (email adresi çıkar) ve lowercase yap
        if reply_to:
            reply_to = email.utils.parseaddr(reply_to)[1].lower()
        
        # From adresini de lowercase yap (email adresleri case-insensitive)
        parsed_address = parsed_address.lower()

        subject_check_value = None
        target_directory = download_folder  
        matched_by_reply_to = False

        # Debug log
        print(f"\n📧 Mail işleniyor:")
        print(f"   From: {parsed_address}")
        print(f"   Reply-To: {reply_to}")
        print(f"   Subject: {subject[:50]}...")

        if subject_check is not None:
            check_row = None
            
            # Önce reply_to ile kontrol et (case-insensitive)
            if reply_to:
                reply_to_column = subject_check["reply_to_mail"].fillna('').str.lower()
                check_row = subject_check[reply_to_column == reply_to]
                if not check_row.empty:
                    matched_by_reply_to = True
                    print(f"   ✅ Reply-To ile eşleşti!")
            
            # reply_to ile bulunamadıysa, normal mail kontrolü yap (case-insensitive)
            if check_row is None or check_row.empty:
                check_row = subject_check[subject_check["mail"].str.lower() == parsed_address]
                matched_by_reply_to = False
                if not check_row.empty:
                    print(f"   ✅ Mail ile eşleşti!")

            if check_row is not None and not check_row.empty:
                subject_check_value = check_row["subject_check"].iloc[0]
                
                # Hangi yöntemle eşleştiyse, SADECE o yöntemi kullan
                if matched_by_reply_to:
                    # Bu reply_to'yu paylaşan TÜM hotel satırlarını bul
                    reply_rows = subject_check[
                        subject_check["reply_to_mail"].fillna('').str.lower() == reply_to
                    ]

                    if len(reply_rows) == 1:
                        target_directory = reply_rows["file"].iloc[0]
                        subject_check_value = reply_rows["subject_check"].iloc[0]
                        print(f"   📁 Dizin (Reply-To tekil): {target_directory}")
                    else:
                        # Paylaşılan reply_to → subject ile oteli ayırt et
                        decoded_subject_lower = decoded_subject.lower()
                        subject_match = self._match_rows_by_filename_parts(reply_rows, decoded_subject_lower)
                        if not subject_match.empty:
                            target_directory = subject_match["file"].iloc[0]
                            subject_check_value = subject_match["subject_check"].iloc[0]
                            print(f"   📁 Dizin (Reply-To+Subject): {target_directory}")
                        else:
                            print(f"   ⚠️ reply_to paylaşımlı, subject eşleşmedi: {decoded_subject[:60]}")
                else:
                    # Aynı sender'dan birden fazla hotel varsa subject ile eşleştir
                    if len(check_row) > 1:
                        decoded_subject_lower = decoded_subject.lower()
                        # Önce subject_check string eşleşmesi (gerçek string ise)
                        subject_match = check_row[
                            check_row["subject_check"].notna() &
                            check_row["subject_check"].apply(
                                lambda s: isinstance(s, str) and s.lower() in decoded_subject_lower
                            )
                        ]
                        # subject_check boolean True ise (veya string eşleşmesi başarısızsa)
                        # reply_to path ile aynı yöntemi kullan: file_name parçaları ile eşleştir
                        if subject_match.empty:
                            subject_match = self._match_rows_by_filename_parts(check_row, decoded_subject_lower)
                        if not subject_match.empty:
                            target_directory = subject_match["file"].iloc[0]
                            subject_check_value = subject_match["subject_check"].iloc[0]
                            print(f"   📁 Dizin (Subject match): {target_directory}")
                        else:
                            matching_key = next((k for k in (targetHotels or {}) if k.lower() == parsed_address), None)
                            if matching_key:
                                target_directory = targetHotels[matching_key]
                                print(f"   📁 Dizin (Mail fallback): {target_directory}")
                            else:
                                print(f"   ⚠️ Mail ile eşleşti ama hiçbir subject eşleşmedi ve targetHotels'de bulunamadı!")
                    else:
                        # Tekil eşleşme: eski dict lookup yeterli
                        matching_key = next((k for k in (targetHotels or {}) if k.lower() == parsed_address), None)
                        if matching_key:
                            target_directory = targetHotels[matching_key]
                            print(f"   📁 Dizin (Mail): {target_directory}")
                        else:
                            print(f"   ⚠️ Mail ile eşleşti ama targetHotels'de bulunamadı!")

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

                att_path = os.path.join(target_directory, filename_with_subject)
                try:
                    with open(att_path, 'wb') as fp:
                        fp.write(part.get_payload(decode=True))
                    att_paths.append(att_path)
                    print(f"Dosya kaydedildi: {att_path}")  # Debugging için eklenen çıktı
                    print(f"Orijinal subject: {subject}")  # Debug için
                    print(f"Decode edilmiş subject: {decoded_subject}")  # Debug için
                    print(f"Orijinal dosya adı: {filename}")  # Debug için
                    print(f"Decode edilmiş dosya adı: {decoded_filename}")  # Debug için
                except Exception as e:
                    print(f"Dosya kaydedilirken hata oluştu: {e}")

        return att_paths


    def fetch_unread_messages(self, since_date, from_addresses=None):
        
        emails = []
        formatted_date = since_date.strftime('%d-%b-%Y')
        message_map = {}  
        
        if from_addresses:
            for address in from_addresses:
                try:
                    # Bağlantı kontrolü
                    if not self._is_connection_alive():
                        self._reconnect()
                    
                    # IMAP search - tüm kriterler tek string içinde olmalı
                    search_criteria = f'(FROM "{address}" SINCE {formatted_date} UNSEEN)'
                    result, messages = self.connection.search(None, search_criteria)
                    if result == "OK" and messages[0].split():
                        message_ids = messages[0].split()
                        
                        for message in message_ids:
                            try:
                                # Her mesaj işlemi öncesi bağlantı kontrolü
                                if not self._is_connection_alive():
                                    self._reconnect()
                                
                                # ÖNEMLİ: Önce sadece HEADER çek (çok daha hızlı)
                                ret, data = self.connection.fetch(message, '(BODY[HEADER.FIELDS (FROM SUBJECT DATE)])')
                                
                                if ret != 'OK' or not data or not data[0]:
                                    print(f"Mail ID {message} header okunamadı!")
                                    continue
                                
                                # Header'dan subject ve date al - data format kontrolü
                                try:
                                    header_data = data[0][1]
                                    if isinstance(header_data, bytes):
                                        header_text = header_data.decode('utf-8', errors='ignore')
                                    elif isinstance(header_data, str):
                                        header_text = header_data
                                    else:
                                        # Beklenmeyen format, tam mesajı çek
                                        ret, full_data = self.connection.fetch(message, '(RFC822)')
                                        if ret != 'OK' or not full_data:
                                            continue
                                        msg = email.message_from_bytes(full_data[0][1])
                                        subject = msg.get('Subject', 'No_Subject')
                                        decoded_subject = self._decode_filename(subject) if subject else 'No_Subject'
                                        date_tuple = email.utils.parsedate_tz(msg.get("Date"))
                                        if not date_tuple:
                                            continue
                                        msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))
                                        dedup_key = f"{address}|{decoded_subject}"
                                        message_map[dedup_key] = {'msg': msg, 'date': msg_date}
                                        
                                        # Store işlemi
                                        max_retries = 3
                                        for attempt in range(max_retries):
                                            try:
                                                if not self._is_connection_alive():
                                                    self._reconnect()
                                                self.connection.store(message, '+FLAGS', '\\Seen')
                                                break
                                            except:
                                                if attempt < max_retries - 1:
                                                    time.sleep(1)
                                                    self._reconnect()
                                        
                                        print(f"{address} Mail adresi {decoded_subject} okunmuştur.")
                                        time.sleep(0.2)  # Rate limiting
                                        continue
                                except Exception as e:
                                    print(f"Header decode hatası: {e}, tam mesaj çekiliyor...")
                                    # Header decode edilemezse tam mesajı çek
                                    ret, full_data = self.connection.fetch(message, '(RFC822)')
                                    if ret != 'OK' or not full_data:
                                        continue
                                    msg = email.message_from_bytes(full_data[0][1])
                                    subject = msg.get('Subject', 'No_Subject')
                                    decoded_subject = self._decode_filename(subject) if subject else 'No_Subject'
                                    date_tuple = email.utils.parsedate_tz(msg.get("Date"))
                                    if not date_tuple:
                                        continue
                                    msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))
                                    dedup_key = f"{address}|{decoded_subject}"
                                    message_map[dedup_key] = {'msg': msg, 'date': msg_date}
                                    
                                    # Store işlemi
                                    max_retries = 3
                                    for attempt in range(max_retries):
                                        try:
                                            if not self._is_connection_alive():
                                                self._reconnect()
                                            self.connection.store(message, '+FLAGS', '\\Seen')
                                            break
                                        except Exception:
                                            if attempt < max_retries - 1:
                                                time.sleep(1)
                                                self._reconnect()
                                    
                                    print(f"{address} Mail adresi {decoded_subject} okunmuştur.")
                                    time.sleep(0.2)  # Rate limiting
                                    continue
                                
                                header_msg = email.message_from_string(header_text)
                                subject = header_msg.get('Subject', 'No_Subject')
                                decoded_subject = self._decode_filename(subject) if subject else 'No_Subject'
                                
                                date_tuple = email.utils.parsedate_tz(header_msg["Date"])
                                if not date_tuple:
                                    print(f"Tarih parse edilemedi: {decoded_subject}")
                                    continue
                                    
                                msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple))
                                
                                # Sender + Subject kombinasyonunu key olarak kullan
                                dedup_key = f"{address}|{decoded_subject}"
                                
                                # Sadece daha yeni mail varsa veya ilk kez görüyorsak TAM mesajı çek
                                should_fetch_full = False
                                if dedup_key in message_map:
                                    if message_map[dedup_key]['date'] < msg_date:
                                        should_fetch_full = True
                                else:
                                    should_fetch_full = True
                                
                                if should_fetch_full:
                                    # Tam mesajı çek
                                    ret, full_data = self.connection.fetch(message, '(RFC822)')
                                    if ret == 'OK' and full_data:
                                        msg = email.message_from_bytes(full_data[0][1])
                                        message_map[dedup_key] = {'msg': msg, 'date': msg_date}
                                
                                # Store işlemi için bağlantı kontrolü ve hata yakalama
                                max_retries = 3
                                for attempt in range(max_retries):
                                    try:
                                        if not self._is_connection_alive():
                                            self._reconnect()
                                        
                                        self.connection.store(message, '+FLAGS', '\\Seen')
                                        break
                                    except (imaplib.IMAP4.abort, imaplib.IMAP4.error) as e:
                                        if attempt < max_retries - 1:
                                            time.sleep(1)
                                            self._reconnect()
                                        else:
                                            print(f"Mail {message} okundu olarak işaretlenemedi!")
                                
                                print(f"{address} Mail adresi {decoded_subject} okunmuştur.")
                                
                                # Rate limiting için kısa bekleme
                                time.sleep(0.2)
                                
                            except Exception as e:
                                print(f"Mail işleme hatası: {e}")
                                # Bağlantı hatası ise yeniden bağlan
                                if "timed out" in str(e).lower() or "abort" in str(e).lower():
                                    try:
                                        self._reconnect()
                                    except:
                                        pass
                                time.sleep(0.5)  # Hata sonrası daha uzun bekle
                                continue
                    else:
                        print(f"{address} Mail adresi bugün gelen mailler içerisinde yoktur.")
                        
                except Exception as e:
                    print(f"{address} adresinden mail çekme hatası: {e}")
                    continue
                            
        else:
            try:
                if not self._is_connection_alive():
                    self._reconnect()
                    
                # IMAP search - tüm kriterler tek string içinde olmalı
                search_criteria = f'(SINCE {formatted_date} UNSEEN)'
                result, messages = self.connection.search(None, search_criteria)
                if result == "OK":
                    for message in messages[0].split():
                        try:
                            if not self._is_connection_alive():
                                self._reconnect()
                                
                            ret, data = self.connection.fetch(message, '(RFC822)')
                            msg = email.message_from_bytes(data[0][1])
                            subject = msg["Subject"] or "No_Subject"
                            sender = email.utils.parseaddr(msg.get('From', ''))[1]
                            date_tuple = email.utils.parsedate_tz(msg["Date"])
                            msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple)) if date_tuple else datetime.now()

                            # Sender + Subject kombinasyonunu key olarak kullan
                            dedup_key = f"{sender}|{subject}"
                            if dedup_key in message_map:
                                if message_map[dedup_key]['date'] < msg_date:
                                    message_map[dedup_key] = {'msg': msg, 'date': msg_date}
                            else:
                                message_map[dedup_key] = {'msg': msg, 'date': msg_date}
                            
                            # Store işlemi için hata yakalama
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
                else:
                    print("Hiç okunmamış e-posta bulunamadı.")
            except Exception as e:
                print(f"Genel mail çekme hatası: {e}")

        for item in message_map.values():
            emails.append(item['msg'])

        return emails


    def fetch_unread_messages_single_pass(self, since_date, known_senders, known_reply_tos):
        """Tek IMAP aramasıyla tüm UNSEEN emailleri çek; yalnızca bilinen gönderenler işlenir.

        GitHub bildirimleri ve diğer bilinmeyen emailler okunmamış bırakılır.
        """
        formatted_date = since_date.strftime('%d-%b-%Y')
        message_map = {}
        skipped_summary = Counter()

        if not self._is_connection_alive():
            self._reconnect()

        result, msgs = self.connection.search(None, f'(SINCE {formatted_date} UNSEEN)')
        if result != 'OK' or not msgs[0]:
            print("Okunmamış email bulunamadı.")
            return []

        all_ids = msgs[0].split()
        print(f"IMAP'ta {len(all_ids)} okunmamış email bulundu, filtreleniyor...")

        for mid in all_ids:
            try:
                if not self._is_connection_alive():
                    self._reconnect()

                # RFC822.HEADER: BODY[HEADER.FIELDS]'den daha güvenilir (Gmail bazı emaillerde NIL döndürür)
                ret, data = self.connection.fetch(mid, '(RFC822.HEADER)')
                if ret != 'OK' or not data or not isinstance(data[0], tuple):
                    continue

                hdr = email.message_from_bytes(data[0][1])
                raw_from = hdr.get('From', '')
                from_addr = email.utils.parseaddr(raw_from)[1].lower()
                reply_to_hdr = email.utils.parseaddr(hdr.get('Reply-To', ''))[1].lower()

                # Bilinmeyen gönderici → okunmamış bırak (GitHub bildirimleri vb.)
                if from_addr not in known_senders and reply_to_hdr not in known_reply_tos:
                    skipped_summary[from_addr or raw_from[:40] or "<boş>"] += 1
                    continue

                # Tam mesajı çek
                ret, full = self.connection.fetch(mid, '(RFC822)')
                if ret != 'OK' or not full or not full[0]:
                    continue
                msg = email.message_from_bytes(full[0][1])

                # Dedup: aynı from+subject → daha yeni olanı tut
                subj_raw = hdr.get('Subject', '') or 'No_Subject'
                decoded_subj = self._decode_filename(subj_raw) or 'No_Subject'
                dedup_key = f"{from_addr}|{decoded_subj}"

                date_tuple = email.utils.parsedate_tz(hdr.get('Date'))
                msg_date = datetime.fromtimestamp(email.utils.mktime_tz(date_tuple)) if date_tuple else datetime.now()

                if dedup_key not in message_map or message_map[dedup_key]['date'] < msg_date:
                    message_map[dedup_key] = {'msg': msg, 'date': msg_date}

                self._mark_seen(mid)
                print(f"✅ {from_addr} — {decoded_subj[:60]} okundu.")

            except Exception as e:
                print(f"Mail {mid} işleme hatası: {e}")
                if "timed out" in str(e).lower() or "abort" in str(e).lower():
                    try:
                        self._reconnect()
                    except Exception:
                        pass
                time.sleep(0.3)

        result_list = [v['msg'] for v in message_map.values()]
        print(f"Toplam {len(result_list)} otel emaili işlenecek ({len(all_ids)} taranandı).")
        if skipped_summary:
            print(f"⏭️ Atlanan email göndericileri ({skipped_summary.total()} email):")
            for addr, count in skipped_summary.most_common():
                print(f"   {count}x {addr}")
        return result_list

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

          