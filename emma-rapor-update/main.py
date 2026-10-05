import pandas as pd
from datetime import datetime, timedelta
from EmmaHFClass import EmmaHFClass

def main():
    # Set up date for file processing
    now = datetime.today() - timedelta(days=0)

    day = now.day
    month = now.month
    year = now.year

    if day < 10:
        day = f'0{day}'

    if month < 10:
        month = f'0{month}'

    collection_date = f'{now.date()}'
    # collection_date = '2025-09-01'
    file_date = f'{day}{month}{year}'

    EmmaHFClass(collection_date=collection_date, file_date=file_date).run()

if __name__ == "__main__":
    main()