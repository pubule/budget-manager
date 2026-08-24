BILANCIO
========

    python server.py

Apre la dashboard nel browser. Da li' si fa tutto: correggere le categorie,
gestire la tassonomia, scrivere le regole, filtrare i grafici.

DOVE VANNO GLI EXPORT
---------------------

    export/                  ci depositi gli estratti conto
      elaborati/2026-08/     dove finiscono dopo il caricamento
      anonimi/               copie senza IBAN

Depositi i file in export/. La dashboard se ne accorge entro pochi secondi e
compare il pulsante "Carica N export". Premi quando hai finito di copiare: la
pipeline gira e il log a destra mostra cosa fa.

Finche' non premi, quei file NON entrano nei conti. E' voluto: il sorveglia-
mento guarda data e dimensione, e un file ancora in copia le cambia entrambe,
quindi elaborare da solo poteva leggere meta' di un .xlsx.

A giro riuscito gli originali si spostano in export/elaborati/AAAA-MM/ e
accanto, in export/anonimi/, compare la copia ripulita. Un file da cui non si
legge nessuna transazione NON viene archiviato: resta in export/ con l'errore
nel log.

Gli archiviati continuano a essere letti a ogni elaborazione: spostarli e'
organizzare, non escludere. Togliere un file da elaborati/ significa togliere
quelle transazioni dal consolidato.

ATTENZIONE a export/anonimi/: "anonimo" vuol dire senza IBAN, numeri di carta
e codici tecnici. Importi, date, negozi e saldi restano tutti. Non e' un file
da mandare in giro alla leggera.

Tutta la cartella export/ e' esclusa da git, anonimi/ compresa.

Il server ascolta SOLO su 127.0.0.1. Sono dati bancari e non devono essere
raggiungibili dalle altre macchine di casa. I dati non escono mai dal PC:
MoneyWiz e' letto in locale, Ollama gira in locale, la pagina non carica
niente dalla rete.

Serve ancora la riga di comando? C'e':

    python bilancio.py                 elabora e rigenera dashboard.html/.xlsx
    python bilancio.py --no-llm        salta Ollama
    python bilancio.py --selfcheck     verifica che il motore non sia rotto


LE SCHEDE
---------

Dashboard          Indicatori e grafici. La barra in alto filtra per periodo,
                   conto, natura, categoria e testo, e i grafici si aggiornano
                   con lei. Cliccare una barra filtra tutto su quella voce.

                   I filtri rapidi seguono il CALENDARIO: "mese in corso" e' il
                   mese di oggi. Se e' vuoto vuol dire che non hai ancora
                   caricato gli export di questo mese, e il messaggio te lo
                   dice indicando dov'e' l'ultimo movimento che hai.

                   "Ultimo mese con dati" e' il pulsante per saltare
                   direttamente li'.

                   Fanno da interruttore: premere di nuovo lo stesso pulsante
                   toglie il filtro.

                   Filtrando su una natura o una categoria, risparmio e tasso
                   di risparmio spariscono: si calcolano sul bilancio intero,
                   e su una fetta di sole uscite darebbero numeri senza senso.
                   Al loro posto compare il peso di quella fetta.

Da rivedere        Le righe con confidenza sotto 1. Scegli la categoria dal
                   menu. Se il negozio si ripete, usa "regola" invece di
                   correggere la singola riga: una regola vale per sempre.

Transazioni        Tutte le righe. Qui si correggono anche data e importo.
                   "annulla" toglie sia la categoria forzata sia la correzione.

Categorie          Rinomina, unisci, cambia natura. "ignora" non cancella le
                   transazioni: dice al motore di non imparare da quella
                   categoria, e le righe vengono ricategorizzate dalle regole.

Regole e merchant  Con anteprima: "prova" dice quante transazioni colpirebbe
                   una regola prima di salvarla.

Componi            Quali riquadri mostrare e in che ordine.

Log                Pannello a destra, si apre col pulsante "Log" e da solo
                   quando premi "Rielabora". Mostra la pipeline riga per riga
                   MENTRE gira, non alla fine.

                   Del modello si vede: la descrizione che gli e' stata data,
                   quanto ci ha messo, il suo ragionamento se lo produce, e la
                   risposta esatta. Verde = categoria accettata, rosso =
                   scartata perche' non esiste o perche' si e' astenuto.

                   Scorrere verso l'alto ferma l'inseguimento della coda, cosi'
                   puoi leggere in pace mentre continua a scrivere; tornare in
                   fondo lo riattiva.


COME DECIDE LA CATEGORIA
------------------------

Sette livelli in cascata. Il primo che risponde vince.

  1. override        la riga e' elencata a mano in override.csv
  2. giroconto       una regola con categoria "Giroconto" riconosce uno
                     spostamento fra conti propri: non e' una spesa, e va
                     riconosciuto PRIMA dello storico perche' capita di aver
                     etichettato a mano un giroconto come spesa vera
  3. storico-esatto  la descrizione esiste identica nello storico MoneyWiz
  4. storico-simile  somiglia a una gia' categorizzata (86% corretto)
  5. regola          combacia con una riga di regole.csv
  6. voto-token      le singole parole votano la categoria (65% corretto)
  7. llm             qwen3:8b via Ollama, solo per cio' che resta

Il passo LLM costa circa 3 secondi a descrizione (14 il primo, a modello
freddo). Per questo NON parte all'avvio del server: si attiva col pulsante
"Rielabora" e quando la cartella cambia. Le risposte finiscono in
categorie_cache.json, quindi ogni descrizione si chiede una volta sola.

    ollama pull qwen3:8b


I DUE ASSI
----------

  Categoria   cosa hai comprato        Casa > Bollette
  Natura      quanto puoi intervenire  Ricorrenti

  Vincolate        mutuo, assicurazioni, tasse    -> si cambia una volta l'anno
  Ricorrenti       bollette, abbonamenti          -> cambio fornitore, disdette
  Quotidiane       spesa, benzina, farmacia       -> punto vendita, frequenza
  Discrezionali    ristoranti, vestiti, regali    -> decisione singola
  Straordinarie    mobili, ristrutturazioni       -> non comprimibile
  Non spesa        giroconti                      -> esclusa dai totali

Le straordinarie vanno guardate a parte: sono episodiche e, lasciate nella
media mensile, fanno sembrare cattivo un mese normale. La dashboard mostra sia
la spesa totale sia la "spesa ordinaria" che le esclude.


I FILE
------

consolidato.csv   E' DERIVATO, non modificarlo: viene riscritto da zero a ogni
                  elaborazione e le modifiche fatte li' andrebbero perse. Tutto
                  quello che correggi finisce nei file qui sotto.

override.csv      La categoria decisa a mano per una singola transazione.
                  Identificata dall'ID, oppure da data+importo se preferisci
                  scriverla a mano senza cercare l'ID.

correzioni.csv    id;campo;valore - corregge importo, data o descrizione.
                  Separato da override.csv perche' cambia i FATTI della
                  transazione, non la sua interpretazione.

regole.csv        pattern;categoria - il pattern e' una regex. Vince la prima
                  riga che combacia, quindi le regole specifiche vanno SOPRA
                  le generiche.

merchant.csv      pattern;merchant - unifica le grafie dello stesso negozio.
                  Iper Rossetto compariva con 56 scritture diverse per 7.828
                  euro: senza questo file non lo vedi in nessuna classifica.

natura.csv        categoria;natura - il secondo asse.

conti.csv         pattern;conto - da quale conto viene un export, dal nome del
                  file. Senza, il "conto" e' il nome del file e due export
                  dello stesso conto in mesi diversi diventano due conti. Non
                  e' estetica: il riconoscimento dei giroconti guarda proprio
                  che i conti siano diversi.

categorie_merge.csv  Unifica le categorie duplicate di MoneyWiz. Colonna 1 com'e'
                  adesso, colonna 3 come deve diventare. Scrivi IGNORA nella
                  colonna 3 per non far imparare quella categoria: serve per i
                  cassetti dei rifiuti tipo "Casa > Altro", che insegnavano al
                  motore a sbagliare.

L'ID di una transazione e' calcolato dai suoi valori ORIGINALI (conto, data,
importo, descrizione) piu' un contatore per i duplicati esatti. Serve perche'
resti lo stesso anche dopo che ne hai corretto l'importo: altrimenti la
correzione si staccherebbe dalla sua riga alla prima rielaborazione.


DA DOVE VENGONO LE CATEGORIE
----------------------------

Da backup/iMoneyWiz-Data-Backup-*.zip, dal database SQLite: 1635 transazioni
gia' categorizzate a mano, con la gerarchia padre/figlio. Viene usato il backup
piu' recente. Per aggiornare lo storico basta metterne uno piu' nuovo in
backup/.

Le TRANSAZIONI dello storico sono state migrate una volta sola in
export/elaborati/storico-moneywiz.csv con:

    python bilancio.py --migra-storico

Da quel file in poi lo storico serve SOLO come maestro delle categorie. Non
rilanciare la migrazione: creerebbe doppioni. Se serve rifarla, cancella
prima export/elaborati/storico-moneywiz.csv.


VERIFICHE
---------

    python bilancio.py --selfcheck     motore di categorizzazione
    node test_app.js state.json        interfaccia, senza aprire un browser

Per il secondo serve lo stato del server. Scaricalo in una cartella
temporanea, NON qui: sono 534 KB di movimenti bancari e finirebbero su iCloud
senza motivo.

    curl -s http://127.0.0.1:8770/api/state -o "$TEMP/state.json"
    node test_app.js "$TEMP/state.json"


NOTE
----

- Solo 2024 e 2025 hanno dati densi (circa 660 transazioni l'anno). Il 2022 ne
  ha 111 e il 2023 ne ha 166: i trend su quegli anni non valgono.
- transaction_processor.py e' la versione precedente, superata da bilancio.py.
- HANDOFF.md racconta lo stato del lavoro, le decisioni prese e le trappole
  gia' incontrate. Va letto prima di rimetterci mano, e aggiornato dopo.
