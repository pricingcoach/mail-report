"""
grppickup Import — Opera grup blok pickup raporunu parse ederek BLK room_count gunceller.

grppickup raporu (GRP6.FMX) her blok icin gun bazli ALLOTTED/PICKUP/AVAIL verir.
AVAIL = ALLOTTED - PICKUP = kalan oda sayisi (pickup edilmemis).

Bu modul:
  - grppickup XML parse eder
  - Her blok icin gun bazli AVAIL degerini cikarir
  - Mevcut BLK kayitlarinin room_count degerini AVAIL ile gunceller
  - SWAP_LIST formatinda DataFrame olusturur

P1432 ile birlikte calisir:
  - P1432 blogu olusturur (metadata: sirket, rate, revenue, tarih)
  - grppickup room_count gunceller (AVAIL)
  - booking_no = BLK-{ALLOTMENT_HEADER_ID} (P1432 ile ayni)

Kullanim:
    python grp_pickup_import.py <dosya> --hotel-id <hotel_id> --hotel-name <otel_adi>
                                [--dry-run] [--save-only]

Ornek:
    python grp_pickup_import.py grppickup.xml --hotel-id 1961 --hotel-name "Sheraton" --dry-run
"""

import argparse
import logging
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hotel_analysis.api_client import upload_dataframe
from hotel_analysis.data_analyzer import DataAnalyzer
from hotel_analysis.config import DAILY_UPLOAD_DIR

logger = logging.getLogger(__name__)

# grppickup'ta aktif blok statusleri - DEF, TE1, TE2
GRPPICKUP_ACTIVE_STATUSES = {'ACT', 'DEF', 'TE1', 'TE2', 'TEN', 'PRO'}


def fetch_blk_metadata(hotel_id, db_url=None):
    """BLK kayitlarinin metadata'sini dondurur.

    VPN bagimsizligi icin dogrudan DB baglanmak yerine bos sozluk doner.
    grppickup_to_dataframe() bos metadata ile calistirildigi zaman:
      - booking_date: begin_date kullanilir
      - company_name: DESCRIPTION (grppickup verisi) kullanilir
      - cf_rate: 0 kullanilir (total_amount = 0)

    Returns:
        dict: {} (bos — grppickup verisindeki alanlar kullanilir)
    """
    logger.info(
        "fetch_blk_metadata: DB baglantisi kaldirildi; grppickup verisi kullanilacak (hotel_id=%s)",
        hotel_id,
    )
    return {}

# Opera tarih formati (grppickup ALLOTMENT_DATE)
ALLOTMENT_DATE_FORMAT = '%d-%b-%y'  # 30-JAN-26


def parse_grppickup_xml(xml_path):
    """grppickup XML dosyasini parse et.

    Returns:
        list[dict]: Her blok+gun kombinasyonu icin:
            {header_id, code, description, status, market, rate, begin_date,
             date, init, allotted, pickup, avail}
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()

    if root.tag != 'GRPPICKUP':
        raise ValueError(f"Beklenmeyen root tag: {root.tag} (GRPPICKUP bekleniyor)")

    rows = []
    for blk in root.findall('.//G_ALLOTMENT_CODE'):
        header_id = (blk.findtext('ALLOTMENT_HEADER_ID', '') or '').strip()
        if not header_id:
            continue

        code = (blk.findtext('ALLOTMENT_CODE', '') or '').strip()
        description = (blk.findtext('DESCRIPTION', '') or '').strip()
        status = (blk.findtext('BOOKING_STATUS', '') or '').strip()
        market = (blk.findtext('MARKET_CODE', '') or '').strip()
        rate = (blk.findtext('RATE_CODE', '') or '').strip()
        begin_date = (blk.findtext('BEGIN_DATE', '') or '').strip()

        for d in blk.findall('.//G_ALLOTMENT_DATE'):
            allot_date_str = (d.findtext('ALLOTMENT_DATE', '') or '').strip()
            if not allot_date_str:
                continue

            for bs in d.findall('.//G_BLOCKSIZE'):
                init_val = int(bs.findtext('INIT', '0') or 0)
                allotted_val = int(bs.findtext('ALLOTTED', '0') or 0)
                pickup_val = int(bs.findtext('PICKUP', '0') or 0)
                avail_val = int(bs.findtext('AVAIL', '0') or 0)

                # Sadece veri olan satirlar (allotted veya pickup > 0)
                if allotted_val > 0 or pickup_val > 0:
                    rows.append({
                        'header_id': header_id,
                        'code': code,
                        'description': description,
                        'status': status,
                        'market': market,
                        'rate': rate,
                        'begin_date': begin_date,
                        'date': allot_date_str,
                        'init': init_val,
                        'allotted': allotted_val,
                        'pickup': pickup_val,
                        'avail': avail_val,
                    })

    logger.info("grppickup parse: %d blok, %d gun-blok satiri (veri olan)",
                len(set(r['header_id'] for r in rows)), len(rows))
    return rows


def aggregate_by_block(rows):
    """Gun bazli satirlari blok bazli ozetle.

    Gecmis tarihli gunler filtrelenir — sadece bugun ve gelecek tarihli
    gunler dahil edilir. Gecmis bloklarin DB'de ACTIVE kalmasini onler.

    Returns:
        list[dict]: Blok bazli ozet
    """
    from collections import defaultdict

    today = datetime.now().date()
    skipped_past = 0

    block_data = defaultdict(lambda: {
        'code': '', 'description': '', 'status': '', 'market': '', 'rate': '',
        'begin_date': '', 'days': 0,
        'total_init': 0, 'total_allotted': 0, 'total_pickup': 0, 'total_avail': 0,
        'day_details': [],
    })

    for r in rows:
        # Gecmis tarihli gunleri atla
        try:
            row_date = datetime.strptime(r['date'], ALLOTMENT_DATE_FORMAT).date()
            if row_date < today:
                skipped_past += 1
                continue
        except ValueError:
            pass

        hid = r['header_id']
        bd = block_data[hid]
        bd['code'] = r['code']
        bd['description'] = r['description']
        bd['status'] = r['status']
        bd['market'] = r['market']
        bd['rate'] = r['rate']
        bd['begin_date'] = r['begin_date']
        bd['days'] += 1
        bd['total_init'] += r['init']
        bd['total_allotted'] += r['allotted']
        bd['total_pickup'] += r['pickup']
        bd['total_avail'] += r['avail']
        bd['day_details'].append(r)

    if skipped_past > 0:
        logger.info("aggregate_by_block: %d gecmis tarihli gun-blok satiri filtrelendi", skipped_past)

    result = []
    for hid, bd in block_data.items():
        result.append({
            'header_id': hid,
            **{k: v for k, v in bd.items() if k != 'day_details'},
            'day_details': bd['day_details'],
        })

    return result


def grppickup_to_dataframe(block_summaries, hotel_id, hotel_name, currency='EUR',
                           default_market=None, default_segment=None, lookups=None,
                           blk_metadata=None):
    """Blok ozetlerini SWAP_LIST formatinda DataFrame'e donustur.

    P1432 ile ayni booking_no kullaniyor: BLK-{HEADER_ID}
    room_count = round(total_avail / gun_sayisi)
    Tam pickup olan bloklar (total_avail=0) CANCELED olarak isaretlenir.

    blk_metadata: fetch_blk_metadata() sonucu. Verilmisse:
        - booking_date: P1432 ENTERED_ON (BEGIN_DATE yerine)
        - company_name: P1432 ACCOUNT_NAME (DESCRIPTION yerine)
        - cf_rate: gecelik fiyat → total_amount = cf_rate × room_count
    """
    from p1432_block_import import fetch_block_lookups

    if blk_metadata is None:
        blk_metadata = {}

    lk_master_room = None
    lk_default_market = default_market
    lk_default_segment = default_segment
    rate_plan_map = {}

    if lookups:
        lk_master_room = lookups.get('master_room')
        if lookups.get('default_market'):
            lk_default_market = lookups['default_market']
        if lookups.get('default_segment'):
            lk_default_segment = lookups['default_segment']
        rate_plan_map = lookups.get('rate_plan_map', {})

    rows = []
    matched_count = 0
    fallback_count = 0
    updated_count = 0
    canceled_count = 0

    for blk in block_summaries:
        hid = blk['header_id']
        booking_no = f"BLK-{hid}"

        # Sadece aktif statusler
        if blk['status'] not in GRPPICKUP_ACTIVE_STATUSES:
            continue

        total_avail = blk['total_avail']
        total_allotted = blk['total_allotted']
        days = blk['days']

        if days <= 0:
            continue

        # room_count = gun basina ortalama AVAIL
        room_count = round(total_avail / days)

        # Tam pickup (avail=0) → room_count=0
        is_fully_picked = total_avail == 0 and blk['total_pickup'] > 0

        # begin_date parse (grppickup'ta 2 format var: DD.MM.YY ve DD-MON-YY)
        begin_dt = None
        for fmt in ['%d.%m.%y', ALLOTMENT_DATE_FORMAT]:
            try:
                begin_dt = datetime.strptime(blk['begin_date'], fmt)
                break
            except ValueError:
                continue

        if begin_dt is None:
            logger.warning("BEGIN_DATE parse hatasi: header_id=%s, begin=%s", hid, blk['begin_date'])
            continue

        # departure = son allotment_date + 1 gun
        last_date = None
        for dd in blk['day_details']:
            try:
                dt = datetime.strptime(dd['date'], ALLOTMENT_DATE_FORMAT)
                if last_date is None or dt > last_date:
                    last_date = dt
            except ValueError:
                continue

        if last_date is None:
            last_date = begin_dt

        departure = last_date + timedelta(days=1)
        night = (departure - begin_dt).days
        if night <= 0:
            night = 1

        # market/segment: grppickup MARKET_CODE oncelikli, yoksa rate_plan_map, yoksa default
        rate_code = blk['rate']
        grp_market = blk['market']  # grppickup raporundan gelen gercek market

        if grp_market and grp_market not in ('', 'None'):
            # 1. grppickup'tan gelen market — en guvenilir
            blk_market = grp_market
            blk_segment = grp_market
            matched_count += 1
        elif rate_code and rate_code in rate_plan_map:
            # 2. DB rate plan lookup — fallback
            blk_market, blk_segment = rate_plan_map[rate_code]
            fallback_count += 1
        else:
            # 3. Otel default
            blk_market = lk_default_market
            blk_segment = lk_default_segment
            fallback_count += 1

        status = 'ACTIVE'
        cancellation_date = None
        if is_fully_picked:
            status = 'CANCELED'
            cancellation_date = datetime.now()
            canceled_count += 1
        else:
            updated_count += 1

        # P1432 metadata (DB'den — booking_date, company_name, cf_rate)
        meta = blk_metadata.get(hid, {})
        blk_booking_date = meta.get('booking_date') or datetime.now()
        blk_company_name = meta.get('company_name') or blk['description']
        blk_cf_rate = float(meta.get('cf_rate', 0) or 0)
        blk_revenue = blk_cf_rate * room_count  # CF_RATE × AVAIL (pickup dustukce duser)

        rows.append({
            'hotel_id': hotel_id,
            'booking_no': booking_no,
            'name': blk['description'],
            'rooms': room_count,
            'total_amount': blk_revenue,
            'required_prepayment': None,
            'repaid_amount': None,
            'payment_method': None,
            'booking_date': blk_booking_date,
            'cancellation_date': cancellation_date,
            'arrival_date': begin_dt,
            'night': night,
            'departure_date': departure,
            'point_of_sale': None,
            'reference_source': None,
            'status': status,
            'original_status': 'SALES_PROJECT',
            'accommodation_property': blk['description'],
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
            'company_name': blk_company_name,
            'source_name': None,
        })

    logger.info("grppickup DataFrame: %d ACTIVE (room_count>0), %d CANCELED (tam pickup), "
                "%d market eslesti, %d fallback",
                updated_count, canceled_count, matched_count, fallback_count)

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.reindex(columns=DataAnalyzer.SWAP_LIST)

    return df


def print_summary(df):
    """grppickup bloklarinin ozetini yazdir."""
    if df is None or df.empty:
        print("Veri yok.")
        return

    print("\n" + "=" * 100)
    print(f"{'booking_no':<22} {'name':<32} {'arrival':<12} {'night':>5} {'rooms':>5} {'guests':>6} {'status':<10} {'rate':<8}")
    print("-" * 100)

    for _, row in df.iterrows():
        arrival = str(row['arrival_date'])[:10] if pd.notna(row['arrival_date']) else ''
        print(f"{str(row['booking_no']):<22} "
              f"{str(row['name'] or '')[:30]:<32} "
              f"{arrival:<12} "
              f"{int(row['night'] or 0):>5} "
              f"{int(row['rooms'] or 0):>5} "
              f"{int(row['guests'] or 0):>6} "
              f"{str(row['status'] or ''):<10} "
              f"{str(row['rate_code'] or ''):<8}")

    active = df[df['status'] == 'ACTIVE']
    canceled = df[df['status'] == 'CANCELED']
    print("-" * 100)
    print(f"ACTIVE:   {len(active):>4} blok, {int(active['rooms'].sum()):>5} oda (AVAIL)")
    print(f"CANCELED: {len(canceled):>4} blok (tam pickup)")
    print(f"TOPLAM:   {len(df):>4} blok")
    print("=" * 100)


def save_excel(df, hotel_name):
    """DataFrame'i Excel olarak kaydet."""
    if df is None or df.empty:
        return None

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    fname = f"grppickup_{hotel_name}_{timestamp}.xlsx"
    fpath = os.path.join(DAILY_UPLOAD_DIR, fname)
    os.makedirs(DAILY_UPLOAD_DIR, exist_ok=True)
    df.to_excel(fpath, index=False)
    logger.info("Excel kaydedildi: %s (%d satir)", fpath, len(df))
    return fpath


def main():
    parser = argparse.ArgumentParser(description='grppickup Import — Opera grup pickup raporunu isle')
    parser.add_argument('file', help='grppickup XML dosyasi')
    parser.add_argument('--hotel-id', type=int, required=True)
    parser.add_argument('--hotel-name', type=str, required=True)
    parser.add_argument('--currency', default='EUR')
    parser.add_argument('--market', default=None)
    parser.add_argument('--segment', default=None)
    parser.add_argument('--dry-run', action='store_true', help='Sadece goster, yukleme')
    parser.add_argument('--save-only', action='store_true', help='Excel kaydet, API yukleme')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    # Parse
    logger.info("Dosya: %s", args.file)
    rows = parse_grppickup_xml(args.file)
    if not rows:
        logger.warning("grppickup'ta veri olan blok yok.")
        return

    # Aggregate
    block_summaries = aggregate_by_block(rows)
    logger.info("Blok ozeti: %d blok (veri olan)", len(block_summaries))

    # DB lookup
    lookups = None
    if not args.dry_run:
        try:
            from p1432_block_import import fetch_block_lookups
            lookups = fetch_block_lookups(args.hotel_id)
        except Exception as e:
            logger.warning("DB lookup hatasi: %s (default degerler kullanilacak)", e)

    # DataFrame
    df = grppickup_to_dataframe(
        block_summaries,
        hotel_id=args.hotel_id,
        hotel_name=args.hotel_name,
        currency=args.currency,
        default_market=args.market,
        default_segment=args.segment,
        lookups=lookups,
    )

    print_summary(df)

    if args.dry_run:
        logger.info("--dry-run: Yukleme yapilmadi.")
        return

    # Save
    save_excel(df, args.hotel_name)

    if args.save_only:
        logger.info("--save-only: API yukleme yapilmadi.")
        return

    # Upload
    if not df.empty:
        upload_dataframe(df)
        logger.info("API yukleme tamamlandi: %d blok", len(df))


if __name__ == '__main__':
    main()
