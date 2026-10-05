import json
import logging
from io import BytesIO

import numpy as np
import requests
import pandas as pd

from hotel_analysis.config import API_AUTH_URL, API_UPLOAD_URL, API_DELETE_URL, API_EMAIL, API_PASSWORD

logger = logging.getLogger(__name__)


def _authenticate():
    auth_payload = {
        "email": API_EMAIL,
        "password": API_PASSWORD,
        "rememberMe": False
    }
    auth_response = requests.post(API_AUTH_URL, json=auth_payload)
    if auth_response.status_code != 200:
        raise RuntimeError(f"Authentication failed: {auth_response.text}")

    token = auth_response.json().get("data", {}).get("token")
    if not token:
        raise RuntimeError(f"Token alinamadi: {auth_response.json()}")

    return {"Authorization": f"Bearer {token}"}


def _dataframe_to_excel_buffer(df):
    excel_buffer = BytesIO()
    df.to_excel(excel_buffer)
    excel_buffer.seek(0)
    return excel_buffer


CHUNK_SIZE = 5000


def _upload_chunk(df_chunk, headers, chunk_label=""):
    """Tek bir chunk'i API'ye yukle."""
    excel_buffer = _dataframe_to_excel_buffer(df_chunk)

    dto = {
        "mailTos": [],
        "subject": "",
        "content": "",
        "sendMail": False
    }

    files = {
        "file": ("data.xlsx", excel_buffer, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "dto": (None, json.dumps(dto), "application/json")
    }
    response = requests.post(API_UPLOAD_URL, files=files, headers=headers)

    if response.status_code != 200:
        raise RuntimeError(
            f"Upload {chunk_label} failed: HTTP {response.status_code} — {response.text[:300]}"
        )

    logger.info("Upload %s Status: %s (%d kayit)", chunk_label, response.status_code, len(df_chunk))
    try:
        logger.info("Upload %s Response: %s", chunk_label, response.json())
    except Exception:
        logger.info("Upload %s Response (non-JSON): %s", chunk_label, response.text)

    return response


def upload_dataframe(df):
    headers = _authenticate()

    if 'cancellation_date' in df.columns:
        df['cancellation_date'] = np.nan

    total = len(df)

    if total <= CHUNK_SIZE:
        return _upload_chunk(df, headers)

    # Buyuk dosyalari chunk'lara bol
    num_chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    logger.info("Buyuk dosya: %d kayit, %d chunk (%d'ser satir)", total, num_chunks, CHUNK_SIZE)

    last_response = None
    failed_chunks = []
    for i in range(num_chunks):
        start = i * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, total)
        chunk = df.iloc[start:end].reset_index(drop=True)
        label = f"[{i+1}/{num_chunks}]"
        last_response = _upload_chunk(chunk, headers, label)

        if last_response.status_code != 200:
            failed_chunks.append(label)

    if failed_chunks:
        logger.error("Basarisiz chunk'lar: %s", ", ".join(failed_chunks))

    return last_response


def delete_dataframe(df):
    headers = _authenticate()

    excel_buffer = _dataframe_to_excel_buffer(df)

    files = {
        "file": ("data.xlsx", excel_buffer, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    }
    response = requests.post(API_DELETE_URL, files=files, headers=headers)

    if response.status_code != 200:
        logger.warning("Delete returned status %s", response.status_code)

    logger.info("Delete Status: %s", response.status_code)
    try:
        logger.info("Delete Response: %s", response.json())
    except Exception:
        logger.info("Delete Response (non-JSON): %s", response.text)

    return response
