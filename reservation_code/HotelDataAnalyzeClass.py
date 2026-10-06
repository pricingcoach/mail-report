import logging
import sys
import os
from datetime import datetime, timedelta

import pandas as pd
from pandas import Timestamp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hotel_analysis.currency_exchange import HotelCurrencyConverter
from hotel_analysis.data_analyzer import DataAnalyzer

logger = logging.getLogger(__name__)


class HotelDataAnalyzeClass(DataAnalyzer):
    """Reservation code data analyzer extending shared DataAnalyzer.

    Inherits: currencyCalculate, roomTypeMatch, getUnmatchedRooms,
              create_master_data, resultReservation, resultCancel, resultEkstraCancel
    Adds: currency conversion via booking-api-py (booking_date based, VPN-tolerant)
    """

    def __init__(self):
        super().__init__()
        self._converter = HotelCurrencyConverter()

    def get_hotel_currency(self, hotel_id=None, company_id=None):
        """Otelin para birimini booking-api-py API'sinden çeker."""
        try:
            return self._converter.get_hotel_currency(hotel_id=hotel_id, company_id=company_id)
        except Exception as e:
            logger.error("Hotel currency bilgisi alinamadi: %s", e)
            return None

    def get_currency_rates(self, start_date, end_date):
        """Döviz kurlarını booking-api-py API'sinden çeker.
        Dönen DataFrame kolonları: Date (datetime), rate (float), symbol (str)
        """
        try:
            return self._converter.get_currency_rates(start_date, end_date)
        except Exception as e:
            logger.error("Kur bilgileri alinamadi: %s", e)
            return pd.DataFrame()

    def convert_currency_by_date(self, data, start_date, end_date, year, target_currency):
        data.reset_index(drop=True, inplace=True)

        currency_rates = self.get_currency_rates(start_date, end_date)

        if currency_rates.empty:
            logger.warning("Kur bilgileri bulunamadi.")
            return data

        for i in data.loc[data['year'] == year].index:
            date = data.loc[i]["booking_date"]
            formatted_date = pd.to_datetime(date).normalize()

            source_currency = data.loc[i]["currency"]
            total_price = data.loc[i]["total_amount"]

            last_day = pd.Timestamp(datetime.today() - timedelta(days=1)).normalize()

            if formatted_date > last_day:
                formatted_date = last_day

            if source_currency == target_currency:
                data.at[i, "currency"] = target_currency
                continue

            source_exchange = currency_rates.loc[
                (currency_rates["Date"] == formatted_date) &
                (currency_rates["symbol"] == source_currency)
            ]

            target_exchange = currency_rates.loc[
                (currency_rates["Date"] == formatted_date) &
                (currency_rates["symbol"] == target_currency)
            ]

            if source_exchange.empty:
                prev_source = currency_rates.loc[
                    (currency_rates["Date"] < formatted_date) &
                    (currency_rates["symbol"] == source_currency)
                ].sort_values(by="Date", ascending=False)

                if not prev_source.empty:
                    source_rate = prev_source.iloc[0]['rate']
                else:
                    logger.warning("%s icin kur bulunamadi.", source_currency)
                    continue
            else:
                source_rate = source_exchange["rate"].values[0]

            if target_exchange.empty:
                prev_target = currency_rates.loc[
                    (currency_rates["Date"] < formatted_date) &
                    (currency_rates["symbol"] == target_currency)
                ].sort_values(by="Date", ascending=False)

                if not prev_target.empty:
                    target_rate = prev_target.iloc[0]['rate']
                else:
                    logger.warning("%s icin kur bulunamadi.", target_currency)
                    continue
            else:
                target_rate = target_exchange["rate"].values[0]

            try:
                converted_price = (total_price / source_rate) * target_rate
                data.at[i, "total_amount"] = converted_price
                data.at[i, "currency"] = target_currency
            except (ZeroDivisionError, TypeError) as e:
                logger.warning("Kur donusumu hatasi: %s", e)

        return data

    def convert_to_hotel_currency(self, data, hotel_id=None, company_id=None, fallback_currency=None):
        hotel_currency = self.get_hotel_currency(hotel_id, company_id)

        if not hotel_currency:
            if fallback_currency:
                hotel_currency = fallback_currency
                logger.warning("API'den para birimi alinamadi, config currency kullaniliyor: %s", fallback_currency)
            else:
                logger.warning("Hotel para birimi alinamadi. Donusum yapilmiyor.")
                return data

        different_currency_data = data.loc[data["currency"] != hotel_currency]

        if different_currency_data.empty:
            logger.info("Tum veriler zaten %s para biriminde.", hotel_currency)
            return data

        logger.info("%s para birimine donusum baslatiliyor...", hotel_currency)

        timestamp_start = Timestamp(different_currency_data["booking_date"].min())
        timestamp_end = Timestamp(different_currency_data["booking_date"].max())

        start_date = timestamp_start.strftime('%Y-%m-%d')
        end_date = timestamp_end.strftime('%Y-%m-%d')

        start_year = timestamp_start.year
        end_year = timestamp_end.year

        data['year'] = data['booking_date'].dt.year

        for year in range(start_year, end_year + 1):
            first_date = datetime(year, 1, 1).strftime('%Y-%m-%d')
            last_date = (datetime(year + 1, 1, 1) - timedelta(days=1)).strftime('%Y-%m-%d')

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            if last_date > yesterday:
                last_date = yesterday
            if first_date > yesterday:
                first_date = yesterday

            logger.info("%d yili icin donusum: %s - %s", year, first_date, last_date)
            data = self.convert_currency_by_date(data, first_date, last_date, year, hotel_currency)
            logger.info("%d yili icin kur donusumu tamamlandi.", year)

        data.drop('year', axis=1, inplace=True)

        failed_indices = [
            j for j in different_currency_data.index
            if j in data.index and data.loc[j]["currency"] != hotel_currency
        ]
        if failed_indices:
            logger.warning(
                "%d kayit icin kur donusumu yapilamadi, atlanıyor: %s",
                len(failed_indices),
                data.loc[failed_indices, "currency"].value_counts().to_dict(),
            )
            data = data.drop(index=failed_indices).reset_index(drop=True)

        logger.info("Tum veriler %s para birimine basariyla donusturuldu.", hotel_currency)
        return data
