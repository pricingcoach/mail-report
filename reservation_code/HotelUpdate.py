import json
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

from hotel_analysis import api_client
from hotel_analysis import log_record
from config import RESERVATION_DIR, LOOKUP_DIR
from hotel_analysis.data_analyzer import DataAnalyzer
from hotel_analysis.fetch_lookup import FetchLookup
from HotelDataAnalyzeClass import HotelDataAnalyzeClass
from EmailIntegration import EmailIntegration
from p1432_block_import import parse_p1432, filter_active_blocks, blocks_to_dataframe, fetch_block_lookups
from grp_pickup_import import parse_grppickup_xml, aggregate_by_block, grppickup_to_dataframe

logger = logging.getLogger(__name__)

HOTEL_CONFIGS_PATH = os.path.join(os.path.dirname(__file__), "..", "hotel_analysis", "hotel_configs.json")



class HotelUpdate:
    def __init__(self):
        self.hotel_id = None
        self.lookup = None
        self.pms_type = None
        self.hotel_name = None
        self.data_cancel = None
        self.unmatch = ""
        self.start_time = None
        self.end_time = None
        self.status = None
        self.error_message = ""

        self.today = datetime.today().date()
        self.fetch = FetchLookup()

        self.lookup_dir = LOOKUP_DIR
        logger.info("Lookup dosyalari dizini: %s", self.lookup_dir)

        # Load hotel configs from JSON
        self.hotel_configs = self._load_hotel_configs()

    @staticmethod
    def _load_hotel_configs():
        with open(HOTEL_CONFIGS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)

        configs = {}
        for hid, cfg in raw.items():
            if not isinstance(cfg, dict):
                continue
            directory = cfg.get("directory")
            if not directory:
                continue  # Reservation pipeline'ına dahil değil
            hotel_name = cfg.get("hotel_name")
            pms_type = cfg.get("pms_type")
            if not hotel_name or not pms_type:
                logger.warning("Eksik hotel_name veya pms_type, atlanıyor: %s", hid)
                continue
            configs[hid] = {
                "directory": os.path.join(RESERVATION_DIR, directory) + "\\",
                "hotel_name": hotel_name,
                "pms_type": pms_type,
                "currency": cfg.get("currency"),
                "company_id_prod": cfg.get("company_id_prod"),
                "email_rules": cfg.get("email_rules", []),
                "block_import": cfg.get("block_import"),
                "rate_code_currencies": cfg.get("rate_code_currencies"),
            }
        return configs

    def get_email_mappings(self):
        """Extract email rules from hotel configs for EmailIntegration.save_attachment()"""
        mappings = []
        for hid, cfg in self.hotel_configs.items():
            for rule in cfg.get("email_rules", []):
                sender = rule.get("sender", "")
                subject = rule.get("subject", "")
                if not sender or not subject:
                    logger.warning(
                        "email_rules'ta sender/subject eksik, atlanıyor: hotel=%s sender=%r subject=%r",
                        hid, sender, subject,
                    )
                    continue
                mappings.append({
                    "mail": sender,
                    "subject": subject,
                    "reply_to_mail": rule.get("reply_to_mail"),
                    "file": cfg["directory"],
                    "file_name": rule.get("file_name"),
                })
        return mappings

    def get_royan_email_mappings(self):
        """Extract Royan email rules from hotel configs for save_royan_attachment()"""
        royan_cfg = self.hotel_configs.get("00504")
        if not royan_cfg:
            return pd.DataFrame()
        rows = []
        for rule in royan_cfg.get("email_rules", []):
            rows.append({
                "subject": rule["subject"],
                "mail": rule["sender"],
                "file": royan_cfg["directory"],
                "room_code": rule.get("royan_room_codes", []),
            })
        return pd.DataFrame(rows)

    def pick_columns(self, data, pms_type):
        """Process hotel data based on PMS type and standardize format"""
        # GROUP_NAME'i kolon seciminden once sakla (reset_index ile kaybolmasin)
        _gn = data['GROUP_NAME'].copy() if 'GROUP_NAME' in data.columns else None

        if pms_type == 'Opera':
            for col in ['CHANNEL', 'COMPANY_NAME', 'SOURCE_NAME']:
                if col not in data.columns:
                    data[col] = None

            if 'CURRENCY_CODE' in data.columns:
                data = data[["RESV_NAME_ID", "FULL_NAME", "RESV_STATUS", "TRUNC_ARRIVAL", "TRUNC_DEPARTURE", "REVENUE", "PERSON",
                    "NO_OF_ROOMS", "ROOM_CATEGORY", "RATE_CODE", "INSERT_DATE", "TRAVEL_AGENT_NAME", "NIGHTS", "COMP_HUSE", 'CURRENCY_CODE', 'MARKET_CODE', 'SOURCE_CODE',
                    'CHANNEL', 'COMPANY_NAME', 'SOURCE_NAME']]

                data.columns = ['booking_no', 'name', 'status', 'arrival_date', 'departure_date', 'total_amount',
                    'guests', 'rooms', 'room_code', 'RATE_CODE', 'booking_date', 'point_of_sale',
                    'night', 'COMP_HOUSE_YN', 'currency', 'segment', 'reference_source',
                    'market', 'company_name', 'source_name']
            else:
                data = data[["RESV_NAME_ID", "FULL_NAME", "RESV_STATUS", "TRUNC_ARRIVAL", "TRUNC_DEPARTURE", "REVENUE", "PERSON",
                    "NO_OF_ROOMS", "ROOM_CATEGORY", "RATE_CODE", "INSERT_DATE", "TRAVEL_AGENT_NAME", "NIGHTS", "COMP_HUSE", 'MARKET_CODE', 'SOURCE_CODE',
                    'CHANNEL', 'COMPANY_NAME', 'SOURCE_NAME']]

                data.columns = ['booking_no', 'name', 'status', 'arrival_date', 'departure_date', 'total_amount',
                    'guests', 'rooms', 'room_code', 'RATE_CODE', 'booking_date', 'point_of_sale',
                    'night', 'COMP_HOUSE_YN', 'segment', 'reference_source',
                    'market', 'company_name', 'source_name']
                data["currency"] = ""

        elif pms_type == 'Opera Cloud':
            # BUG FIX: FULL_NAME eklendi (name kolonu eksikti)
            if 'FULL_NAME' not in data.columns:
                data['FULL_NAME'] = None
            data = data[['RESV_NAME_ID', 'FULL_NAME',
                'RESV_STATUS', 'DEPARTURE',
                'PERSONS', 'NO_OF_ROOMS', 'ROOM_CATEGORY_LABEL',
                'RATE_CODE', 'INSERT_DATE', 'TRAVEL_AGENT_NAME', 'ARRIVAL', 'NIGHTS',
                'COMP_HOUSE_YN',
                'SHARE_AMOUNT_PER_STAY']].copy()
            data.columns = ['booking_no', 'name', 'status', 'departure_date', 'guests', 'rooms', 'room_code',
                'RATE_CODE', 'booking_date', 'point_of_sale',
                'arrival_date', 'night', 'COMP_HOUSE_YN', 'total_amount']
            data["currency"] = ""

        elif pms_type == "Ramada":
            data = data[["RESV_NAME_ID", "FULL_NAME", "RESV_STATUS", "ARRIVAL", "DEPARTURE", "SHARE_AMOUNT_PER_STAY", "PERSONS",
                "NO_OF_ROOMS", "C_T_S_NAME", "ROOM_CATEGORY_LABEL", "RATE_CODE", "INSERT_DATE", "TRAVEL_AGENT_NAME", "NIGHTS", "COMP_HOUSE_YN"]].copy()
            # BUG FIX: 'Source' -> 'point_of_sale' (dogru kolon ismi)
            data.columns = ['booking_no', 'name', 'status', 'arrival_date', 'departure_date', 'total_amount',
                'guests', 'rooms', 'C_T_S_NAME',
                'room_code', 'RATE_CODE', 'booking_date', 'point_of_sale',
                'night', 'COMP_HOUSE_YN']
            data["currency"] = ""

        # GROUP_NAME'i _group_name olarak ekle (index-safe: kolon secimi + reset_index ile birlikte tasir)
        if _gn is not None:
            data['_group_name'] = _gn.reindex(data.index).values

        return data

    def process_royan_room_mapping(self, data):
        """Apply room type mapping specific to Royan Hotel"""
        room_dict = {
            'BSTD': 'Standard Room',
            'BSTDXX': 'Standard Room - Royan',
            'PSUP': 'Superior Room',
            'PSUPSV': 'Standard Room - Sea View',
            'PSUPXX': 'Superior Room - Triple',
            'JUNSV': 'Junior Suite - Sea View & Hammam',
            'S1BSV': 'Suite sea view & jacuzzi',
            'PRSSV': 'Presidential Suite - Sea View & Hammam'
        }
        data['room_type'] = data['room_type'].str.replace('-', '').map(room_dict)
        data = data.dropna(subset=['room_type'])
        return data

    def _process_opera_family(self, data, pms_type):
        """Shared processing for Opera, Opera Cloud, and Ramada"""
        data = data.loc[data["rooms"] > 0].reset_index(drop=True)
        data = data[data['booking_no'].notna()]

        if 'currency' in data.columns:
            data.loc[data['currency'] == 'TRL', 'currency'] = 'TRY'

        data['booking_no'] = data['booking_no'].astype(int)

        data["special_offer"] = None
        data.loc[data["COMP_HOUSE_YN"] == "C", "special_offer"] = "Complimentary"
        data.loc[data["COMP_HOUSE_YN"] == "C", "RATE_CODE"] = "Complimentary"
        data.loc[data["COMP_HOUSE_YN"] == "H", "special_offer"] = "House Use"

        return data

    def _process_opera_dates(self, data):
        """Date processing for Opera PMS type"""
        for col in ['booking_date', 'arrival_date', 'departure_date']:
            data[col] = pd.to_datetime(data[col], errors='coerce')
        bad = data[['booking_date', 'arrival_date', 'departure_date']].isna().any(axis=1)
        if bad.any():
            logger.warning("%d kayit gecersiz tarih nedeniyle atlanıyor.", bad.sum())
            data = data[~bad].reset_index(drop=True)
        data.loc[data['status'] != 'cancel', 'status'] = 'ACTIVE'
        return data

    def _process_opera_cloud_dates(self, data):
        """Date processing for Opera Cloud PMS type"""
        data.loc[data['status'] != 'CANCELLED', 'status'] = 'ACTIVE'
        data.loc[data['status'] == 'CANCELLED', 'status'] = 'CANCELED'
        for col in ['booking_date', 'arrival_date', 'departure_date']:
            data[col] = pd.to_datetime(data[col], dayfirst=True)
        return data

    def _process_ramada_dates(self, data):
        """Date processing for Ramada PMS type"""
        data.loc[data['status'] != 'CANCELLED', 'status'] = 'ACTIVE'
        data.loc[data['status'] == 'CANCELLED', 'status'] = 'CANCELED'
        for col in ['booking_date', 'arrival_date', 'departure_date']:
            data[col] = data[col].apply(lambda x: pd.to_datetime(x, format='%d.%m.%y', dayfirst=True))
        data['booking_no'] = data['booking_no'].astype(int)
        # BUG FIX: 'Source' -> 'point_of_sale' (pick_columns ile uyumlu)
        data["point_of_sale"] = data["point_of_sale"].fillna(data["C_T_S_NAME"])
        return data

    def processing_data(self, data, lookup, pms_type, hotel_id, hotel_name, config_currency=None, company_id=None):
        data = self.pick_columns(data, pms_type=pms_type)
        process = HotelDataAnalyzeClass()

        if pms_type in ("Opera", "Opera Cloud", "Ramada"):
            data = self._process_opera_family(data, pms_type)

            if pms_type == 'Opera':
                data = self._process_opera_dates(data)
            elif pms_type == 'Opera Cloud':
                data = self._process_opera_cloud_dates(data)
            elif pms_type == 'Ramada':
                data = self._process_ramada_dates(data)

            data['night'] = (pd.to_datetime(data['departure_date']) - pd.to_datetime(data['arrival_date'])).dt.days
            data.loc[data['departure_date'] == data['arrival_date'], 'night'] = 1
            # Opera Cloud: rate_code_currencies varsa currencyCalculate (basamak sayısı) devre dışı kalır.
            if pms_type == 'Opera Cloud' and 'RATE_CODE' in data.columns:
                _rc_currencies = (self.hotel_configs.get(hotel_id, {}).get('rate_code_currencies')) or {}
                if _rc_currencies:
                    data['currency'] = config_currency or 'EUR'
                    for _cur, _codes in _rc_currencies.items():
                        _mask = data['RATE_CODE'].isin(_codes)
                        data.loc[_mask, 'currency'] = _cur
                        if _mask.any():
                            logger.info("Opera Cloud: %d rezervasyon rate_code ile %s olarak belirlendi.", _mask.sum(), _cur)
                else:
                    data = process.currencyCalculate(data)
            else:
                data = process.currencyCalculate(data)

            data = process.convert_to_hotel_currency(data, company_id=company_id or hotel_id, fallback_currency=config_currency)
            data = process.roomTypeMatch(data, lookup)

            # create_master_data reindex ile gereksiz kolonlari otomatik temizler

            # RATE_CODE → rate_code (SWAP_LIST reindex icin)
            # Comp/HouseUse zaten _process_opera_family'de RATE_CODE'a yazildi
            # rename ile dondur: yeni kolon + eski kolon = create_master_data.columns.lower() ile duplicate olusur
            if 'RATE_CODE' in data.columns:
                data = data.rename(columns={'RATE_CODE': 'rate_code'})

            # GROUP_NAME'i create_master_data oncesi sakla (reindex ile dusecek)
            _gn_safe = data['_group_name'].copy() if '_group_name' in data.columns else None

            data = process.create_master_data(data, hotel_id, hotel_name)

            # GROUP_NAME → accommodation_property (block pickup eslestirmesi icin)
            if _gn_safe is not None:
                data["accommodation_property"] = _gn_safe.values

            if not process.getUnmatchedRooms().empty:
                self.unmatch = [process.getUnmatchedRooms(), hotel_name]
            else:
                self.unmatch = ""

        elif pms_type == "Lasagrada":
            data["ARRIVAL_DATE"] = pd.to_datetime(data["ARRIVAL_DATE"]).dt.normalize()
            data["DEPARTURE_DATE"] = pd.to_datetime(data["DEPARTURE_DATE"]).dt.normalize()
            data["BOOKING_DATE"] = pd.to_datetime(data["BOOKING_DATE"]).dt.normalize()
            data["TOTAL_AMOUNT"] = data["TOTAL_AMOUNT"].astype(float)
            data.loc[data['STATUS'] != 'CANCELLED', 'STATUS'] = 'ACTIVE'

            data = data.loc[data["ROOMS"] > 0].reset_index(drop=True)
            data = data.loc[data["TOTAL_AMOUNT"] >= 0].reset_index(drop=True)
            data = data[data['BOOKING_NO'].notna()]

            # Vectorized special_offer
            data.loc[data["MARKET_CODE"] == "COMP", "SPECIAL_OFFER"] = "Complimentary"

            # BUG FIX: Room type matching via pd.merge (O(n*m) loop yerine)
            data = data.rename(columns={"ROOM_TYPE": "room_code"})
            lookup_copy = lookup.copy()
            lookup_copy['room_code'] = lookup_copy['room_code'].str.strip().str.lower()
            data['room_code'] = data['room_code'].str.strip().str.lower()
            data = pd.merge(data, lookup_copy[['room_code', 'room_type']].drop_duplicates(), on='room_code', how='left')
            data['room_type'] = data['room_type'].fillna("Not specified")
            data.drop('room_code', axis=1, inplace=True)
            data = data.rename(columns={'room_type': 'Room Type'})

            # Vectorized price calculation
            data["TOTAL_AMOUNT"] = data["TOTAL_AMOUNT"] * data["ROOMS"]

            data.drop(["COUNTRY", "REFERENCE_SOURCE"], axis=1, inplace=True)
            data = data.rename(columns={
                "GUEST_NAME": "name",
                "NATIONALITY": "country",
                "MARKET_CODE": "reference_source"
            })
            data["currency"] = "EUR"
            data = process.create_master_data(data, hotel_id, hotel_name)

        elif pms_type == "Royan":
            data = data[[
                "Reservation Nº", "Room", "Room Rev.", "Crea. Date",
                "Arrival Date", "RN", "Departure", "Main client",
                "Room Type", "Board", "In-House", "Segment",
            ]]

            data["hotel_id"] = hotel_id
            data["name"] = hotel_name
            data["accommodation_property"] = hotel_name
            data["required_prepayment"] = None
            data["repaid_amount"] = None
            data["payment_method"] = None
            data["cancellation_date"] = None
            data["customer"] = None
            data["gender"] = None
            data["country"] = None
            data["special_offer"] = None
            data["rate_code"] = None
            data["discounts"] = None
            data["status"] = "Active"

            data = data.rename(columns={
                "Reservation Nº": "booking_no",
                "Room": "rooms",
                "Room Rev.": "total_amount",
                "Crea. Date": "booking_date",
                "Arrival Date": "arrival_date",
                "RN": "night",
                "Departure": "departure_date",
                "Main client": "point_of_sale",
                "Room Type": "room_type",
                "Board": "extra_services",
                "In-House": "guests",
                "Segment": "reference_source",
            })

            data = self.process_royan_room_mapping(data)

            data["currency"] = "EUR"
            data = data.reindex(columns=DataAnalyzer.SWAP_LIST)

            for col in ["booking_date", "arrival_date", "departure_date"]:
                data[col] = data[col].ffill()

            data = data[data['total_amount'] > 0]
            data = data[data['rooms'] > 0]
            data = data[data['guests'] > 0]

        return data

    def process_cancel_data(self):
        logger.info("Processing cancel data...")
        self.data_cancel["hotel_id"] = self.hotel_id

        if "RESV_NAME_ID" in self.data_cancel.columns:
            df_cancel = self.data_cancel[["hotel_id", "RESV_NAME_ID"]]
        elif "RESV_NAME_ID2" in self.data_cancel.columns:
            df_cancel = self.data_cancel[["hotel_id", "RESV_NAME_ID2"]]
        else:
            logger.error("Cancel data'da RESV_NAME_ID kolonu bulunamadi.")
            return

        df_cancel.columns = ['hotel_id', 'booking_no']
        my_instance = HotelDataAnalyzeClass()
        data_ek_cancel = my_instance.resultEkstraCancel(df_cancel, self.hotel_id)
        api_client.delete_dataframe(data_ek_cancel)
        logger.info("Cancel data processing completed.")

    def get_royan_filename(self):
        """Generate the expected Royan Hotel filename based on current date"""
        now = datetime.today()
        return f"ROYAN HOTEL {now.strftime('%d.%m')} RES. ACT. REPORT.xlsx"

    def _process_and_log(self, data_format, is_cancel=False, integration_type="OPERA_RESERVATION"):
        """Common try/except + log_record wrapper"""
        try:
            self.start_time = datetime.now()
            if is_cancel:
                self.process_cancel_data()
            else:
                api_client.upload_dataframe(data_format)
            logger.info("Reservation Verilerini Yazdirma Islemi Basariyla Tamamlanmistir.")
            self.error_message = str(self.unmatch) if not is_cancel else ""
            self.end_time = datetime.now()
            self.status = "SUCCESS"
        except Exception as e:
            self.status = "FAILURE"
            self.error_message = str(e)
            self.end_time = datetime.now()
            logger.error("Islem hatasi: %s", e)

        log_record.save_log(
            data_format,
            integration_type=integration_type,
            company_id=self.hotel_id,
            status=self.status,
            error_message=self.error_message,
            duration_ms=round((self.end_time - self.start_time).total_seconds() * 1000, 3),
            start_time=self.start_time,
            end_time=self.end_time,
        )

    def process_hotels(self, hotel_ids=None, skip_date_check=False):
        """Process all hotels in the configuration (or only specified hotel_ids)"""
        for hid, config in self.hotel_configs.items():
            if hotel_ids and hid not in hotel_ids:
                continue
            directory = config["directory"]
            self.hotel_name = config["hotel_name"]
            self.pms_type = config.get("pms_type")
            self.hotel_id = hid

            if not os.path.exists(directory):
                logger.warning("Directory does not exist: %s", directory)
                continue

            if self.pms_type == "Royan":
                self._process_royan_hotel(directory)
            else:
                self._process_standard_hotel(directory, skip_date_check=skip_date_check)

    def _process_royan_hotel(self, directory):
        """Handle Royan hotel processing"""
        expected_filename = self.get_royan_filename()
        fpath = os.path.join(directory, expected_filename)

        if not os.path.exists(fpath):
            logger.info("Royan file not found: %s", fpath)
            return

        logger.info("Hotel ID: %s | Name: %s | PMS: %s | File: %s",
                     self.hotel_id, self.hotel_name, self.pms_type, expected_filename)
        try:
            self.lookup = FetchLookup().fetch_data(self.hotel_id)
            data = pd.read_excel(fpath)
            config = self.hotel_configs.get(self.hotel_id, {})
            data_format = self.processing_data(
                data, lookup=self.lookup, pms_type=self.pms_type,
                hotel_id=self.hotel_id, hotel_name=self.hotel_name,
                config_currency=config.get("currency"),
                company_id=config.get("company_id_prod"),
            )
            self._process_and_log(data_format)
        except Exception as e:
            logger.error("Error processing Royan file %s: %s", fpath, e)

    def _convert_xml_if_needed(self, fpath):
        """XML/TXT dosyasini Excel'e donustur, xlsx yolunu dondur."""
        fpath_lower = fpath.lower()
        if not (fpath_lower.endswith('.xml') or fpath_lower.endswith('.txt')):
            return fpath

        # Guard: P1432 dosyasi buraya gelmemeli — ayri akista islenir
        if self._is_p1432_file(os.path.basename(fpath)):
            raise ValueError(f"P1432 dosyasi standart akisa girmemeli: {os.path.basename(fpath)}")

        email_util = EmailIntegration.__new__(EmailIntegration)
        xlsx_path = os.path.splitext(fpath)[0] + '.xlsx'
        fname_lower = os.path.basename(fpath).lower()

        if fpath_lower.endswith('.txt'):
            email_util.parse_txt_to_excel(fpath, xlsx_path)
        elif 'can' in fname_lower:
            email_util.parse_cancel_report(fpath).to_excel(xlsx_path, index=False)
        elif 'indgrp' in fname_lower:
            email_util.parse_xml_to_excel(fpath, xlsx_path)
        elif 'resenteredon' in fname_lower:
            email_util.parse_resenteredon_xml_to_excel(fpath, xlsx_path)
        else:
            email_util.convert_xml_to_dataframe(fpath).to_excel(xlsx_path, index=False)

        os.remove(fpath)
        logger.info("-> Excel donusturuldu: %s", os.path.basename(xlsx_path))
        return xlsx_path

    def _is_p1432_file(self, fname):
        """P1432 blok raporu dosyasi mi?"""
        fname_lower = fname.lower()
        return 'p1432' in fname_lower or 'blockenteredonby' in fname_lower

    def _is_grppickup_file(self, fname):
        """grppickup (Group Pickup) raporu dosyasi mi?"""
        return 'grppickup' in fname.lower()

    def _process_grppickup_file(self, fpath, config):
        """grppickup dosyasini isle ve upload et.

        grppickup raporu (GRP6.FMX) her blok icin gun bazli AVAIL verir.
        P1432 ile ayni booking_no (BLK-{HEADER_ID}) kullanarak room_count gunceller.
        """
        block_cfg = config.get("block_import")
        if not block_cfg:
            logger.warning("grppickup dosyasi bulundu ama block_import config yok: %s", self.hotel_id)
            return

        # Guard: XML root tag dogrulamasi
        if fpath.lower().endswith('.xml'):
            import xml.etree.ElementTree as ET
            try:
                root_tag = ET.parse(fpath).getroot().tag.upper()
                if root_tag != 'GRPPICKUP':
                    logger.error("grppickup root tag uyusmazligi: %s (root=%s). Dosya atlaniyor.",
                                os.path.basename(fpath), root_tag)
                    return
            except ET.ParseError as e:
                logger.error("grppickup XML parse hatasi: %s — %s", os.path.basename(fpath), e)
                return

        logger.info("grppickup Import basliyor: %s | Hotel: %s", os.path.basename(fpath), self.hotel_name)

        # 1. Parse
        rows = parse_grppickup_xml(fpath)
        if not rows:
            logger.info("grppickup: Veri olan blok bulunamadi.")
            return

        # 2. Blok bazli ozet
        block_summaries = aggregate_by_block(rows)

        # 3. DB lookup (P1432 ile ayni)
        lookups = fetch_block_lookups(int(self.hotel_id))

        # 4. P1432 metadata cek (booking_date, company_name, cf_rate)
        from grp_pickup_import import fetch_blk_metadata
        blk_metadata = fetch_blk_metadata(int(self.hotel_id))

        # 5. DataFrame olustur
        df = grppickup_to_dataframe(
            block_summaries,
            hotel_id=self.hotel_id,
            hotel_name=self.hotel_name,
            currency=block_cfg.get("currency", "EUR"),
            default_market=block_cfg.get("default_market"),
            default_segment=block_cfg.get("default_segment"),
            lookups=lookups,
            blk_metadata=blk_metadata,
        )

        if df.empty:
            logger.info("grppickup: DataFrame bos, yukleme yapilmadi.")
            return

        # 5. Upload
        self._process_and_log(df, integration_type="GRPPICKUP_IMPORT")
        active = df[df['status'] == 'ACTIVE']
        logger.info("grppickup Import tamamlandi: %d blok (%d ACTIVE, %d oda AVAIL)",
                     len(df), len(active), int(active['rooms'].sum()) if not active.empty else 0)

    def _process_p1432_file(self, fpath, config):
        """P1432 blok dosyasini isle ve upload et (XML veya TXT)."""
        block_cfg = config.get("block_import")
        if not block_cfg:
            logger.warning("P1432 dosyasi bulundu ama block_import config yok: %s", self.hotel_id)
            return

        # Guard: XML dosyalari icin root tag dogrulamasi
        if fpath.lower().endswith('.xml'):
            import xml.etree.ElementTree as ET
            try:
                root_tag = ET.parse(fpath).getroot().tag.upper()
                if 'P1432' not in root_tag and 'BLOCKENTEREDONBY' not in root_tag:
                    logger.error("P1432 root tag uyusmazligi: %s (root=%s). Dosya atlaniyor.",
                                os.path.basename(fpath), root_tag)
                    return
            except ET.ParseError as e:
                logger.error("P1432 XML parse hatasi: %s — %s", os.path.basename(fpath), e)
                return

        logger.info("P1432 Block Import basliyor: %s | Hotel: %s", os.path.basename(fpath), self.hotel_name)

        # 1. Parse & filter (XML veya TXT otomatik algilanir)
        blocks = parse_p1432(fpath)
        active_blocks, cancelled_blocks = filter_active_blocks(blocks)

        all_import_blocks = active_blocks + cancelled_blocks

        if not all_import_blocks:
            logger.info("P1432: Import edilecek blok bulunamadi.")
            return

        # 2. DB'den dinamik lookup (room_type, market, segment)
        lookups = fetch_block_lookups(int(self.hotel_id))

        # 3. DataFrame olustur — aktif + iptal bloklar birlikte (status ayrı)
        df = blocks_to_dataframe(
            all_import_blocks,
            hotel_id=self.hotel_id,
            hotel_name=self.hotel_name,
            master_room=block_cfg.get("master_room", "DKNG"),
            currency=block_cfg.get("currency", "EUR"),
            market=block_cfg.get("default_market"),
            segment=block_cfg.get("default_segment"),
            lookups=lookups,
        )

        if df.empty:
            logger.info("P1432: DataFrame bos, yukleme yapilmadi.")
            return

        # 3. Upload
        self._process_and_log(df, integration_type="P1432_BLOCK_IMPORT")
        logger.info("P1432 Block Import tamamlandi: %d blok, %d oda",
                     len(df), int(df['rooms'].sum()))

    @staticmethod
    def _file_priority(fname):
        """Dosya isleme onceligi: P1432 > grppickup > P2617 > cancel."""
        fname_lower = fname.lower()
        if 'p1432' in fname_lower or 'blockenteredonby' in fname_lower:
            return 0  # Ilk: blok olustur
        elif 'grppickup' in fname_lower:
            return 1  # Ikinci: room_count guncelle
        elif 'can' in fname_lower:
            return 3  # Son: iptal
        else:
            return 2  # Ortada: P2617 (IND)

    def _process_standard_hotel(self, directory, skip_date_check=False):
        """Handle standard hotel processing (Opera, Opera Cloud, Ramada, Lasagrada)"""
        config = self.hotel_configs.get(self.hotel_id, {})
        processed_bases = set()

        # Paylasilan dizinlerde hangi dosyalarin bu otele ait oldugunu belirle
        email_rules = config.get("email_rules", [])
        block_import = config.get("block_import")
        allowed_patterns, cancel_patterns = [], []
        for r in email_rules:
            if r.get("file_name"):
                fn = r["file_name"].lower()
                allowed_patterns.append(fn)
                if "cancel" in r.get("subject", "").lower():
                    cancel_patterns.append(fn)
        # P1432 ve grppickup dosyalari her zaman izinli (block_import aktifse)
        if block_import:
            allowed_patterns.extend(["p1432", "grppickup"])

        self.lookup = FetchLookup().fetch_data(self.hotel_id)

        # Dosyalari oncelik sirasina gore sirala: P1432 > grppickup > P2617 > cancel
        all_files = sorted(os.listdir(directory), key=self._file_priority)

        for fname in all_files:
            fpath = os.path.join(directory, fname)
            if not os.path.isfile(fpath):
                continue

            # Ayni dosyanin hem txt/xml hem xlsx halini islememek icin
            base = os.path.splitext(fname)[0].lower()
            if base in processed_bases:
                continue

            # Paylasilan dizinlerde sadece bu otele ait dosyalari isle
            if allowed_patterns and not any(pat in base for pat in allowed_patterns):
                logger.debug("Atlanıyor (baska otele ait): %s", fname)
                continue

            if not skip_date_check:
                modified_date = datetime.fromtimestamp(os.path.getmtime(fpath)).date()
                if modified_date != self.today:
                    continue

            processed_bases.add(base)

            logger.info("Hotel ID: %s | Name: %s | PMS: %s",
                         self.hotel_id, self.hotel_name, self.pms_type)

            try:
                # P1432 blok raporu ayri akista isle
                if self._is_p1432_file(fname):
                    self._process_p1432_file(fpath, config)
                    if self.status == "SUCCESS" and os.path.exists(fpath):
                        os.remove(fpath)
                        logger.info("P1432 dosyasi silindi: %s", fname)
                    continue

                # grppickup raporu ayri akista isle
                if self._is_grppickup_file(fname):
                    self._process_grppickup_file(fpath, config)
                    if self.status == "SUCCESS" and os.path.exists(fpath):
                        os.remove(fpath)
                        logger.info("grppickup dosyasi silindi: %s", fname)
                    continue

                fpath = self._convert_xml_if_needed(fpath)

                is_cancel = ('can' in base) or (cancel_patterns and any(pat in base for pat in cancel_patterns))
                if not is_cancel:
                    data = pd.read_excel(fpath)
                    data_format = self.processing_data(
                        data, lookup=self.lookup, pms_type=self.pms_type,
                        hotel_id=self.hotel_id, hotel_name=self.hotel_name,
                        config_currency=config.get("currency"),
                        company_id=config.get("company_id_prod"),
                    )
                    self._process_and_log(data_format)
                else:
                    self.data_cancel = pd.read_excel(fpath)
                    self._process_and_log(self.data_cancel, is_cancel=True)

                if self.status == "SUCCESS" and os.path.exists(fpath):
                    os.remove(fpath)
                    logger.info("Islenen dosya silindi: %s", os.path.basename(fpath))

            except Exception as e:
                logger.error("Error processing %s: %s", fpath, e)


if __name__ == "__main__":
    hotel_updater = HotelUpdate()
    hotel_updater.process_hotels()
