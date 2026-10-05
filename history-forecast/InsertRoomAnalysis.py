import sys
import os
import json
import argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from FileManager import FileManager
from FetchEmail import FetchEmail
from RoomAnalysis import RoomAnalysis
from hotel_analysis import log_record
from datetime import datetime, timedelta
import pandas as pd


# ── Ayarlar ──────────────────────────────────────────────────
# CLI:     python InsertRoomAnalysis.py --no-mail --company 366
# Env var: SKIP_MAIL=true ONLY_COMPANIES=366 python InsertRoomAnalysis.py
# ─────────────────────────────────────────────────────────────

_env_skip_mail = os.environ.get("SKIP_MAIL", "false").lower() == "true"
_env_only_companies = os.environ.get("ONLY_COMPANIES", None)

parser = argparse.ArgumentParser(description="History-Forecast Pipeline")
parser.add_argument("--no-mail", action="store_true",
                    help="Email okumadan sadece dizindeki dosyalari isle (move_files de atlanir)")
parser.add_argument("--company", type=str, default=None,
                    help="Sadece belirtilen company_id_prod degerlerini isle (virgul ile ayir: 366,491)")
args = parser.parse_args()

if _env_skip_mail and not args.no_mail:
    args.no_mail = True
if _env_only_companies and not args.company:
    args.company = _env_only_companies

if args.no_mail:
    print("*** SKIP_MAIL aktif — email okuma ve move_files DEVRE DISI! Sadece dizindeki dosyalar islenir. ***")
if args.company:
    print(f"*** ONLY_COMPANIES aktif — sadece su company_id'ler islenir: {args.company} ***")

mail_server = 'imap.gmail.com'
mail_user = os.environ.get("PC_HF_EMAIL_USER", "skutlu@pricing-coach.com")
mail_password = os.environ.get("PC_HF_EMAIL_PASS", "e m u o n k k w e l v d g a d x")
day = datetime.today() - timedelta(1)

collection_date = datetime.today().date()

def _build_lookup_from_json(json_path):
    """hotel_configs.json'dan history-forecast lookup DataFrame'i oluşturur.
    Üretilen DataFrame, eski file-maping.xlsx ile aynı kolon yapısına sahiptir.
    """
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    rows = []
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        ra = hotel.get("room_analysis")
        if not ra:
            continue
        currency = hotel.get("currency", "EUR")
        for rule in ra.get("mail_rules", []):
            rows.append({
                "file_read":   ra.get("directory"),
                "file_history": ra.get("directory_history"),
                "file":        ra.get("directory"),
                "file_hotel":  ra.get("folder_name"),
                "pms":         ra.get("pms"),
                "currency":    currency,
                "test_insert": False,
                "company_id_prod": ra.get("company_id_prod"),
                "company_id_test": ra.get("company_id_test"),
                "mail":        rule.get("mail"),
                "file_name":   rule.get("file_name"),
                "subject_check": rule.get("subject_check"),
                "reply_to_mail": rule.get("reply_to_mail"),
                "hotel_booking_room_type_id_prod": rule.get("hotel_booking_room_type_id_prod"),
                "hotel_booking_room_type_id_test": rule.get("hotel_booking_room_type_id_test"),
            })
    return pd.DataFrame(rows)


_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hotel_analysis", "hotel_configs.json"
)
if not os.path.exists(_JSON_PATH):
    raise FileNotFoundError(f"hotel_configs.json bulunamadı: {_JSON_PATH}")

lookup = _build_lookup_from_json(_JSON_PATH)

subject_check = lookup[["mail","subject_check","reply_to_mail","file","file_name"]]
from_addresses_and_targets_from = dict(zip(
    lookup["mail"].dropna(),
    lookup.loc[lookup["mail"].notna(), "file"]
))

# Email işlemleri için hata yakalama
if not args.no_mail:
    fetcher = None
    try:
        fetcher = FetchEmail(mail_server, mail_user, mail_password)
        fetcher.move_files_with_timestamp(lookup)

        known_senders = set(lookup["mail"].dropna().str.lower())
        known_reply_tos = set(lookup["reply_to_mail"].dropna().str.lower())

        print("Email çekme işlemi başlatılıyor...")
        unread_emails = fetcher.fetch_unread_messages_single_pass(day, known_senders, known_reply_tos)

        for email_msg in unread_emails:
            try:
                attachment_path = fetcher.save_attachment(
                    email_msg,
                    targetHotels=from_addresses_and_targets_from,
                    subject_check=subject_check,
                )
                if attachment_path:
                    print(f"  ✅ Attachment saved: {attachment_path}")
            except Exception as e:
                print(f"❌ Attachment kaydetme hatası: {e}")

    except Exception as e:
        print(f"Email işleme sırasında genel hata: {e}")
        print("Hata detayları:")
        import traceback
        traceback.print_exc()

    finally:
        if fetcher:
            try:
                fetcher.close_connection()
                print("✅ IMAP bağlantısı kapatıldı.")
            except Exception as e:
                print(f"Bağlantı kapatma hatası: {e}")

print("\n" + "="*50)
print("Email işlemleri tamamlandı.")
print("="*50 + "\n")

##### Reading File

# --company filtresi: sadece belirtilen company_id'lerin dizinlerini isle
active_lookup = lookup
if args.company:
    only_ids = {int(c.strip()) for c in args.company.split(",")}
    active_lookup = lookup[lookup["company_id_prod"].isin(only_ids)]
    if active_lookup.empty:
        print(f"UYARI: Belirtilen company_id bulunamadi: {args.company}")
        sys.exit(0)

file_manager = FileManager()

for i, test_insert in active_lookup[["file_read", "test_insert"]].drop_duplicates().itertuples(index=False):

    # Dizini paylaşan tüm şirket ID'leri (aynı dizinde birden fazla otel olabilir — örn. Ramada)
    company_ids = list(active_lookup[active_lookup["file_read"] == i]["company_id_prod"].dropna().unique())

    if len(os.listdir(i)) != 0:

        print(i)
        start_time = datetime.now()
        try:
            dfValue = file_manager.read_files_in_directory(i, active_lookup)

            if dfValue is None or dfValue.empty:
                print(f"{i} için veri bulunamadı veya boş. Bir sonraki otele geçiliyor...")
                end_time = datetime.now()
                duration_ms = (end_time - start_time).total_seconds() * 1000
                for cid in company_ids:
                    log_record.save_log(
                        df=pd.DataFrame(),
                        integration_type="HISTORY_FORECAST",
                        company_id=int(cid),
                        status="NO_DATA",
                        duration_ms=duration_ms,
                        start_time=start_time,
                        end_time=end_time,
                        error_message=f"Dizin boş veya veri yok: {i}",
                    )
                continue

            data_processor = RoomAnalysis(dfValue, collection_date)

            data_processor.calculate_occupancy_rate()
            data_processor.reindex_columns()
            data_processor.manipulate_dates()
            data_processor.manipulate_values()
            data_processor.round_numbers()
            data_processor.insert_to_database()

            end_time = datetime.now()
            duration_ms = (end_time - start_time).total_seconds() * 1000
            # Her şirket için ayrı log — dizini paylaşan otellerde doğru company_id kaydedilsin
            for cid in dfValue["company_id_prod"].dropna().unique():
                log_record.save_log(
                    df=dfValue[dfValue["company_id_prod"] == cid],
                    integration_type="HISTORY_FORECAST",
                    company_id=int(cid),
                    status="SUCCESS",
                    duration_ms=duration_ms,
                    start_time=start_time,
                    end_time=end_time,
                )
            print(f"✅ {i} başarıyla işlendi.")

        except Exception as e:
            end_time = datetime.now()
            duration_ms = (end_time - start_time).total_seconds() * 1000
            print(f"❌ {i} işlenirken hata oluştu: {e}")
            print("Bir sonraki otele geçiliyor...")
            import traceback
            traceback.print_exc()
            for cid in company_ids:
                log_record.save_log(
                    df=pd.DataFrame(),
                    integration_type="HISTORY_FORECAST",
                    company_id=int(cid),
                    status="FAILURE",
                    duration_ms=duration_ms,
                    start_time=start_time,
                    end_time=end_time,
                    error_message=str(e)[:500],
                )
            continue

    else:
        print(f"{i} Dosya dizini boştur.")