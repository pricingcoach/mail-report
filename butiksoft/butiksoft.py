import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotel_analysis import log_record
from hotel_analysis.analysis import Analysis
from hotel_analysis.config import BASE_DIR
from hotel_analysis.currency_exchange import HotelCurrencyConverter

# ─── Sabitler ────────────────────────────────────────────────────────────────
BUTIKSOFT_DIR = os.path.join(BASE_DIR, "hotels-document", "butiksoft")

_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hotel_analysis", "hotel_configs.json"
)


def _load_hotel_configs_from_json(json_path):
    """hotel_configs.json'daki 'butiksoft' bloklarından HOTEL_CONFIGS dict'ini oluşturur."""
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    result = {}
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        bs = hotel.get("butiksoft")
        if not bs:
            continue
        company_id_prod = str(bs["company_id_prod"])
        result[company_id_prod] = {
            "hotel_name": hotel.get("hotel_name", hotel_key),
            "hotel_booking_room_type_id_prod": bs.get("hotel_booking_room_type_id_prod"),
            "company_id_test": str(bs["company_id_test"]) if bs.get("company_id_test") is not None else None,
            "file_name": bs.get("file_name"),
            "currency": bs.get("currency", "EUR"),
            "capacity": bs.get("capacity"),
        }
    return result


HOTEL_CONFIGS = _load_hotel_configs_from_json(_JSON_PATH)

# ─── Ana İşlem ────────────────────────────────────────────────────────────────
created_date = datetime.now()
formatted_date = created_date.strftime("%d-%m-%Y")

hotel_analysis = Analysis()


def hesapla_occ(df):
    df['CF_OCC'] = (df['Revpar'] / df['Adr']) * 100
    return df


for company_id, config in HOTEL_CONFIGS.items():
    currency = config["currency"]
    hotel_name = config["hotel_name"]
    hotel_booking_room_type_id_prod = config["hotel_booking_room_type_id_prod"]
    capacity = config["capacity"]

    readFile = os.path.join(BUTIKSOFT_DIR, f"{config['file_name']}_{formatted_date}.xlsx")

    if not os.path.exists(readFile):
        print(f"Dosya bulunamadı: {readFile}")
        continue
    else:
        print(f"Dosya bulundu: {readFile}")

    if capacity is None:
        print(f"HATA: company_id={company_id} ({hotel_name}) için capacity tanımlı değil. "
              f"hotel_configs.json içindeki butiksoft bloğunu güncelleyin.")
        continue

    start_time = datetime.now()
    try:
        data = pd.read_excel(readFile)

        data['INVENTORY_ROOMS'] = capacity
        data['currency'] = 'TRY'  # Excel verisi TRY cinsinden gelir

        data = hesapla_occ(data)

        swapList = ['No', 'Tarih', 'Satılan Oda', 'Yetiskin', 'Cocuk', 'Kon. Geliri', 'Adr', 'INVENTORY_ROOMS', 'currency', 'CF_OCC']
        data = data.reindex(columns=swapList)
        data.columns = ['No', 'CONSIDERED_DATE', 'NO_ROOMS', 'ADULTS', 'CHILDREN', 'NET_ROOM_REVENUE', 'CF_ADR_BY_ROOM', 'INVENTORY_ROOMS', 'currency', 'CF_OCC']

        swapList = ['NET_ROOM_REVENUE', 'CONSIDERED_DATE', 'NO_PERSONS', 'NO_ROOMS',
                    'ADULTS', 'CHILDREN', 'ARRIVAL_ROOMS', 'DEPARTURE_ROOMS', 'IND_ROOMS',
                    'GRP_ROOMS', 'INVENTORY_ROOMS', 'CF_OCC', 'CF_ADR_BY_ROOM',
                    'created_date', 'created_by', 'last_modified_date', 'last_modified_by',
                    'company_id', 'hotel_booking_room_type_id', 'currency']
        data = data.reindex(columns=swapList)

        data.columns = [x.lower() for x in data.columns]
        data.columns = data.columns.str.replace("[ ]", "_", regex=True)

        data['considered_date'] = pd.to_datetime(data['considered_date'])
        data['created_date'] = datetime.now()

        # TRY → EUR dönüşümü öncesi orijinal değerleri sakla
        data['original_currency'] = 'TRY'
        data['original_net_room_revenue'] = data['net_room_revenue']
        data['original_cf_adr_by_room'] = data['cf_adr_by_room']

        process = HotelCurrencyConverter()
        data = process.convert_to_hotel_currency(data=data, company_id=company_id)
        data['currency'] = currency  # dönüşüm sonrası hedef para birimi (EUR)

        data['considered_date'] = pd.to_datetime(data['considered_date']).dt.strftime('%Y-%m-%d')
        data['created_date'] = pd.to_datetime(data['created_date']).dt.strftime('%Y-%m-%dT%H:%M:%S')
        data['last_modified_date'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')

        numeric_columns = ['net_room_revenue', 'cf_adr_by_room', 'cf_occ', 'no_persons',
                           'no_rooms', 'adults', 'children', 'arrival_rooms', 'departure_rooms',
                           'ind_rooms', 'grp_rooms', 'inventory_rooms']

        for col in numeric_columns:
            if col in data.columns:
                data[col] = pd.to_numeric(data[col], errors='coerce')

        data["hotel_booking_room_type_id"] = None
        data['no_persons'] = data['adults'].fillna(0) + data['children'].fillna(0)
        data["net_room_revenue"] = data["net_room_revenue"].fillna(0).round(2)
        data["cf_occ"] = data["cf_occ"].fillna(0).round(2)
        data["cf_adr_by_room"] = data["cf_adr_by_room"].fillna(0).round(2)
        data["no_rooms"] = data["no_rooms"].fillna(0)

        dataProd = data.copy()
        dataProd["company_id"] = company_id
        dataProd['company_id'] = dataProd['company_id'].astype(int)

        hotel_analysis.insert_room_analysis(dataProd)
        print(" Otel Geneli Excel dosyası Prod PostgreSQL tablosuna başarıyla eklendi.")

        dataProd["hotel_booking_room_type_id"] = hotel_booking_room_type_id_prod
        hotel_analysis.insert_room_analysis(dataProd)
        print(" Oda Tipli Excel dosyası Prod PostgreSQL tablosuna başarıyla eklendi.")

        end_time = datetime.now()
        duration_ms = (end_time - start_time).total_seconds() * 1000
        log_record.save_log(
            df=dataProd,
            integration_type="BUTIKSOFT",
            company_id=int(company_id),
            status="SUCCESS",
            duration_ms=duration_ms,
            start_time=start_time,
            end_time=end_time,
        )

    except Exception as e:
        end_time = datetime.now()
        duration_ms = (end_time - start_time).total_seconds() * 1000
        print(f"HATA: company_id={company_id} ({hotel_name}) işlenirken hata oluştu: {e}")
        log_record.save_log(
            df=pd.DataFrame(),
            integration_type="BUTIKSOFT",
            company_id=int(company_id),
            status="FAILURE",
            duration_ms=duration_ms,
            start_time=start_time,
            end_time=end_time,
            error_message=str(e)[:500],
        )
        continue


# ─── Dosyaları history klasörüne taşı ────────────────────────────────────────
src_dir = Path(BUTIKSOFT_DIR)
dst_dir = src_dir / "history"
date_pattern = re.compile(re.escape(formatted_date))
moved = []

for f in src_dir.iterdir():
    if f.is_file() and date_pattern.search(f.name):
        dest = dst_dir / f.name
        shutil.move(str(f), str(dest))
        print(f"Taşındı: {f.name}")
        moved.append(f.name)

print(f"\nToplam taşınan dosya: {len(moved)}")
