"""
P1432 Block Import — Opera grup blok rezervasyonlarini sentetik booking olarak yukler.

Desteklenen formatlar:
  - XML (Sheraton vb.): Oracle Reports XML ciktisi
  - TXT/TSV (Grand Halic vb.): Tab-separated text ciktisi

Kullanim:
    python p1432_block_import.py <dosya> --hotel-id <hotel_id> --hotel-name <otel_adi>
                                 [--master-room DKNG] [--currency EUR]
                                 [--market GCO] [--segment GCO]
                                 [--dry-run] [--refresh]

Ornek:
    python p1432_block_import.py p1432_block.xml --hotel-id 1961 --hotel-name "Sheraton" --dry-run
    python p1432_block_import.py p1432_block.txt --hotel-id 1337 --hotel-name "Grand Hotel Halic" --dry-run
"""

import argparse
import logging
import math
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

import numpy as np
import pandas as pd

# Proje kok dizinini path'e ekle
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import requests

from hotel_analysis.api_client import upload_dataframe, delete_dataframe
from hotel_analysis.data_analyzer import DataAnalyzer
from hotel_analysis.config import DAILY_UPLOAD_DIR, LOG_AUTH_URL, ROOM_TYPES_BY_HOTEL_URL, API_EMAIL, API_PASSWORD

logger = logging.getLogger(__name__)

# Aktif blok statusleri (pick-up yapilmamis) - DEF, TE1, TE2
ACTIVE_STATUSES = {'ACT', 'DEF', 'TE1', 'TE2', 'TEN', 'PRO'}

# Iptal blok statusleri
CANCEL_STATUSES = {'CAN', 'LOS'}

# Blok rezervasyonlar icin sabit original_status degeri
BLOCK_ORIGINAL_STATUS = 'SALES_PROJECT'

# Opera tarih formati
DATE_FORMAT = '%d-%b-%y'  # 27-JAN-26


def _get_api_token() -> str:
    """booking-api-py bearer token'ı alır."""
    payload = {"username": API_EMAIL, "password": API_PASSWORD}
    response = requests.post(LOG_AUTH_URL, data=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_block_lookups(hotel_id, db_url=None):
    """booking-api-py'dan P1432 icin dinamik alan bilgilerini ceker.

    Master oda tipi booking-api-py /roomanalysis/room-types/{hotel_id} endpoint'inden
    (en yuksek kapasiteli oda tipi) alinir.
    Rate plan → market/segment mapping ve default market/segment VPN gerektirdigi icin
    bos degerlerle donar; import default parametrelerle calisir.

    Returns:
        dict: {
            'master_room': str or None,
            'rate_plan_map': {},
            'default_market': None,
            'default_segment': None,
        }
    """
    result = {
        'master_room': None,
        'rate_plan_map': {},
        'default_market': None,
        'default_segment': None,
    }

    try:
        token = _get_api_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{ROOM_TYPES_BY_HOTEL_URL}/{hotel_id}"
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        room_types = response.json()

        if room_types:
            # En yuksek kapasiteli oda tipini master oda olarak sec
            sorted_rooms = sorted(
                room_types,
                key=lambda x: x.get("capacity") or 0,
                reverse=True,
            )
            result["master_room"] = sorted_rooms[0].get("room_type")
            logger.info("Master oda tipi: %s (hotel_id=%d)", result["master_room"], hotel_id)
        else:
            logger.info("Oda tipi bulunamadi (hotel_id=%d), master_room=None", hotel_id)

    except Exception as e:
        logger.warning(
            "fetch_block_lookups API hatasi (hotel_id=%d): %s — default degerler kullanilacak",
            hotel_id, e,
        )

    return result


def parse_p1432_xml(xml_path):
    """P1432 XML dosyasini parse et, tum bloklari dondur."""
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Guard: root tag dogrulamasi — p2617 XML'i buraya girmemeli
    root_tag = root.tag.upper()
    if 'P1432' not in root_tag and 'BLOCKENTEREDONBY' not in root_tag:
        raise ValueError(
            f"P1432 XML degil! Root tag: <{root.tag}>. "
            f"Bu dosya p2617 veya baska bir rapor olabilir: {xml_path}"
        )

    blocks = []
    for block_el in root.iter('G_BLOCK_NAME'):
        block = {}
        for field in ['BLOCK_NAME', 'ACCOUNT_NAME', 'OWNER_CODE',
                       'BEGIN_DATE', 'END_DATE', 'ATTENDEES', 'ROOM_NIGHT',
                       'RATE_CODE', 'ROOM_REVENUE', 'ENTERED_ON', 'ENTERED_BY',
                       'BOOKING_STATUS', 'CAT_STATUS', 'BOOK_ID', 'CF_RATE']:
            el = block_el.find(field)
            block[field] = el.text.strip() if el is not None and el.text else ''
        blocks.append(block)

    logger.info("P1432 parse: %d blok bulundu (%s)", len(blocks), xml_path)
    return blocks


def parse_p1432_txt(txt_path):
    """P1432 TXT (TSV) dosyasini parse et, tum bloklari dondur.

    Format: Tab-separated, ilk satir header.
    Kolonlar: ORDER_CODE, GROUP_BY_CODE, ..., BLOCK_NAME, ACCOUNT_NAME, ..., BOOK_ID, ...
    Son satirlar summary (SUMATTPERREPORT, 0/0/0) — atlanir.
    """
    blocks = []

    with open(txt_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    if len(lines) < 2:
        logger.warning("TXT dosyasi bos veya sadece header: %s", txt_path)
        return blocks

    # Header
    headers = lines[0].strip().split('\t')
    logger.info("TXT kolonlar (%d): %s...", len(headers), headers[:8])

    # Gerekli kolonlarin index'leri
    REQUIRED_FIELDS = ['BLOCK_NAME', 'ACCOUNT_NAME', 'OWNER_CODE',
                        'BEGIN_DATE', 'END_DATE', 'ATTENDEES', 'ROOM_NIGHT',
                        'RATE_CODE', 'ROOM_REVENUE', 'ENTERED_ON', 'ENTERED_BY',
                        'BOOKING_STATUS', 'CAT_STATUS', 'BOOK_ID', 'CF_RATE']

    col_idx = {}
    for field in REQUIRED_FIELDS:
        if field in headers:
            col_idx[field] = headers.index(field)
        else:
            logger.warning("TXT'de kolon bulunamadi: %s", field)

    # Data satirlari (summary satirlarini atla)
    for i, line in enumerate(lines[1:], start=2):
        line = line.strip()
        if not line:
            continue

        cols = line.split('\t')

        # Summary satirlarini atla
        first_col = cols[0].strip() if cols else ''
        if first_col in ('SUMATTPERREPORT', 'SUMRNPERREPORT', 'SUMROOMREVPERREPORT'):
            continue
        if first_col == '0' and len(cols) < len(headers) // 2:
            continue

        # BOOK_ID olmayan satirlari atla
        book_id_idx = col_idx.get('BOOK_ID')
        if book_id_idx is None or book_id_idx >= len(cols):
            continue
        book_id = cols[book_id_idx].strip()
        if not book_id or not book_id.isdigit():
            continue

        block = {}
        for field in REQUIRED_FIELDS:
            idx = col_idx.get(field)
            if idx is not None and idx < len(cols):
                block[field] = cols[idx].strip()
            else:
                block[field] = ''
        blocks.append(block)

    logger.info("P1432 TXT parse: %d blok bulundu (%s)", len(blocks), txt_path)
    return blocks


def parse_p1432(file_path):
    """Dosya formatini otomatik algila (XML vs TXT) ve parse et."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == '.xml':
        return parse_p1432_xml(file_path)

    if ext in ('.txt', '.tsv', '.csv'):
        return parse_p1432_txt(file_path)

    # Uzanti belirsiz — icerigi kontrol et
    with open(file_path, 'r', encoding='utf-8') as f:
        first_line = f.readline(200)

    if first_line.strip().startswith('<?xml') or first_line.strip().startswith('<'):
        return parse_p1432_xml(file_path)
    else:
        return parse_p1432_txt(file_path)


def filter_active_blocks(blocks):
    """Aktif ve gelecek tarihli bloklari filtrele.

    Dahil edilen bloklar:
      1. STATUS in (DEF, TEN, TE1, PRO) VE END_DATE > bugun
      2. STATUS = ACT VE END_DATE > bugun (gerceklesmis ama henuz bitmemis)
    """
    today = datetime.now()
    active = []
    act_future = []

    for b in blocks:
        status = b['BOOKING_STATUS']

        # Tarih parse
        try:
            end_date = datetime.strptime(b['END_DATE'], DATE_FORMAT)
        except ValueError:
            logger.warning("Gecersiz tarih: BOOK_ID=%s, END_DATE=%s", b['BOOK_ID'], b['END_DATE'])
            continue

        is_future = end_date >= today

        # 1. Standart aktif statusler
        if status in ACTIVE_STATUSES and is_future:
            active.append(b)
            continue

        # 2. ACT + END_DATE henuz gecmemis → dahil et
        if status == 'ACT' and is_future:
            act_future.append(b)
            active.append(b)
            continue

    if act_future:
        logger.warning("ACT + END_DATE >= bugun: %d blok dahil edildi: %s",
                        len(act_future),
                        ', '.join(b['BOOK_ID'] for b in act_future))

    # 3. CAN/LOS bloklari — CANCELED olarak yüklenecek
    cancelled = [b for b in blocks if b['BOOKING_STATUS'] in CANCEL_STATUSES]
    if cancelled:
        logger.info("Iptal blok: %d (STATUS in %s) — CANCELED olarak yuklenecek",
                     len(cancelled), CANCEL_STATUSES)

    logger.info("Aktif blok: %d / %d (STATUS in %s veya ACT+gelecek, END_DATE >= bugun)",
                len(active), len(blocks), ACTIVE_STATUSES)
    return active, cancelled


def blocks_to_dataframe(blocks, hotel_id, hotel_name, master_room, currency, market, segment,
                        lookups=None):
    """Bloklari SWAP_LIST formatinda DataFrame'e donustur.

    Args:
        lookups: fetch_block_lookups() sonucu. Verilmisse:
            - room_type: lookups['master_room'] (fallback: master_room parametresi)
            - market/segment: RATE_CODE bazli lookup (fallback: market/segment parametresi)
    """
    # Lookup varsa, degerlerini kullan
    lk_master_room = master_room
    lk_default_market = market
    lk_default_segment = segment
    rate_plan_map = {}

    if lookups:
        if lookups.get('master_room'):
            lk_master_room = lookups['master_room']
        if lookups.get('default_market'):
            lk_default_market = lookups['default_market']
        if lookups.get('default_segment'):
            lk_default_segment = lookups['default_segment']
        rate_plan_map = lookups.get('rate_plan_map', {})

    rows = []
    seen_book_ids = set()
    matched_count = 0
    fallback_count = 0

    for b in blocks:
        # Duplicate korumasi — ayni BOOK_ID iki kez eklenmemeli
        book_id = b['BOOK_ID']
        if book_id in seen_book_ids:
            logger.warning("Duplicate BOOK_ID atlandi: %s (%s)", book_id, b['BLOCK_NAME'])
            continue
        seen_book_ids.add(book_id)

        try:
            begin = datetime.strptime(b['BEGIN_DATE'], DATE_FORMAT)
            end = datetime.strptime(b['END_DATE'], DATE_FORMAT)
        except ValueError:
            logger.warning("Tarih parse hatasi: BOOK_ID=%s", b['BOOK_ID'])
            continue

        night = (end - begin).days
        if night <= 0:
            night = 1

        is_cancelled = b['BOOKING_STATUS'] in CANCEL_STATUSES
        room_night = int(b['ROOM_NIGHT']) if b['ROOM_NIGHT'] else 0
        if room_night <= 0 and not is_cancelled:
            continue

        room_count = max(1, round(room_night / night)) if room_night > 0 else 0

        # Revenue
        revenue = 0.0
        if b['ROOM_REVENUE']:
            try:
                revenue = float(b['ROOM_REVENUE'])
            except ValueError:
                pass

        # booking_date = P1432 ENTERED_ON (gercek giris tarihi)
        booking_date = datetime.now()
        if b['ENTERED_ON']:
            try:
                booking_date = datetime.strptime(b['ENTERED_ON'], DATE_FORMAT)
            except ValueError:
                logger.warning("ENTERED_ON parse hatasi: BOOK_ID=%s, ENTERED_ON=%s", book_id, b['ENTERED_ON'])

        # booking_no = BLK-{BOOK_ID} (sabit — room_count degisse de ayni kalir)
        booking_no = f"BLK-{b['BOOK_ID']}"

        # market/segment: RATE_CODE bazli DB lookup, yoksa otel default
        rate_code = b['RATE_CODE']
        if rate_code and rate_code in rate_plan_map:
            blk_market, blk_segment = rate_plan_map[rate_code]
            matched_count += 1
        else:
            blk_market = lk_default_market
            blk_segment = lk_default_segment
            fallback_count += 1

        rows.append({
            'hotel_id': hotel_id,
            'booking_no': booking_no,
            'name': b['BLOCK_NAME'],
            'rooms': room_count,
            'total_amount': revenue,
            'required_prepayment': None,
            'repaid_amount': None,
            'payment_method': None,
            'booking_date': booking_date,
            'cancellation_date': datetime.now() if is_cancelled else None,
            'arrival_date': begin,
            'night': night,
            'departure_date': end,
            'point_of_sale': None,
            'reference_source': None,
            'status': 'CANCELED' if b['BOOKING_STATUS'] in CANCEL_STATUSES else 'ACTIVE',
            'original_status': BLOCK_ORIGINAL_STATUS,
            'accommodation_property': b['BLOCK_NAME'],
            'room_type': lk_master_room,
            'price_room_type': None,
            'extra_services': None,
            'customer': None,
            'gender': None,
            'country': None,
            'guests': None,
            'special_offer': None,
            'rate_code': rate_code,
            'discounts': None,
            'currency': currency,
            'original_total_price': None,
            'original_currency': None,
            'tax_amount': None,
            'original_tax_amount': None,
            'original_base_amount': None,
            'market': blk_market,
            'segment': blk_segment,
            'company_name': b['ACCOUNT_NAME'],
            'source_name': None,
        })

    if rate_plan_map:
        logger.info("Market/segment lookup: %d eslesti, %d fallback (otel default)",
                    matched_count, fallback_count)

    df = pd.DataFrame(rows)

    # SWAP_LIST sirasina gore reindex
    df = df.reindex(columns=DataAnalyzer.SWAP_LIST)

    logger.info("DataFrame: %d blok, toplam %d oda, toplam gelir: %.2f %s, room_type: %s",
                len(df), df['rooms'].sum() if not df.empty else 0,
                df['total_amount'].sum() if not df.empty else 0, currency, lk_master_room)

    return df


def print_summary(df):
    """Bloklarin ozetini yazdir."""
    if df.empty:
        logger.info("Yuklenecek blok yok.")
        return

    print("\n" + "=" * 90)
    print(f"{'booking_no':<22} {'name':<30} {'arrival':<12} {'night':>5} {'rooms':>5} {'revenue':>10} {'rate_code':<10}")
    print("-" * 90)

    for _, row in df.iterrows():
        arrival = pd.to_datetime(row['arrival_date']).strftime('%Y-%m-%d') if pd.notna(row['arrival_date']) else ''
        print(f"{str(row['booking_no']):<22} "
              f"{str(row['name'] or '')[:28]:<30} "
              f"{arrival:<12} "
              f"{int(row['night'] or 0):>5} "
              f"{int(row['rooms'] or 0):>5} "
              f"{float(row['total_amount'] or 0):>10.0f} "
              f"{str(row['rate_code'] or ''):<10}")

    print("-" * 90)
    print(f"{'TOPLAM':<22} {'':<30} {'':<12} "
          f"{int(df['night'].sum()):>5} "
          f"{int(df['rooms'].sum()):>5} "
          f"{float(df['total_amount'].sum()):>10.0f}")
    print("=" * 90)


def save_excel(df, hotel_name):
    """Upload oncesi Excel dosyasini kaydet."""
    os.makedirs(DAILY_UPLOAD_DIR, exist_ok=True)

    date_str = datetime.now().strftime('%Y-%m-%d')
    filename = f"{hotel_name} {date_str} Block Reservation Database Update.xlsx"
    filepath = os.path.join(DAILY_UPLOAD_DIR, filename)

    # Reservation olarak kaydet (cancellation_date NaN, name kalsin)
    df_out = df.copy()
    df_out['cancellation_date'] = np.nan

    df_out.to_excel(filepath, index=True)
    logger.info("Excel kaydedildi: %s", filepath)
    return filepath


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    parser = argparse.ArgumentParser(description='P1432 Block Import')
    parser.add_argument('file', help='P1432 dosya yolu (XML veya TXT)')
    parser.add_argument('--hotel-id', required=True, type=int, help='Hotel ID')
    parser.add_argument('--hotel-name', required=True, help='Otel adi')
    parser.add_argument('--master-room', default='DKNG', help='Master oda tipi (default: DKNG)')
    parser.add_argument('--currency', default='EUR', help='Para birimi (default: EUR)')
    parser.add_argument('--market', default=None, help='Market (default: None)')
    parser.add_argument('--segment', default=None, help='Segment (default: None)')
    parser.add_argument('--dry-run', action='store_true', help='Sadece ozet goster, yukleme yapma')
    parser.add_argument('--save-only', action='store_true', help='Excel kaydet ama API yukleme yapma')

    args = parser.parse_args()

    if not os.path.exists(args.file):
        logger.error("Dosya bulunamadi: %s", args.file)
        sys.exit(1)

    # 1. Parse (XML veya TXT otomatik algilanir)
    blocks = parse_p1432(args.file)

    # 2. Filter
    active_blocks, cancelled_blocks = filter_active_blocks(blocks)

    if not active_blocks:
        logger.info("Aktif blok bulunamadi. Cikiliyor.")
        sys.exit(0)

    # 3. DB'den dinamik lookup (room_type, market, segment)
    logger.info("DB lookup basliyor (hotel_id=%d)...", args.hotel_id)
    lookups = fetch_block_lookups(args.hotel_id)

    # 4. DataFrame olustur
    df = blocks_to_dataframe(
        active_blocks,
        hotel_id=args.hotel_id,
        hotel_name=args.hotel_name,
        master_room=args.master_room,
        currency=args.currency,
        market=args.market,
        segment=args.segment,
        lookups=lookups,
    )

    # 4. Ozet
    print_summary(df)

    if args.dry_run:
        logger.info("--dry-run: Yukleme yapilmadi.")
        return

    # 5. Excel kaydet
    filepath = save_excel(df, args.hotel_name)

    if args.save_only:
        logger.info("--save-only: Excel kaydedildi, API yukleme yapilmadi.")
        return

    # 6. API upload
    logger.info("API yukleme basliyor...")
    response = upload_dataframe(df)
    if response and response.status_code == 200:
        logger.info("Yukleme basarili! %d blok booking olusturuldu.", len(df))
    else:
        logger.error("Yukleme hatasi: %s", response.text if response else "No response")


if __name__ == '__main__':
    main()
