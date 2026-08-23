#!/usr/bin/env python3
"""
Transaction File Processor
Elabora file CSV ed Excel contenenti transazioni bancarie e crea un file CSV consolidato.
"""

import pandas as pd
import os
import re
import argparse
from pathlib import Path
from datetime import datetime
import sys

class TransactionProcessor:
    def __init__(self, input_folder, output_file="consolidated_transactions.csv"):
        self.input_folder = Path(input_folder)
        self.output_file = output_file
        self.consolidated_data = []
        self.categories_mapping = {}  # Dizionario per mappare descrizioni a categorie
        self.mortgage_amounts = []  # Buffer per transazioni mutuo
        
        # Pattern per identificare stringhe tecniche da rimuovere
        self.technical_patterns = [
            r'\d{2}[A-Z]{5}\d{8}[A-Z]{4}\d{10}',  # 02INTER20250502HSRT1382833170
            r'\d{10,}\d{6}IT',                     # 0929624995604114480160101601IT
            r'IT\d{2}[A-Z]\d{11}[A-Z]{2}\d{9}',   # IT12U32772243000ER000123456
            r'[A-Z]{2}\d{2}[A-Z0-9]{4}\d{6}[A-Z0-9]{11}',  # IBAN generici
        ]
    
    def load_categories_from_report(self):
        """Carica le categorie dal file report per creare un mapping descrizione->categoria"""
        print("Cercando file report per le categorie...")
        
        report_file = None
        for file_path in self.input_folder.glob('*'):
            if 'report' in file_path.name.lower() and file_path.suffix.lower() in ['.csv', '.xlsx', '.xls']:
                report_file = file_path
                break
        
        if not report_file:
            print("File report non trovato, le categorie non saranno disponibili")
            return
        
        try:
            print(f"Caricando categorie da: {report_file}")
            
            # Gestisce file CSV con formato speciale (sep= nella prima riga)
            if report_file.suffix.lower() == '.csv':
                # Prima legge la prima riga per vedere se c'è il problema sep=
                with open(report_file, 'r', encoding='utf-8-sig') as f:  # utf-8-sig rimuove BOM
                    first_line = f.readline().strip()
                
                print(f"Prima riga del file (dopo rimozione BOM): '{first_line}'")
                
                if first_line.startswith('sep='):
                    # Salta la prima riga problematica e legge il resto
                    df = pd.read_csv(report_file, skiprows=1, encoding='utf-8-sig')
                    print("File CSV letto saltando la prima riga (sep=)")
                else:
                    df = pd.read_csv(report_file, encoding='utf-8-sig')
            else:
                df = pd.read_excel(report_file)
            
            print(f"Colonne trovate nel file report: {list(df.columns)}")
            print(f"Shape del DataFrame: {df.shape}")
            print(f"Prime 3 righe del file report:")
            print(df.head(3).to_string())
            
            # Cerca colonne per descrizione e categoria
            desc_col = None
            cat_col = None
            
            for col in df.columns:
                col_lower = str(col).lower()
                if 'descrizione' in col_lower:
                    desc_col = col
                elif 'categoria' in col_lower:
                    cat_col = col
            
            print(f"Colonne identificate - desc_col: '{desc_col}', cat_col: '{cat_col}'")
            
            if not desc_col or not cat_col:
                print("Impossibile identificare le colonne automaticamente.")
                print("Disponibili:", list(df.columns))
                return
            
            # Crea mapping descrizione -> categoria
            categories_loaded = 0
            print(f"\nElaborando {len(df)} righe del file report...")
            
            for _, row in df.iterrows():
                desc = str(row[desc_col]).strip()
                cat = str(row[cat_col]).strip()
                
                if desc and desc != 'nan' and desc != '' and cat and cat != 'nan' and cat != '':
                    # Pulisce la descrizione come negli altri file
                    clean_desc = self.clean_description(desc).lower()
                    if clean_desc:
                        self.categories_mapping[clean_desc] = cat
                        categories_loaded += 1
                        
                        # Mostra i primi 5 mapping per debug
                        if categories_loaded <= 5:
                            print(f"  Mapping #{categories_loaded}: '{desc}' -> '{clean_desc}' -> '{cat}'")
            
            print(f"Caricate {categories_loaded} categorie dal file report")
            
            if categories_loaded == 0:
                print("ATTENZIONE: Nessuna categoria caricata!")
                print("Esempio di riga del report:")
                if len(df) > 0:
                    sample_row = df.iloc[0]
                    print(f"  Descrizione: '{sample_row[desc_col] if desc_col else 'N/A'}'")
                    print(f"  Categoria: '{sample_row[cat_col] if cat_col else 'N/A'}'")
            else:
                print(f"Prime 5 categorie caricate:")
                for i, (desc, cat) in enumerate(list(self.categories_mapping.items())[:5]):
                    print(f"  '{desc}' -> '{cat}'")
        
        except Exception as e:
            print(f"Errore nel caricamento del file report: {e}")
            import traceback
            traceback.print_exc()
    
    def clean_description(self, description):
        """Rimuove informazioni tecniche dalla descrizione"""
        if pd.isna(description):
            return ""
        
        desc = str(description).strip()
        
        # Rimuove pattern tecnici specifici
        for pattern in self.technical_patterns:
            desc = re.sub(pattern, '', desc, flags=re.IGNORECASE)
        
        # Pattern più specifico per stringhe alfanumeriche lunghe (solo se contengono numeri E lettere insieme)
        # Evita di rimuovere nomi propri come "Martinelli"
        desc = re.sub(r'\b[A-Z0-9]*\d+[A-Z0-9]*[A-Z]+[A-Z0-9]*\b', '', desc, flags=re.IGNORECASE)
        desc = re.sub(r'\b[A-Z0-9]*[A-Z]+[A-Z0-9]*\d+[A-Z0-9]*\b', '', desc, flags=re.IGNORECASE)
        
        # Rimuove spazi multipli e pulisce
        desc = re.sub(r'\s+', ' ', desc).strip()
        
        return desc

    def parse_date(self, date_str):
        """Converte vari formati di data in formato DD-MM-YYYY"""
        if pd.isna(date_str) or str(date_str).strip() == '':
            return ""
        
        date_str = str(date_str).strip()
        
        # Formati comuni da provare (aggiunto formato con timestamp)
        date_formats = [
            '%d/%m/%Y',           # 23/05/2025
            '%Y-%m-%d %H:%M:%S',  # 2025-05-14 00:00:00
            '%Y-%m-%d',           # 2025-05-14
            '%d-%m-%Y',           # 13-06-2018
            '%Y/%m/%d',           # 2018/06/13
            '%d.%m.%Y',           # 13.06.2018
            '%m/%d/%Y',           # 06/13/2018 (formato US)
            '%d-%m-%y',           # 13-06-18
            '%d/%m/%y',           # 13/06/18
        ]
        
        for fmt in date_formats:
            try:
                parsed_date = datetime.strptime(date_str, fmt)
                result = parsed_date.strftime('%d-%m-%Y')
                return result
            except ValueError:
                continue
        
        # Se nessun formato funziona, prova a estrarre numeri
        numbers = re.findall(r'\d+', date_str)
        if len(numbers) >= 3:
            try:
                day, month, year = numbers[:3]
                if len(year) == 2:
                    year = '20' + year if int(year) < 50 else '19' + year
                result = f"{int(day):02d}-{int(month):02d}-{year}"
                return result
            except:
                pass
        
        return date_str  # Restituisce originale se non riesce a parsare

    def parse_amount(self, amount_str):
        """Converte stringhe di importo in float mantenendo il segno originale"""
        if pd.isna(amount_str):
            return 0.0
        
        amount_str = str(amount_str).strip()
        
        # Rimuove simboli di valuta e spazi
        amount_str = re.sub(r'[€$£¥\s]', '', amount_str)
        
        # Gestisce formati europei (virgola come decimale)
        if ',' in amount_str and '.' in amount_str:
            # Formato: 1.234,56
            amount_str = amount_str.replace('.', '').replace(',', '.')
        elif ',' in amount_str:
            # Formato: 1234,56
            if len(amount_str.split(',')[1]) <= 2:
                amount_str = amount_str.replace(',', '.')
        
        # Rimuove caratteri non numerici eccetto punto e segno meno
        amount_str = re.sub(r'[^\d.-]', '', amount_str)
        
        try:
            amount = float(amount_str)
            # Mantiene il segno originale
            return amount
        except ValueError:
            return 0.0

    def is_mortgage_transaction(self, amount):
        """Verifica se è una transazione relativa al mutuo"""
        mortgage_amounts = [462.03, 416.56, 439.29]
        abs_amount = abs(amount)
        
        # Verifica match con tolleranza
        for target_amount in mortgage_amounts:
            if abs(abs_amount - target_amount) <= 0.02:
                print(f"🏠 MUTUO: Rilevato importo {abs_amount}€ → match con {target_amount}€")
                return True
        
        return False

    def process_mortgage_transactions(self, amount, date_str, original_description):
        """Gestisce le transazioni del mutuo accumulando i tre importi"""
        abs_amount = abs(amount)
        
        self.mortgage_amounts.append({
            'amount': abs_amount,
            'date': date_str,
            'original_desc': original_description
        })
        
        # Correzione: usa virgolette doppie per evitare conflitti negli f-string
        amounts_str = ', '.join([f"{t['amount']}€" for t in self.mortgage_amounts])
        print(f"🏠 MUTUO: Buffer {len(self.mortgage_amounts)}/3 transazioni [{amounts_str}]")
        
        # Controlla se abbiamo tutti e 3 gli importi
        if len(self.mortgage_amounts) >= 3:
            amounts_found = [t['amount'] for t in self.mortgage_amounts]
            required_amounts = [462.03, 416.56, 439.29]
            
            # Verifica match completo
            matches = 0
            for required in required_amounts:
                if any(abs(found - required) <= 0.02 for found in amounts_found):
                    matches += 1
            
            if matches == 3:
                latest_date = max(self.mortgage_amounts, key=lambda x: x['date'])['date']
                
                self.consolidated_data.append({
                    'Date': latest_date,
                    'Description': 'PAGAMENTO RATA MUTUO',
                    'Amount': -439.29,
                    'Category': 'Casa > Ipoteca/Affitto'
                })
                
                print(f"🏠 ✅ MUTUO CONSOLIDATO: Data {latest_date}, Importo -439.29€")
                self.mortgage_amounts = []
                return True
            else:
                print(f"🏠 ⏳ MUTUO: Trovati {matches}/3 importi richiesti")
        
        return False

    def is_hype_recharge(self, description, amount):
        """Verifica se è una ricarica Hype da ignorare"""
        if not description:
            return False
            
        desc_lower = description.lower()
        abs_amount = abs(amount)
        
        # Controlla parole chiave Hype
        hype_keywords = ['hype', 'ricarica', 'bonifico istantaneo da voi disposto a favore di hype', 'fibkitmmxxx']
        has_hype_keyword = any(keyword in desc_lower for keyword in hype_keywords)
        
        # Controllo principale: keyword + importo ~200€
        if has_hype_keyword and 190 <= abs_amount <= 210:
            print(f"💳 HYPE IGNORATA: {description[:50]}... ({amount}€)")
            return True
        
        # Controllo secondario: bonifico istantaneo ~200€
        if 'bonifico istantaneo' in desc_lower and 190 <= abs_amount <= 210:
            print(f"💳 POSSIBILE HYPE IGNORATA: {description[:50]}... ({amount}€)")
            return True
        
        return False

    def get_category_for_description(self, description):
        """Trova la categoria più appropriata per una descrizione"""
        if not self.categories_mapping or not description:
            return ""
        
        desc_lower = description.lower().strip()
        
        # Cerca match esatto prima di tutto
        if desc_lower in self.categories_mapping:
            return self.categories_mapping[desc_lower]
        
        # Cerca match parziale (la descrizione contiene una chiave del mapping)
        best_match = None
        best_match_length = 0
        
        for mapped_desc, category in self.categories_mapping.items():
            # Cerca se una delle due contiene l'altra
            if mapped_desc in desc_lower:
                if len(mapped_desc) > best_match_length:
                    best_match = (mapped_desc, category)
                    best_match_length = len(mapped_desc)
            elif desc_lower in mapped_desc:
                if len(desc_lower) > best_match_length:
                    best_match = (mapped_desc, category)
                    best_match_length = len(desc_lower)
        
        if best_match:
            return best_match[1]
        
        # Cerca match per parole chiave - solo per parole lunghe e specifiche
        desc_words = desc_lower.split()
        for word in desc_words:
            if len(word) > 4:  # Solo parole più lunghe e specifiche (era 3, ora 4)
                # Evita parole generiche che potrebbero creare match errati
                if word in ['game', 'pagamento', 'bonifico', 'carta', 'euro', 'data', 'numero']:
                    continue
                    
                for mapped_desc, category in self.categories_mapping.items():
                    # Match solo se la parola è significativa nella descrizione mappata
                    mapped_words = mapped_desc.split()
                    if word in mapped_words:  # Parola intera, non substring
                        return category
        
        return ""  # Nessuna categoria trovata
    
    def process_splitwise_file(self, file_path):
        """Elabora file Splitwise con struttura fissa: Data,Descrizione,Categorie,Costo,Valuta,Mikela bogoni,Fabio Stocco"""
        print(f"Elaborando file Splitwise: {file_path}")
        
        try:
            # Prova diversi separatori per il file Splitwise
            separators = [',', ';']
            df = None
            
            for sep in separators:
                try:
                    print(f"Tentativo parsing con separatore '{sep}'...")
                    if file_path.suffix.lower() == '.csv':
                        df = pd.read_csv(file_path, sep=sep)
                    else:
                        df = pd.read_excel(file_path)
                    print(f"✅ Parsing riuscito con separatore '{sep}'")
                    break
                except Exception as e:
                    print(f"❌ Errore con separatore '{sep}': {str(e)[:100]}...")
                    continue
            
            if df is None:
                print("❌ Impossibile parsare il file Splitwise")
                return
            
            # Rimuove righe completamente vuote
            df = df.dropna(how='all')
            
            # Verifica che le colonne necessarie esistano
            required_cols = ['Data', 'Descrizione', 'Fabio Stocco']
            missing_cols = []
            
            for col in required_cols:
                if col not in df.columns:
                    missing_cols.append(col)
            
            if missing_cols:
                print(f"Attenzione: Colonne mancanti nel file Splitwise {file_path}: {missing_cols}")
                print(f"Colonne disponibili: {list(df.columns)}")
                return
            
            print(f"Elaborando file Splitwise con {len(df)} righe")
            
            # Elabora le righe usando le colonne fisse
            rows_added = 0
            for _, row in df.iterrows():
                # Salta righe vuote o con descrizione "bilancio totale"
                description_raw = str(row['Descrizione']).strip().lower()
                if (pd.isna(row['Descrizione']) or 
                    description_raw == '' or 
                    description_raw == 'nan' or
                    'bilancio totale' in description_raw):
                    continue
                
                # Prende l'importo dalla colonna "Fabio Stocco" e lo rende negativo
                amount_str = str(row['Fabio Stocco']).strip()
                amount = 0.0
                
                if amount_str and amount_str != 'nan' and amount_str != '':
                    # Rimuove simboli di valuta e spazi
                    amount_str = re.sub(r'[€$£¥\s]', '', amount_str)
                    
                    # Gestisce formati europei (virgola come decimale)
                    if ',' in amount_str and '.' in amount_str:
                        amount_str = amount_str.replace('.', '').replace(',', '.')
                    elif ',' in amount_str:
                        if len(amount_str.split(',')[1]) <= 2:
                            amount_str = amount_str.replace(',', '.')
                    
                    # Rimuove caratteri non numerici eccetto punto e segno meno
                    amount_str = re.sub(r'[^\d.-]', '', amount_str)
                    
                    try:
                        parsed_amount = float(amount_str)
                        # Per Splitwise: sempre negativo
                        amount = -abs(parsed_amount)
                    except ValueError:
                        amount = 0.0
                
                if amount != 0:  # Solo se c'è un importo
                    date_str = self.parse_date(row['Data'])
                    description = self.clean_description(row['Descrizione'])
                    
                    # Salta ricariche Hype
                    if self.is_hype_recharge(description, amount):
                        continue
                    
                    # Gestisce transazioni mutuo
                    if self.is_mortgage_transaction(amount):
                        if self.process_mortgage_transactions(amount, date_str, description):
                            # Transazione mutuo consolidata creata, non aggiungere la singola
                            continue
                        else:
                            # Transazione mutuo nel buffer, non aggiungere ora
                            continue
                    
                    category = self.get_category_for_description(description)
                    
                    self.consolidated_data.append({
                        'Date': date_str,
                        'Description': description,
                        'Amount': amount,
                        'Category': category
                    })
                    rows_added += 1
            
            print(f"Aggiunte {rows_added} righe dal file Splitwise")
            return True  # Indica che il file è stato elaborato
        
        except Exception as e:
            print(f"Errore nell'elaborazione del file Splitwise {file_path}: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def process_regular_file(self, file_path):
        """Elabora file CSV/Excel regolari con parsing robusto"""
        print(f"Elaborando file: {file_path}")
        
        try:
            if file_path.suffix.lower() == '.csv':
                # Parsing con configurazioni multiple
                parsing_attempts = [
                    (',', 'utf-8', '.'),           # Standard CSV
                    (';', 'utf-8', ','),           # European CSV
                    (';', 'utf-8-sig', ','),       # European CSV + BOM  
                    ('\t', 'utf-8', '.'),          # Tab-separated
                ]
                
                df = None
                successful_config = None
                
                for sep, encoding, decimal in parsing_attempts:
                    try:
                        df = pd.read_csv(file_path, sep=sep, encoding=encoding, decimal=decimal)
                        if len(df.columns) >= 3 and len(df) > 0:
                            successful_config = (sep, encoding, decimal)
                            print(f"📄 PARSING OK: sep='{sep}', enc='{encoding}', dec='{decimal}' → {len(df)} righe, {len(df.columns)} col")
                            break
                        else:
                            df = None
                    except:
                        continue
                
                if df is None:
                    try:
                        df = pd.read_csv(file_path, sep=None, engine='python', encoding='utf-8-sig')
                        print(f"📄 AUTO-DETECT OK: {len(df)} righe, {len(df.columns)} colonne")
                    except:
                        print(f"❌ PARSING FALLITO: {file_path.name}")
                        return
                        
            else:
                df = pd.read_excel(file_path)
                print(f"📊 EXCEL: {len(df)} righe, {len(df.columns)} colonne")
            
            # Pulizia dati
            original_rows = len(df)
            df = df.dropna(how='all').dropna(axis=1, how='all')
            if len(df) < original_rows:
                print(f"🧹 PULIZIA: {original_rows} → {len(df)} righe (rimosse righe vuote)")
            
            # Identifica colonne automaticamente
            date_col = desc_col = amount_col = None
            
            for col in df.columns:
                col_lower = str(col).lower().strip()
                if not date_col and any(term in col_lower for term in ['data registrazione', 'data operazione', 'data']):
                    date_col = col
                elif not desc_col and any(term in col_lower for term in ['descrizione', 'operazione']):
                    desc_col = col
                elif not amount_col and any(term in col_lower for term in ['importo', 'amount']):
                    amount_col = col
            
            # Fallback colonne
            if not date_col: date_col = df.columns[0]
            if not desc_col: desc_col = df.columns[1] if len(df.columns) > 1 else None
            if not amount_col: amount_col = df.columns[-1] if len(df.columns) > 2 else None
            
            print(f"🔍 COLONNE: Data='{date_col}' | Desc='{desc_col}' | Importo='{amount_col}'")
            
            # Elaborazione righe
            rows_added = 0
            high_amounts_found = 0
            
            for i, row in df.iterrows():
                # Controllo importi alti per debug mutuo
                if amount_col:
                    raw_amount = row[amount_col]
                    if pd.notna(raw_amount):
                        parsed_amount = self.parse_amount(raw_amount)
                        abs_parsed = abs(parsed_amount)
                        
                        if abs_parsed > 400:
                            high_amounts_found += 1
                            status = ""
                            if 460 <= abs_parsed <= 470: status = " [MUTUO?]"
                            elif 410 <= abs_parsed <= 420: status = " [MUTUO?]"
                            elif 435 <= abs_parsed <= 445: status = " [MUTUO?]"
                            print(f"💰 ALTO: {abs_parsed}€{status}")
                
                # Elaborazione standard
                if desc_col and pd.notna(row[desc_col]):
                    description_raw = str(row[desc_col]).strip().lower()
                    if description_raw and description_raw != 'nan' and 'bilancio totale' not in description_raw:
                        
                        date_str = self.parse_date(row[date_col]) if date_col else ""
                        description = self.clean_description(row[desc_col])
                        amount = self.parse_amount(row[amount_col]) if amount_col else 0.0
                        
                        # Filtri
                        if self.is_hype_recharge(description, amount):
                            continue
                        
                        if self.is_mortgage_transaction(amount):
                            if self.process_mortgage_transactions(amount, date_str, description):
                                continue
                            else:
                                continue
                        
                        # Aggiunge transazione
                        if amount != 0 or description:
                            category = self.get_category_for_description(description)
                            self.consolidated_data.append({
                                'Date': date_str,
                                'Description': description,
                                'Amount': amount,
                                'Category': category
                            })
                            rows_added += 1
            
            print(f"✅ ELABORATO: {rows_added} transazioni aggiunte")
            if high_amounts_found > 0:
                print(f"💰 RILEVATI: {high_amounts_found} importi >400€")
        
        except Exception as e:
            print(f"❌ Errore nell'elaborazione del file {file_path}: {e}")
            import traceback
            traceback.print_exc()
    
    def process_all_files(self):
        """Elabora tutti i file nella cartella di input"""
        if not self.input_folder.exists():
            print(f"Errore: La cartella {self.input_folder} non esiste")
            return False
        
        # Prima carica le categorie dal file report
        self.load_categories_from_report()
        
        files_processed = 0
        
        # Cerca file CSV ed Excel (esclude file report e output)
        for file_path in self.input_folder.glob('*'):
            if file_path.suffix.lower() in ['.csv', '.xlsx', '.xls']:
                # Salta il file di output e il file report
                if (file_path.name == self.output_file or 
                    'report' in file_path.name.lower()):
                    print(f"⏭️ Saltando file: {file_path.name}")
                    continue
                    
                files_processed += 1
                
                if 'splitwise' in file_path.name.lower():
                    print(f"\n=== 📊 SPLITWISE: {file_path.name} ===")
                    self.process_splitwise_file(file_path)
                else:
                    print(f"\n=== 📄 NORMALE: {file_path.name} ===")
                    self.process_regular_file(file_path)
        
        if files_processed == 0:
            print("Nessun file CSV o Excel trovato nella cartella specificata")
            return False
        
        print(f"✅ Elaborati {files_processed} file")
        return True
    
    def save_consolidated_file(self):
        """Salva il file CSV consolidato"""
        if not self.consolidated_data:
            print("Nessun dato da salvare")
            return
        
        # Prima di salvare, controlla se ci sono transazioni mutuo incomplete nel buffer
        if self.mortgage_amounts:
            print(f"⚠️ Attenzione: {len(self.mortgage_amounts)} transazioni mutuo incomplete nel buffer")
            print("Transazioni mutuo non consolidate:")
            for t in self.mortgage_amounts:
                print(f"  - {t['amount']}€ del {t['date']}: {t['original_desc']}")
        
        df = pd.DataFrame(self.consolidated_data)
        
        # Ordina per data se possibile
        try:
            df['Date_parsed'] = pd.to_datetime(df['Date'], format='%d-%m-%Y', errors='coerce')
            df = df.sort_values('Date_parsed').drop('Date_parsed', axis=1)
        except:
            pass
        
        # Salva il file
        output_path = self.input_folder / self.output_file
        df.to_csv(output_path, index=False, encoding='utf-8')
        
        print(f"💾 File consolidato salvato: {output_path}")
        print(f"📊 Totale transazioni: {len(df)}")
        
        # Mostra un'anteprima
        print("\n📋 Anteprima dei primi 5 record:")
        print(df.head().to_string(index=False))

def main():
    parser = argparse.ArgumentParser(
        description="Elabora file CSV ed Excel di transazioni bancarie",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi di utilizzo:
    python transaction_processor.py -f /path/to/transactions
    python transaction_processor.py -f ./data -o my_transactions.csv
    python transaction_processor.py --folder C:\\Users\\User\\Documents\\Transactions
        """
    )
    
    parser.add_argument('-f', '--folder', 
                       required=True,
                       help='Cartella contenente i file CSV ed Excel da elaborare')
    
    parser.add_argument('-o', '--output',
                       default='consolidated_transactions.csv',
                       help='Nome del file CSV di output (default: consolidated_transactions.csv)')
    
    args = parser.parse_args()
    
    # Inizializza e esegue il processore
    processor = TransactionProcessor(args.folder, args.output)
    
    print("=== 🚀 Transaction File Processor ===")
    print(f"📁 Cartella input: {processor.input_folder}")
    print(f"📄 File output: {args.output}")
    print()
    
    if processor.process_all_files():
        processor.save_consolidated_file()
        print("\n✅ Elaborazione completata con successo!")
    else:
        print("❌ Elaborazione fallita")
        sys.exit(1)

if __name__ == "__main__":
    main()