import logging

import pandas as pd
import requests

from hotel_analysis.config import LOG_AUTH_URL, API_EMAIL, API_PASSWORD

logger = logging.getLogger(__name__)

ROOM_TYPES_BASE_URL = "https://bookingapi-py.pricing-coach.com/api/v1/roomanalysis/room-types"
ROOM_TYPES_BY_COMPANY_URL = f"{ROOM_TYPES_BASE_URL}/by-company"


def _get_bearer_token() -> str:
    payload = {"username": API_EMAIL, "password": API_PASSWORD}
    response = requests.post(LOG_AUTH_URL, data=payload, timeout=15)
    response.raise_for_status()
    return response.json()["access_token"]


def fetch_emma_capacity(company_id: int) -> pd.DataFrame:
    """company_id bazında oda tipi + kapasite bilgisini API'den çeker.

    'no_show' kategorisi endpoint tarafında filtrelenmiş gelir.

    Returns:
        DataFrame kolonları: room_type_code, room_type_id, capacity
    """
    try:
        token = _get_bearer_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{ROOM_TYPES_BY_COMPANY_URL}/{company_id}"
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data:
            logger.warning("Emma kapasite bulunamadi: company_id=%s", company_id)
            return pd.DataFrame(columns=["room_type_code", "room_type_id", "capacity"])
        return pd.DataFrame(data)
    except Exception as e:
        logger.error("fetch_emma_capacity error (company_id=%s): %s", company_id, e)
        return pd.DataFrame(columns=["room_type_code", "room_type_id", "capacity"])


class FetchLookup:
    """hotel_booking_room_type verilerini booking-api-py üzerinden çeker.

    Eski implementasyon doğrudan DB'ye bağlanıyordu (create_engine + pd.read_sql_query).
    Yeni implementasyon /roomanalysis/room-types/{hotel_id} endpoint'ini kullanır;
    böylece VPN koptuğunda bu modül DB bağlantısı yüzünden durmaz.
    """

    def __init__(self, db_url=None):
        # db_url parametresi backward-compatibility için tutuldu, kullanılmıyor
        pass

    def fetch_data(self, hotel_id) -> pd.DataFrame:
        """hotel_id'ye göre oda tiplerini API'den çeker.

        Returns:
            DataFrame kolonları: room_code, room_type, capacity
        """
        try:
            token = _get_bearer_token()
            headers = {"Authorization": f"Bearer {token}"}
            url = f"{ROOM_TYPES_BASE_URL}/{int(hotel_id)}"
            response = requests.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            if not data:
                logger.warning("Oda tipi bulunamadı: hotel_id=%s", hotel_id)
                return pd.DataFrame(columns=["room_code", "room_type", "capacity"])
            return pd.DataFrame(data)
        except Exception as e:
            logger.error("FetchLookup.fetch_data error (hotel_id=%s): %s", hotel_id, e)
            return pd.DataFrame(columns=["room_code", "room_type", "capacity"])

