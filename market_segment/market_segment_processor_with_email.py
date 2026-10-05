"""
Market Segment Processor with Email Integration
Bu dosya, history-forecast'teki InsertRoomAnalysis.py benzeri bir akış sağlar
ancak market segment verileri için tasarlanmıştır.
"""

from FetchEmail import FetchEmail
from datetime import datetime, timedelta
import json
import pandas as pd
import os
import sys
import time

# email_processor_v3.py'deki fonksiyonları import et
from email_processor_v3 import (
    process_all_companies,
    CONFIG
)

# ==================== YAPILANDIRMA ====================

# Mail sunucu ayarları
MAIL_CONFIG = {
    'server': 'imap.gmail.com',
    'user': os.environ.get("PC_HF_EMAIL_USER", "skutlu@pricing-coach.com"),
    'password': os.environ.get("PC_HF_EMAIL_PASS", "e m u o n k k w e l v d g a d x"),
}

# Tarih ayarları - Esnek tarih filtreleme
# ==========================================
# START_DAYS_AGO: Kaç gün öncesinden başla (0 = bugün)
# END_DAYS_AGO: Kaç gün öncesine kadar (0 = bugün dahil)
# ==========================================
# Örnekler:
#   Sadece bugün:          START_DAYS_AGO=0, END_DAYS_AGO=0
#   Sadece dün:            START_DAYS_AGO=1, END_DAYS_AGO=1
#   Sadece 3 gün önce:     START_DAYS_AGO=3, END_DAYS_AGO=3
#   Son 3 gün:             START_DAYS_AGO=3, END_DAYS_AGO=0
#   Son 7 gün:             START_DAYS_AGO=7, END_DAYS_AGO=0
#   3-7 gün arası:         START_DAYS_AGO=7, END_DAYS_AGO=3
# ==========================================
START_DAYS_AGO = 0  # Başlangıç: 0 gün önce (bugün)
END_DAYS_AGO = 0    # Bitiş: Bugün dahil

# Tarih hesaplamaları
start_date = datetime.today() - timedelta(START_DAYS_AGO)
end_date = datetime.today() - timedelta(END_DAYS_AGO) + timedelta(1)  # END_DAYS_AGO günü dahil etmek için +1

# Subject filtreleme - Market segment raporlarını içeren anahtar kelimeler
SUBJECT_FILTERS = [
    'market',
    'segment',
    'pazar',
    'segmen',
    'p2910',  # Europrotel rapor kodu
    'market_segment',
    'marketsegment'
]

# JSON yapılandırma dosyası
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_CONFIG_PATH = os.path.join(SCRIPT_DIR, '..', 'hotel_analysis', 'hotel_configs.json')


def _build_lookup_from_json(json_path):
    """hotel_configs.json'dan market_segment lookup DataFrame'i oluşturur.
    Üretilen DataFrame, eski market-segment.xlsx ile aynı kolon yapısına sahiptir.
    """
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    rows = []
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        ms = hotel.get("market_segment")
        if not ms:
            continue
        # block-level currency önce (Divan Erbil gibi istisnalar için), sonra top-level
        currency = ms.get("currency") or hotel.get("currency") or "EUR"
        for rule in ms.get("mail_rules", []):
            rows.append({
                "file_read":    ms.get("directory"),
                "file_history": None,
                "file":         ms.get("directory"),
                "file_hotel":   ms.get("folder_name"),
                "pms":          ms.get("pms"),
                "currency":     currency,
                "company_id":   ms.get("company_id"),
                "mail":         rule.get("mail"),
                "file_name":    rule.get("file_name"),
                "subject_check": rule.get("subject_check"),
                "reply_to_mail": rule.get("reply_to_mail"),
            })
    return pd.DataFrame(rows)

# ==================== ANA İŞLEM AKIŞI ====================

def main():
    """Ana işlem akışı"""
    try:
        print("="*60)
        print("MARKET SEGMENT PROCESSOR - EMAIL INTEGRATION")
        print("="*60)
        print(f"Start Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Script Directory: {SCRIPT_DIR}")
        print(f"Config File: {JSON_CONFIG_PATH}")
        print()

        # 1. JSON yapılandırmasını oku
        print("[1/4] Loading JSON configuration...")
        resolved_json_path = os.path.normpath(JSON_CONFIG_PATH)
        if not os.path.exists(resolved_json_path):
            print(f"❌ hotel_configs.json bulunamadı: {resolved_json_path}")
            return

        lookup = _build_lookup_from_json(resolved_json_path)
        print(f"✓ Loaded {len(lookup)} companies from hotel_configs.json")

        # Excel'de olması gereken kolonları kontrol et
        required_columns = ['company_id', 'file_read']
        optional_columns = ['mail', 'reply_to_mail', 'subject_check', 'file_name', 'file_hotel', 'file_history', 'pms']

        missing_required = [col for col in required_columns if col not in lookup.columns]
        if missing_required:
            print(f"❌ Required columns missing: {missing_required}")
            print(f"Available columns: {lookup.columns.tolist()}")
            print("hotel_configs.json'daki market_segment bloklarını kontrol edin.")
            return

        print(f"Available columns: {lookup.columns.tolist()}")

        # Mail kolonları var mı kontrol et
        has_email_config = all(col in lookup.columns for col in ['mail', 'file_read'])

        if not has_email_config:
            print("⚠️ Email configuration columns not found in Excel.")
            print("   Skipping email fetching, processing existing files only...")
        else:
            # 2. E-posta işlemleri
            print("\n[2/4] Processing emails...")

            # Subject kontrolü için DataFrame hazırla
            if 'subject_check' in lookup.columns and 'mail' in lookup.columns:
                # CRITICAL: file_read ve file_name kolonlarını da ekle (aynı mail için farklı company'ler olabilir)
                subject_check = lookup[["mail", "subject_check", "reply_to_mail", "file_read", "file_name"]].copy()
                # Mail adreslerindeki boşlukları temizle (NaN değerleri önce string'e çevir)
                if 'mail' in subject_check.columns:
                    subject_check['mail'] = subject_check['mail'].fillna('').astype(str).str.strip()
                if 'reply_to_mail' in subject_check.columns:
                    subject_check['reply_to_mail'] = subject_check['reply_to_mail'].fillna('').astype(str).str.strip()
            else:
                subject_check = None

            # Mail adresi ve hedef dizin eşleşmeleri
            from_addresses_and_targets_from = {}
            from_addresses_and_targets_reply = {}
            reply_to_addresses_list = []

            if 'mail' in lookup.columns and 'file_read' in lookup.columns:
                # CRITICAL FIX: Sadece reply_to_mail BOŞSA olan satırların From adreslerini al
                # Reply-To olan emailler için From'u ignore et (duplicate önleme)
                if 'reply_to_mail' in lookup.columns:
                    mail_mapping = lookup[lookup['reply_to_mail'].isna()][['mail', 'file_read']].dropna().copy()
                else:
                    mail_mapping = lookup[['mail', 'file_read']].dropna().copy()
                mail_mapping['mail'] = mail_mapping['mail'].str.strip()
                from_addresses_and_targets_from = dict(zip(mail_mapping['mail'], mail_mapping['file_read']))
                print(f"From addresses (only without Reply-To): {list(from_addresses_and_targets_from.keys())}")

            if 'reply_to_mail' in lookup.columns and 'file_read' in lookup.columns:
                reply_mapping = lookup[['reply_to_mail', 'file_read']].dropna().copy()
                reply_mapping['reply_to_mail'] = reply_mapping['reply_to_mail'].str.strip()
                from_addresses_and_targets_reply = dict(zip(reply_mapping['reply_to_mail'], reply_mapping['file_read']))
                # Reply-To adreslerini listeye ekle
                reply_to_addresses_list = list(from_addresses_and_targets_reply.keys())

            fetcher = None
            try:
                # Email bağlantısı kur
                print(f"Connecting to {MAIL_CONFIG['server']}...")
                fetcher = FetchEmail(MAIL_CONFIG['server'], MAIL_CONFIG['user'], MAIL_CONFIG['password'])

                # Eski dosyaları history klasörüne taşı (eğer file_history kolonu varsa)
                if 'file_history' in lookup.columns:
                    print("Moving old files to history folder...")
                    try:
                        fetcher.move_files_with_timestamp(lookup)
                        print("✓ Old files moved to history")
                    except Exception as e:
                        print(f"⚠️ Error moving files to history: {e}")

                # Tüm e-postaları çek (okunmuş/okunmamış fark etmeksizin)
                if START_DAYS_AGO == END_DAYS_AGO:
                    if START_DAYS_AGO == 0:
                        date_range_desc = "today only"
                    elif START_DAYS_AGO == 1:
                        date_range_desc = "yesterday only"
                    else:
                        date_range_desc = f"{START_DAYS_AGO} days ago only"
                else:
                    date_range_desc = f"from {START_DAYS_AGO} days ago to {END_DAYS_AGO} days ago"

                print(f"Fetching emails: {date_range_desc}")
                print(f"  Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
                print(f"Subject filters: {SUBJECT_FILTERS}")
                if reply_to_addresses_list:
                    print(f"Reply-To addresses: {reply_to_addresses_list}")
                if from_addresses_and_targets_from:
                    print(f"From addresses: {list(from_addresses_and_targets_from.keys())}")

                all_emails = fetcher.fetch_all_messages(
                    start_date,
                    end_date,
                    from_addresses=from_addresses_and_targets_from.keys() if from_addresses_and_targets_from else None,
                    reply_to_addresses=reply_to_addresses_list if reply_to_addresses_list else None,
                    mark_as_seen=False,  # Mailleri okundu olarak işaretleme
                    subject_filters=SUBJECT_FILTERS  # Subject filtreleme ekle
                )

                print(f"Found {len(all_emails)} matching emails (after subject filtering)")

                # Her e-postanın eklerini kaydet
                processed_count = 0
                skipped_count = 0

                for idx, email_msg in enumerate(all_emails, 1):
                    try:
                        # Önce attachment var mı kontrol et (hızlı)
                        if not fetcher.has_attachments(email_msg):
                            skipped_count += 1
                            continue

                        print(f"\nProcessing email {idx}/{len(all_emails)}...")
                        attachment_paths = fetcher.save_attachment(
                            email_msg,
                            targetHotels=from_addresses_and_targets_from,
                            subject_check=subject_check,
                            targetHotelsReply=from_addresses_and_targets_reply
                        )

                        if attachment_paths:
                            processed_count += 1
                            print(f"✓ Saved {len(attachment_paths)} attachments")
                            for path in attachment_paths:
                                print(f"  - {path}")
                        else:
                            skipped_count += 1

                    except Exception as e:
                        print(f"❌ Error processing email {idx}: {e}")
                        skipped_count += 1
                        continue

                print(f"\n✓ Email processing completed")
                print(f"  Processed: {processed_count} emails with attachments")
                print(f"  Skipped: {skipped_count} emails (no attachments or errors)")

            except Exception as e:
                print(f"❌ Email processing error: {e}")
                import traceback
                traceback.print_exc()

            finally:
                if fetcher:
                    try:
                        fetcher.close_connection()
                    except Exception as e:
                        print(f"Error closing connection: {e}")

        # 3. Dosyaları işle
        print("\n[3/4] Processing market segment files...")

        # Tüm şirketleri işle
        total_processed, successful_companies, failed_companies = process_all_companies(lookup)

        # 4. Özet rapor
        print("\n[4/4] Final Summary")
        print("="*60)
        print(f"Total companies: {len(lookup)}")
        print(f"Successfully processed: {len(successful_companies)}")
        print(f"Failed: {len(failed_companies)}")
        print(f"End Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*60)

    except Exception as e:
        print(f"\n❌ Critical error in main process: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

# ==================== PROGRAM BAŞLANGICI ====================

if __name__ == "__main__":
    main()
