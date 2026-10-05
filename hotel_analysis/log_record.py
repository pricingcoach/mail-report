import logging

import requests

from hotel_analysis.config import LOG_API_URL, LOG_AUTH_URL, API_EMAIL, API_PASSWORD

logger = logging.getLogger(__name__)


def save_log(df, integration_type, company_id, status, duration_ms, start_time, end_time, error_message=""):
    data = {
        "hotel_id": company_id,
        "created_by": "OPERA_RESERVATION",
        "status": status,
        "integration_type": integration_type,
        "record_count": len(df),
        "error_message": error_message,
        "start_time": start_time.isoformat(),
        "end_time": end_time.isoformat(),
        "duration_ms": duration_ms
    }

    logger.info("Log data: %s", data)

    try:
        auth_payload = {
            "username": API_EMAIL,
            "password": API_PASSWORD
        }
        auth_response = requests.post(LOG_AUTH_URL, data=auth_payload)
        token_data = auth_response.json()
        headers = {"Authorization": f"Bearer {token_data['access_token']}"}

        response = requests.post(LOG_API_URL, json=data, headers=headers)
        logger.info("Log status: %s | response: %s", response.status_code, response.json())
    except Exception as e:
        logger.error("Log kaydetme hatasi: %s", e)
