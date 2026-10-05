import logging
import os

import numpy as np
import pandas as pd
from pandas import Timestamp

from hotel_analysis.config import DAILY_UPLOAD_DIR

logger = logging.getLogger(__name__)


class DataAnalyzer:
    """Base class for hotel data analysis with common business logic."""

    dataTitle = None

    # Default column order for master data (must match Java fromExcelToDto column indices)
    SWAP_LIST = [
        'hotel_id', 'booking_no', 'name', 'rooms', 'total_amount',
        'required_prepayment', 'repaid_amount', 'payment_method',
        'booking_date', 'cancellation_date', 'arrival_date', 'night',
        'departure_date', 'point_of_sale', 'reference_source', 'status',
        'original_status',
        'accommodation_property', 'room_type',
        'price_room_type',
        'extra_services',
        'customer', 'gender', 'country', 'guests',
        'special_offer', 'rate_code', 'discounts', 'currency',
        'original_total_price', 'original_currency',
        'tax_amount', 'original_tax_amount', 'original_base_amount',
        'market', 'segment', 'company_name', 'source_name', 'share_number'
    ]

    # Status filters (default: reservation_code behavior)
    RESERVATION_EXCLUDE_STATUSES = ["Cancelled", "Lost/Declined"]
    CANCEL_INCLUDE_STATUSES = ["Cancelled", "Lost/Declined"]

    def __init__(self):
        self.unmatch = pd.Series(dtype=str)

    def currencyCalculate(self, data):
        """Vectorized currency detection based on digit count."""
        mask = data["currency"] == ""
        if mask.any():
            digit_counts = data.loc[mask, "total_amount"].abs().astype(int).astype(str).str.len()
            data.loc[mask, "currency"] = np.where(digit_counts <= 3, "EUR", "TRY")
        return data

    def roomTypeMatch(self, data, lookup):
        """Match room codes to room types via exact match + slash fallback."""
        data['room_code'] = data['room_code'].str.strip().str.lower()
        lookup['room_code'] = lookup['room_code'].str.strip().str.lower()
        lookup = lookup[lookup['room_code'].notna()].copy()

        # Step 1: Exact match via left merge
        data = pd.merge(
            data,
            lookup[['room_code', 'room_type']],
            on='room_code',
            how='left'
        )

        # Step 2: Slash-separated fallback for unmatched (dict-based O(n+m))
        unmatched_mask = data['room_type'].isna()
        if unmatched_mask.any():
            lookup_with_slash = lookup[lookup['room_code'].str.contains('/', na=False)].copy()
            if not lookup_with_slash.empty:
                slash_mapping = {}
                for _, row in lookup_with_slash.iterrows():
                    for code in row['room_code'].split('/'):
                        slash_mapping[code.strip()] = row['room_type']
                data.loc[unmatched_mask, 'room_type'] = data.loc[unmatched_mask, 'room_code'].map(slash_mapping)

        nsRoomType = data['room_type'].isna().sum()
        if nsRoomType != 0:
            logger.warning("Eslesmayan Oda Tipi Adedi: %d", nsRoomType)
            logger.warning("%s", data.loc[data['room_type'].isna(), 'room_code'].value_counts())
        else:
            logger.info("Eslesmayan Oda Tipi Yoktur.")

        self.unmatch = data.loc[data['room_type'].isna(), 'room_code']

        data.drop('room_code', axis=1, inplace=True)
        data = data[data['room_type'].notna()].copy()
        return data

    def getUnmatchedRooms(self):
        return self.unmatch

    def create_master_data(self, dataframe_param, hotel_id, hotel_name):
        """Standardize column names, reorder, and write reservation/cancel files."""
        dataframe_param.columns = [x.lower() for x in dataframe_param.columns]
        dataframe_param.columns = dataframe_param.columns.str.replace("[ ]", "_", regex=True)

        if dataframe_param.empty:
            logger.warning("create_master_data: DataFrame bos (%s). Oda tipi eslesmesi yok — lookup tanimli mi?", hotel_name)
            return dataframe_param

        if dataframe_param["booking_date"].value_counts().count() > 1:
            title = hotel_name + "  Master Data"
        else:
            dataDate = Timestamp(dataframe_param.iloc[0]["booking_date"])
            date_only = dataDate.strftime('%Y-%m-%d')
            title = hotel_name + " " + str(date_only)

        DataAnalyzer.dataTitle = title

        dataframe_param["hotel_id"] = hotel_id
        dataframe_param["accommodation_property"] = hotel_name

        dataframe_param = dataframe_param.reindex(columns=self.SWAP_LIST)

        self.resultReservation(dataframe_param, title)
        self.resultCancel(dataframe_param, title)

        return dataframe_param

    def resultReservation(self, dataframe_param, title):
        """Filter active reservations and write to Excel."""
        mask = ~dataframe_param["status"].isin(self.RESERVATION_EXCLUDE_STATUSES)
        dataBooking = dataframe_param.loc[mask].reset_index(drop=True)

        dataBooking["cancellation_date"] = np.nan
        dataBooking["name"] = np.nan

        dosya_adi_rez = f"{title} Reservation Database Update.xlsx"
        dosya_yolu_rez = os.path.join(DAILY_UPLOAD_DIR, dosya_adi_rez)

        dataBooking.to_excel(dosya_yolu_rez, index=True)
        logger.info("Reservation verileri yazildi: %s", dosya_adi_rez)

    def resultCancel(self, dataframe_param, title):
        """Filter cancelled reservations and write to Excel."""
        mask = dataframe_param["status"].isin(self.CANCEL_INCLUDE_STATUSES)
        dataCancel = dataframe_param.loc[mask].reset_index(drop=True)

        dcData = dataCancel[["hotel_id", "booking_no"]]

        dosya_adi_cancel = f"{title} Cancel Database Update.xlsx"
        dosya_yolu_cancel = os.path.join(DAILY_UPLOAD_DIR, dosya_adi_cancel)

        dcData.to_excel(dosya_yolu_cancel, index=True)
        logger.info("Cancel verileri yazildi: %s", dosya_adi_cancel)

    def resultEkstraCancel(self, dataEkCancel, hotel_id):
        """Process extra cancel data."""
        dataEkCancel["hotel_id"] = hotel_id
        dataEkCancel = dataEkCancel[["hotel_id", "booking_no"]]
        return dataEkCancel
