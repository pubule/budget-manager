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
global.alert = m => console.log("  alert:", m);
global.prompt = () => null;

// Espone le funzioni interne per poterle interrogare.
// eval() su codice nostro: serve a raggiungere funzioni che app.html non
// esporta. Nessun dato esterno finisce nella stringa valutata.
const api = eval(`(function(){
${js}
;return {euro, filtered, groupSum, barChart, lineChart, tableHTML,
 CARDS, VIEWS, outflow, inflow, sum, monthsOf, spending, uncategorized,
 setStatus, el, whole,
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
check("barChart produce SVG", bars.startsWith("<svg") && bars.endsWith("</svg>"));
check("barChart ha una barra per voce",
      (bars.match(/<rect/g) || []).length === perNatura.length);
check("barChart marca le zone cliccabili", bars.includes('data-drill="nature"'));
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
check("il pulsante dell'LLM porta il conteggio vero",
      api.el("btn-llm").textContent === `Analizza con LLM (${aperte})`,
      api.el("btn-llm").textContent);
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
  const orfane = state.transactions.filter(t => t.Categoria && !t.Sottocategoria);
  check("nei dati veri ogni riga categorizzata ha due livelli",
        orfane.length === 0,
        orfane.length + " righe con categoria ma senza sottocategoria");
  const conSotto = state.transactions.filter(t => t.Sottocategoria);
  check("il nome intero porta il separatore quando i livelli sono due",
        conSotto.length === 0 || api.whole(conSotto[0]).includes(" > "));
}

console.log(failures ? `\n${failures} controlli falliti` : "\nTutti i controlli passati");
process.exit(failures ? 1 : 0);
