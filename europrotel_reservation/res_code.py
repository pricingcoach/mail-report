import json
import logging
import os
from datetime import datetime

import pandas as pd

from hotel_analysis import api_client
from hotel_analysis import log_record
from hotel_analysis.config import RESERVATION_DIR
from hotel_analysis.fetch_lookup import FetchLookup
from HotelDataAnalyzeClass import HotelDataAnalyzeClass

logger = logging.getLogger(__name__)

_JSON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "hotel_analysis", "hotel_configs.json"
)


def _load_hotel_configs_from_json(json_path, reservation_dir):
    """hotel_configs.json'daki 'europrotel_reservation' bloklarından hotel_configs dict'ini oluşturur."""
    with open(json_path, encoding="utf-8") as f:
        configs = json.load(f)
    result = {}
    for hotel_key, hotel in configs.items():
        if hotel_key.startswith("_"):
            continue
        er = hotel.get("europrotel_reservation")
        if not er:
            continue
        company_id = er["company_id"]
        folder_name = er["folder_name"]
        result[company_id] = {
            "directory": os.path.join(reservation_dir, folder_name) + "/",
            "hotel_name": er["hotel_name"],
            "email_rules": er.get("email_rules", []),
        }
    return result


class ResCode:
    def __init__(self):
        self.hotel_id = None
        self.lookup = None
        self.hotel_name = None
        self.data_cancel = None
        self.start_time = None
        self.end_time = None
        self.status = None
        self.error_message = ""

        self.today = datetime.today().date()
        self.fetch = FetchLookup()

        self.hotel_configs = _load_hotel_configs_from_json(_JSON_PATH, RESERVATION_DIR)

    def get_email_mappings(self):
        """Extract email rules from hotel configs for EmailIntegration.save_attachment()"""
        mappings = []
        for cfg in self.hotel_configs.values():
            for rule in cfg.get("email_rules", []):
                mappings.append({
                    "mail": rule["sender"],
                    "subject": rule["subject"],
                    "reply_to_mail": rule.get("reply_to_mail"),
                    "file": cfg["directory"],
                    "file_name": rule.get("file_name"),
                })
        return mappings

    def process_data(self, data, lookup, hotel_id, hotel_name):
        process = HotelDataAnalyzeClass()
        if 'KisiSayisi' not in data.columns:
            data['KisiSayisi'] = None

        data = data[['booking_no', 'rooms', 'total_amount', 'booking_date', 'cancellation_date', 'arrival_date',
                      'KisiSayisi', 'night', 'departure_date', 'status', 'room_code', 'currency', 'point_of_sale']]
        data = data.rename(columns={'KisiSayisi': 'guests'})
        data = data.loc[data["rooms"] > 0].reset_index(drop=True)
        data = data[data['booking_no'].notna()]

        data.loc[data['currency'] == 'TRL', 'currency'] = 'TRY'

        data['booking_no'] = data['booking_no'].astype(int)

        data.loc[data["status"] == "NO SHOW", "room_code"] = "PM"

        # BUG FIX: Gereksiz datetime->string->datetime round-trip kaldirildi
        for col in ['booking_date', 'arrival_date', 'departure_date']:
            data[col] = pd.to_datetime(data[col])

        data['night'] = (pd.to_datetime(data['departure_date']) - pd.to_datetime(data['arrival_date'])).dt.days
        data.loc[data['departure_date'] == data['arrival_date'], 'night'] = 1
        data = process.currencyCalculate(data)

        data = process.currencyCheck(data)

        data = process.roomTypeMatch(data, lookup)

        data = process.create_master_data(data, hotel_id, hotel_name)

        return data

    def process_cancel_data(self):
        logger.info("Processing cancel data...")

        try:
            self.data_cancel["hotel_id"] = self.hotel_id

            if "booking_no" in self.data_cancel.columns:
                df_cancel = self.data_cancel[["hotel_id", "booking_no"]]
            else:
                logger.error("Cancel data'da booking_no kolonu bulunamadi.")
                return
            df_cancel.columns = ['hotel_id', 'booking_no']

            my_instance = HotelDataAnalyzeClass()

            data_ek_cancel = my_instance.resultEkstraCancel(df_cancel, self.hotel_id, self.hotel_name)
            api_client.delete_dataframe(data_ek_cancel)
            logger.info("Cancel data processing completed.")

        except Exception as e:
            logger.error("Cancel data processing failed: %s", e)

    def _process_and_log(self, data_format, is_cancel=False):
        """Common try/except + log_record wrapper"""
        try:
            self.start_time = datetime.now()
            if is_cancel:
                self.process_cancel_data()
            else:
                api_client.upload_dataframe(data_format)
            logger.info("Reservation Verilerini Yazdirma Islemi Basariyla Tamamlanmistir.")
            self.error_message = ""
            self.end_time = datetime.now()
            self.status = "SUCCESS"
        except Exception as e:
            self.status = "FAILURE"
            self.error_message = str(e)
            self.end_time = datetime.now()
            logger.error("Islem hatasi: %s", e)

        # BUG FIX: Cancel log'da data_format yerine self.data_cancel kullanilmali
        log_data = self.data_cancel if is_cancel else data_format
        log_record.save_log(
            log_data,
            integration_type="OPERA_RESERVATION",
            company_id=self.hotel_id,
            status=self.status,
            error_message=self.error_message,
            duration_ms=round((self.end_time - self.start_time).total_seconds() * 1000, 3),
            start_time=self.start_time,
            end_time=self.end_time,
        )

    def process_hotels(self):
        """Process all hotels in the configuration"""
        for hid, config in self.hotel_configs.items():
            directory = config["directory"]
            self.hotel_name = config["hotel_name"]
            self.hotel_id = hid

            if not os.path.exists(directory):
                logger.warning("Directory does not exist: %s", directory)
                continue

            for fname in os.listdir(directory):
                fpath = os.path.join(directory, fname)
                if not os.path.isfile(fpath):
                    continue

                modified_date = datetime.fromtimestamp(os.path.getmtime(fpath)).date()
                if modified_date != self.today:
                    continue

                logger.info("Hotel ID: %s | Name: %s", self.hotel_id, self.hotel_name)

                try:
                    self.lookup = FetchLookup().fetch_data(self.hotel_id)
                    logger.info("Lookup loaded: %d rows", len(self.lookup))

                    if 'Can' not in fpath:
                        data = pd.read_excel(fpath)
                        data_format = self.process_data(
                            data, lookup=self.lookup,
                            hotel_id=self.hotel_id, hotel_name=self.hotel_name
                        )
                        self._process_and_log(data_format)
                    else:
                        self.data_cancel = pd.read_excel(fpath)
                        self._process_and_log(self.data_cancel, is_cancel=True)

                except Exception as e:
                    logger.error("Error processing %s: %s", fpath, e)
