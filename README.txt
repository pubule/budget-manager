BILANCIO
========

    python server.py

Apre la dashboard nel browser. Da li' si fa tutto: correggere le categorie,
gestire la tassonomia, scrivere le regole, filtrare i grafici.

Sorveglia la cartella: appena copi dentro un export nuovo, rielabora da solo
entro pochi secondi. Non serve lanciare nessun comando.

Il server ascolta SOLO su 127.0.0.1. Sono dati bancari e non devono essere
raggiungibili dalle altre macchine di casa. I dati non escono mai dal PC:
MoneyWiz e' letto in locale, Ollama gira in locale, la pagina non carica
niente dalla rete.

Serve ancora la riga di comando? C'e':

    python bilancio.py                 elabora e rigenera dashboard.html/.xlsx
    python bilancio.py --da-storico    usa MoneyWiz invece degli export
    python bilancio.py --no-llm        salta Ollama
    python bilancio.py --selfcheck     verifica che il motore non sia rotto


LE SCHEDE
---------

Dashboard          Indicatori e grafici. La barra in alto filtra per periodo,
                   conto, natura, categoria e testo, e i grafici si aggiornano
                   con lei. Cliccare una barra filtra tutto su quella voce.

                   I filtri rapidi (mese in corso, mese scorso, ultimi 3 mesi,
                   anno in corso, anno scorso) fanno da interruttore: premere
                   di nuovo lo stesso pulsante toglie il filtro.

                   ATTENZIONE al riferimento: "mese in corso" NON e' il mese
                   di oggi, e' il mese dell'ultima transazione che hai. Se gli
                   export sono fermi a gennaio, contare da oggi darebbe sempre
                   zero righe. Quando le due date non coincidono la barra lo
                   scrive: "riferimento: ultima transazione ...".

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

Finche' non ci sono export nella cartella, la dashboard lavora sullo storico
MoneyWiz, cosi' c'e' subito qualcosa da guardare. Appena arriva un export
passa a quello.


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
