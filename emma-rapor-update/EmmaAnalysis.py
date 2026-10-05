import json
import os
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from hotel_analysis import log_record as ha_log_record
from hotel_analysis.config import LOG_AUTH_URL, API_EMAIL, API_PASSWORD

# Emma'da company_id (52, 640 ...) ≠ booking-api hotel_id (237, 301 ...)
# Integration log doğru hotel_id ile kaydedilmesi için mapping gerekli
COMPANY_TO_HOTEL_ID = {
    "52": "237", "640": "301", "806": "302",
    "44": "229", "45": "230", "360": "245",
    "2736": "02002", "2640": "00566",
}

ROOM_ANALYSIS_INSERT_URL = "https://bookingapi-py.pricing-coach.com/api/v1/roomanalysis/insert_room_analysis"


class EmmaAnalysis:
    def __init__(self, data_room_type, data_room_type_select, company, room_type, rooms, name, collection_date):
        self.data_room_type = data_room_type
        self.data_room_type_select = data_room_type_select
        self.database_table = 'hotel_booking_room_analysis'
        self.database_table_history = 'hotel_booking_room_analysis_history'

        self.company_id = company
        self.hotel_booking_room_type_id = room_type
        self.inventory_rooms = rooms
        self.collection_date = collection_date

        self.file_prefix = name

    def kolon_ismini_duzenle(self, kolon_ismi):
        return kolon_ismi.split(' ')[0].split('-')[0]

    def hesapla_adr(self, df):
        df['Room Nights'] = pd.to_numeric(df['Room Nights'], errors='coerce').fillna(0)
        df['Revenue room'] = pd.to_numeric(df['Revenue room'], errors='coerce').fillna(0)

        df['Room Nights'].fillna(0, inplace=True)
        df['Revenue room'].fillna(0, inplace=True)
        df['ADR'] = np.where(
            df['Room Nights'] == 0,
            df['Revenue room'],
            df['Revenue room'] / df['Room Nights']
        )
        return df

    def hesapla_occ(self, df):
        df['cf_occ'] = (df['no_rooms'] / df['inventory_rooms']) * 100
        return df

    def aggregate_by_date(self):
        sum_columns = ['net_room_revenue']
        first_columns = [
            'company_id', 'hotel_booking_room_type_id', 'inventory_rooms',
            'currency', 'created_date', 'collection_date', 'adults', 'children',
            'arrival_rooms', 'departure_rooms', 'ind_rooms', 'grp_rooms', 'no_persons'
        ]

        if 'occ forecast' in self.file_prefix:
            sum_columns.append('no_rooms')
        else:
            first_columns.append('no_rooms')

        agg_dict = {}
        for col in sum_columns:
            if col in self.data.columns:
                agg_dict[col] = 'sum'
        for col in first_columns:
            if col in self.data.columns:
                agg_dict[col] = 'first'

        self.data = self.data.groupby('considered_date').agg(agg_dict).reset_index()

        if 'net_room_revenue' in self.data.columns and 'no_rooms' in self.data.columns:
            self.data['cf_adr_by_room'] = np.where(
                self.data['no_rooms'] == 0,
                0,
                self.data['net_room_revenue'] / self.data['no_rooms']
            )

        self.data = self.hesapla_occ(self.data)

        print(f"Data aggregated. Rows after aggregation: {len(self.data)}")
        return self.data

    def preprocess_data(self):
        self.data_room_type_select = self.data_room_type_select[self.data_room_type_select['Period numb.'].notna()].reset_index(drop=True)

        if self.data_room_type is None:
            raise ValueError(f"data_room_type is None. The 'occroomtype' CSV file could not be loaded for {self.file_prefix}.")

        self.data_room_type.columns = [self.kolon_ismini_duzenle(kolon) for kolon in self.data_room_type.columns]
        self.data_room_type_select = self.hesapla_adr(self.data_room_type_select)

        self.data_room_type_select['Period'] = pd.to_datetime(self.data_room_type_select['Period'], format='%d.%m.%Y')
        self.data_room_type_select['Period'] = self.data_room_type_select['Period'].dt.strftime('%Y-%m-%d')
        self.data_room_type_select = self.data_room_type_select.sort_values(by='Period')

        self.data_room_type_select = self.data_room_type_select.rename(columns={"Revenue room": "NET_ROOM_REVENUE", "ADR": "CF_ADR_BY_ROOM", "Period": "Date"})
        self.data_room_type['Date'] = pd.to_datetime(self.data_room_type['Date'], dayfirst=True)
        self.data_room_type_select['Date'] = pd.to_datetime(self.data_room_type_select['Date'])

        duzenlenmis_kolonlar = {self.kolon_ismini_duzenle(kolon): kolon for kolon in self.data_room_type.columns if kolon != 'Date'}
        eslesen_kolon = duzenlenmis_kolonlar.get(self.file_prefix, None)

        if eslesen_kolon:
            self.merged_df = pd.merge(self.data_room_type_select, self.data_room_type[['Date', eslesen_kolon]], on='Date')
        else:
            self.merged_df = self.data_room_type_select.copy()
            print(f"Eşleşen kolon bulunamadı: {self.file_prefix}")

        self.data = self.merged_df.iloc[:, :6]
        self.data = self.data.rename(columns={"Date": "CONSIDERED_DATE"})
        self.data = self.data.sort_values(by='CONSIDERED_DATE')

        if 'occ forecast' in self.file_prefix:
            self.data = self.data.rename(columns={'Room Nights': "NO_ROOMS"})
        else:
            self.data = self.data.rename(columns={f"{self.file_prefix}": "NO_ROOMS"})

    def add_additional_columns(self):
        self.data["company_id"] = self.company_id
        self.data["hotel_booking_room_type_id"] = self.hotel_booking_room_type_id
        self.data["INVENTORY_ROOMS"] = self.inventory_rooms
        self.data["currency"] = 'EUR'
        self.data['created_date'] = datetime.now()
        self.data['collection_date'] = self.collection_date
        print(self.data['created_date'])

    def reorder_columns(self):
        swap_list = ['NET_ROOM_REVENUE', 'CONSIDERED_DATE', 'NO_PERSONS', 'NO_ROOMS', 'ADULTS', 'CHILDREN', 'ARRIVAL_ROOMS', 'DEPARTURE_ROOMS', 'IND_ROOMS', 'GRP_ROOMS', 'INVENTORY_ROOMS', 'CF_OCC', 'CF_ADR_BY_ROOM', 'created_date', 'created_by', 'last_modified_date', 'last_modified_by', 'company_id', 'hotel_booking_room_type_id', 'currency', 'collection_date']
        self.data = self.data.reindex(columns=swap_list)
        self.data.columns = [x.lower() for x in self.data.columns]
        self.data.columns = self.data.columns.str.replace("[ ]", "_", regex=True)

    def inventory_room_check(self, df):
        df['inventory_rooms'] = df['inventory_rooms'].fillna(self.inventory_rooms)
        return df

    def finalize_data(self):
        self.data['no_rooms'] = self.data['no_rooms'].astype(str).str.replace(r'[^\d.]', '', regex=True)
        self.data['inventory_rooms'] = self.data['inventory_rooms'].astype(str).str.replace(r'[^\d.]', '', regex=True)

        self.data['no_rooms'] = pd.to_numeric(self.data['no_rooms'])
        self.data['inventory_rooms'] = pd.to_numeric(self.data['inventory_rooms'])

        self.aggregate_by_date()
        print(self.data)

    def finalize_occ(self):
        self.data = self.inventory_room_check(self.data)

        self.data['no_rooms'] = self.data['no_rooms'].astype(str).str.replace(r'[^\d.]', '', regex=True)
        self.data['inventory_rooms'] = self.data['inventory_rooms'].astype(str).str.replace(r'[^\d.]', '', regex=True)

        self.data['no_rooms'] = pd.to_numeric(self.data['no_rooms'])
        self.data['inventory_rooms'] = pd.to_numeric(self.data['inventory_rooms'])

        self.data['currency'] = 'EUR'
        self.aggregate_by_date()
        print(self.data)

    def manipulate_values(self):
        self.data['considered_date'] = pd.to_datetime(self.data['considered_date']).dt.date
        self.data['net_room_revenue'] = pd.to_numeric(self.data['net_room_revenue'])
        self.data['cf_adr_by_room'] = pd.to_numeric(self.data['cf_adr_by_room'])
        self.data['cf_occ'] = pd.to_numeric(self.data['cf_occ'])
        self.data['no_persons'] = pd.to_numeric(self.data['no_persons'])
        self.data['adults'] = pd.to_numeric(self.data['adults'])
        self.data['children'] = pd.to_numeric(self.data['children'])
        self.data['arrival_rooms'] = pd.to_numeric(self.data['arrival_rooms'])
        self.data['departure_rooms'] = pd.to_numeric(self.data['departure_rooms'])
        self.data['ind_rooms'] = pd.to_numeric(self.data['ind_rooms'])
        self.data['grp_rooms'] = pd.to_numeric(self.data['grp_rooms'])

        self.data["net_room_revenue"] = self.data["net_room_revenue"].round(2)
        self.data["cf_occ"] = self.data["cf_occ"].round(2)
        self.data["cf_adr_by_room"] = self.data["cf_adr_by_room"].round(2)
        self.data['no_rooms'] = self.data['no_rooms'].fillna(0).astype(int)

    def get_bearer_token(self):
        auth_payload = {"username": API_EMAIL, "password": API_PASSWORD}
        auth_response = requests.post(LOG_AUTH_URL, data=auth_payload)
        auth_response.raise_for_status()
        return auth_response.json()["access_token"]

    def insert_room_analysis(self):
        json_data = self.data.to_json(orient='records', date_format='iso')
        json_list = json.loads(json_data)

        token = self.get_bearer_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        self.start_time = datetime.now()
        try:
            response = requests.post(ROOM_ANALYSIS_INSERT_URL, json=json_list, headers=headers, timeout=600)
            response.raise_for_status()
            print("Ana tablo ve History tablosuna en güncel veriler başarıyla eklendi veya güncellendi.")
            self.error_message = ""
            self.end_time = datetime.now()
            self.status = "SUCCESS"
        except requests.exceptions.RequestException as e:
            print("Kayıt başarısız:", e)
            self.status = "FAILURE"
            self.error_message = str(e)
            self.end_time = datetime.now()

        hotel_id = COMPANY_TO_HOTEL_ID.get(str(self.company_id), str(self.company_id))
        ha_log_record.save_log(
            self.data,
            integration_type="EMMA_HF_UPDATE",
            company_id=hotel_id,
            status=self.status,
            error_message=self.error_message,
            duration_ms=round((self.end_time - self.start_time).total_seconds() * 1000, 3),
            start_time=self.start_time,
            end_time=self.end_time,
        )
