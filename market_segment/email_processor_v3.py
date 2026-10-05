import json
import pandas as pd
import numpy as np
import xml.etree.ElementTree as ET
import requests
from datetime import datetime, timedelta
import os
import sys
from difflib import SequenceMatcher
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hotel_analysis.config import (
    LOG_AUTH_URL,
    MARKET_ANALYSIS_INSERT_URL,
    API_EMAIL,
    API_PASSWORD,
)

# Yapılandırma parametreleri
# Scriptın bulunduğu dizini al
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Eğer __file__ çalışmazsa (örneğin interaktif modda), mevcut çalışma dizinini kullan
if not SCRIPT_DIR or SCRIPT_DIR == '':
    SCRIPT_DIR = os.getcwd()

print(f"Script directory: {SCRIPT_DIR}")

# JSON yapılandırma dosyası
_JSON_CONFIG_PATH = os.path.join(SCRIPT_DIR, '..', 'hotel_analysis', 'hotel_configs.json')


def _build_lookup_from_json(json_path):
    """hotel_configs.json'dan market_segment lookup DataFrame'i oluşturur."""
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    rows = []
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        ms = hotel.get("market_segment")
        if not ms:
            continue
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


CONFIG = {
    'files': {
        'attachments_dir': os.path.join(SCRIPT_DIR, 'attachments'),
        'history_dir': os.path.join(SCRIPT_DIR, 'history'),
        'similarity_threshold': 0.4  # Dosya adı benzerlik eşiği (0-1 arası) - daha dengeli
    },
    'xml': {
        'fields': ["COUNT_LENGTH_OF_STAY", "ROOM_REVENUE", "CF_AVGROOMRATE", "CF_OCC", "STAY_PERSONS"]
    }
}

# Tarih bilgisi
date_format = datetime.today()
collection_date = date_format.date()
created_date = datetime.now()  # Default olarak current date

# ---------------------- Yardımcı Fonksiyonlar ----------------------

def calculate_similarity(str1, str2):
    """İki string arasındaki benzerlik oranını hesaplar - geliştirilmiş versiyon"""
    if not str1 or not str2 or not isinstance(str1, str) or not isinstance(str2, str):
        return 0
    
    # Stringleri temizle ve normalize et
    str1_clean = str1.lower().replace('_', ' ').replace('-', ' ')
    str2_clean = str2.lower().replace('_', ' ').replace('-', ' ')
    
    # Temel benzerlik
    basic_similarity = SequenceMatcher(None, str1_clean, str2_clean).ratio()
    
    # Kelime bazlı benzerlik (daha güvenli)
    words1 = set(str1_clean.split())
    words2 = set(str2_clean.split())
    
    if len(words1) == 0 or len(words2) == 0:
        word_similarity = 0
    else:
        common_words = words1.intersection(words2)
        word_similarity = len(common_words) / max(len(words1), len(words2))
    
    # Anahtar kelime eşleşmeleri için bonus
    key_patterns = ['marketsegment', 'market', 'segment', 'p2910']
    key_bonus = 0
    for pattern in key_patterns:
        if pattern in str1_clean and pattern in str2_clean:
            key_bonus += 0.1
    
    # Toplam skor (maksimum %100)
    total_score = min(1.0, (basic_similarity * 0.6) + (word_similarity * 0.4) + key_bonus)
    
    return total_score

def is_filename_match(filename, expected_filename):
    """Dosya adı benzerliği kontrolü"""
    if not expected_filename or not isinstance(expected_filename, str):
        return False
    
    # Doğrudan içerme kontrolü
    if expected_filename.lower() in filename.lower():
        return True
    
    # Benzerlik oranı kontrolü
    similarity = calculate_similarity(filename, expected_filename)
    return similarity >= CONFIG['files']['similarity_threshold']

def find_files_recursively(start_path, file_pattern="*.xml"):
    """Bir dizinde ve alt dizinlerinde XML dosyalarını bulur"""
    import glob
    found_files = []
    
    try:
        # Mevcut dizinde ara
        pattern = os.path.join(start_path, "**", file_pattern)
        found_files = glob.glob(pattern, recursive=True)
    except Exception as e:
        print(f"Error searching in {start_path}: {e}")
    
    return found_files

def get_alternative_directories():
    """Alternatif arama dizinlerini döndürür"""
    current_dir = os.getcwd()
    script_dir = SCRIPT_DIR
    
    # Olası dizinler
    possible_dirs = [
        current_dir,
        script_dir,
        os.path.join(current_dir, "attachments"),
        os.path.join(script_dir, "attachments"),
        os.path.join(current_dir, "data"),
        os.path.join(script_dir, "data"),
        os.path.join(current_dir, "files"),
        os.path.join(script_dir, "files"),
        # Bir üst dizin
        os.path.dirname(current_dir),
        os.path.dirname(script_dir)
    ]
    
    # Var olan dizinleri döndür
    return [d for d in possible_dirs if os.path.exists(d)]
def find_best_matching_file_for_company(company_row, directory_path):
    """Şirket için en uygun dosyayı bulur - geliştirilmiş eşleştirme"""
    company_id = company_row['company_id']
    expected_filename = company_row.get('file_name', '') if 'file_name' in company_row else ''
    file_hotel = company_row.get('file_hotel', '') if 'file_hotel' in company_row else ''
    pms = company_row.get('pms', 'opera') if 'pms' in company_row.index else 'opera'
    if pd.isna(pms) or not pms:
        pms = 'opera'

    # PMS tipine göre dosya uzantısı belirle
    file_pattern = "*.xlsx" if pms.lower() == 'opera_cloud' else "*.xml"

    # Önce belirtilen dizini kontrol et
    directories_to_search = []
    
    if os.path.exists(directory_path):
        directories_to_search.append(directory_path)
        print(f"Company ID {company_id}: Searching in specified directory: {directory_path}")
    else:
        print(f"Company ID {company_id}: Specified directory does not exist: {directory_path}")
        print(f"Searching in alternative directories...")
        
        # Alternatif dizinleri ekle
        alternative_dirs = get_alternative_directories()
        directories_to_search.extend(alternative_dirs)
    
    best_match = None
    best_similarity = 0
    all_candidates = []  # Tüm adayları sakla
    
    # Her dizinde ara
    for search_dir in directories_to_search:
        print(f"  Searching in: {search_dir}")
        
        # Bu dizinde ve alt dizinlerinde dosyaları bul
        xml_files = find_files_recursively(search_dir, file_pattern)

        if not xml_files:
            print(f"    No {file_pattern} files found")
            continue

        print(f"    Found {len(xml_files)} {file_pattern} files")

        for file_path in xml_files:
            filename = os.path.basename(file_path)
            
            # Çoklu kriter skorlama sistemi
            scores = {
                'filename_similarity': 0,
                'hotel_match': 0,
                'pattern_match': 0,
                'file_age': 0,
                'total': 0
            }
            
            # 1. Dosya adı benzerliği
            if expected_filename and isinstance(expected_filename, str):
                scores['filename_similarity'] = calculate_similarity(filename, expected_filename)
            
            # 2. Hotel adı eşleşmesi
            if file_hotel and file_hotel.lower() in filename.lower():
                scores['hotel_match'] = 0.3
            
            # 3. Anahtar pattern eşleşmeleri
            key_patterns = ['marketsegment', 'market', 'segment', 'p2910']
            pattern_matches = sum(1 for pattern in key_patterns if pattern in filename.lower())
            scores['pattern_match'] = min(0.2, pattern_matches * 0.05)
            
            # 4. Dosya yaşı ve timestamp (en son dosya için bonus)
            try:
                file_mtime = os.path.getmtime(file_path)
                file_age_days = (datetime.now() - datetime.fromtimestamp(file_mtime)).days
                if file_age_days <= 7:
                    scores['file_age'] = 0.1
                # Timestamp'i sakla (aynı skorlarda en yeniyi seçmek için)
                scores['file_timestamp'] = file_mtime
            except:
                scores['file_timestamp'] = 0
            
            # 5. Toplam skor
            scores['total'] = (
                scores['filename_similarity'] * 0.6 +  # %60 dosya adı
                scores['hotel_match'] * 1.0 +          # %30 hotel eşleşmesi  
                scores['pattern_match'] * 1.0 +        # %20 pattern eşleşmesi
                scores['file_age'] * 1.0               # %10 dosya yaşı
            )
            
            # Adayları sakla
            candidate = {
                'filename': filename,
                'full_path': file_path,
                'scores': scores,
                'similarity': scores['total'],  # Geriye uyumluluk için
                'found_in': search_dir
            }
            all_candidates.append(candidate)
            
            print(f"    {filename}")
            print(f"      - Filename similarity: {scores['filename_similarity']:.3f}")
            print(f"      - Hotel match: {scores['hotel_match']:.3f}")  
            print(f"      - Pattern match: {scores['pattern_match']:.3f}")
            print(f"      - File age bonus: {scores['file_age']:.3f}")
            print(f"      - Total score: {scores['total']:.3f}")
            
            # En iyi eşleşmeyi güncelle (aynı skorsa en yeni dosyayı seç)
            if scores['total'] > best_similarity:
                best_similarity = scores['total']
                best_match = candidate
            elif scores['total'] == best_similarity and best_match:
                # Aynı skor - en yeni dosyayı seç (timestamp karşılaştırması)
                if scores.get('file_timestamp', 0) > best_match['scores'].get('file_timestamp', 0):
                    best_match = candidate
                    print(f"      ⭐ Same score but newer file - selecting this one")
        
        # Eğer çok iyi bir eşleşme bulunursa diğer dizinleri arama
        if best_match and best_match['similarity'] >= 0.8:
            break
    
    # Eşik kontrolü ve alternatif seçenekler
    threshold = CONFIG['files']['similarity_threshold']
    
    if best_match and best_match['similarity'] >= threshold:
        print(f"  ✓ Best match: {best_match['filename']} (Score: {best_match['similarity']:.3f})")
        print(f"    Found in: {best_match['found_in']}")
        return best_match
    elif best_match:
        print(f"  ⚠ Best match below threshold: {best_match['filename']} (Score: {best_match['similarity']:.3f})")
        
        # Kullanıcıya alternatifleri göster
        print(f"  📋 Top 3 candidates:")
        sorted_candidates = sorted(all_candidates, key=lambda x: x['similarity'], reverse=True)[:3]
        for i, candidate in enumerate(sorted_candidates, 1):
            print(f"    {i}. {candidate['filename']} (Score: {candidate['similarity']:.3f})")
        
        # Eşik altında olsa bile en iyisini döndür (manuel kontrol için)
        print(f"  ⚠ Using best candidate despite low score for manual review")
        return best_match
    else:
        print(f"  ✗ No {file_pattern} files found in any directory")
        
    return None

# ---------------------- XML İşleme Fonksiyonları ----------------------

def parse_europrotel_market_segment(file_path):
    """Europrotel PMS için Market Segment XML dosyasını parse eder"""
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()

        # Namespace'i kontrol et
        namespace = ''
        if root.tag.startswith('{'):
            namespace = root.tag.split('}')[0] + '}'

        # Verileri saklamak için liste
        data = []

        # Details elementlerini bul
        details_xpath = f'.//{namespace}Details' if namespace else './/Details'
        for detail in root.findall(details_xpath):
            record = {}

            # Attribute'leri oku
            if 'market_code' in detail.attrib:
                record['market_code'] = detail.attrib.get('market_code', '')
            if 'market_description' in detail.attrib:
                record['market_description'] = detail.attrib.get('market_description', '')
            if 'length_of_stay' in detail.attrib:
                record['length_of_stay'] = detail.attrib.get('length_of_stay', '0')
            if 'stay_persons' in detail.attrib:
                record['stay_persons'] = detail.attrib.get('stay_persons', '0')
            if 'net_room_revenue2' in detail.attrib:
                record['room_revenue'] = detail.attrib.get('net_room_revenue2', '0')
            if 'adr' in detail.attrib:
                record['cf_adr_by_room'] = detail.attrib.get('adr', '0')
            if 'occupancy_rates' in detail.attrib:
                record['cf_occ'] = detail.attrib.get('occupancy_rates', '0')

            data.append(record)

        # DataFrame oluştur
        df = pd.DataFrame(data)

        if df.empty:
            print(f"Europrotel XML'de veri bulunamadı: {file_path}")
            return pd.DataFrame()

        # Boş market_description olanları filtrele
        df = df[df['market_description'].notna()]
        df = df[df['market_description'] != '']

        print(f"Europrotel Market Segment: {len(df)} kayıt bulundu")
        return df

    except ET.ParseError as e:
        print(f"Europrotel XML Parse Error for {file_path}: {e}")
        return pd.DataFrame()
    except Exception as e:
        print(f"Europrotel XML işleme hatası for {file_path}: {e}")
        return pd.DataFrame()

def parse_opera_market_segment(file_path, swap_list, company_id=None):
    """Opera PMS için Market Segment XML dosyasını parse eder"""
    try:
        # XML dosyasını yükle
        tree = ET.parse(file_path)
        root = tree.getroot()

        # Verileri saklamak için bir liste
        data = []

        # company_id 3477 için özel XML yapısı (G_MARKET_GROUP seviyesinde)
        if company_id == 3477:
            # G_MARKET_GROUP döngüsü (üst seviye) - MARKET_GROUP_DESCRIPTION burada
            for g_market_group in root.findall('.//G_MARKET_GROUP'):
                # MARKET_GROUP_DESCRIPTION'ı al
                market_group_desc_element = g_market_group.find("MARKET_GROUP_DESCRIPTION")
                market_description = market_group_desc_element.text if market_group_desc_element is not None else None
                
                # G_MARKET_CODE'ları döngüye al
                for g_market_code in g_market_group.findall('.//G_MARKET_CODE'):
                    # Her bir G_DETAIL için alt alanları topla
                    for g_detail in g_market_code.findall('./LIST_G_DETAIL/G_DETAIL'):
                        record = {"MARKET_DESCRIPTION": market_description}  # Üst seviyedeki alanı ekle

                        # Sadece swapList içindeki alanları al
                        for field in swap_list:
                            element = g_detail.find(field)
                            record[field] = element.text if element is not None else None

                        # Kaydı listeye ekle
                        data.append(record)
        else:
            # Diğer şirketler için standart yapı (G_MARKET_CODE seviyesinde)
            for g_market_code in root.findall('.//G_MARKET_CODE'):
                # MARKET_DESCRIPTION üst seviyede bulunuyor
                market_description = g_market_code.find("MARKET_DESCRIPTION").text if g_market_code.find("MARKET_DESCRIPTION") is not None else None

                # Her bir G_DETAIL için alt alanları topla
                for g_detail in g_market_code.findall('./LIST_G_DETAIL/G_DETAIL'):
                    record = {"MARKET_DESCRIPTION": market_description}  # Üst seviyedeki alanı ekle

                    # Sadece swapList içindeki alanları al
                    for field in swap_list:
                        element = g_detail.find(field)
                        record[field] = element.text if element is not None else None

                    # Kaydı listeye ekle
                    data.append(record)

        # DataFrame oluştur
        df = pd.DataFrame(data)

        # Boş market_description olanları filtrele
        df = df[df['MARKET_DESCRIPTION'].notna()]

        # Sütun adlarını düzenle
        column_mapping = {
            'MARKET_DESCRIPTION': 'market_description',
            'COUNT_LENGTH_OF_STAY': 'length_of_stay',
            'ROOM_REVENUE': 'room_revenue',
            'CF_AVGROOMRATE': 'cf_adr_by_room',
            'CF_OCC': 'cf_occ',
            'STAY_PERSONS': 'stay_persons'
        }
        df.rename(columns=column_mapping, inplace=True)

        # company_id 3477 için market_description'a göre gruplama
        if company_id == 3477 and not df.empty:
            print(f"Company 3477: Grouping {len(df)} records by market_description...")
            
            # Önce numerik alanlara çevir
            numeric_columns = ['length_of_stay', 'room_revenue', 'stay_persons', 'cf_occ']
            for col in numeric_columns:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
            
            # Gruplama işlemi
            grouped = df.groupby('market_description', as_index=False).agg({
                'length_of_stay': 'sum',       # Topla
                'room_revenue': 'sum',          # Topla
                'stay_persons': 'sum',          # Topla
                'cf_occ': 'mean'                # Ortalama
            })
            
            # cf_adr_by_room hesapla (room_revenue / length_of_stay)
            grouped['cf_adr_by_room'] = grouped.apply(
                lambda row: row['room_revenue'] / row['length_of_stay'] 
                if pd.notna(row['length_of_stay']) and row['length_of_stay'] > 0 
                else None, 
                axis=1
            )
            
            df = grouped
            print(f"Company 3477: Grouped into {len(df)} unique market segments")

        return df

    except ET.ParseError as e:
        print(f"Opera XML Parse Error for {file_path}: {e}")
        return pd.DataFrame()

def parse_opera_cloud_market_segment(file_path):
    """Opera Cloud PMS için Market Segment XLSX dosyasını parse eder"""
    try:
        # Row 0 başlık satırı, Row 1 kolon başlıkları
        df = pd.read_excel(file_path, header=1)

        print(f"Opera Cloud XLSX columns: {df.columns.tolist()}")

        # Market Code NaN olanları filtrele (totals satırı + metadata satırları)
        if 'Market Code' not in df.columns:
            print(f"Opera Cloud XLSX'de 'Market Code' kolonu bulunamadı: {file_path}")
            return pd.DataFrame()

        df = df[df['Market Code'].notna()]
        df = df[df['Market Code'] != '']

        # Metadata satırlarını filtrele (numerik kolonları olmayan satırlar)
        numeric_check_cols = ['Stay Persons', 'Room Revenue EUR']
        existing_check_cols = [c for c in numeric_check_cols if c in df.columns]
        if existing_check_cols:
            for col in existing_check_cols:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df = df.dropna(subset=existing_check_cols, how='all')

        # Kolon eşleştirmesi (EUR kolonları kullanılıyor)
        column_mapping = {
            'Market Code': 'market_description',
            'Avg. Length Of Stay': 'length_of_stay',
            'Room Revenue EUR': 'room_revenue',
            'ADR Revenue EUR': 'cf_adr_by_room',
            'Occ.%': 'cf_occ',
            'Stay Persons': 'stay_persons'
        }

        # Eksik kolon kontrolü
        missing_cols = [col for col in column_mapping.keys() if col not in df.columns]
        if missing_cols:
            print(f"Opera Cloud XLSX'de eksik kolonlar: {missing_cols}")
            return pd.DataFrame()

        # Sadece gerekli kolonları al ve yeniden adlandır
        df = df[list(column_mapping.keys())].rename(columns=column_mapping)

        # Numerik alanlara çevir
        numeric_columns = ['length_of_stay', 'room_revenue', 'cf_adr_by_room', 'cf_occ', 'stay_persons']
        for col in numeric_columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        print(f"Opera Cloud Market Segment: {len(df)} kayıt bulundu")
        return df

    except Exception as e:
        print(f"Opera Cloud XLSX işleme hatası for {file_path}: {e}")
        return pd.DataFrame()

def parse_filtered_fields(file_path, swap_list, pms='opera', company_id=None):
    """XML dosyasından verileri çıkartıp DataFrame'e dönüştürür - PMS bazlı"""
    try:
        # PMS tipine göre farklı parser kullan
        if pms and pms.lower() == 'europrotel':
            return parse_europrotel_market_segment(file_path)
        elif pms and pms.lower() == 'opera_cloud':
            return parse_opera_cloud_market_segment(file_path)
        else:
            return parse_opera_market_segment(file_path, swap_list, company_id)

    except Exception as e:
        print(f"XML Parse Error for {file_path}: {e}")
        return pd.DataFrame()

# ---------------------- Veritabanı İşlemleri ----------------------

def _get_api_token() -> str:
    """booking-api-py bearer token'ı alır."""
    payload = {"username": API_EMAIL, "password": API_PASSWORD}
    response = requests.post(LOG_AUTH_URL, data=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def _save_to_market_analysis_api(data: pd.DataFrame) -> None:
    """Market segment verilerini booking-api-py /market-analysis/insert endpoint'ine gönderir.

    Eski delete-insert-to-database() işlevi yerini aldı.
    Endpoint aynı company_id + ay/yıl için mevcut kayıtları siler,
    ardından hem ana tabloya hem history tablosuna ekler.
    """
    if data is None or data.empty:
        print("⚠️ Gönderilecek veri yok.")
        return

    # NaN → None dönüşümü (JSON serileştirme için)
    data = data.where(pd.notna(data), None)

    # collection_date ve created_date alanlarını ISO string'e çevir
    for col in ['collection_date', 'created_date']:
        if col in data.columns:
            data[col] = data[col].apply(
                lambda v: v.isoformat() if hasattr(v, 'isoformat') else (str(v) if v is not None else None)
            )

    records = data.to_dict(orient='records')

    token = _get_api_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        MARKET_ANALYSIS_INSERT_URL,
        json=records,
        headers=headers,
        timeout=60,
    )
    response.raise_for_status()
    result = response.json()
    print(
        f"✅ Market analysis API: {result.get('inserted', '?')} kayıt eklendi, "
        f"{result.get('deleted', '?')} kayıt silindi."
    )
    
def save_to_database(company_id, data):
    """Verileri işleyip veritabanına kaydeder"""
    try:
        # market_code kolonu varsa sil (veritabanında bu kolon yok)
        if 'market_code' in data.columns:
            data = data.drop(columns=['market_code'])

        # Verilere company_id ve collection_date ekle
        data['company_id'] = company_id
        data['collection_date'] = collection_date
        data['created_date'] = created_date
        
        # Veri türlerini düzenle
        for col in data.columns:
            if col not in ['market_description', 'company_id', 'collection_date', 'created_date']:
                # Numerik alanlara çevir
                data[col] = pd.to_numeric(data[col], errors='coerce')
        
        # NaN değerleri None ile değiştir
        data = data.replace({np.nan: None})
        
        # API aracılığıyla kaydet
        print(data)
        print(f"Sending {len(data)} records to API for company {company_id}")
        _save_to_market_analysis_api(data)
        print(f"Data saved successfully for company {company_id}")
    
    except Exception as e:
        print(f"Error saving to database for company {company_id}: {e}")

# ---------------------- Dosya İşleme Fonksiyonları ----------------------

def get_company_directory(company_id, companies_df):
    """Excel'den company_id'ye karşılık gelen dosya dizinini döndürür."""
    row = companies_df[companies_df['company_id'] == company_id]
    if not row.empty and 'file_read' in row.columns:
        directory = row['file_read'].iloc[0]
        if pd.notna(directory) and isinstance(directory, str) and directory.strip():
            # Windows yollarını normalize et
            directory = directory.strip().replace('/', '\\')
            return directory
    # Eğer file_read yoksa file sütununu dene
    if not row.empty and 'file' in row.columns:
        directory = row['file'].iloc[0]
        if pd.notna(directory) and isinstance(directory, str) and directory.strip():
            # Windows yollarını normalize et  
            directory = directory.strip().replace('/', '\\')
            return directory
    # Eğer excelde yoksa varsayılan attachments dizini kullan
    return os.path.join(CONFIG['files']['attachments_dir'], str(company_id))

def validate_xml_file(file_path, company_id, pms='opera'):
    """XML/XLSX dosyasının geçerli olup olmadığını kontrol eder - PMS bazlı"""
    try:
        # Dosya boyutu kontrolü (çok küçük dosyalar muhtemelen boş)
        file_size = os.path.getsize(file_path)
        if file_size < 100:  # 100 byte'dan küçükse
            print(f"    ⚠ Warning: File too small ({file_size} bytes)")
            return False

        # Opera Cloud XLSX validasyonu
        if pms and pms.lower() == 'opera_cloud':
            if not file_path.lower().endswith('.xlsx'):
                print(f"    ⚠ Warning: Expected .xlsx file for opera_cloud PMS")
                return False
            try:
                df = pd.read_excel(file_path, header=1)
                if 'Market Code' not in df.columns:
                    print(f"    ⚠ Warning: 'Market Code' column not found in XLSX")
                    return False
                valid_records = df['Market Code'].dropna().shape[0]
                if valid_records == 0:
                    print(f"    ⚠ Warning: No valid market segment records in XLSX")
                    return False
                print(f"    ✓ Valid opera_cloud XLSX file with {valid_records} market segment records")
                return True
            except Exception as e:
                print(f"    ✗ XLSX validation error: {e}")
                return False

        # XML parse edilebilirlik kontrolü
        tree = ET.parse(file_path)
        root = tree.getroot()

        valid_records = 0

        # PMS tipine göre farklı validasyon
        if pms and pms.lower() == 'europrotel':
            # Europrotel formatı için validasyon
            namespace = ''
            if root.tag.startswith('{'):
                namespace = root.tag.split('}')[0] + '}'

            details_xpath = f'.//{namespace}Details' if namespace else './/Details'
            details = root.findall(details_xpath)

            if not details:
                print(f"    ⚠ Warning: No Europrotel market segment data found")
                return False

            # Geçerli kayıtları say
            for detail in details:
                if 'market_description' in detail.attrib and detail.attrib['market_description']:
                    valid_records += 1

        else:
            # Opera formatı için validasyon
            market_codes = root.findall('.//G_MARKET_CODE')
            if not market_codes:
                print(f"    ⚠ Warning: No market segment data found")
                return False

            # En az bir geçerli kayıt var mı kontrol et
            for g_market_code in market_codes:
                market_description = g_market_code.find("MARKET_DESCRIPTION")
                if market_description is not None and market_description.text:
                    g_details = g_market_code.findall('./LIST_G_DETAIL/G_DETAIL')
                    if g_details:
                        valid_records += len(g_details)

        if valid_records == 0:
            print(f"    ⚠ Warning: No valid market segment records found")
            return False

        print(f"    ✓ Valid {pms} XML file with {valid_records} market segment records")
        return True

    except ET.ParseError as e:
        print(f"    ✗ XML Parse Error: {e}")
        return False
    except Exception as e:
        print(f"    ✗ File validation error: {e}")
        return False

def process_company_file(company_id, file_path, pms='opera'):
    """Tek bir dosyayı işler ve veritabanına kaydeder - PMS bazlı"""
    try:
        print(f"Processing file: {file_path}")
        print(f"PMS Type: {pms}")

        # Önce dosyayı doğrula
        if not validate_xml_file(file_path, company_id, pms):
            print(f"File validation failed for {file_path}")
            return False

        # Dosyayı PMS tipine göre parse et
        data = parse_filtered_fields(file_path, CONFIG['xml']['fields'], pms, company_id)

        if not data.empty:
            # Veri kalitesi kontrolü
            print(f"Parsed {len(data)} records")

            # Boş olmayan kayıt sayısını kontrol et
            non_empty_records = data.dropna(subset=['market_description']).shape[0]
            if non_empty_records == 0:
                print(f"No valid market descriptions found")
                return False

            print(f"Found {non_empty_records} valid market segment records")

            # Verileri veritabanına kaydet
            save_to_database(company_id, data)
            return True
        else:
            print(f"No valid data found in {file_path}")
            return False

    except Exception as e:
        print(f"Error processing file {file_path}: {e}")
        return False

def process_all_companies(companies_df):
    """Tüm şirketler için en uygun dosyaları bulur ve işler - PMS bazlı"""
    total_processed = 0
    successful_companies = []
    failed_companies = []

    print(f"Processing {len(companies_df)} companies...")

    for index, company_row in companies_df.iterrows():
        try:
            company_id = company_row['company_id']

            # NaN kontrolü
            if pd.isna(company_id):
                print(f"Skipping row {index}: company_id is NaN")
                continue

            # Company ID'yi uygun formata çevir
            if isinstance(company_id, float) and company_id.is_integer():
                company_id = int(company_id)

            print(f"\n--- Processing Company ID: {company_id} ---")

            # PMS bilgisini al
            pms = company_row.get('pms', 'opera') if 'pms' in company_row else 'opera'
            if pd.isna(pms) or not pms:
                pms = 'opera'  # Varsayılan
            print(f"PMS Type: {pms}")

            # Şirketin dosya dizinini al
            company_dir = get_company_directory(company_id, companies_df)

            # En uygun dosyayı bul
            best_match = find_best_matching_file_for_company(company_row, company_dir)

            if best_match:
                # Dosyayı PMS tipine göre işle
                if process_company_file(company_id, best_match['full_path'], pms):
                    total_processed += 1
                    successful_companies.append({
                        'company_id': company_id,
                        'file': best_match['filename'],
                        'similarity': best_match['similarity'],
                        'pms': pms
                    })
                    print(f"✓ Successfully processed Company ID {company_id} ({pms})")
                else:
                    failed_companies.append({
                        'company_id': company_id,
                        'error': 'Failed to process XML data',
                        'pms': pms
                    })
                    print(f"✗ Failed to process data for Company ID {company_id} ({pms})")
            else:
                failed_companies.append({
                    'company_id': company_id,
                    'error': 'No matching file found',
                    'pms': pms
                })
                print(f"✗ No suitable file found for Company ID {company_id} ({pms})")

        except Exception as e:
            failed_companies.append({
                'company_id': company_id if 'company_id' in locals() else f"Row {index}",
                'error': str(e),
                'pms': pms if 'pms' in locals() else 'unknown'
            })
            print(f"✗ Error processing Company ID {company_id if 'company_id' in locals() else f'Row {index}'}: {e}")
            continue

    # Özet rapor
    print(f"\n{'='*50}")
    print(f"PROCESSING SUMMARY")
    print(f"{'='*50}")
    print(f"Total companies in Excel: {len(companies_df)}")
    print(f"Successfully processed: {len(successful_companies)}")
    print(f"Failed: {len(failed_companies)}")

    if successful_companies:
        print(f"\n✓ SUCCESSFUL COMPANIES:")
        for company in successful_companies:
            print(f"  Company {company['company_id']}: {company['file']} (PMS: {company['pms']}, Similarity: {company['similarity']:.3f})")

    if failed_companies:
        print(f"\n✗ FAILED COMPANIES:")
        for company in failed_companies:
            print(f"  Company {company['company_id']}: {company['error']} (PMS: {company.get('pms', 'unknown')})")

    return total_processed, successful_companies, failed_companies

# ---------------------- Excel İşleme Fonksiyonları ----------------------

def normalize_column_names(df):
    """Excel dosyasındaki sütun adlarını normalleştir - Excel kolonları aynen kullanılacak"""
    # Sadece boşlukları temizle, kolon adlarını değiştirme
    df.columns = [col.strip() for col in df.columns]
    
    print("Excel columns found:", df.columns.tolist())
    
    # Gerekli sütunların varlığını kontrol et
    required_columns = ['company_id']
    missing_columns = [col for col in required_columns if col not in df.columns]
    
    if missing_columns:
        print(f"Error: The following required columns are missing: {', '.join(missing_columns)}")
        print("Available columns: ", df.columns.tolist())
        raise ValueError(f"Required column 'company_id' not found in Excel file")
    
    return df

# ---------------------- Ana İşleme Fonksiyonu ----------------------

def process_files_from_json():
    """hotel_configs.json'daki market_segment şirketler için dosyaları işler"""
    try:
        print("Starting file processing...")

        json_path = os.path.normpath(_JSON_CONFIG_PATH)
        print(f"Loading configuration from: {json_path}")

        if not os.path.exists(json_path):
            raise FileNotFoundError(f"hotel_configs.json bulunamadı: {json_path}")

        companies_df = _build_lookup_from_json(json_path)
        print(f"Loaded {len(companies_df)} market_segment rows from hotel_configs.json")
        print(f"Available columns: {companies_df.columns.tolist()}")

        if 'company_id' not in companies_df.columns:
            raise ValueError("hotel_configs.json'daki market_segment bloklarında 'company_id' eksik.")

        # Dizin varlığını kontrol et
        missing_dirs = []
        for _, row in companies_df.iterrows():
            if 'file_read' in row and pd.notna(row['file_read']):
                dir_path = str(row['file_read']).strip()
                if not os.path.exists(dir_path):
                    missing_dirs.append(f"Company {row['company_id']}: {dir_path}")
                else:
                    print(f"✓ Directory exists for Company {row['company_id']}: {dir_path}")

        if missing_dirs:
            print("Warning: Some directories do not exist:")
            for missing in missing_dirs:
                print(f"  ✗ {missing}")
            print("Continuing with existing directories...")

        # Tüm şirketleri işle
        total_processed, successful_companies, failed_companies = process_all_companies(companies_df)

        print(f"\nFile processing completed! Processed {total_processed} companies successfully.")

    except Exception as e:
        print(f"Error in main processing: {e}")


# Backward-compatibility alias
def process_files_from_excel():
    """Alias kept for backward compatibility — delegates to process_files_from_json()."""
    process_files_from_json()

# ---------------------- Program Başlangıcı ----------------------

if __name__ == "__main__":
    # Ana işleme fonksiyonunu çağır (hotel_configs.json'dan okur)
    process_files_from_json()