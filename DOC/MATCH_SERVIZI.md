# Come avviene il match tra `service_name` e i monitor di Uptime Kuma

Documento di diagnosi, aggiornato al 2026-10-12. Descrive i passaggi **nell'ordine in cui il codice li esegue** (`uptime_kuma_connector.py` → `kuma_client.py`), per capire perché una richiesta non trova il monitor atteso.

## Visione d'insieme

```
service_name (dal modello)
   │
   ▼
[0] Configurazione e lettura di /metrics ──── errore ──────────────► unknown
   │
   ▼
[1] Parsing di /metrics: elenco monitor {id → nome} e {id → stato}
   │            nessun monitor valido ─────────────────────────────► unknown
   ▼
[2] Pulizia della richiesta ───────────────── vuota ───────────────► ambiguous
   │
   ▼
[3] Alias ────── trovato ──► monitor degli id indicati (fine ricerca)
   │ no
   ▼
[4] Nome esatto ─ trovato ─► (fine ricerca)
   │ no
   ▼
[5] Contenimento ─ trovato ─► (fine ricerca)
   │ no
   ▼
[6] Fallback senza parole generiche ─ trovato ─► (fine ricerca)
   │ no
   ▼
nessun match ──────────────────────────────────────────────────────► not_monitored
```

La ricerca si ferma **al primo passaggio che trova almeno un monitor**. I passaggi successivi non vengono eseguiti.

## Passaggi e verifiche

### 0. Prima del match
- La configurazione deve avere URL e API key. Se manca una delle due, l'esito è `unknown`.
- La lettura di `GET /metrics` può fallire per timeout, DNS, risposta non 2xx, risposta troppo grande, codifica diversa da identity o troppe richieste in corso. Ogni errore dà `unknown` e il log riporta il tipo di errore: `could not read the monitoring endpoint (...)`.

### 1. Parsing di `/metrics`
Per ogni riga `monitor_status{...} valore` si ricavano id, nome e stato. **Una riga viene scartata in silenzio**, e quel monitor non esiste più per il match, se:
- non è una riga `monitor_status{` ben formata, o le etichette sono malformate;
- manca `monitor_id` o `monitor_name`, oppure l'id non è un intero;
- il valore non è un numero intero (`NaN`, `Inf`, `1.5`);
- il nome è vuoto, è più lungo di 160 caratteri o contiene caratteri di controllo o di formattazione.

Un monitor in pausa non compare in `/metrics`. Se nessuna riga è valida, l'esito è `unknown` e il log scrive `the monitoring endpoint answered but no readable monitor line was found` (non `request succeeded`, non `no monitor matches`): il problema è la risposta, non il nome cercato.

### 2. Pulizia della richiesta
- Caratteri non sicuri sostituiti da spazio, poi **troncamento a 80 caratteri**.
- **Forma normalizzata** (`normalise_name`): toglie `http://` e `https://`, trasforma `-`, `.`, `_` in spazi, comprime gli spazi, minuscolo. Esempio: `Portale-XX` → `portale xx`.
- **Forma fusa** (`fuse_name`): toglie `http(s)://`, **elimina** `-`, `.`, `_`, comprime gli spazi, minuscolo. Esempio: `U-GOV - Autenticazione` → `ugov autenticazione`.
- In **entrambe** le forme parentesi, slash, virgole, due punti e simili (`() [] {} <> / \ , ; : | " + & ! ?`) diventano spazi: sono confini di parola. Esempio: `Easy academy (planning)` → `easy academy planning`.
- Le stesse due forme si calcolano sul nome di ogni monitor, e ogni confronto sotto si fa su entrambe.
- Se la forma normalizzata è vuota, l'esito è `ambiguous` ("la richiesta non indica alcun servizio").

### 3. Alias (impostazioni → mappa alias)
- Confronto **esatto**, su una chiave senza spazi (`_squash`): `Portale Demo` e `portale-demo` sono la stessa chiave, ma `Portale Demo` non raggiunge `Portale Demo Test`.
- Ogni riga della mappa è divisa all'**ultimo** `:`: un alias può essere un indirizzo con lo schema (`https://www.sns.it: 42` equivale a `www.sns.it: 42`). Un altro `:` sul lato dell'alias rende la riga malformata: viene scartata e il log scrive `alias map, line N: ...`.
- Se la chiave esiste, **la ricerca per nome non viene eseguita**: valgono solo gli id dell'alias.
- Id assenti da `/metrics` (monitor in pausa, scartato al passaggio 1, id sbagliato):
  - nessun id presente → `unknown`, non `not_monitored`;
  - alcuni presenti → i presenti vengono usati, e il log scrive una volta `alias '...' refers to monitor ids absent from /metrics`.
- Più di 10 monitor → `ambiguous`.

### 4. Nome esatto
Il monitor corrisponde se il suo nome normalizzato **o** fuso è uguale a quello della richiesta. Evita che un nome esatto sia oscurato da uno più lungo.

### 5. Contenimento
Per ogni monitor, su forma normalizzata **e** su forma fusa, c'è match se vale una di queste:
- una frase è contenuta nell'altra **su confini di parola**, e **entrambe** hanno almeno 3 caratteri: `easy academy` sta in `easy academy planning`, ma `mail` non sta in `gmail` né `esse` in `esse3`;
- l'insieme delle parole di una è incluso in quello dell'altra, in qualsiasi ordine (`autenticazione ugov` ↔ `ugov autenticazione`). Sotto i 3 caratteri conta solo la parola intera (`pa` trova `PA`, non `archivio`).

Un nome di monitor che dopo la normalizzazione è vuoto (solo punteggiatura) non viene mai confrontato.

### 6. Fallback senza parole generiche
Si arriva qui solo se i passaggi 4 e 5 non hanno trovato nulla. Dalla richiesta si tolgono le parole generiche, **monitor per monitor**, e si rifà il contenimento.
- Lista chiusa italiano e inglese: `programma, portale, sito, servizio, sistema, piattaforma, applicazione, applicativo, pagina, program, portal, site, website, service, system, platform, application, app, page`, più articoli e preposizioni (`il, lo, la, di, del, the, of, my, for…`).
- Una parola generica **presente nel nome di quel monitor resta**.
- Se restano solo parole generiche, o non ne è stata tolta nessuna, **non c'è match**.
- Una parola che non è nella lista **non viene mai tolta**.
- Il log scrive `resolution: fallback`.

## Esito finale

| Monitor trovati | Esito |
| --- | --- |
| 0 | `not_monitored` ("non concluderne che il servizio funzioni") |
| 1–5 (10 con alias) | `known`, uno stato per controllo (`controllo 1`, `controllo 2`, …) |
| più del limite | `ambiguous` |
| almeno uno con codice di stato non riconosciuto (≠ 0, 1, 2, 3) | `unknown` |

## Come ricostruire un mancato match

1. **Guarda cosa ha passato il modello.** Il log scrive `no monitor matches '<testo>'` con il valore ricevuto, già troncato a 80 caratteri. È il primo controllo: spesso il modello ha tradotto, abbreviato o aggiunto parole.
2. **Guarda i "Closest monitor names"** sulla stessa riga: fino a 3 nomi vicini. Se il monitor atteso è lì, la differenza sta nel testo; se non c'è, vai al punto 4.
3. **Leggi `resolution:`** nella riga di successo: `alias`, `name`, `fallback` (match dopo lo scarto di parole generiche, da controllare: un alias lo rende esplicito) o `none`. Se invece c'è `no readable monitor line was found`, il problema è `/metrics`, non il nome.
4. **Il monitor esiste in `/metrics`?** Può essere in pausa, avere un nome scartato al passaggio 1 (più di 160 caratteri, caratteri di controllo) o una riga malformata. In tutti questi casi per il plugin non esiste.

Cause tipiche, dalla più frequente:

| Sintomo | Causa probabile |
| --- | --- |
| La richiesta ha una parola in più che non è nella lista del passaggio 6 (`Esse3 test`, `portale studenti Pisa`) | Il contenimento richiede che tutte le parole di un lato stiano nell'altro. Rimedio: alias |
| Il modello ha tradotto o cambiato lingua (`planning program` → `pianificazione`) | Nessuna parola in comune. Rimedio: alias |
| La richiesta contiene una parola a metà (`esse` per `ESSE3`), o il nome del monitor è dentro un'altra parola (`mail` in `Gmail`) | Il contenimento è su parole intere. Rimedio: alias |
| La richiesta è solo un acronimo o una parola breve | Sotto i 3 caratteri conta solo la parola intera; un acronimo vago può dare `ambiguous` per troppi match |
| Un alias non funziona | Riga malformata, scartata da sola: cerca `alias map, line N` nel log. Controlla anche gli id con `alias '...' refers to monitor ids absent from /metrics` |
| Il nome del monitor è un codice (`srv-ugov-prod-01`) | Nessun match automatico possibile. Rimedio: alias |
| Si ottiene `unknown` invece di `not_monitored` | Errore di lettura o di configurazione (passaggio 0), alias con id inesistenti, o codice di stato non riconosciuto |
| Si ottiene `ambiguous` | Troppi monitor corrispondono (più di 5 per nome, più di 10 per alias) o la richiesta era vuota |
| Nessuna riga di log del plugin | Il tool non è stato scelto dal modello (`procedural_memory_k` a 0, o domanda non riconosciuta), non è un problema di match |

## Cosa non fa il match

- Non confronta tag, URL o tipo di monitor: solo il nome (e gli id negli alias).
- Non corregge errori di battitura né traduce.
- Non usa cache: ogni domanda rilegge `/metrics`.
- I nomi dei monitor non arrivano mai al modello (solo `controllo 1`, `controllo 2`) e finiscono solo nel log.
