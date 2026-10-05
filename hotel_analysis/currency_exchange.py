import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
from pandas import Timestamp

from hotel_analysis.config import LOG_AUTH_URL, API_EMAIL, API_PASSWORD

logger = logging.getLogger(__name__)

CURRENCY_API_BASE = "https://bookingapi-py.pricing-coach.com/api/v1"
CURRENCY_RATES_URL = f"{CURRENCY_API_BASE}/currency/rates"
HOTEL_CURRENCY_URL = f"{CURRENCY_API_BASE}/currency/hotel"


def _get_bearer_token() -> str:
    payload = {"username": API_EMAIL, "password": API_PASSWORD}
    response = requests.post(LOG_AUTH_URL, data=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


class HotelCurrencyConverter:
    """Döviz dönüşümü için booking-api-py API istemcisi.

    Eski implementasyon doğrudan DB'ye bağlanıyordu (ConnectionDb inheritance).
    Yeni implementasyon /currency/rates ve /currency/hotel/{company_id} endpoint'lerini kullanır.
    VPN koptuğunda DB bağlantısı sorunu yaşanmaz; API çağrısı başarısız olursa
    ValueError raise edilir ve veri DB'ye yanlış para birimiyle yazılmaz.
    """

    def get_hotel_currency(self, hotel_id=None, company_id=None) -> str:
        """Otelin para birimini API'den çeker."""
        try:
            lookup_id = company_id if company_id is not None else hotel_id
            if lookup_id is None:
                raise ValueError("hotel_id veya company_id parametrelerinden biri zorunlu.")

            token = _get_bearer_token()
            headers = {"Authorization": f"Bearer {token}"}
            url = f"{HOTEL_CURRENCY_URL}/{int(lookup_id)}"
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            currency = data.get("currency")
            if not currency:
                raise ValueError(f"Para birimi bulunamadı: company_id={lookup_id}")
            logger.info("Hotel currency: company_id=%s → %s", lookup_id, currency)
            return currency
        except Exception as e:
            logger.error("get_hotel_currency hatası: %s", e)
            raise

    def get_currency_rates(self, start_date: str, end_date: str) -> pd.DataFrame:
        """Tarih aralığındaki döviz kurlarını API'den çeker."""
        try:
            token = _get_bearer_token()
            headers = {"Authorization": f"Bearer {token}"}
            params = {"start_date": start_date, "end_date": end_date}
            response = requests.get(CURRENCY_RATES_URL, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
            if not data:
                logger.warning("Kur verisi bulunamadı: %s → %s", start_date, end_date)
                return pd.DataFrame()
            df = pd.DataFrame(data)
            df["Date"] = pd.to_datetime(df["date"]).dt.normalize()
            df = df.drop(columns=["date"])
            logger.info("%d kur kaydı çekildi: %s → %s", len(df), start_date, end_date)
            return df
        except Exception as e:
            logger.error("get_currency_rates hatası (%s → %s): %s", start_date, end_date, e)
            raise

    def convert_currency_by_date(self, data, start_date, end_date, year, target_currency):
        """Belirli bir yıl için tarihsel kur dönüşümü."""
        data.reset_index(drop=True, inplace=True)

        currency_rates = self.get_currency_rates(start_date, end_date)

        if currency_rates.empty:
            raise ValueError(f"Kur bilgileri alınamadı: {start_date} → {end_date}. Dönüşüm yapılamaz.")

        for i in data.loc[data["year"] == year].index:
            date = data.loc[i]["considered_date"]
            formatted_date = pd.to_datetime(date).normalize()

            source_currency = data.loc[i]["currency"]
            total_price = data.loc[i]["net_room_revenue"]
            adr = data.loc[i]["cf_adr_by_room"]

            last_day = pd.Timestamp(datetime.today() - timedelta(days=1)).normalize()

            if formatted_date > last_day:
                formatted_date = last_day

            if source_currency == target_currency:
                data.at[i, f"Price_{target_currency}"] = total_price
                data.at[i, f"ADR_{target_currency}"] = adr
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
                    source_rate = prev_source.iloc[0]["rate"]
                else:
                    logger.warning("%s için kur bulunamadı.", source_currency)
                    continue
            else:
                source_rate = source_exchange["rate"].values[0]

            if target_exchange.empty:
                prev_target = currency_rates.loc[
                    (currency_rates["Date"] < formatted_date) &
                    (currency_rates["symbol"] == target_currency)
                ].sort_values(by="Date", ascending=False)

                if not prev_target.empty:
                    target_rate = prev_target.iloc[0]["rate"]
                else:
                    logger.warning("%s için kur bulunamadı.", target_currency)
                    continue
            else:
                target_rate = target_exchange["rate"].values[0]

            try:
                converted_price = (total_price / source_rate) * target_rate
                converted_adr = (adr / source_rate) * target_rate

                data.at[i, f"Price_{target_currency}"] = converted_price
                data.at[i, f"ADR_{target_currency}"] = converted_adr

            except (ZeroDivisionError, TypeError) as e:
                logger.warning("Kur dönüşümü hatası satır %d: %s", i, e)

        return data

    def convert_to_hotel_currency(self, data, hotel_id=None, company_id=None):
        """Ana dönüşüm metodu: veriyi otelin para birimine çevirir.

        API çağrısı başarısız olursa ValueError raise eder —
        yanlış para birimiyle veri DB'ye yazılmaz.
        """
        hotel_currency = self.get_hotel_currency(hotel_id=hotel_id, company_id=company_id)

        different_currency_data = data.loc[data["currency"] != hotel_currency]

        if different_currency_data.empty:
            logger.info("Tüm veriler zaten %s para biriminde.", hotel_currency)
            return data

        logger.info("%s para birimine dönüşüm başlatılıyor...", hotel_currency)

        timestamp_start = Timestamp(different_currency_data["considered_date"].min())
        timestamp_end = Timestamp(different_currency_data["considered_date"].max())

        start_date = timestamp_start.strftime("%Y-%m-%d")
        end_date = timestamp_end.strftime("%Y-%m-%d")

        start_year = timestamp_start.year
        end_year = timestamp_end.year

        data[f"Price_{hotel_currency}"] = None
        data[f"ADR_{hotel_currency}"] = None
        data["year"] = data["considered_date"].dt.year

        for year in range(start_year, end_year + 1):
            first_date = datetime(year, 1, 1).strftime("%Y-%m-%d")
            last_date = (datetime(year + 1, 1, 1) - timedelta(days=1)).strftime("%Y-%m-%d")

            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            if last_date > yesterday:
                last_date = yesterday
            if first_date > yesterday:
                # Gelecek yıl verisi: son 30 günlük kur verisiyle fallback yapılabilsin
                first_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

            logger.info("%d yılı için dönüşüm: %s - %s", year, first_date, last_date)
            data = self.convert_currency_by_date(data, first_date, last_date, year, hotel_currency)
            logger.info("%d yılı için kur dönüşümü tamamlandı.", year)

        data.drop("year", axis=1, inplace=True)

        for j in different_currency_data.index:
            if data.loc[j]["currency"] != hotel_currency:
                converted = data.loc[j].get(f"Price_{hotel_currency}")
                if pd.isna(converted):
                    logger.warning("Satır %d için kur dönüşümü yapılamadı.", j)

        data.drop(["net_room_revenue", "cf_adr_by_room"], axis=1, inplace=True)
        data = data.rename(columns={
            f"Price_{hotel_currency}": "net_room_revenue",
            f"ADR_{hotel_currency}": "cf_adr_by_room",
        })

        data["net_room_revenue"] = data["net_room_revenue"].fillna(0)
        data["cf_adr_by_room"] = data["cf_adr_by_room"].fillna(0)

        logger.info("Tüm veriler %s para birimine başarıyla dönüştürüldü.", hotel_currency)
        return data

    def process_multiple_entities(self, data, entity_type="hotel_id"):
        unique_entities = data[entity_type].unique()
        entity_name = "Hotel" if entity_type == "hotel_id" else "Company"

        logger.info("Toplam %d farklı %s: %s", len(unique_entities), entity_name, unique_entities)

        if len(unique_entities) == 1:
            entity_id = unique_entities[0]
            kwargs = {entity_type: entity_id}
            return self.convert_to_hotel_currency(data, **kwargs)
        else:
            results = {}
            for entity_id in unique_entities:
                entity_data = data[data[entity_type] == entity_id].copy()
                kwargs = {entity_type: entity_id}
                results[entity_id] = self.convert_to_hotel_currency(entity_data, **kwargs)
            return pd.concat(results.values(), ignore_index=True)

