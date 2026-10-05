import pandas as pd
from datetime import datetime
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotel_analysis.currency_exchange import HotelCurrencyConverter
from hotel_analysis.analysis import Analysis

class RoomAnalysis:
    def __init__(self, data, collection_date):
        self.data = data
        self.database_table = 'hotel_booking_room_analysis'
        self.database_table_history = 'hotel_booking_room_analysis_history'

        self.collection_date = collection_date

        self.swap_list = ['NET_ROOM_REVENUE', 'CONSIDERED_DATE', 'NO_PERSONS', 'NO_ROOMS',
                          'ADULTS', 'CHILDREN', 'ARRIVAL_ROOMS', 'DEPARTURE_ROOMS', 'IND_ROOMS',
                          'GRP_ROOMS', 'INVENTORY_ROOMS', 'CF_OCC', 'CF_ADR_BY_ROOM','EXCH_RATE',
                          'created_date', 'created_by', 'last_modified_date', 'last_modified_by',
                          'company_id_prod','company_id_test', 'hotel_booking_room_type_id_prod','hotel_booking_room_type_id_test','collection_date',
                          'currency','original_cf_adr_by_room','original_net_room_revenue']
        
    def calculate_occupancy_rate(self):
        if self.data is None or self.data.empty:
            print("Veri bulunamadı, occupancy rate hesaplanmadı.")
            return
        df = self.data.copy()

        # ── Company 1463: XML'den gelen INVENTORY_ROOMS ile OCC yeniden hesapla ─
        mask_1463 = df["company_id_prod"] == 1463
        if mask_1463.any():
            occ_col = "CF_OCCUPANCY" if "CF_OCCUPANCY" in df.columns else "CF_OCC"
            inventory = pd.to_numeric(df.loc[mask_1463, "INVENTORY_ROOMS"], errors="coerce").fillna(0)
            no_rooms  = pd.to_numeric(df.loc[mask_1463, "NO_ROOMS"], errors="coerce").fillna(0)
            df.loc[mask_1463, occ_col] = (
                (no_rooms / inventory.replace(0, 1) * 100).where(inventory != 0, 0).round(4)
            )
            self.data = df
            print("Company 1463: OCC, XML INVENTORY_ROOMS kullanılarak yeniden hesaplandı.")
        # ─────────────────────────────────────────────────────────────────────────

        target_company_ids = [3404, 2326, 3942, 4478]

        # Bu ID'ler yoksa methoddan çık
        if not df["company_id_prod"].isin(target_company_ids).any():
            print("Occupacy rates kolonu korundu, değişiklik yapılmadı")
            return

        df["INVENTORY_ROOMS"] = pd.to_numeric(df["INVENTORY_ROOMS"], errors="coerce")
        df["CF_OOO_ROOMS"] = pd.to_numeric(df["CF_OOO_ROOMS"], errors="coerce")
        df["NO_ROOMS"] = pd.to_numeric(df["NO_ROOMS"], errors="coerce")

        if "CF_OOO_ROOMS" in df.columns and df["CF_OOO_ROOMS"].ne(0).any():
            mask = df["CF_OOO_ROOMS"] != 0
            inventory_room = df.loc[mask, "INVENTORY_ROOMS"] - df.loc[mask, "CF_OOO_ROOMS"]
        
            # Negatif veya null değerleri 0 yap
            inventory_room = inventory_room.apply(lambda x: x if x > 0 else 0)
        
            # 0’a bölme hatasını engelle, occupancy = 0 yap
            occupancy = df.loc[mask, "NO_ROOMS"] / inventory_room.replace(0, 1) * 100
            occupancy = occupancy.where(inventory_room != 0, 0)
        
            df.loc[mask, "INVENTORY_ROOMS"] = inventory_room
        
            if "CF_OCCUPANCY" in df.columns:
                df.loc[mask, "CF_OCCUPANCY"] = occupancy
            elif "CF_OCC" in df.columns:
                df.loc[mask, "CF_OCC"] = occupancy
            else:
                df.loc[mask, "CF_OCCUPANCY"] = occupancy
        
            self.data = df  
            print("İlgili company id için out of order mevcut, occupancy rates güncellendi.")
        
        else:
            print("CF_OOO_ROOMS sütunu yok veya tüm değerler sıfır.")


    def reindex_columns(self):

        columns_to_check = ['NET_ROOM_REVENUE', 'CONSIDERED_DATE', 'NO_PERSONS', 'NO_ROOMS',
                        'ADULTS', 'CHILDREN', 'ARRIVAL_ROOMS', 'DEPARTURE_ROOMS', 'IND_ROOMS',
                        'GRP_ROOMS', 'INVENTORY_ROOMS', 'CF_OCC', 'CF_ADR_BY_ROOM']
        
        required_columns = ['NET_ROOM_REVENUE', 'CONSIDERED_DATE', 'NO_ROOMS',
                    'IND_ROOMS', 'INVENTORY_ROOMS', 'CF_OCC', 'CF_ADR_BY_ROOM']

        existing_columns = self.data.columns
        missing_columns = [col for col in columns_to_check if col not in existing_columns]
    
        # Eski kolon isimleri mapping'i
        new_column_names = {
            'CF_AVERAGE_ROOM_RATE': 'CF_ADR_BY_ROOM',
            'CF_OCCUPANCY': 'CF_OCC',
            'REVENUE': 'NET_ROOM_REVENUE',
            'IND_DEDUCT_ROOMS':'IND_ROOMS',
            'GRP_DEDUCT_ROOMS':'GRP_ROOMS'
        }
        
        old_columns_exist = any(old in self.data.columns for old in new_column_names.keys())
        
        if old_columns_exist:
            print("Eski kolon isimleri tespit edildi, düzenleme yapılıyor...")
            
            for old, new in new_column_names.items():
                if old in self.data.columns and new in self.data.columns:
                    print(f"⚠️ Hem '{old}' hem '{new}' mevcut!")
                    mask = self.data[old].notna()
                    if mask.any():
                        self.data.loc[mask, new] = self.data.loc[mask, old]
                        print(f"  → '{old}' kolonundaki {mask.sum()} değer '{new}' kolonuna aktarıldı")
                    self.data.drop(columns=[old], inplace=True)
                    print(f"  → '{old}' kolonu silindi")
            
            columns_to_rename = {}
            for old, new in new_column_names.items():
                if old in self.data.columns and new not in self.data.columns:
                    columns_to_rename[old] = new
            
            if columns_to_rename:
                print(f"Yeniden adlandırılacak kolonlar: {columns_to_rename}")
                self.data.rename(columns=columns_to_rename, inplace=True)
                print("Kolon isimleri güncellendi.")
            
            existing_columns = self.data.columns
            missing_columns = [col for col in columns_to_check if col not in existing_columns]
        
        if len(missing_columns) == 0:
            print("Tüm kolonlar mevcut.")
        else:
            print("Mevcut olmayan kolonlar:", missing_columns)

            if any(col in required_columns for col in missing_columns):
                print("Mevcut olmayan zorunlu kolonlar var:", missing_columns)
                raise ValueError(f"Zorunlu kolonlar eksik: {missing_columns}")

            elif all(col not in required_columns for col in missing_columns):
                print("Eksik kolonlar arasında zorunlu olan bir kolon yok. İşlem devam ediyor.")

            else:
                print("Mevcut olmayan kolonlar var. İşlem tamamlanamadı.", missing_columns)
                raise ValueError(f"Zorunlu kolonlar eksik: {missing_columns}")

        self.data = self.data.reindex(columns=self.swap_list)
        self.data.columns = [x.lower() for x in self.data.columns]
        self.data.columns = self.data.columns.str.replace("[ ]", "_", regex=True)
        self.data = self.data.reset_index(drop=True)
        self.data['original_net_room_revenue'] = self.data['net_room_revenue']  
        self.data['original_cf_adr_by_room'] = self.data['cf_adr_by_room']
        self.data['currency'] = self.data['currency']
        self.data['original_currency'] = self.data['currency']
        self.data['created_by'] = 'booking_api_py'

    def fix_date(self, date_str):
        """Farklı tarih formatlarını destekler (Opera, Europrotel XML ve Excel)"""
        # Pandas Timestamp veya datetime nesnesi ise direkt formatla
        if isinstance(date_str, (datetime, pd.Timestamp)):
            return date_str.strftime("%Y-%m-%d")
        # Excel serial date (sayısal değer) ise dönüştür
        if isinstance(date_str, (int, float)):
            from datetime import timedelta
            excel_epoch = datetime(1899, 12, 30)
            return (excel_epoch + timedelta(days=int(date_str))).strftime("%Y-%m-%d")
        try:
            # Opera formatı: "01-JAN-25" veya "01-Jan-25"
            date_object = datetime.strptime(date_str, "%d-%b-%y")
            return date_object.strftime("%Y-%m-%d")
        except ValueError:
            try:
                # Europrotel formatı: "2025-11-17"
                date_object = datetime.strptime(date_str, "%Y-%m-%d")
                return date_object.strftime("%Y-%m-%d")
            except ValueError:
                print(f"Bilinmeyen tarih formatı: {date_str}")
                return date_str

    def manipulate_dates(self):
        self.data['considered_date'] = self.data['considered_date'].apply(self.fix_date)
        self.data['considered_date'] = pd.to_datetime(self.data['considered_date'])  # Datetime olarak tut
        self.data['created_date'] = datetime.now()
        #self.data['created_date'] = pd.Timestamp('2025-01-30 00:00:00')
        self.data['collection_date'] = self.collection_date

    def manipulate_values(self):
        self.data['net_room_revenue'] = pd.to_numeric(self.data['net_room_revenue'])
        self.data['cf_adr_by_room'] = pd.to_numeric(self.data['cf_adr_by_room'])
        self.data['original_net_room_revenue'] = pd.to_numeric(self.data['original_net_room_revenue'])
        self.data['original_cf_adr_by_room'] = pd.to_numeric(self.data['original_cf_adr_by_room'])
        self.data['cf_occ'] = pd.to_numeric(self.data['cf_occ'])
        self.data['no_persons'] = pd.to_numeric(self.data['no_persons'])
        self.data['no_rooms'] = pd.to_numeric(self.data['no_rooms'])
        self.data['adults'] = pd.to_numeric(self.data['adults'])
        self.data['children'] = pd.to_numeric(self.data['children'])
        self.data['arrival_rooms'] = pd.to_numeric(self.data['arrival_rooms'])
        self.data['departure_rooms'] = pd.to_numeric(self.data['departure_rooms'])
        self.data['ind_rooms'] = pd.to_numeric(self.data['ind_rooms'])
        self.data['grp_rooms'] = pd.to_numeric(self.data['grp_rooms'])
        self.data['inventory_rooms'] = pd.to_numeric(self.data['inventory_rooms'])  

    def round_numbers(self):
        #self.data = self.exchange_rates(self.data)

        self.data["original_net_room_revenue"] = self.data["original_net_room_revenue"].round(2)
        self.data["original_cf_adr_by_room"] = self.data["original_cf_adr_by_room"].round(2)
        self.data["net_room_revenue"] = self.data["net_room_revenue"].round(2)
        self.data["cf_occ"] = self.data["cf_occ"].round(2)
        self.data["cf_adr_by_room"] = self.data["cf_adr_by_room"].round(2)

    def insert_to_database(self):
        # Analysis sınıfını kullanarak insert işlemi yapacağız
        insert_table = Analysis()

        # PROD veritabanı için kolon listesi
        swap_list_prod = ['net_room_revenue', 'considered_date', 'no_persons', 'no_rooms',
                          'adults', 'children', 'arrival_rooms', 'departure_rooms', 'ind_rooms',
                          'grp_rooms', 'inventory_rooms', 'cf_occ', 'cf_adr_by_room',
                          'created_date', 'created_by', 'last_modified_date', 'last_modified_by',
                          'company_id_prod', 'hotel_booking_room_type_id_prod',
                          'cf_adr_by_room_tl', 'cf_adr_by_room_usd', 'faulty_rooms', 'collection_date','original_currency','original_cf_adr_by_room','original_net_room_revenue','currency']

        dataProd = self.data.reindex(columns=swap_list_prod)

        dataProd.columns = ['net_room_revenue', 'considered_date', 'no_persons', 'no_rooms',
                            'adults', 'children', 'arrival_rooms', 'departure_rooms', 'ind_rooms',
                            'grp_rooms', 'inventory_rooms', 'cf_occ', 'cf_adr_by_room',
                            'created_date', 'created_by', 'last_modified_date', 'last_modified_by',
                            'company_id', 'hotel_booking_room_type_id', 'cf_adr_by_room_tl',
                            'cf_adr_by_room_usd', 'faulty_rooms', 'collection_date','original_currency','original_cf_adr_by_room','original_net_room_revenue','currency']

        company_id = dataProd['company_id'].iloc[0]
        exchange = HotelCurrencyConverter()
        exchange.process_multiple_entities(dataProd, entity_type='company_id')

        # hotel_analysis.analysis.insert_room_analysis() kullan
        insert_table.insert_room_analysis(dataProd)

        print("Excel dosyası Prod PostgreSQL tablosuna başarıyla eklendi.")
    
    
