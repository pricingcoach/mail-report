import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google_auth_httplib2 import AuthorizedHttp
import io
import httplib2
import pandas as pd

SCOPES = ['https://www.googleapis.com/auth/drive']

archive_ids = {"TRASR1": "1WaPoTKZAqKlIgxLOT9Z4cb8Vo9Lk6CH5",
               "TRISTVD1": "1mIQrFsKxFuTshEavtWQXXllU1FsJFZ0r",
               "TRISTVD2": "12kcCRtd5FG8xTScQybZvg2rGaD0XiUF4",
               "TRISTBEY": "1Sw77ZdeEJYn7jDcpSia8jwcvJSCp2TYb",
               "TRISTSUK": "1m08-eKD6ONqoyBadRwmd3tpwcICMCC_X",
               "TRISTTEM": "1SvflAtm7OyO_5hNCKU7b60WyRMRIw76o",
               "TRMKGAAA": "16m2ntYob-WDjx62Z0SYqaqGjjUCRmHTw",
               "TRASRERC": "15bRok7poKArXRfhGDhV7ISQ_6PaHH19t"
               }


def authenticate():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(script_dir, 'service_account.json')

    creds = service_account.Credentials.from_service_account_file(key_path, scopes=SCOPES)

    http = httplib2.Http(timeout=60)
    authed_http = AuthorizedHttp(creds, http=http)
    return build('drive', 'v3', http=authed_http, cache_discovery=False)


def get_folder_id(service, folder_name):
    query = f"name = '{folder_name}' and mimeType = 'application/vnd.google-apps.folder'"
    results = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    folders = results.get('files', [])
    return folders[0]['id'] if folders else None

def list_files_in_folder(service, folder_id):
    query = f"'{folder_id}' in parents"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    return results.get('files', [])

def read_file(service, file_id, ext):
    request = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()

    fh.seek(0)
    if ext == 'xlsx':
        return pd.read_excel(fh, engine='openpyxl')
    else:
        df = pd.read_csv(fh, skiprows=9, sep='\t', engine='python')
        df = df.dropna(axis=1, how='all')
        return df


def move_to_archive(file_id, folder_name):
    service = authenticate()
    file = service.files().get(fileId=file_id, fields='parents').execute()
    previous_parents = ",".join(file.get('parents'))

    file = service.files().update(
        fileId=file_id,
        addParents=archive_ids[folder_name],
        removeParents=previous_parents,
        fields='id, parents'
    ).execute()

    return file

def get_file_names(folder_name, date):
    service = authenticate()
    folder_id = get_folder_id(service, folder_name)
    if not folder_id:
        print("Folder not found.")
        return []

    files = list_files_in_folder(service, folder_id)
    current_files = []

    for file in files:
        if date in file['name']:
            print(f"Downloading: {file['name']}")
            current_files.append(file)
    return current_files

def main(file_id, ext):
    service = authenticate()
    return read_file(service, file_id, ext)
