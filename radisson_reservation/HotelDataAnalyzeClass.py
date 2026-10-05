import logging
import math
from datetime import datetime, timedelta
from io import BytesIO

import numpy as np
import pandas as pd
from pandas import Timestamp

from hotel_analysis import api_client
from hotel_analysis import log_record as ha_log_record
from hotel_analysis.currency_exchange import HotelCurrencyConverter
from hotel_analysis.data_analyzer import DataAnalyzer

logger = logging.getLogger(__name__)


class HotelDataAnalyzeClass:

    dataTitle = None

    def __init__(self):
        self._currency_converter = HotelCurrencyConverter()

    @staticmethod
    def currentDate():
        now = datetime.today()
        day = now.day
        month = now.month

        if day < 10:
            day = f'0{day}'

        if month < 10:
            month = f'0{month}'

        return [month, day]

    def businessRules(self, data, hotel_id):
        if hotel_id == 230:
            data = data[data['booking_no'].notna()]

            data['room_type'].fillna('PM', inplace=True)

            data['booking_no'] = data['booking_no'].astype(int)

            data.loc[(data["rooms"] > 0) & (data["new_guest"] > 0) & (data["night"] > 0) & (data["total_amount"] == 0), "Subsegment"] = "Complimentary"

            data.loc[(data["Subsegment"] == "NO-SHOWS, CANCELLATIONS & DAY-USE"), "Subsegment"] = "Complimentary"

            data.loc[(data["Subsegment"] == "COMPLIMENTARY / HOUSE USE"), "Subsegment"] = "Complimentary"

            data.loc[ data["room_type"] == "AA1B--AC1K", "room_type"] = "AA1B"

            data.loc[(data["Subsegment"] != "Complimentary") & (data["total_amount"] < 0), "total_amount"] = 0

            data['room_type'] = data['room_type'].str.replace(r'[-]+[\d\w]*', '', regex=True)
            data['room_type'] = data['room_type'].str.strip()

            return data

        elif hotel_id == 229:
            data = data[data['booking_no'].notna()]

            data['room_type'].fillna('PM', inplace=True)

            data['booking_no'] = data['booking_no'].astype(int)

            data.loc[(data["rooms"] > 0) & (data["new_guest"] > 0) & (data["night"] > 0) & (data["total_amount"] == 0), "Subsegment"] = "Complimentary"

            data.loc[(data["Subsegment"] != "COMPLIMENTARY / HOUSE USE") & (data["total_amount"] < 0), "total_amount"] = 0

            data['room_type'] = data['room_type'].str.replace(r'[-]+[\d\w]*', '', regex=True)
            data['room_type'] = data['room_type'].str.strip()

            return data

    def currenyConvert(self, data, start_date, end_date, year):
        data.reset_index(drop=True, inplace=True)

        currency_rates = self._currency_converter.get_currency_rates(start_date, end_date)

        if currency_rates.empty:
            logger.warning("Kur bilgileri alinamadi: %s -> %s", start_date, end_date)
            return data

        yesterday = pd.Timestamp(datetime.today() - timedelta(days=1)).normalize()

        for i in data.loc[data['year'] == year].index:
            booking_date = pd.to_datetime(data.at[i, "booking_date"]).normalize()
            curr = data.at[i, "currency"]
            totalPrice = data.at[i, "total_amount"]

            if curr == "EUR":
                data.at[i, "Price_EUR"] = totalPrice
                continue

            if booking_date > yesterday:
                booking_date = yesterday

            rate_match = currency_rates.loc[
                (currency_rates["Date"] == booking_date) & (currency_rates["symbol"] == curr)
            ]

            if rate_match.empty:
                prev_rates = currency_rates.loc[
                    (currency_rates["Date"] < booking_date) & (currency_rates["symbol"] == curr)
                ].sort_values(by="Date", ascending=False)

                if prev_rates.empty:
                    logger.warning("Satir %d: %s icin kur bulunamadi, atlaniyor.", i, curr)
                    continue
                exchange_rate = prev_rates.iloc[0]["rate"]
            else:
                exchange_rate = rate_match["rate"].values[0]

            try:
                data.at[i, "Price_EUR"] = totalPrice / exchange_rate
            except (ZeroDivisionError, TypeError) as e:
                logger.warning("Kur donusumu hatasi satir %d: %s", i, e)

        return data

    def currencyCheck(self, data):
        non_eur = data.loc[data["currency"] != "EUR"]
        if non_eur.empty:
            logger.info("Tum veriler EUR.")
            return data

        logger.info("Farkli kurdan veriler mevcut. Kur donusumu yapilacak.")

        timestamp_start = Timestamp(non_eur["booking_date"].min())
        timestamp_end = Timestamp(non_eur["booking_date"].max())

        sYil = timestamp_start.year
        eYil = timestamp_end.year

        data["Price_EUR"] = None
        data['year'] = data['booking_date'].dt.year

        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        for i in range(sYil, eYil + 1):
            first_date = datetime(i, 1, 1).strftime('%Y-%m-%d')
            last_date = (datetime(i + 1, 1, 1) - timedelta(days=1)).strftime('%Y-%m-%d')
            if last_date > yesterday:
                last_date = yesterday
            if first_date > yesterday:
                first_date = yesterday
            data = self.currenyConvert(data, str(first_date), str(last_date), i)
            logger.info("Kur donusumu %d senesi icin yapildi.", i)

        data.drop('year', axis=1, inplace=True)

        unconverted = data[(data["currency"] != "EUR") & data["Price_EUR"].isna()]
        if not unconverted.empty:
            logger.warning("Kur donusumu yapilamayan kayitlar: %d satir.", len(unconverted))

        data.drop(["total_amount"], axis=1, inplace=True)
        data = data.rename(columns={"Price_EUR": "total_amount"})
        data['total_amount'] = data['total_amount'].fillna(0)
        data['currency'] = "EUR"
        return data

    def totalAmountCalculate(self, data):
        totalAmountList = []
        for i in data.index:
            price = data.loc[i]["price"]
            night = data.loc[i]["night"]
            rooms = data.loc[i]["rooms"]

            total_amount = price * (night * rooms)
            totalAmountList.append({"total_price": total_amount})

        totalAmountListDf = pd.DataFrame(totalAmountList)
        data["total_amount"] = totalAmountListDf

        return data

    def currencyCalculate(self, data):
        currencyList = []
        for i in data.index:
            totalPrice = data.loc[i]["price"]

            tam_kisim = int(abs(totalPrice))
            basamak_sayisi = len(str(tam_kisim))

            if basamak_sayisi <= 3:
                currencyList.append({"currency": 'EUR'})
            elif basamak_sayisi >= 4:
                currencyList.append({"currency": 'TRY'})

        currencyListDf = pd.DataFrame(currencyList)
        data["currency"] = currencyListDf

        return data

    def nightCalculate(self, data):
        nightList = []
        for idx in data.index:
            start_date = data.loc[idx]["arrival_date"]
            end_date = data.loc[idx]["departure_date"]

            bb = pd.date_range(start_date, end_date, inclusive='left', freq="D")
            dayCount = bb.value_counts().sum()

            count = dayCount if dayCount > 0 else 0
            nightList.append({"night": count})

        nightListDf = pd.DataFrame(nightList)
        data["new_night"] = nightListDf

        return data

    def guestCalculate(self, data):
        guestList = []

        for i in data.index:
            guests = data.loc[i]["guests"]
            night = data.loc[i]["night"]

            if night == 0 or guests == 0:
                new_guest = guests
            elif guests != 0 or night != 0:
                new_guest = math.ceil(guests / night)
            else:
                new_guest = guests

            guestList.append({"new_guest": new_guest})

        guestListDf = pd.DataFrame(guestList)
        data["new_guest"] = guestListDf

        data.drop(columns=["guests"], axis=1, inplace=True)
        data = data.rename(columns={"new_guest": "guests"})

        return data

    def roomTypeMatch(self, data, lookup):
        data['room_code'] = data['room_code'].str.replace(r'[-]+.*', '', regex=True).str.strip().str.lower()
        lookup['room_code'] = lookup['room_code'].str.strip().str.lower()

        data = pd.merge(data, lookup[['room_code', 'room_type']], on='room_code', how='left')
        data['room_type'] = data['room_type'].fillna('Not Specified')

        unmatched = data['room_type'].isna().sum()
        if unmatched != 0:
            logger.warning("Eslesmeyen Oda Tipi: %d adet — %s", unmatched,
                           data.loc[data['room_type'].isna(), 'room_code'].value_counts().to_dict())
        else:
            logger.info("Eslesmeyen Oda Tipi yoktur.")

        data.drop('room_code', axis=1, inplace=True)

        return data

    def familyRoomCalculate(self, data, familyMaping, revenueLookup):
        data['year'] = pd.to_datetime(data['arrival_date']).dt.year
        data['month'] = pd.to_datetime(data['arrival_date']).dt.month

        for i in familyMaping["room_type"].unique():
            dataFilter = data.loc[data["room_type"] == i].reset_index(drop=True)

            for j in dataFilter.index:
                fBookingNo = dataFilter.loc[j]["booking_no"]
                fTotalAmount = dataFilter.loc[j]["total_amount"]
                fGuest = dataFilter.loc[j]["guests"]
                fYear = dataFilter.loc[j]["year"]
                fMonth = dataFilter.loc[j]["month"]
                dataCopy = dataFilter.loc[j]

                toplam = 0
                for k in familyMaping.loc[familyMaping["room_type"] == i].index:
                    newRow = dataCopy
                    newRoomType = familyMaping.loc[k]["room_maping"]
                    newBookingNo = familyMaping.loc[k]["room_booking"]
                    newPax = familyMaping.loc[k]["max_pax"]

                    newRow["booking_no"] = f'{fBookingNo}{newBookingNo}'
                    newRow['room_type'] = newRoomType

                    rate = 0
                    for l in revenueLookup.loc[
                        (revenueLookup["room_type"] == i) &
                        (revenueLookup["room_type_match"] == newRoomType) &
                        (revenueLookup["year"] == fYear) &
                        (revenueLookup["month"] == fMonth)
                    ].index:
                        rate = revenueLookup.loc[l]["ratio"]

                    newRow['total_amount'] = fTotalAmount * rate

                    if (fGuest - toplam) > newPax:
                        newRow["guests"] = newPax
                        toplam += newPax
                    elif (fGuest - toplam) == newPax:
                        newRow["guests"] = newPax
                        toplam += newPax
                    elif (fGuest - toplam) < newPax:
                        newRow["guests"] = fGuest - toplam
                        toplam += fGuest - toplam

                    data = pd.concat([data, newRow.to_frame().T], ignore_index=True)

        return data

    def familyRoomCancelCalculate(self, data, familyMaping):
        for i in familyMaping["room_type"].unique():
            dataFilter = data.loc[data["room_type"] == i].reset_index(drop=True)

            for j in dataFilter.index:
                fBookingNo = dataFilter.loc[j]["booking_no"]
                dataCopy = dataFilter.loc[j]

                for k in familyMaping.loc[familyMaping["room_type"] == i].index:
                    newRow = dataCopy
                    newRoomType = familyMaping.loc[k]["room_maping"]
                    newBookingNo = familyMaping.loc[k]["room_booking"]

                    newRow["booking_no"] = f'{fBookingNo}{newBookingNo}'
                    newRow['room_type'] = newRoomType

                    data = pd.concat([data, newRow.to_frame().T], ignore_index=True)

        return data

    def create_master_data(self, dataframe_param, hotel_id, hotel_name):
        dataframe_param.columns = [x.lower() for x in dataframe_param.columns]
        dataframe_param.columns = dataframe_param.columns.str.replace("[ ]", "_", regex=True)

        if dataframe_param["booking_date"].value_counts().count() > 1:
            title = hotel_name + "  Master Data"
        else:
            dataDate = Timestamp(dataframe_param.loc[0]["booking_date"])
            date_only = dataDate.strftime('%Y-%m-%d')
            title = hotel_name + " " + str(date_only)

        HotelDataAnalyzeClass.dataTitle = title

        dataframe_param["hotel_id"] = hotel_id
        dataframe_param["accommodation_property"] = hotel_name
        dataframe_param = dataframe_param.reindex(columns=DataAnalyzer.SWAP_LIST)
        dataframe_param['status'] = dataframe_param['status'].str.title()

        self.resultReservation(dataframe_param, title)

        ha_log_record.save_log(
            dataframe_param,
            integration_type="RADISSON_RESERVATION",
            company_id=hotel_id,
            status=self.status,
            error_message=self.error_message,
            duration_ms=round((self.end_time - self.start_time).total_seconds() * 1000, 3),
            start_time=self.start_time,
            end_time=self.end_time,
        )

        return dataframe_param

    def resultReservation(self, dataframe_param, title):
        self.dataCancel = dataframe_param.loc[
            (dataframe_param["status"] == "Cancelled") | (dataframe_param["status"] == "Lost/Declined")
        ][["hotel_id", "booking_no"]]

        dataBooking = dataframe_param.loc[
            (dataframe_param["status"] != "Cancelled") & (dataframe_param["status"] != "Lost/Declined")
        ].reset_index(drop=True)

        dataBooking.loc[dataBooking["status"] == "No Show", "room_type"] = "Posting Master"
        dataBooking["cancellation_date"] = np.nan
        dataBooking["name"] = np.nan

        try:
            self.start_time = datetime.now()
            api_client.upload_dataframe(dataBooking)
            logger.info("Reservation verileri basariyla yuklendi.")
            self.error_message = ""
            self.end_time = datetime.now()
            self.status = "SUCCESS"
        except Exception as e:
            self.status = "FAILURE"
            self.error_message = str(e)
            self.end_time = datetime.now()
            logger.error("Reservation yukleme hatasi: %s", e)

    def resultEkstraCancel(self, dataEkCancel, hotel_id):
        dataEkCancel["hotel_id"] = hotel_id
        dataEkCancel = dataEkCancel[["hotel_id", "booking_no"]]

        df_merged = pd.merge(dataEkCancel, self.dataCancel, on=["hotel_id", "booking_no"], how="left")

        try:
            self.start_time = datetime.now()
            api_client.delete_dataframe(df_merged)
            logger.info("Ek Cancel verileri basariyla silindi.")
            self.error_message = ""
            self.end_time = datetime.now()
            self.status = "SUCCESS"
        except Exception as e:
            self.status = "FAILURE"
            self.error_message = str(e)
            self.end_time = datetime.now()
            logger.error("Ek Cancel silme hatasi: %s", e)

        ha_log_record.save_log(
            df_merged,
            integration_type="RADISSON_RESERVATION_CANCEL",
            company_id=hotel_id,
            status=self.status,
            error_message=self.error_message,
            duration_ms=round((self.end_time - self.start_time).total_seconds() * 1000, 3),
            start_time=self.start_time,
            end_time=self.end_time,
        )

        logger.info("Ek Cancel Verilerini Yazdirma Islemi Basariyla Tamamlanmistir.")
