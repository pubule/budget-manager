// Controlli sull'interfaccia senza aprire un browser.
//
//     curl -s http://127.0.0.1:8770/api/state -o state.json
//     node test_app.js state.json
//
// Verifica filtri, aggregazioni, grafici, ogni riquadro e ogni scheda
// sui dati veri, piu' la neutralizzazione dell'HTML nelle descrizioni.
const fs = require("fs");
const path = require("path");

const FOLDER = __dirname;

const html = fs.readFileSync(path.join(FOLDER, "app.html"), "utf-8");
const js = html.match(/<script>([\s\S]*)<\/script>/)[1];
// Lo stato arriva dal server se e' acceso, altrimenti da consolidato.csv
// convertito al volo: il test deve poter girare comunque.
const stateFile = process.argv[2] || path.join(FOLDER, "state.json");
if (!fs.existsSync(stateFile)) {
  console.error("serve lo stato: avvia server.py e salva /api/state in " + stateFile);
  console.error("  curl -s http://127.0.0.1:8770/api/state -o state.json");
  process.exit(2);
}
const state = JSON.parse(fs.readFileSync(stateFile, "utf-8"));

// DOM minimo: basta che le funzioni pure girino senza esplodere.
// I nodi si tengono per id invece di crearne uno nuovo a ogni chiamata, cosi'
// si puo' anche RILEGGERE cio' che il codice ci ha scritto: e' l'unico modo di
// verificare l'etichetta dei pulsanti senza aprire un browser.
const nodes = {};
const node = id => (nodes[id] || (nodes[id] = {
  value: "", innerHTML: "", textContent: "", disabled: false, title: "",
  dataset: {}, classList: {toggle(){}, add(){}, remove(){}},
  addEventListener(){}, closest(){ return null; },
}));
global.document = {
  getElementById: node,
  querySelector: node,
  querySelectorAll: () => [],
  addEventListener(){},
};
global.fetch = () => Promise.resolve({json: () => Promise.resolve(state)});
global.setInterval = () => 0;
global.setTimeout = () => 0;
global.clearTimeout = () => {};
// Nessuno stub per alert/confirm/prompt: l'app non li usa piu' e non deve
// tornare a usarli. Se li richiamasse, qui non esistono e il test esplode.

// Espone le funzioni interne per poterle interrogare.
// eval() su codice nostro: serve a raggiungere funzioni che app.html non
// esporta. Nessun dato esterno finisce nella stringa valutata.
const api = eval(`(function(){
${js}
;return {euro, filtered, groupSum, barChart, lineChart, tableHTML,
 CARDS, VIEWS, outflow, inflow, sum, monthsOf, spending, uncategorized,
 setStatus, el, whole, uniq, patternNegozio, meseLeggibile,
 indicatori, spesa, scarto, VISTE, mesiEffettivi, monthsOf, quotaDi,
 confronti, confrontoScelto, finestraConfronto, giorniConDati, formaDelPeriodo,
 setState: s => { S = s; }, getF: () => F,
 QUICK, today, lastDataMonth, monthRange, renderQuick, monthsBetween,
 coverage, mesi};
})()`);

api.setState(state);
let failures = 0;
const check = (label, condition, detail) => {
  if (condition) { console.log(`  ok   ${label}`); }
  else { console.log(`  FALLITO  ${label}${detail ? " -> " + detail : ""}`); failures++; }
};

console.log("=== formattazione ===");
check("euro negativo", api.euro(-1234.6) === "-1.235 €", api.euro(-1234.6));
check("euro positivo", api.euro(2500) === "2.500 €", api.euro(2500));

console.log("=== entrate contro uscite ===");
{
  // inflow e outflow devono SPARTIRSI le righe di spesa, senza sovrapporsi e
  // senza perderne: e' quello che tiene il risparmio uguale comunque si
  // decida cosa sia reddito. Se una riga finisse in tutte e due, o in
  // nessuna, il risparmio mentirebbe senza che niente lo dica.
  const tutte = api.spending(state.transactions);
  const dentro = api.inflow(tutte), fuori = api.outflow(tutte);
  check("entrate e uscite si spartiscono ogni riga",
        dentro.length + fuori.length === tutte.length,
        `${dentro.length} + ${fuori.length} != ${tutte.length}`);
  const ids = new Set(dentro.map(t => t.ID));
  check("nessuna riga sta in tutte e due", !fuori.some(t => ids.has(t.ID)));
  check("il risparmio e' la somma di tutto",
        Math.abs((api.sum(dentro) + api.sum(fuori)) - api.sum(tutte)) < 0.01);
  // Il reddito e' solo quello di natura "Entrate": un rimborso col segno
  // positivo dentro una categoria di spesa non e' guadagno.
  check("il reddito e' solo la natura Entrate",
        dentro.every(t => t.Natura === "Entrate"));
  const rimborsi = fuori.filter(t => t.Importo > 0);
  check("i rimborsi stanno fra le uscite, dove riducono la voce",
        rimborsi.every(t => t.Natura !== "Entrate"),
        `${rimborsi.length} rimborsi`);
}

console.log("=== i mesi contati come sono davvero ===");
{
  // Un agosto fermo al 25 non e' un mese intero. Contandolo per uno, ogni
  // media "al mese" scende: sul mutuo diceva 733 euro invece dei 786 veri, e
  // per capire perche' bisognava aprire il consolidato.
  const mese = r => ({Data: r});
  check("un mese intero vale uno",
        Math.abs(api.mesiEffettivi([mese("2026-01-31")]) - 1) < 0.001,
        String(api.mesiEffettivi([mese("2026-01-31")])));
  const meta = api.mesiEffettivi([mese("2026-08-01"), mese("2026-08-25")]);
  check("un mese fermo al 25 vale 25/31", Math.abs(meta - 25/31) < 0.001,
        String(meta));
  const due = api.mesiEffettivi([mese("2026-07-10"), mese("2026-08-25")]);
  check("solo l'ULTIMO mese si ritaglia", Math.abs(due - (1 + 25/31)) < 0.001,
        String(due));
  // Un mese vuoto in mezzo e' un mese in cui non hai speso, e vale uno:
  // toglierlo alzerebbe la media di un dato che non esiste.
  const buco = api.mesiEffettivi([mese("2026-01-15"), mese("2026-03-31")]);
  check("un mese vuoto in mezzo non si toglie", Math.abs(buco - 2) < 0.001,
        String(buco));
  check("senza righe non si divide per zero", api.mesiEffettivi([]) === 1);
  // Su febbraio il denominatore deve seguire i giorni veri del mese.
  const feb = api.mesiEffettivi([mese("2024-02-14")]);
  check("febbraio bisestile ha 29 giorni", Math.abs(feb - 14/29) < 0.001,
        String(feb));
}

console.log("=== il metro del confronto ===");
{
  const F0 = api.getF();
  const mettiPeriodo = (da, a, confronto) => {
    F0.from = da; F0.to = a; F0.confronto = confronto || "";
  };
  const ultimo = state.transactions.map(t => t.Data).filter(Boolean)
    .reduce((x, y) => x > y ? x : y);
  const mese = ultimo.slice(0, 7);
  const giorno = +ultimo.slice(8, 10);

  // Senza un periodo scelto non c'e' niente con cui confrontare: il riquadro
  // mostra la media su tutto lo storico, e prima di tutto lo storico non c'e'
  // nulla. Nessuna opzione, nessun confronto.
  mettiPeriodo("", "");
  check("senza periodo non ci sono metri", api.confronti().length === 0);
  check("senza periodo non c'e' finestra", api.finestraConfronto() === null);

  // Un mese di calendario: tre metri.
  mettiPeriodo(mese + "-01", mese + "-31");
  const perMese = api.confronti();
  check("per un mese ci sono tre metri", perMese.length === 3,
        perMese.map(o => o.etichetta).join(", "));
  check("il primo e' il mese prima", perMese[0].chiave === "precedente");

  // IL RITAGLIO, che e' il motivo per cui tutto questo esiste. Il periodo ha
  // dati fino al giorno N, quindi il metro dura N giorni dal SUO inizio:
  // agosto fino al 25 si confronta con luglio fino al 25, non con luglio
  // intero. Senza, un mese a meta' sembrava un crollo.
  const durata = api.giorniConDati();
  check("i giorni con dati arrivano all'ultimo movimento", durata === giorno,
        durata + " invece di " + giorno);
  const w = api.finestraConfronto();
  const giorni = Math.round(
    (new Date(w.a + "T00:00:00Z") - new Date(w.da + "T00:00:00Z")) / 86400000) + 1;
  check("il metro dura quanto i dati del periodo", giorni === durata,
        giorni + " giorni invece di " + durata);
  check("il ritaglio viene dichiarato nell'etichetta",
        !w.parziale || /\(1.\d+\)/.test(w.etichetta), w.etichetta);

  // Un periodo COMPLETO non si ritaglia: il caso normale non cambia.
  mettiPeriodo("2025-01-01", "2025-12-31");
  const intero = api.finestraConfronto();
  check("un periodo completo non viene ritagliato",
        !intero.parziale && intero.da === "2024-01-01" && intero.a === "2024-12-31",
        intero.da + " .. " + intero.a);

  // Un anno: due metri, e le medie coprono davvero i mesi che dichiarano.
  const perAnno = api.confronti();
  check("per un anno ci sono due metri", perAnno.length === 2,
        perAnno.map(o => o.etichetta).join(", "));
  mettiPeriodo("2025-01-01", "2025-12-31", "media3");
  const m3 = api.finestraConfronto();
  const mesiCoperti = (+m3.a.slice(0,4)*12 + +m3.a.slice(5,7))
                    - (+m3.da.slice(0,4)*12 + +m3.da.slice(5,7)) + 1;
  check("\"media 3 anni\" copre trentasei mesi", mesiCoperti === 36,
        mesiCoperti + " mesi: " + m3.da + " .. " + m3.a);
  mettiPeriodo(mese + "-01", mese + "-31", "media12");
  const m12 = api.finestraConfronto();
  const mesi12 = (+m12.a.slice(0,4)*12 + +m12.a.slice(5,7))
               - (+m12.da.slice(0,4)*12 + +m12.da.slice(5,7)) + 1;
  check("\"media 12 mesi\" copre dodici mesi", mesi12 === 12,
        mesi12 + " mesi: " + m12.da + " .. " + m12.a);

  // Un intervallo qualsiasi: un metro solo.
  mettiPeriodo("2026-03-10", "2026-05-20");
  check("per un intervallo a mano c'e' un metro solo",
        api.confronti().length === 1);

  // Cambiando periodo una scelta non piu' valida non resta appesa.
  mettiPeriodo(mese + "-01", mese + "-31", "media12");
  check("la scelta vale finche' esiste",
        api.confrontoScelto().chiave === "media12");
  mettiPeriodo("2025-01-01", "2025-12-31", "media12");
  check("una scelta non piu' valida ricade sulla prima",
        api.confrontoScelto().chiave === api.confronti()[0].chiave,
        api.confrontoScelto().chiave);

  F0.from = ""; F0.to = ""; F0.confronto = "";
}

console.log("=== le due letture ===");
{
  const F6 = api.getF();
  const quota = (pagante, q) => api.quotaDi({"Pagato da":pagante, Quota:q});
  F6.lettura = "";
  check("nella lettura predefinita ogni riga pesa per intero",
        quota("io","meta") === 1 && quota("","") === 1);
  F6.lettura = "mia";
  check("a meta' la riga pesa la meta'", quota("io","meta") === 0.5);
  check("una riga non condivisa pesa uguale in tutte e due le letture",
        quota("","") === 1);
  // Ho pagato io e la quota e' tutta sua: di quella spesa non e' mio niente.
  check("quota intera a carico suo: non e' mia", quota("io","tutto") === 0);
  // Ha pagato lei e la quota e' tutta mia: e' mia per intero.
  check("quota intera a carico mio: e' tutta mia", quota("lei","tutto") === 1);
  check("un rimborso non e' una spesa e non pesa", quota("lei","saldo") === 0);
  F6.lettura = "";
}

console.log("=== filtri ===");
const all = api.filtered();
check("nessun filtro restituisce tutto", all.length === state.transactions.length,
      `${all.length} vs ${state.transactions.length}`);
const F = api.getF();
// La natura si sceglie dai dati, non a mano: con un dataset piccolo o diverso
// un nome fisso farebbe fallire il test senza che ci sia niente di rotto.
const nature = [...new Set(all.map(t => t.Natura))].filter(Boolean);
const scelta = nature.find(n => all.filter(t => t.Natura === n).length < all.length)
               || nature[0];
F.nature = scelta;
const vinc = api.filtered();
check(`filtro natura riduce (${scelta})`,
      vinc.length > 0 && vinc.length < all.length, `${vinc.length}/${all.length}`);
check("filtro natura e' esatto", vinc.every(t => t.Natura === scelta));
F.nature = ""; F.from = "2025-01-01"; F.to = "2025-12-31";
const y2025 = api.filtered();
check("filtro periodo", y2025.every(t => t.Data >= "2025-01-01" && t.Data <= "2025-12-31"),
      `${y2025.length} righe`);
F.from = ""; F.to = "";

console.log("=== filtri rapidi ===");
const oggi = api.today();
const ultimo = api.lastDataMonth();
console.log(`  oggi: ${oggi.iso}  ·  ultimo movimento: ${ultimo.iso}`);
const vero = new Date();
check("il riferimento e' il calendario, non l'ultimo dato",
      oggi.y === vero.getFullYear() && oggi.m === vero.getMonth()+1,
      `${oggi.y}-${oggi.m}`);
check("l'ultimo mese con dati e' una cosa separata",
      ultimo.iso === state.transactions.map(t => t.Data).filter(Boolean)
                    .reduce((x, y) => x > y ? x : y), ultimo.iso);
// Il caso che ha fatto emergere il difetto: dati fermi a gennaio, oggi agosto.
// "Mese in corso" deve dare il mese di OGGI, anche se e' vuoto.
const [mc] = api.QUICK[0][1]();
check("mese in corso segue il calendario",
      mc === `${oggi.y}-${String(oggi.m).padStart(2,"0")}-01`, mc);

// Il cambio d'anno e' il caso che rompe l'aritmetica dei mesi fatta a mano.
const [g1, g2] = api.monthRange(2026, 1, -1);
check("mese precedente attraversa l'anno", g1 === "2025-12-01" && g2 === "2025-12-31",
      `${g1} .. ${g2}`);
const [f1, f2] = api.monthRange(2024, 2, 0);
check("febbraio bisestile ha 29 giorni", f2 === "2024-02-29", f2);
const [n1, n2] = api.monthRange(2025, 2, 0);
check("febbraio normale ha 28 giorni", n2 === "2025-02-28", n2);

for(const [label, range] of api.QUICK){
  const [from, to] = range();
  if(!from) continue;
  const F2 = api.getF();
  F2.from = from; F2.to = to;
  const rows = api.filtered();
  const fuori = rows.filter(t => t.Data < from || t.Data > to);
  check(`${label}: ${from} .. ${to}, ${rows.length} righe`,
        from <= to && !fuori.length, `${fuori.length} righe fuori intervallo`);
  F2.from = ""; F2.to = "";
}
// "Ultimi 3 mesi" deve contenere davvero tre mesi, non due o quattro.
const tre = api.QUICK[2][1]();
check("ultimi 3 mesi coprono 3 mesi",
      (+tre[1].slice(0,4)*12 + +tre[1].slice(5,7))
      - (+tre[0].slice(0,4)*12 + +tre[0].slice(5,7)) === 2,
      `${tre[0]} .. ${tre[1]}`);

const [ud1, ud2] = api.QUICK[5][1]();
const Fu = api.getF(); Fu.from = ud1; Fu.to = ud2;
const conDati = api.filtered();
Fu.from = ""; Fu.to = "";
check(`"Ultimo mese con dati" trova righe (${ud1} .. ${ud2})`, conDati.length > 0,
      String(conDati.length));

console.log("=== periodo chiesto contro periodo coperto ===");
check("mesi in un anno pieno", api.monthsBetween("2026-01-01","2026-12-31") === 12,
      String(api.monthsBetween("2026-01-01","2026-12-31")));
check("mesi a cavallo d'anno", api.monthsBetween("2025-11-01","2026-01-31") === 3,
      String(api.monthsBetween("2025-11-01","2026-01-31")));
check("un solo mese", api.monthsBetween("2026-01-01","2026-01-31") === 1);
check("intervallo rovesciato da 0", api.monthsBetween("2026-05-01","2026-01-31") === 0);
check("plurale corretto", api.mesi(1) === "1 mese" && api.mesi(3) === "3 mesi",
      api.mesi(1) + " / " + api.mesi(3));

// Il caso segnalato: anno in corso con dati solo a gennaio non deve sembrare
// un filtro rotto, deve dire che l'anno e' appena cominciato.
const Fc = api.getF();
const [ay, az] = api.QUICK[3][1]();
Fc.from = ay; Fc.to = az;
const annoRows = api.filtered();
const annoMesi = [...new Set(annoRows.map(t => (t.Data||"").slice(0,7)))].filter(Boolean).sort();
const testo = api.coverage(annoRows, annoMesi);
console.log("  " + testo.trim());
check("dichiara il periodo chiesto", testo.includes(ay) && testo.includes(az));
check("dichiara quanti mesi di dati ci sono davvero",
      annoMesi.length === 12 || testo.includes("dati presenti per"));
check("avverte sul denominatore delle medie",
      annoMesi.length === 12 || testo.includes("Le medie mensili usano"));
Fc.from = ""; Fc.to = "";
check("senza filtro non parla di periodo chiesto",
      !api.coverage(api.filtered(), annoMesi).includes("periodo chiesto"));

console.log("=== aggregazioni ===");
const out = api.outflow(all);
const totale = api.sum(out);
check("i giroconti sono esclusi dalle uscite",
      !api.spending(all).some(t => t.Natura === "Non spesa"));
const perNatura = api.groupSum(out, "Natura");
const somma = perNatura.reduce((a, g) => a + g.total, 0);
check("quadratura per natura", Math.abs(somma - totale) < 0.01,
      `${somma.toFixed(2)} vs ${totale.toFixed(2)}`);
console.log(`  uscite ${totale.toFixed(2)} su ${api.monthsOf(api.spending(all))} mesi`);
for (const g of perNatura) console.log(`    ${g.name.padEnd(16)} ${g.total.toFixed(0)}`);

console.log("=== grafici ===");
const bars = api.barChart(perNatura.map(g => ({name: g.name, value: g.total})),
                          {drill: "nature"});
// Le barre sono HTML, non SVG: un viewBox fisso rimpicciolirebbe anche le
// etichette quando il riquadro si stringe.
check("barChart produce HTML", bars.startsWith('<div class="bars">')
      && bars.endsWith("</div>"));
check("barChart ha una barra per voce",
      (bars.match(/class="bar-track"/g) || []).length === perNatura.length);
check("barChart marca le zone cliccabili", bars.includes('data-drill="nature"'));
// Una polilinea di un punto solo non disegna NIENTE: filtrando su un mese il
// riquadro dell'andamento restava vuoto pur avendo i dati sotto.
{
  const unMese = api.lineChart(["2026-08"], [{name:"x", values:[100], color:"#000"}]);
  check("con un punto solo il grafico disegna comunque qualcosa",
        unMese.includes("<circle"));
  const tanti = api.lineChart(
    Array.from({length: 24}, (_, i) => "2025-" + i),
    [{name:"x", values: Array(24).fill(10), color:"#000"}]);
  check("con molti punti resta una linea pulita, senza pallini",
        !tanti.includes("<circle"));
  const F3 = api.getF();
  const [da, a] = api.QUICK[0][1]();          // mese in corso
  F3.from = da; F3.to = a;
  // L'andamento guarda lo storico anche con un periodo scelto: ritagliarlo al
  // mese lo riduceva a un punto, cioe' a niente. Il periodo diventa una fascia.
  const reso = api.CARDS.andamento(api.filtered());
  const mesiStorici = api.uniq(api.spending(state.transactions)
    .map(t => (t.Data||"").slice(0,7))).length;
  check("l'andamento resta sullo storico anche con un mese scelto",
        (reso.match(/<text[^>]*class="axis"[^>]*text-anchor/g)||[]).length > 1,
        "un punto solo su " + mesiStorici + " mesi");
  check("il periodo scelto si vede come fascia", reso.includes("class=\"fascia\""));
  F3.from = ""; F3.to = "";
  const senza = api.CARDS.andamento(api.filtered());
  check("senza periodo non c'e' nessuna fascia", !senza.includes("class=\"fascia\""));

  // Il valore sotto al mouse: una fascia per mese, non solo il pallino, cosi'
  // basta avvicinarsi e funziona anche dove i pallini non ci sono.
  const dodici = api.lineChart(
    ["2026-01","2026-02","2026-03"],
    [{name:"entrate", values:[100,200,300], color:"#000"},
     {name:"uscite", values:[-50,-60,-70], color:"#111"}]);
  const fasce = (dodici.match(/data-tip="/g) || []).length;
  check("una fascia sensibile al mouse per ogni mese", fasce === 3, String(fasce));
  check("la fascia riporta il mese per esteso e tutte le serie",
        dodici.includes("gennaio 2026") && dodici.includes("entrate:")
        && dodici.includes("uscite:"));
  check("la fascia e' invisibile ma cliccabile dal mouse",
        dodici.includes('fill="transparent"'));
  // Anche a cinquantasei mesi, dove i pallini non si disegnano.
  const lungo = api.lineChart(
    Array.from({length: 30}, (_, i) => "2024-" + String((i % 12) + 1).padStart(2, "0")),
    [{name:"x", values: Array(30).fill(10), color:"#000"}]);
  check("le fasce ci sono anche senza pallini",
        !lungo.includes("<circle") && (lungo.match(/data-tip="/g) || []).length === 30);
  check("il mese per esteso regge un'etichetta strana",
        api.meseLeggibile("boh") === "boh" && api.meseLeggibile("") === "");
  // La riga verticale spiega perche' il tooltip porta TUTTE le serie: legge
  // la colonna del mese, non il punto piu' vicino.
  check("c'e' una riga verticale, una sola per grafico",
        (dodici.match(/class="guida hide"/g) || []).length === 1);
  check("ogni fascia sa dove mettere la riga",
        (dodici.match(/data-x="/g) || []).length === 3);
  check("la riga parte nascosta", dodici.includes('class="guida hide"'));
}
check("le barre sono in percentuale, cosi' si adattano al riquadro",
      /width:\d+(\.\d+)?%/.test(bars));
check("la barra piu' lunga arriva al 100%", bars.includes("width:100.0%"));
const line = api.lineChart(["2025-01", "2025-02", "2025-03"],
  [{name: "x", values: [10, 40, 20], color: "#000"}]);
check("lineChart produce SVG", line.includes("<polyline"));
check("lineChart non produce NaN", !/NaN/.test(line) && !/NaN/.test(bars));

console.log("=== riquadri ===");
for (const [id, fn] of Object.entries(api.CARDS)) {
  let out2 = "";
  try { out2 = fn(all); } catch (e) { out2 = "ERRORE: " + e.message; }
  check(`riquadro ${id}`, out2.length > 20 && !out2.startsWith("ERRORE"),
        out2.slice(0, 90));
}

console.log("=== schede ===");
for (const [id, fn] of Object.entries(api.VIEWS)) {
  let out3 = "";
  try { out3 = fn(all); } catch (e) { out3 = "ERRORE: " + e.message; }
  check(`scheda ${id}`, out3.length > 20 && !out3.startsWith("ERRORE"),
        out3.slice(0, 120));
}

console.log("=== iniezione HTML ===");
const evil = {...state.transactions[0], Descrizione: '<img src=x onerror=alert(1)>',
              Merchant: '"><script>bad()</script>'};
api.setState({...state, transactions: [evil]});
const rendered = api.VIEWS.transazioni(api.filtered());
check("la descrizione ostile viene neutralizzata",
      !rendered.includes("<img src=x") && !rendered.includes("<script>bad"));

// Il conteggio sul pulsante "Analizza con LLM". Conta le righe senza
// categoria, comprese quelle a stringa vuota: il consolidato le scrive vuote,
// il payload del server le manda come null, e il pulsante deve dire lo stesso
// numero in entrambi i casi.
check("il conteggio per l'LLM prende vuoti e null",
      api.uncategorized([{Categoria: "Alimentari"}, {Categoria: ""},
                         {Categoria: null}, {}]).length === 3);
api.setState(state);
check("nessuna riga categorizzata finisce nel conteggio per l'LLM",
      api.uncategorized(state.transactions).every(t => !t.Categoria));

// Il pulsante dell'LLM sui dati veri: il numero deve essere quello, e a zero
// deve spegnersi invece di sparire.
const aperte = api.uncategorized(state.transactions).length;
api.setStatus();
// A zero il numero sparisce dall'etichetta ma il pulsante resta: "(0)" e'
// rumore, e il pulsante che se ne va confonderebbe.
check("il pulsante dell'LLM porta il conteggio vero",
      api.el("btn-llm").textContent === (aperte
        ? `Analizza con LLM (${aperte})` : "Analizza con LLM"),
      api.el("btn-llm").textContent + " con " + aperte + " aperte");
check("il pulsante dell'LLM e' acceso se c'e' da lavorare",
      api.el("btn-llm").disabled === !aperte);
api.setState({...state, transactions: state.transactions.filter(t => t.Categoria)});
api.setStatus();
check("senza righe scoperte il pulsante dell'LLM si spegne ma resta",
      api.el("btn-llm").disabled === true
      && api.el("btn-llm").textContent === "Analizza con LLM",
      api.el("btn-llm").textContent);
api.setState({...state, running: true});
api.setStatus();
check("durante un giro i pulsanti sono tutti spenti",
      api.el("btn-llm").disabled && api.el("btn-run").disabled
      && api.el("btn-load").disabled);
api.setState(state);

// I due livelli della categoria. whole() ricompone il nome intero, che resta
// la chiave con cui il browser parla col server: se si rompe, il menu di
// correzione seleziona la voce sbagliata e gli override finiscono altrove.
check("il nome intero si ricompone dai due livelli",
      api.whole({Categoria:"Casa", Sottocategoria:"Casalinghi"}) === "Casa > Casalinghi");
check("senza sottocategoria resta la sola area",
      api.whole({Categoria:"Stipendio", Sottocategoria:""}) === "Stipendio");
check("senza sottocategoria definita non spunta 'undefined'",
      api.whole({Categoria:"Stipendio"}) === "Stipendio");
check("una riga senza categoria da' stringa vuota", api.whole({}) === "");
{
  // Un livello solo ce l'hanno le voci che non sono spese vere: la natura le
  // descrive gia'. Tutte le altre devono avere il dettaglio, altrimenti la
  // classifica per voce rimette insieme cose che non c'entrano.
  const UNLIVELLO = new Set(["Stipendio", "Affitti incassati", "Giroconto",
                             "Da identificare"]);
  const orfane = state.transactions
    .filter(t => t.Categoria && !t.Sottocategoria && !UNLIVELLO.has(t.Categoria));
  check("solo le voci che non sono spese stanno a un livello",
        orfane.length === 0,
        orfane.length + " righe di spesa senza sottocategoria");
  // Una categoria non deve chiamarsi come una natura: comparirebbe due volte
  // nella barra dei filtri, in due menu diversi, e sembrerebbe un errore.
  const nature = new Set(state.transactions.map(t => t.Natura).filter(Boolean));
  const scontri = api.uniq(state.transactions.map(t => t.Categoria))
    .filter(c => c && nature.has(c));
  check("nessuna categoria si chiama come una natura", scontri.length === 0,
        scontri.join(", "));
  const conSotto = state.transactions.filter(t => t.Sottocategoria);
  check("il nome intero porta il separatore quando i livelli sono due",
        conSotto.length === 0 || api.whole(conSotto[0]).includes(" > "));
}

// Il pannello: il primo riquadro acceso prende la colonna larga, gli altri si
// impilano a destra. E' la regola che tiene le due colonne alte uguali; se
// saltasse, tornerebbero i quattrocento pixel di vuoto fra due riquadri
// affiancati che questa disposizione esiste per togliere.
{
  const html = api.VIEWS.dashboard(api.filtered());
  check("la dashboard e' un pannello a due colonne",
        html.startsWith('<div class="pannello">'));
  check("c'e' una pila a destra", html.includes('<div class="pila">'));
  const accesi = state.layout.filter(c => c.visibile && api.CARDS[c.id]);
  const quadri = (html.match(/<div class="card">/g) || []).length;
  check("ogni riquadro acceso viene disegnato una volta sola",
        quadri === accesi.length, quadri + " invece di " + accesi.length);
  // Il primo riquadro sta FUORI dalla pila: e' quello che porta il peso.
  check("il primo riquadro non finisce nella pila",
        html.indexOf('<div class="card">') < html.indexOf('<div class="pila">'));
}

// Cinque riquadri sono diventati uno con tre viste. Le viste devono guardare
// gli stessi euro da lati diversi, non essere tre tabelle scollegate: se una
// vista sparisse o cambiasse nome, i pulsanti resterebbero e non farebbero
// niente.
{
  const F5 = api.getF();
  const righe = api.filtered();
  for(const [chiave, etichetta] of api.VISTE){
    F5.vista = chiave;
    const html = api.CARDS.dove(righe);
    check(`la vista "${etichetta}" disegna righe`,
          html.includes("<tbody>") && html.includes("</tr>"));
    check(`la vista "${etichetta}" e' quella accesa`,
          html.includes(`data-vista="${chiave}" class="on"`));
  }
  F5.vista = "";
  check("senza scelta si parte dalla vista per area",
        api.CARDS.dove(righe).includes('data-vista="area" class="on"'));
  // Una vista inventata non deve svuotare il riquadro.
  F5.vista = "non esiste";
  check("una vista sconosciuta ricade su quella per area",
        api.CARDS.dove(righe).includes('data-vista="area" class="on"'));
  F5.vista = "";
}

// Nessun segno meno sulle spese: la colonna dice gia' che sono uscite, e il
// meno su ogni riga non aggiunge niente da leggere. E nessuna percentuale
// fuori scala: -943% non e' informazione, "da 110 a 1.152 euro" si'.
{
  check("una spesa si scrive senza il meno",
        api.spesa(-1234) === api.spesa(1234) && !api.spesa(-1234).includes("-"),
        api.spesa(-1234));
  const cresciuta = api.scarto(-100, -180);
  check("una spesa cresciuta si dice a parole",
        cresciuta.verso === "peggio" && cresciuta.testo.includes("in piu'"),
        JSON.stringify(cresciuta));
  const calata = api.scarto(-180, -100);
  check("una spesa calata si dice a parole",
        calata.verso === "meglio" && calata.testo.includes("in meno"),
        JSON.stringify(calata));
  const fuoriScala = api.scarto(-110, -1152);
  check("uno scarto fuori scala dice da dove a dove",
        fuoriScala.testo.includes("da ") && !fuoriScala.testo.includes("%"),
        fuoriScala.testo);
  check("due periodi uguali non inventano uno scarto",
        api.scarto(-100, -100).verso === "pari");
  check("senza spesa in nessuno dei due periodi non c'e' scarto",
        api.scarto(0, 0) === null);
}

// Gli indicatori vivono nella barra: tornano celle, non un riquadro. Se
// tornassero di nuovo un <div class="kpis"> finirebbero dentro alla barra
// come un blocco solo e la riga si spezzerebbe.
{
  const celle = api.indicatori(api.filtered());
  check("gli indicatori tornano celle, non un riquadro",
        celle.startsWith('<div class="kpi') && !celle.includes('class="kpis"'));
  check("gli indicatori non portano note a pie' di pagina",
        !celle.includes('class="note"'));
}

// I campi correggibili sono un contratto fra due file: l'interfaccia scrive
// data-fix="Conto", il motore accetta CORREGGIBILI in bilancio.py. Se uno dei
// due cambia senza l'altro, la modifica si salva e non succede niente - e
// nessun errore lo dice.
{
  const ATTESI = ["Data", "Descrizione", "Merchant", "Importo", "Conto"];
  const usati = [...new Set([...html.matchAll(/data-fix="([^"]+)"/g)]
    .map(m => m[1]))].sort();
  check("l'interfaccia corregge esattamente i campi che il motore accetta",
        JSON.stringify(usati) === JSON.stringify([...ATTESI].sort()),
        "trovati: " + usati.join(", "));
  const py = fs.readFileSync(path.join(FOLDER, "bilancio.py"), "utf-8");
  const riga = py.match(/^CORREGGIBILI = \(([^)]*)\)/m);
  const motore = riga ? [...riga[1].matchAll(/"([^"]+)"/g)].map(m => m[1]).sort() : [];
  check("e il motore accetta esattamente quelli",
        JSON.stringify(motore) === JSON.stringify([...ATTESI].sort()),
        "bilancio.py: " + motore.join(", "));
  check("la natura non e' fra i campi correggibili a mano",
        !motore.includes("Natura") && !usati.includes("Natura"));
}

// La scheda Regole scrive il nome INTERO della categoria: nel file sta su due
// colonne ed e' il server a dividerlo. Mostrare la sola area faceva sparire la
// sottocategoria al primo salvataggio, senza nessun errore.
{
  const conSotto = state.rules.find(r => r.sottocategoria);
  const reso = api.VIEWS.regole();
  if (conSotto) {
    // Il nome finisce dentro un attributo HTML, quindi passa da esc():
    // il confronto va fatto sulla forma sfuggita, non su quella grezza.
    const intero = conSotto.categoria + " > " + conSotto.sottocategoria;
    const atteso = intero.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                         .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
    check("la scheda Regole mostra la categoria intera",
          reso.includes('value="' + atteso + '"'), "cercavo " + atteso);
  } else {
    check("la scheda Regole mostra la categoria intera", true, "nessuna regola a due livelli");
  }
  // I riquadri di dialogo sono nostri. Quelli del browser ignorano il tema,
  // non stanno dietro a un messaggio di piu' di una riga, e bloccano la
  // pagina in un modo che nemmeno si puo' provare da qui.
  const sistema = [...js.matchAll(/(?:^|[^.\w])(alert|confirm|prompt)\s*\(/g)]
    .map(m => m[1]);
  check("nessun popup del browser", sistema.length === 0, sistema.join(", "));
  for (const nome of ["modale", "avvisa", "chiedi", "domanda"])
    check(`c'e' ${nome}()`, new RegExp("(function|const) " + nome + "\\b").test(js));
  check("il riquadro di dialogo esiste nel documento",
        html.includes('<dialog id="modale"'));
  check("il testo del dialogo va nel DOM come testo, non come HTML",
        js.includes('el("modale-testo").textContent'));
  // La promessa non deve dipendere dall'evento "close": su alcuni browser non
  // arriva, e la pagina resterebbe bloccata dietro a un riquadro immobile.
  // Verificato in pagina: ne' "close" ne' "cancel" scattano in quel caso.
  check("la risposta non dipende dall'evento close",
        !/addEventListener\(\s*["']close["']/.test(js)
        && !/\.onclose\s*=/.test(js));
  check("Esc chiude anche senza l'evento cancel",
        /onkeydown[\s\S]{0,200}Escape/.test(js));
  // Il pulsante "regola" deve bastare a se stesso: leggere la categoria dal
  // menu accanto era un vicolo cieco, perche' sceglierla li' salva subito e
  // la riga esce dalla coda portandosi via il pulsante.
  check("il pulsante regola chiede lui la categoria",
        js.includes("valore: gia || SCEGLI, scelte"));
  check("non preseleziona una categoria a caso",
        /scegli una categoria/.test(js));
  // Il nome del negozio salta le parole vuote, quindi cercarlo intero dentro
  // la descrizione non trova niente: "Zoom Progress" contro "ZOOM IN PROGRESS".
  {
    const righe = state.transactions;
    // Il nome salta le parole vuote: "Zoom Progress" non e' dentro "ZOOM IN
    // PROGRESS", quindi le parole vanno unite con .*
    check("il pattern aggancia anche col nome accorciato",
          new RegExp(api.patternNegozio("Zoom Progress",
            "ZOOM IN PROGRESS SRL CUMIANA"), "i")
            .test("ZOOM IN PROGRESS SRL CUMIANA"));
    // E quando il nome viene da merchant.csv puo' non somigliare affatto alla
    // descrizione: li' si ripiega sulle parole della descrizione.
    check("il pattern si ripiega quando il nome canonico non compare",
          new RegExp(api.patternNegozio("NordVPN",
            "PAYPAL *NORDSEC BV Amsterdam"), "i")
            .test("PAYPAL *NORDSEC BV Amsterdam"));
    // La prova che conta: premendo "regola" su una riga qualsiasi, la regola
    // deve almeno agganciare quella riga. Altrimenti si crea una regola inerte.
    const inerti = righe.filter(t => t.Merchant).filter(t => {
      const p = api.patternNegozio(t.Merchant, t.Descrizione);
      try { return !new RegExp(p, "i").test(t.Descrizione || ""); }
      catch (e) { return true; }
    });
    check("una regola creata da una riga aggancia almeno quella riga",
          inerti.length === 0,
          inerti.slice(0, 4).map(t => t.Merchant + " / " + t.Descrizione).join(" | "));
  }

  check("nessuna regola perde la sottocategoria",
        state.rules.every(r => !r.categoria
          || ["Giroconto","Stipendio","Affitti incassati","Da identificare"]
             .includes(r.categoria) || r.sottocategoria),
        state.rules.filter(r => r.categoria && !r.sottocategoria)
          .map(r => r.categoria).join(", "));
}

console.log(failures ? `\n${failures} controlli falliti` : "\nTutti i controlli passati");
process.exit(failures ? 1 : 0);
