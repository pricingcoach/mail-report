import json
import logging

import pandas as pd
import requests

from hotel_analysis.config import LOG_AUTH_URL, API_EMAIL, API_PASSWORD

logger = logging.getLogger(__name__)

# Room analysis API
ROOM_ANALYSIS_BASE_URL = "https://bookingapi-py.pricing-coach.com/api/v1"
ROOM_ANALYSIS_URL = f"{ROOM_ANALYSIS_BASE_URL}/roomanalysis/insert_room_analysis"


class Analysis:

    def get_bearer_token(self):
        payload = {
            "username": API_EMAIL,
            "password": API_PASSWORD
        }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        response = requests.post(LOG_AUTH_URL, data=payload, headers=headers)

        if response.status_code == 200:
            token = response.json().get("access_token")
            logger.info("Token alindi.")
            return token
        else:
            raise Exception(f"Token alinamadi: {response.text}")

    def insert_room_analysis(self, data: pd.DataFrame):
        json_data = data.to_json(orient='records', date_format='iso')
        json_list = json.loads(json_data)

        token = self.get_bearer_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.post(ROOM_ANALYSIS_URL, json=json_list, headers=headers)
            response.raise_for_status()
            logger.info("Veri basariyla kaydedildi.")
        except requests.exceptions.RequestException as e:
            logger.error("Kayit basarisiz: %s", e)
