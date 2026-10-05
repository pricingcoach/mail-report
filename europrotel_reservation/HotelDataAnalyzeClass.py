import logging
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from pandas import Timestamp

from hotel_analysis.data_analyzer import DataAnalyzer
from hotel_analysis.config import DAILY_UPLOAD_DIR
from hotel_analysis.currency_exchange import HotelCurrencyConverter

logger = logging.getLogger(__name__)


class HotelDataAnalyzeClass(DataAnalyzer):
    """Europrotel data analyzer extending shared DataAnalyzer.

    Overrides: create_master_data (title-case status),
               resultReservation (exclude Tentative, No Show -> Posting Master),
               resultCancel (Tentative + Lost/Declined),
               resultEkstraCancel (writes to file)
    Adds: currencyCheck/currencyConvert (API-based, VPN-independent)
    Inherits: currencyCalculate, roomTypeMatch, getUnmatchedRooms
    """

    RESERVATION_EXCLUDE_STATUSES = ["Tentative"]
    CANCEL_INCLUDE_STATUSES = ["Tentative", "Lost/Declined"]

    def __init__(self):
        super().__init__()
        self._currency_converter = HotelCurrencyConverter()

    def get_currency_rates(self, start_date, end_date):
        """Fetch exchange rates via booking-api-py (VPN-independent)."""
        return self._currency_converter.get_currency_rates(start_date, end_date)

    def currencyConvert(self, data, start_date, end_date, year):
        """Currency conversion with fallback to previous date."""
        data = data.reset_index(drop=True)

        currency_rates = self.get_currency_rates(start_date, end_date)
        if currency_rates.empty:
            logger.warning("Kur bilgileri bulunamadi.")
            return data

        yesterday = pd.Timestamp(datetime.today() - timedelta(days=1)).normalize()

        for i in data.loc[data['year'] == year].index:
            booking_date = pd.to_datetime(data.at[i, "booking_date"]).normalize()
            curr = data.at[i, "currency"]
            total_price = data.at[i, "total_amount"]

            if curr == "EUR":
                data.at[i, "Price_EUR"] = total_price
                continue

            # Future date cap
            if booking_date > yesterday:
                booking_date = yesterday

            # Find exchange rate for this date and currency
            rate_match = currency_rates.loc[
                (currency_rates["Date"] == booking_date) & (currency_rates["symbol"] == curr)
            ]

            if rate_match.empty:
                # Fallback: find the most recent previous rate
                prev_rates = currency_rates.loc[
                    (currency_rates["Date"] < booking_date) & (currency_rates["symbol"] == curr)
                ].sort_values(by="Date", ascending=False)

                if not prev_rates.empty:
                    exchange_rate = prev_rates.iloc[0]['rate']
                else:
                    logger.warning("Satir %d: %s icin kur bulunamadi, atlaniyor.", i, curr)
                    continue
            else:
                exchange_rate = rate_match["rate"].values[0]

            try:
                data.at[i, "Price_EUR"] = total_price / exchange_rate
            except (ZeroDivisionError, TypeError) as e:
                logger.warning("Kur donusumu hatasi satir %d: %s", i, e)

        return data

    def currencyCheck(self, data):
        """Check and convert currencies to EUR using internal Europrotel DB."""
        non_eur = data.loc[data["currency"] != "EUR"]
        if non_eur.empty:
            logger.info("Tum veriler EUR.")
            return data

        logger.info("Farkli kurdan veriler mevcut. Kur donusumu yapilacak.")

        timestamp_start = Timestamp(non_eur["booking_date"].min())
        timestamp_end = Timestamp(non_eur["booking_date"].max())

        start_year = timestamp_start.year
        end_year = timestamp_end.year

        data["Price_EUR"] = None
        data['year'] = data['booking_date'].dt.year

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        for year in range(start_year, end_year + 1):
            first_date = datetime(year, 1, 1).strftime('%Y-%m-%d')
            last_date = (datetime(year + 1, 1, 1) - timedelta(days=1)).strftime('%Y-%m-%d')

            if last_date > yesterday:
                last_date = yesterday
            if first_date > yesterday:
                first_date = yesterday

            data = self.currencyConvert(data, first_date, last_date, year)
            logger.info("Kur donusumu %d senesi icin yapildi.", year)

        data = data.drop('year', axis=1)

        # Warn about unconverted rows
        unconverted = data[(data["currency"] != "EUR") & data["Price_EUR"].isna()]
        if not unconverted.empty:
            logger.warning("%d satir icin kur donusumu yapilamadi.", len(unconverted))

        data = data.drop("total_amount", axis=1)
        data = data.rename(columns={"Price_EUR": "total_amount"})
        data['total_amount'] = data['total_amount'].fillna(0)
        data['currency'] = "EUR"
        return data

    def create_master_data(self, dataframe_param, hotel_id, hotel_name):
        """Override: adds status title-case conversion for Europrotel."""
        dataframe_param.columns = [x.lower() for x in dataframe_param.columns]
        dataframe_param.columns = dataframe_param.columns.str.replace("[ ]", "_", regex=True)

        if dataframe_param["booking_date"].value_counts().count() > 1:
            title = hotel_name + "  Master Data"
        else:
            dataDate = Timestamp(dataframe_param.iloc[0]["booking_date"])
            date_only = dataDate.strftime('%Y-%m-%d')
            title = hotel_name + " " + str(date_only)

        HotelDataAnalyzeClass.dataTitle = title

        dataframe_param["hotel_id"] = hotel_id
        dataframe_param["accommodation_property"] = hotel_name

        dataframe_param = dataframe_param.reindex(columns=self.SWAP_LIST)

        # Europrotel-specific: convert status to title case
        dataframe_param['status'] = dataframe_param['status'].str.title()

        self.resultReservation(dataframe_param, title)
        self.resultCancel(dataframe_param, title)

        return dataframe_param

    def resultReservation(self, dataframe_param, title):
        """Override: exclude Tentative, map No Show -> Posting Master."""
        mask = ~dataframe_param["status"].isin(self.RESERVATION_EXCLUDE_STATUSES)
        dataBooking = dataframe_param.loc[mask].reset_index(drop=True)

        # Europrotel-specific: No Show gets Posting Master room type
        dataBooking.loc[dataBooking["status"] == "No Show", "room_type"] = "Posting Master"

        dataBooking["cancellation_date"] = np.nan
        dataBooking["name"] = np.nan

        dosya_adi_rez = f"{title} Reservation Database Update.xlsx"
        dosya_yolu_rez = os.path.join(DAILY_UPLOAD_DIR, dosya_adi_rez)

        dataBooking.to_excel(dosya_yolu_rez, index=True)
        logger.info("Reservation verileri yazildi: %s", dosya_adi_rez)

    def resultEkstraCancel(self, dataEkCancel, hotel_id, hotel_name=None):
        """Override: writes extra cancel file to disk."""
        dataEkCancel["hotel_id"] = hotel_id
        dataEkCancel = dataEkCancel[["hotel_id", "booking_no"]]

        if hotel_name:
            dosya_adi_ek_cancel = f"{hotel_name} Cancel - Ek Database Update Ek Cancel.xlsx"
            dosya_yolu_ek = os.path.join(DAILY_UPLOAD_DIR, dosya_adi_ek_cancel)
            dataEkCancel.to_excel(dosya_yolu_ek, index=True)
            logger.info("Ek Cancel verileri yazildi: %s", dosya_adi_ek_cancel)

        return dataEkCancel
