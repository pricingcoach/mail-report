import os
import re
import time
import pandas as pd
import xml.etree.ElementTree as ET
from datetime import datetime
import logging
import requests
from fuzzywuzzy import fuzz
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotel_analysis.config import LOG_AUTH_URL, API_EMAIL, API_PASSWORD

logging.basicConfig(filename='log_dosyasi.txt',
                    level=logging.INFO,
                    format='%(asctime)s - %(message)s')

ROOM_TYPES_BY_COMPANY_URL = "https://bookingapi-py.pricing-coach.com/api/v1/roomanalysis/room-types/by-company"

_token_cache = {"token": None, "expires": 0.0}


def _get_bearer_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires"]:
        return _token_cache["token"]
    payload = {"username": API_EMAIL, "password": API_PASSWORD}
    response = requests.post(LOG_AUTH_URL, data=payload, timeout=15)
    response.raise_for_status()
    token = response.json()["access_token"]
    _token_cache["token"] = token
    _token_cache["expires"] = time.time() + 3600  # 1 saat
    return token


class FileManager:
    def __init__(self):
        # DB bağlantısı kaldırıldı — tüm DB işlemleri booking-api-py üzerinden yapılıyor
        self._room_type_cache = {}  # company_id → mapping dict (per-run cache)

    def read_files_in_directory(self, directory, lookup):
        lookup_in_directory = lookup[lookup["file_read"] == directory].reset_index(drop=True)

        if not lookup_in_directory.empty:
            # PMS bilgisini al (dizindeki ilk kaydın PMS'i tüm dizin için geçerli)
            pms = lookup_in_directory["pms"].iloc[0] if "pms" in lookup_in_directory.columns else "opera"
            print(f"PMS Tipi: {pms}")

            # Dizindeki dosyaları listele
            files = [file for file in os.listdir(directory) if file.endswith('.xlsx') or file.endswith('.xls') or file.endswith('.xml') or file.endswith('.XML') or file.endswith('.XLSX') ]

            dfs = []
            processed_files = set()

            for file in files:
                file_name = os.path.splitext(os.path.basename(file))[0]
                match = re.match(r'^(.*?)\d+$', file_name)
                if match:
                    file_name = match.group(1)
                else:
                    file_name = file_name

                similar_file = self.find_similar_file(lookup_in_directory, file_name)

                if similar_file in processed_files:
                    print(f"{similar_file} dosyası zaten işlendi.")
                    continue

                if not lookup_in_directory[lookup_in_directory["file_name"] == similar_file].empty:
                    index = lookup_in_directory[lookup_in_directory["file_name"] == similar_file].index
                    room_type_id_prod = lookup_in_directory.iloc[index]["hotel_booking_room_type_id_prod"].iloc[0]
                    company_id_prod = lookup_in_directory.iloc[index]["company_id_prod"].iloc[0]

                    currency = lookup_in_directory.iloc[index]["currency"].iloc[0]

                    room_type_id_test = lookup_in_directory.iloc[index]["hotel_booking_room_type_id_test"].iloc[0]
                    company_id_test = lookup_in_directory.iloc[index]["company_id_test"].iloc[0]

                    processed_key = (similar_file)

                    file_path = os.path.join(directory, file)
                    print(f"Reading file: {file_path}")
                    try:
                        # Europrotel için özel işlem
                        if pms.lower() == "europrotel":
                            # Otel geneli rapor kontrolü
                            if "otel" in similar_file.lower() and "geneli" in similar_file.lower():
                                print("✅ Otel geneli rapor tespit edildi, hotel_booking_room_type_id NULL olacak")
                                df = self.convert_europrotel_xml_to_dataframe_overall(
                                    file_path,
                                    company_id_prod,
                                    company_id_test,
                                    currency
                                )
                            # Oda tipi kırılımlı rapor kontrolü
                            elif ("oda" in similar_file.lower() and "tipi" in similar_file.lower()) or \
                                 ("kırılımlı" in similar_file.lower() or "kirılımlı" in similar_file.lower()):
                                print("✅ Oda tipi kırılımlı rapor tespit edildi")
                                room_type_mapping = self.get_room_type_mapping_from_db(company_id_prod)
                                if not room_type_mapping:
                                    print("❌ Veritabanından oda tipi mapping çekilemedi!")
                                    continue
                                df = self.convert_europrotel_xml_to_dataframe(
                                    file_path,
                                    room_type_mapping,
                                    company_id_prod,
                                    company_id_test,
                                    currency
                                )
                            # Bilinmeyen Europrotel rapor formatı
                            else:
                                print(f"⚠️ Bilinmeyen Europrotel rapor formatı: {similar_file}")
                                print("   Beklenen: 'Otel Geneli' veya 'Oda Tipi Kırılımlı'")
                                continue
                        # Opera ve diğer PMS'ler için mevcut akış
                        elif file.endswith('.xml') or file.endswith('.XML'):
                            df = self.convert_xml_to_dataframe(file_path)
                            df['company_id_prod'] = company_id_prod
                            df['hotel_booking_room_type_id_prod'] = room_type_id_prod
                            df['company_id_test'] = company_id_test
                            df['hotel_booking_room_type_id_test'] = room_type_id_test
                            df['currency'] = currency
                        else:
                            df = pd.read_excel(file_path)
                            df['company_id_prod'] = company_id_prod
                            df['hotel_booking_room_type_id_prod'] = room_type_id_prod
                            df['company_id_test'] = company_id_test
                            df['hotel_booking_room_type_id_test'] = room_type_id_test
                            df['currency'] = currency

                        df["file_name"] = similar_file

                        print(df.head())
                        dfs.append(df)

                        processed_files.add(processed_key)

                    except Exception as e:
                        print(f"Hata: {e}")
                        import traceback
                        traceback.print_exc()
                else:
                    continue

            if dfs:
                combined_df = pd.concat(dfs, ignore_index=True)

                for i in lookup_in_directory["file_name"]:
                    if i is not None:
                        if not combined_df.loc[combined_df["file_name"] == i].empty:
                            continue
                        else:
                            log_message = f"{i} Dosyası gelen mailler içerisinde yoktur!"
                            logging.info(log_message)
                    else:
                        continue
                return combined_df
            else:
                logging.info("Hiçbir dosya işlenmedi.")
                return None

        else:
            logging.info("Belirtilen dizin için uygun bir eşleştirme bulunamadı.")
            return None

    def find_similar_file(self, lookup_in_directory, file_name):
        cleaned_file_name = os.path.splitext(os.path.basename(file_name))[0]
        cleaned_file_name = re.sub(r'\d{5,}$', '', cleaned_file_name)
        cleaned_file_name = cleaned_file_name.replace('\xa0', ' ')

        # 1. Tam eşleşme kontrol et (case-sensitive)
        for name in lookup_in_directory["file_name"]:
            name_clean = str(name).replace('\xa0', ' ')
            if name_clean == cleaned_file_name:
                return name

        # 2. Case-insensitive tam eşleşme kontrol et
        for name in lookup_in_directory["file_name"]:
            name_clean = str(name).replace('\xa0', ' ')
            if name_clean.lower() == cleaned_file_name.lower():
                return name

        # 3. Oda tipi kodları için özel kontrol (1KS, PKS, DKS, UTS, UKC, UTC, UKS gibi)
        # Bu kodlar genellikle dosya isminin başındadır
        file_code = None
        name_code = None
        
        # Dosya isminden kod çıkar (başındaki 1-3 karakter + KS/TS/KC/TC pattern)
        code_match = re.match(r'^([A-Z0-9]{1,3}[KCTS]+)', cleaned_file_name.upper())
        if code_match:
            file_code = code_match.group(1)
            
        for name in lookup_in_directory["file_name"]:
            name_clean = str(name).replace('\xa0', ' ')
            name_match = re.match(r'^([A-Z0-9]{1,3}[KCTS]+)', name_clean.upper())
            if name_match:
                name_code = name_match.group(1)
                
                # Oda tipi kodları tam eşleşiyor mu?
                if file_code and name_code and file_code == name_code:
                    # Geri kalan kısım da benziyor mu? (Mersin, MERSİN gibi)
                    remaining_file = cleaned_file_name[len(file_code):].strip()
                    remaining_name = name_clean[len(name_code):].strip()
                    
                    # Türkçe karakterler ve case insensitive karşılaştırma
                    remaining_file_normalized = self.normalize_turkish_text(remaining_file)
                    remaining_name_normalized = self.normalize_turkish_text(remaining_name)
                    
                    if remaining_file_normalized == remaining_name_normalized:
                        return name

        # 4. Son çare olarak fuzzy matching kullan (daha yüksek threshold)
        max_similarity = 0
        similar_file = None
        for name in lookup_in_directory["file_name"]:
            name_clean = str(name).replace('\xa0', ' ')
            
            # Türkçe karakterleri normalize ederek de karşılaştır
            name_normalized = self.normalize_turkish_text(name_clean)
            file_normalized = self.normalize_turkish_text(cleaned_file_name)
            
            similarity = fuzz.ratio(name_normalized, file_normalized)
            # Threshold'u 90'a düşürdük UTS MERSİN vs UTS Mersin için
            if similarity > max_similarity and similarity >= 90:
                max_similarity = similarity
                similar_file = name
        return similar_file
    
    def normalize_turkish_text(self, text):
        """Türkçe karakterleri normalize et ve küçük harfe çevir"""
        turkish_map = {
            'ş': 's', 'Ş': 's',
            'ğ': 'g', 'Ğ': 'g', 
            'ü': 'u', 'Ü': 'u',
            'ı': 'i', 'I': 'i', 'İ': 'i', 'i': 'i',  # Türkçe İ ve ı karakterleri
            'ö': 'o', 'Ö': 'o',
            'ç': 'c', 'Ç': 'c'
        }
        
        normalized = text.lower()
        for turkish_char, latin_char in turkish_map.items():
            normalized = normalized.replace(turkish_char, latin_char)
        
        return normalized.strip()

    def get_room_type_mapping_from_db(self, company_id_prod):
        """company_id'ye göre oda tipi kodları ve ID'lerini booking-api-py API'sinden çeker.

        Eski implementasyon doğrudan DB sorguluyordu (pd.read_sql + self.conn_prod).
        Yeni implementasyon /roomanalysis/room-types/by-company/{company_id} endpoint'ini kullanır.
        VPN koptuğunda DB yerine API'ye istek atıldığı için bağlantı sorunu yaşanmaz.

        Aynı company_id için tekrar çağrıldığında API'ye gitmez, cache'den döner.

        Returns:
            dict: {'ACC': 123, 'DBL': 456, ...} — room_type_code → room_type_id
        """
        key = int(company_id_prod)
        if key in self._room_type_cache:
            print(f"Cache'den oda tipi mapping alındı (company_id={key})")
            return self._room_type_cache[key]
        try:
            token = _get_bearer_token()
            headers = {"Authorization": f"Bearer {token}"}
            url = f"{ROOM_TYPES_BY_COMPANY_URL}/{key}"
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            mapping = {row["room_type_code"]: row["room_type_id"] for row in data}
            print(f"API'den {len(mapping)} oda tipi eşleştirmesi çekildi: {mapping}")
            self._room_type_cache[key] = mapping
            return mapping
        except Exception as e:
            print(f"Oda tipi mapping API hatası (company_id={company_id_prod}): {e}")
            return {}

    def convert_europrotel_xml_to_dataframe_overall(self, file_path, company_id_prod, company_id_test, currency):
        """Europrotel PMS için XML/Excel dönüştürme - Otel Geneli Rapor (hotel_booking_room_type_id NULL)"""
        try:
            file_ext = os.path.splitext(file_path)[1].lower()

            if file_ext in ['.xlsx', '.xls']:
                # Excel dosyası oku
                df = pd.read_excel(file_path)
                # Metadata sütunlarını düşür
                metadata_cols = [col for col in df.columns if col in ['Name', 'ExecutionTime', 'Textbox22', 'Textbox20', 'Textbox30']]
                if metadata_cols:
                    df.drop(columns=metadata_cols, inplace=True)
                print(f"Europrotel Otel Geneli Excel'den {len(df)} satır okundu.")
            else:
                # XML dosyası oku
                tree = ET.parse(file_path)
                root = tree.getroot()

                # Namespace'i kontrol et
                namespace = root.tag.replace('Details_Collection', '').replace('{', '').replace('}', '')

                # XML'den tüm Details elementlerini çek
                data = []
                for detail in root.findall('.//{' + namespace + '}Details') if namespace else root.findall('.//Details'):
                    record = detail.attrib  # Tüm attribute'leri al
                    data.append(record)

                if not data:
                    print("Europrotel XML'de veri bulunamadı!")
                    return pd.DataFrame()

                df = pd.DataFrame(data)
                print(f"Europrotel Otel Geneli XML'den {len(df)} satır okundu.")

            # Europrotel kolon isimlerini standart kolon isimlerine dönüştür
            europrotel_column_mapping = {
                'considered_date': 'CONSIDERED_DATE',
                'net_room_revenue': 'NET_ROOM_REVENUE',
                'no_rooms': 'NO_ROOMS',
                'no_person': 'NO_PERSONS',
                'arrival_rooms': 'ARRIVAL_ROOMS',
                'departure_rooms': 'DEPARTURE_ROOMS',
                'complimentary_rooms': 'CF_COMPLIMENTARY_ROOMS',
                'house_use_rooms': 'CF_HOUSE_USE_ROOMS',
                'days_use_rooms': 'CF_DAY_USE_ROOMS',
                'out_of_order_rooms': 'CF_OOO_ROOMS',
                'no_show_rooms': 'CF_NO_SHOW_ROOMS',
                'inventory_rooms': 'INVENTORY_ROOMS',
                'adr': 'CF_ADR_BY_ROOM',
                'occupancy_rates': 'CF_OCC',
                'currency': 'currency'
            }

            df.rename(columns=europrotel_column_mapping, inplace=True)

            # Otel geneli için hotel_booking_room_type_id NULL olacak
            df['company_id_prod'] = company_id_prod
            df['hotel_booking_room_type_id_prod'] = None  # NULL
            df['company_id_test'] = company_id_test
            df['hotel_booking_room_type_id_test'] = None  # NULL
            # CURRENCY XML'den geliyor, ezmeyelim

            # Eksik kolonları ekle (Opera formatıyla uyumlu olması için)
            if 'ADULTS' not in df.columns:
                df['ADULTS'] = None
            if 'CHILDREN' not in df.columns:
                df['CHILDREN'] = None
            if 'IND_ROOMS' not in df.columns:
                df['IND_ROOMS'] = None
            if 'GRP_ROOMS' not in df.columns:
                df['GRP_ROOMS'] = None

            print(f"Otel geneli rapor hazırlandı: {len(df)} satır, hotel_booking_room_type_id = NULL")
            return df

        except Exception as e:
            print(f"Europrotel Otel Geneli XML dönüştürme hatası: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()

    def convert_xml_to_dataframe(self, file_path):
        """Opera PMS için XML dönüştürme (mevcut sistem)"""
        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            # İlk path: 'LIST_G_GPAGEID/G_GPAGEID/LIST_G_REC_TYPE/G_REC_TYPE/LIST_G_CONSIDERED_DATE/G_CONSIDERED_DATE'
            data = []
            for g_rec_type_desc in root.findall('LIST_G_GPAGEID/G_GPAGEID/LIST_G_REC_TYPE/G_REC_TYPE/LIST_G_CONSIDERED_DATE/G_CONSIDERED_DATE'):
                record = {}
                for element in g_rec_type_desc:
                    record[element.tag] = element.text
                data.append(record)

            # Eğer dataframe boşsa ikinci path'i kontrol et
            if not data:
                for g_rec_type_desc in root.findall('LIST_G_REC_TYPE_DESC/G_REC_TYPE_DESC/LIST_G_CONSIDERED_DATE/G_CONSIDERED_DATE'):
                    record = {}
                    for element in g_rec_type_desc:
                        record[element.tag] = element.text
                    data.append(record)

            df = pd.DataFrame(data)
            return df
        except Exception as e:
            print(f"Hata: {e}")

    def convert_europrotel_xml_to_dataframe(self, file_path, room_type_mapping, company_id_prod, company_id_test, currency):
        """Europrotel PMS için XML/Excel dönüştürme - tüm oda tipleri tek dosyada"""
        try:
            file_ext = os.path.splitext(file_path)[1].lower()

            if file_ext in ['.xlsx', '.xls']:
                # Excel dosyası oku
                df = pd.read_excel(file_path)
                # Metadata sütunlarını düşür
                metadata_cols = [col for col in df.columns if col in ['Name', 'ExecutionTime', 'Textbox22', 'Textbox20', 'Textbox30']]
                if metadata_cols:
                    df.drop(columns=metadata_cols, inplace=True)
                print(f"Europrotel Excel'den {len(df)} satır okundu.")
            else:
                # XML dosyası oku
                tree = ET.parse(file_path)
                root = tree.getroot()

                # Namespace'i kontrol et
                namespace = root.tag.replace('Details_Collection', '').replace('{', '').replace('}', '')

                # XML'den tüm Details elementlerini çek
                data = []
                for detail in root.findall('.//{' + namespace + '}Details') if namespace else root.findall('.//Details'):
                    record = detail.attrib  # Tüm attribute'leri al
                    data.append(record)

                if not data:
                    print("Europrotel XML'de veri bulunamadı!")
                    return pd.DataFrame()

                df = pd.DataFrame(data)
                print(f"Europrotel XML'den {len(df)} satır okundu.")

            # Europrotel kolon isimlerini standart kolon isimlerine dönüştür
            europrotel_column_mapping = {
                'considered_date': 'CONSIDERED_DATE',
                'net_room_revenue': 'NET_ROOM_REVENUE',
                'no_rooms': 'NO_ROOMS',
                'no_person': 'NO_PERSONS',
                'arrival_rooms': 'ARRIVAL_ROOMS',
                'departure_rooms': 'DEPARTURE_ROOMS',
                'complimentary_rooms': 'CF_COMPLIMENTARY_ROOMS',
                'house_use_rooms': 'CF_HOUSE_USE_ROOMS',
                'days_use_rooms': 'CF_DAY_USE_ROOMS',
                'out_of_order_rooms': 'CF_OOO_ROOMS',
                'no_show_rooms': 'CF_NO_SHOW_ROOMS',
                'inventory_rooms': 'INVENTORY_ROOMS',
                'adr': 'CF_ADR_BY_ROOM',
                'occupancy_rates': 'CF_OCC',
                'currency': 'currency'
            }

            df.rename(columns=europrotel_column_mapping, inplace=True)

            # Oda tipi kodlarına göre filtrele ve room_type_id ekle
            result_dfs = []
            for room_code, room_type_id in room_type_mapping.items():
                # Bu oda koduna ait satırları filtrele
                room_df = df[df['room_type'] == room_code].copy()

                if not room_df.empty:
                    # ID'leri ekle
                    room_df['company_id_prod'] = company_id_prod
                    room_df['hotel_booking_room_type_id_prod'] = room_type_id
                    room_df['company_id_test'] = company_id_test
                    room_df['hotel_booking_room_type_id_test'] = room_type_id  # Test için de aynı olabilir
                    # CURRENCY XML'den geliyor, ezmeyelim
                    room_df['file_name'] = room_code  # Oda kodunu file_name olarak kullan

                    # Eksik kolonları ekle (Opera formatıyla uyumlu olması için)
                    if 'ADULTS' not in room_df.columns:
                        room_df['ADULTS'] = None
                    if 'CHILDREN' not in room_df.columns:
                        room_df['CHILDREN'] = None
                    if 'IND_ROOMS' not in room_df.columns:
                        room_df['IND_ROOMS'] = None
                    if 'GRP_ROOMS' not in room_df.columns:
                        room_df['GRP_ROOMS'] = None

                    result_dfs.append(room_df)
                    print(f"Oda tipi {room_code} (ID: {room_type_id}) için {len(room_df)} satır eklendi.")

            if result_dfs:
                final_df = pd.concat(result_dfs, ignore_index=True)
                print(f"Toplam {len(final_df)} satır Europrotel verisi hazırlandı.")
                return final_df
            else:
                print("Eşleşen oda tipi bulunamadı!")
                return pd.DataFrame()

        except Exception as e:
            print(f"Europrotel XML dönüştürme hatası: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()
   


