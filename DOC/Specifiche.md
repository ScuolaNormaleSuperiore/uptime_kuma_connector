# Uptime Kuma Connector — Specifiche

## 1. Obiettivo

Dare al chatbot di help desk la capacità di rispondere a una domanda sola:
**questo servizio è attivo?**

Serve a distinguere un guasto del servizio da un problema del dispositivo
dell'utente. È la domanda più frequente a un help desk di primo livello, ed è
quella su cui oggi il chatbot non ha alcun dato: risponde con la procedura
corretta a un utente il cui problema è che il servizio è giù.

Le frasi da cui tutto parte sono di questa forma:

```
"non riesco ad autenticarmi su U-GOV"
"non riesco ad accedere a Esse3"
"la VPN non va, è un problema mio?"
```

Tutto il resto è fuori scopo, e l'elenco è nella sezione 7.

## 2. Decisioni prese

Le cinque scelte da cui dipende tutto il resto. Sono presentate come decisioni
e non come premesse, con il costo di ciascuna, perché accettare queste
specifiche significa accettarle.

### 2.1 Si legge da `/metrics`, non dalla status page

Uptime Kuma non ha una REST API generale: la dashboard usa Socket.IO dopo
l'autenticazione. Restano pubblici pochi endpoint, e per il nostro scopo i
candidati sono due.

| | `/api/status-page/<slug>` + `/heartbeat/<slug>` | `/metrics` |
| --- | --- | --- |
| Chiamate necessarie | **due**, più un join sull'id | **una** |
| Nome del monitor | primo endpoint | nella stessa riga dello stato |
| Id del monitor | secondo endpoint | nella stessa riga dello stato |
| Autenticazione | nessuna | **API key** |
| Prerequisito lato Kuma | **status page pubblicata** | nessuno |

**Si sceglie `/metrics`**, per due ragioni di peso diverso.

La prima è tecnica: una riga contiene nome, id e stato insieme, quindi
scompare il pezzo più fragile dell'alternativa — correlare due risposte
diverse sull'id del monitor.

La seconda è la ragione decisiva, e non è tecnica: gli endpoint della status
page funzionano **solo se la status page è pubblicata**, cioè pubblica. Per
l'help desk ICT di un ateneo questo significa esporre a chiunque lo stato dei
servizi interni. `/metrics` non richiede niente di pubblico.

**Il costo, dichiarato:** si passa da zero segreti a un segreto di lettura,
che dà accesso a tutto il monitoraggio. Come si gestisce è in 2.3.

**Alternativa scartata, a verbale:** se in futuro una status page pubblica
esistesse comunque per altri motivi, l'approccio senza chiave tornerebbe
praticabile. La scelta va rivista in quel caso, non prima.

#### Che cosa è `/metrics`

È l'endpoint che Uptime Kuma espone per **Prometheus**. Non è una API pensata
per essere consumata da un'applicazione: è il punto da cui un sistema di
raccolta metriche fa scraping periodico. Risponde in `text/plain`, nel formato
di esposizione Prometheus — una riga per metrica per monitor, con le etichette
fra graffe e il valore numerico alla fine.

Questo comporta tre assenze, e ognuna ha una conseguenza su questo plugin.

**Non c'è alcun timestamp, e non c'è la storia dei battiti.** Il formato
Prometheus assume per convenzione che il valore esposto sia quello attuale.
Quindi la *freschezza* dello stato non è osservabile da qui: il plugin non può
sapere se Kuma ha misurato quel valore un secondo o un'ora fa. Il design
precedente, che leggeva `heartbeatList` dalla status page, vedeva i battiti con
i loro orari; questo no. Ne segue che l'esito `known` della sezione 4 **non può**
essere definito come «il monitor ha un battito recente», e non lo è.

**~~Non ci sono i tag dei monitor.~~ Falso: i tag ci sono.** Verificato su
un'istanza reale il 2026-09-08. Uptime Kuma espone ogni tag come **nome di
etichetta con valore vuoto** — `CategoriaA=""` — e le etichette dei tag
precedono `monitor_id` sulla riga. Sulle 49 righe osservate ognuna portava
almeno un tag, otto ne portavano due, e il valore era sempre la stringa vuota.

Questo apre la terza fonte di correlazione che questa sezione dichiarava
inesistente: un alias gestito **dentro Kuma**, come tag, invece che nella
configurazione del plugin. È l'opzione che non costa manutenzione al plugin e
che chi amministra i monitor può cambiare da solo.

Non è però una decisione già presa: usare i tag significa concordare una
convenzione di naming con chi gestisce l'istanza, e i nomi dei tag passano dal
vincolo delle etichette Prometheus, che non ammettono spazi né trattini — un tag
scritto con quei caratteri arriva qui trasformato o non arriva affatto, e non è
stato verificato quale delle due. La sezione 3.2 resta com'è, e la scelta è
registrata come lavoro aperto nella sezione 8.

**Non è detto che un monitor in pausa sia distinguibile.** Se Kuma smettesse di
controllare un monitor, `/metrics` potrebbe continuare a riportarne l'ultimo
stato conosciuto senza che noi possiamo accorgercene. Il comportamento reale è
**da verificare** ed è registrato in sezione 8: se un monitor in pausa emettesse
`0`, il plugin annuncerebbe come guasto un servizio semplicemente non più
controllato — il falso allarme speculare alla falsa rassicurazione che tutto
questo design cerca di evitare.

### 2.2 Lo stato si interroga in tempo reale, senza cache

Il tool interroga Kuma **nel momento in cui la domanda arriva**. Non esiste
uno stato conservato, non esiste un battito, non esiste un ciclo di
aggiornamento in background.

Questo è il punto in cui il nostro caso d'uso differisce da un'integrazione di
monitoraggio classica. Un'applicazione che mostra un pallino verde in una lista
ha bisogno di uno stato in cache, perché disegna quella lista continuamente e
non può interrogare il monitor a ogni render. Un chatbot no: interroga una
volta, quando qualcuno chiede, e la risposta deve essere vera **in quel
momento**. Uno stato di quaranta secondi prima è un'informazione peggiore di
nessuna, perché è indistinguibile da una attuale.

**Il costo, dichiarato:** ogni domanda pertinente costa una chiamata HTTP dentro
il turno, con l'utente che aspetta. Da qui il timeout basso di 2 secondi in 2.5.

**Punto aperto:** se il modello invocasse il tool due volte nello stesso turno,
Kuma riceverebbe due chiamate identiche. Una finestra di pochi secondi
risolverebbe, ma è un'ottimizzazione da fare su un problema misurato, non su uno
previsto.

### 2.3 La API key sta nel pannello, e solo lì

**Un campo nel pannello, `Uptime Kuma: API key`.** Nessuna variabile d'ambiente,
nessun ripiego, nessuna precedenza da ricordare.

*Decisione rivista il 2026-09-08.* La versione precedente di questo documento
prescriveva l'opposto — chiave solo da `UPTIME_KUMA_API_KEY`, nessun campo — e la
motivazione di allora resta vera: il core persiste i settings in `settings.json`
sotto la cartella del plugin, in chiaro. La decisione è cambiata lo stesso, e le
due ragioni vanno dette per intero.

La prima è che tutto ciò che serve per raggiungere Kuma si configura in un posto
solo, da chi amministra l'istanza, senza toccare il deployment. La seconda è che
la variabile d'ambiente, su questa installazione, **non aveva un posto dove
essere impostata**: `compose.yml` non la passa al container e la riga
`env_file: - .env` è commentata. Una configurazione che non si può configurare
non è più sicura: è solo inutilizzabile, e spinge chi deve farla funzionare
verso soluzioni peggiori.

**Il costo resta, e non è mitigato da niente.** La chiave finisce in chiaro in
`settings.json`, quindi in ogni backup, copia del container e snapshot di
supporto di quella cartella, e il pannello la mostra come testo normale.
`settings.json` è in `.gitignore`, il che la tiene fuori dal repository e da
nessun altro posto. Ne seguono due obblighi operativi, non due consigli:

- la chiave deve essere di **sola lettura** — il plugin non scrive mai su Kuma;
- va **ruotata** se quella cartella viene copiata da qualche parte.

Resta invece invariata la regola sui log: la chiave e l'header `Authorization`
non compaiono mai in una riga di log, e sul percorso della chiamata si registra
il tipo dell'eccezione, non il suo testo.

La chiave si genera nella dashboard di Kuma, in *Settings → API Keys*.

L'autenticazione ha una forma non ovvia, ed è la convenzione di Uptime Kuma:
**basic auth con username vuoto e la chiave come password**.

```
Authorization: Basic base64(":" + API_KEY)
```

**HTTP è accettato, e questa è la decisione messa a verbale.** La chiave viaggia
in un header Basic Auth e base64 è una codifica, non una cifratura: su HTTP
semplice la credenziale transita in chiaro, leggibile da qualunque cosa veda il
traffico.

Si accetta comunque, per due ragioni. La prima è che l'istanza Kuma potrebbe
essere raggiunta su una **rete privata** — se gira sullo stesso host Docker,
l'URL è il nome del servizio, `http://uptime-kuma:3001`, e il traffico non lascia
mai la rete virtuale del container: chi può intercettarlo è già dentro l'host,
dove ha problemi peggiori a disposizione. La seconda è che rifiutare HTTP
spingerebbe l'URL in un posto che nessuno valida.

**Non deve però diventare invisibile:** il validatore accetta `http://`, e
l'adapter registra un warning nel log quando l'URL configurato non è HTTPS. Una
casella «accetto» nel pannello sarebbe peggio — si spunta una volta e si
dimentica, mentre una riga di log ricompare.

**Da escludere, ed è la peggiore delle tre opzioni:** HTTPS con la verifica del
certificato disattivata. Espone la chiave esattamente come HTTP a chi si mette
in mezzo, e in più fa credere che il canale sia protetto. Se si rinuncia ai
certificati, si rinuncia in chiaro. Per un'istanza con certificato di una CA
privata la soluzione corretta è far fidare il container di quella CA, non
abbassare la verifica.

**Punto aperto:** dove giri Kuma rispetto a Cheshire Cat non è ancora
determinato, quindi non è deciso se HTTP resti la configurazione normale o solo
una possibilità. Vedi sezione 8.

### 2.4 Quattro stati, e la manutenzione non è un guasto

Uptime Kuma ha quattro stati: `0` down, `1` up, `2` pending, `3` manutenzione.
Vengono mantenuti **tutti e quattro**, distinti.

La tentazione da evitare è il modello a due stati, «`1` è attivo, tutto il resto
è giù». Ha una conseguenza concreta e sbagliata: un servizio in **manutenzione
programmata** verrebbe annunciato all'utente come guasto, e l'utente aprirebbe
un ticket per un intervento pianificato di cui il chatbot poteva informarlo.

`pending` va detto per quello che è — rilevazioni intermittenti — perché è
l'informazione che spiega un problema che l'utente sta vivendo a intermittenza.

Un codice **non riconosciuto** non è un quinto stato: è l'esito `unknown` della
sezione 4. Non si mappa su `down` per comodità.

**Da confermare durante l'implementazione:** che `monitor_status` in `/metrics`
emetta davvero anche `2` e `3`, e non solo `0` e `1`. Se emettesse solo due
valori, la distinzione si perde e va documentata come limite invece di dedurla.

### 2.5 Un timeout basso, e nessun fallimento che esca dal tool

Timeout di **2 secondi**. La chiamata sta dentro il turno, davanti a un utente
che aspetta: un'integrazione di monitoraggio in background si concede dieci
secondi, qui sarebbero dieci secondi di silenzio in una conversazione.

Qualunque fallimento — timeout, rete, HTTP non 2xx, risposta non interpretabile
— viene catturato e diventa l'esito `unknown` di cui alla sezione 4. **Nessuna
eccezione esce dal tool.**

## 3. Come funziona

**Un tool solo**, invocato dal modello quando l'utente segnala che qualcosa non
funziona.

```
utente:   "Non riesco a collegarmi alla VPN, è un problema mio?"
modello:  invoca service_status("VPN")
tool:     GET <istanza>/metrics   (basic auth, timeout 2 s)
tool:     -> "Il servizio VPN - GlobalProtect risulta attivo."
modello:  fonde quella frase con la procedura recuperata dalla knowledge base
```

Il tool **non risponde all'utente**: consegna un fatto al modello, che lo unisce
a quello che ha recuperato. È la ragione per cui va registrato con
`return_direct=False`.

**Perché uno solo, e non anche un tool di discovery.** L'alternativa naturale
sarebbe un secondo tool che elenca i servizi monitorati, lasciando al modello la
risoluzione del nome. Si scarta per due ragioni. I tool vivono nella memoria
procedurale e vengono recuperati per similarità con `k` risultati: un secondo
tool competerebbe con quello che serve, e su un'istanza con `k` basso dimezza la
probabilità di recuperare quello giusto. E il discovery non serve al modello:
serve a un amministratore, che lo ottiene dal log (sezione 3.4).

### 3.1 Cosa si legge da `/metrics`

Le righe che interessano hanno questa forma. **Verificata su un'istanza reale
il 2026-09-08**, ed è diversa da quella che questo documento riportava prima:

```
monitor_status{CategoriaA="",monitor_id="1",monitor_name="VPN - GlobalProtect",monitor_type="http",monitor_url="https://…",monitor_hostname="null",monitor_port="null"} 1
```

Tre differenze rispetto alla forma assunta, e la prima è quella che rompe il
parsing:

- **`monitor_id` non è la prima etichetta.** Le etichette dei tag vengono prima.
  Un parser ancorato su `monitor_status{monitor_id=` non trova **nessuna** riga
  su questa istanza.
- **Ci sono tre etichette in più**: `monitor_url`, `monitor_hostname` e
  `monitor_port`. Le ultime due valgono spesso la stringa `"null"`, che è testo
  e non un valore nullo.
- **I tag compaiono come nomi di etichetta con valore vuoto**, sezione 2.1.

Il parsing, di conseguenza: per ogni riga che comincia con `monitor_status{`,
estrarre **tutte** le coppie `chiave="valore"` senza fare ipotesi sul loro
ordine, prendere `monitor_id` e `monitor_name`, e leggere il valore numerico
finale. La fixture catturata è in `tests/unit/fixtures/metrics_sample.txt`, con
nomi, URL e tag sostituiti e la forma intatta. Una riga malformata, o priva di una delle due etichette,
**si scarta da sola**: non interrompe il parsing delle altre e non solleva.

Kuma espone su `/metrics` anche altre metriche — `monitor_response_time`,
`monitor_cert_days_remaining`, `monitor_cert_is_valid`. Non servono a rispondere
alla domanda della sezione 1 e si ignorano.

**Il formato delle etichette è la parte che più facilmente si assume sbagliata**,
quindi le fixture dei test vanno costruite da una risposta reale e non scritte a
mano. Vedi la sezione 6.

### 3.2 Come si risolve il nome che l'utente ha scritto

L'utente scrive «U-GOV», il monitor si chiama «U-GOV - Autenticazione Cineca».
Come si è visto in 2.1, i soli dati di correlazione che `/metrics` offre sono
l'id e il nome del monitor. Ne segue che l'informazione che collega la frase
dell'utente al monitor giusto può stare **in due soli posti**: nel nome del
monitor dentro Kuma, o nella configurazione di questo plugin. Non esiste una
terza fonte.

Si usano **entrambi**, perché risolvono problemi diversi.

Il matching sul nome copre il caso normale a costo zero di manutenzione: chi
aggiunge un monitor in Kuma lo nomina, e il chatbot lo trova da subito senza che
nessuno tocchi il plugin. La mappa in configurazione copre il caso patologico:
un monitor chiamato `srv-ugov-prod-01` non sarà mai collegato a «U-GOV» da
nessuna euristica.

Il costo di ciascuna metà, dichiarato. Il matching automatico impone un
**prerequisito operativo**: i nomi dei monitor in Kuma devono contenere il nome
con cui gli utenti chiamano il servizio, e questo va concordato con chi gestisce
l'istanza. La mappa impone **manutenzione manuale**, e deriva in silenzio quando
un servizio nuovo non viene aggiunto.

#### Normalizzazione

Prima di qualunque confronto, sia la richiesta sia i nomi dei monitor sia le
chiavi della mappa vengono normalizzati: minuscole, spazi interni compattati,
spazi esterni rimossi, e **i caratteri `-`, `.` e `_` eliminati**.

L'ultima regola vale il suo costo: fa sì che «UGOV» trovi `U-GOV - Autenticazione`
da sola, e con essa sparisce un'intera classe di alias che altrimenti andrebbero
scritti a mano.

La richiesta viene inoltre **troncata a 80 caratteri** prima dell'uso. È testo
che arriva dal modello e che ricompare nella frase di risposta, quindi nel
prompt: non deve essere una stringa di lunghezza arbitraria.

#### Ordine di risoluzione

1. **Mappa degli alias.** Se la richiesta normalizzata corrisponde a una chiave
   della mappa, si usano gli id indicati. Un alias è una decisione umana
   esplicita e ha la precedenza su qualunque euristica.
2. **Corrispondenza esatta sul nome.** Un monitor chiamato esattamente come
   richiesto non viene mai oscurato da uno più lungo che lo contiene.
3. **Corrispondenza per contenimento**, nei due sensi: la richiesta contenuta nel
   nome, il nome contenuto nella richiesta, oppure le stesse parole normalizzate
   in ordine diverso. L'ultimo caso serve perché il modello può passare
   «autenticazione U-GOV» invece di «U-GOV».
4. **Nessuna corrispondenza** → esito `not_monitored`, sezione 3.4.

#### La mappa degli alias

Un campo di testo nel pannello, una voce per riga:

```
U-GOV, UGOV, Ugov: 12
Esse3, Segreteria online: 14, 15, 16
VPN: 17
```

A sinistra dei due punti uno o più alias separati da virgola; a destra uno o più
**id numerici** di monitor separati da virgola. Righe vuote e righe che iniziano
con `#` si ignorano.

**Alias verso l'id, non verso il nome.** L'id si legge nell'URL del monitor in
Kuma, `/dashboard/<id>`, è stabile e non ambiguo. Il nome cambia con una
rinomina, e la correlazione per URL è stata provata su Solution Map e scartata
perché instabile e ambigua quando più monitor controllano host simili.

**Più id per un alias** è previsto e utile: è il modo in cui un amministratore
raggruppa esplicitamente i componenti di un servizio, senza dipendere da come
sono stati nominati. Il risultato è un match multiplo, sezione 3.3.

**Aiuto pratico per verificare un id:** aprire `<url_istanza>/dashboard/<id>`. Se
si apre il monitor di un altro servizio, il numero è sbagliato. È l'unico modo
semplice, per un operatore, di accorgersene.

#### Il parsing della mappa non abbandona tutto su una riga sbagliata

Una riga malformata — due punti mancanti, id non numerico, alias vuoto — viene
scartata **da sola**, con una riga di log che la identifica, e tutte le altre
restano vive. Un alias già definito in una riga precedente non viene
sovrascritto: vince il primo, e il secondo si registra nel log.

Questa non è pignoleria. È la forma esatta di un difetto reale documentato in
`DEV/AGENTS/PROJECT.md`: il core di Cheshire Cat legge `requirements.txt`
chiamando `packaging.Requirement()` in un ciclo dentro un `try` che alla prima
riga non valida **abbandona l'intero elenco**, installando zero dipendenze e
registrando un errore solo. Lo stesso errore su questa mappa spegnerebbe la
risoluzione di tutti i servizi per un carattere di troppo su uno. Per la stessa
ragione il formato è una riga per voce e non JSON: un JSON rotto è la medesima
trappola.

#### Un alias che punta a un id inesistente è `unknown`, non `not_monitored`

Riceviamo l'elenco completo dei monitor a **ogni** chiamata, quindi ogni id della
mappa si può validare contro quell'elenco. Se nessuno degli id di un alias
compare fra le righe di `/metrics`, l'esito è `unknown` con una riga di log di
livello warning che nomina l'alias e gli id mancanti. Se solo alcuni mancano, si
risponde con quelli trovati e i mancanti finiscono nel log.

Dire «non è monitorato» sarebbe una bugia: quel servizio *è* configurato come
monitorato, è la configurazione a essere rotta. È anche il punto in cui questo
plugin fa meglio dell'integrazione da cui deriva, dove un id sbagliato restava
silenzioso per sempre perché nessun canale vedeva l'elenco completo.

### 3.3 Quando la richiesta corrisponde a più monitor

«Esse3» corrisponde a `Esse3 - Web`, `Esse3 - DB` e `Esse3 - Batch`. In questo
caso **si riportano tutti, ciascuno con il proprio stato**, e non se ne sceglie
uno.

```
"Per Esse3 risultano tre controlli: Esse3 - Web risulta attivo,
 Esse3 - DB non risulta attivo, Esse3 - Batch risulta attivo."
```

Non è una deroga alla regola «mai scegliere per supposizione»: sceglierne uno
sarebbe una supposizione, elencarli tutti è un fatto più ricco. Ed è
precisamente l'informazione che serve a un help desk, perché dice **quale
componente** è in difficoltà.

**Oltre cinque corrispondenze la richiesta non identifica un servizio.** Una
parola come «portale» o «web» può corrispondere a decine di monitor: elencarli
tutti farebbe entrare nel prompt una frase enorme a ogni chiamata. Sopra la
soglia l'esito è `ambiguous`, sezione 4, e la frase invita il modello a chiedere
all'utente quale servizio intenda. La soglia è un valore di progetto, da
rivedere su un'istanza reale.

### 3.4 Quando nessun monitor corrisponde

La frase per il modello **non contiene l'elenco dei servizi monitorati**, e
questa è una decisione presa contro la versione precedente di queste specifiche.

L'elenco serviva a due scopi diversi, che vanno separati.

Il primo è rendere visibile un **errore di configurazione**: un id sbagliato o un
nome scritto male non deve produrre per sempre un «non monitorato»
indistinguibile dalla risposta corretta. È un'esigenza reale, ma il suo
destinatario è un amministratore, non il modello: va nel **log**, non nel prompt.
A ogni `not_monitored` si registra il termine cercato e i nomi più simili fra
quelli visti — al massimo tre — così chi gestisce l'istanza ha esattamente ciò
che serve per scrivere l'alias.

Il secondo era recuperare in corsa un nome che il plugin non ha matchato e un
umano sì. Questo dovrebbe raggiungere il modello, ma porta un rischio che la
versione precedente non nominava: dando al modello un elenco di **altri**
servizi, lo si invita a rispondere su un servizio diverso da quello chiesto.
«Forse intendeva Esse3, che risulta giù», detto a chi ha chiesto di U-GOV, è un
falso allarme prodotto dal nostro elenco — il gemello speculare della falsa
rassicurazione. Inoltre la scelta della sezione 3.3 rende il contenimento molto
più tollerante, quindi i casi in cui un servizio realmente monitorato finisce
qui si riducono di molto.

Un elenco di candidati vicini nella frase resta un'opzione da **aggiungere se si
misura che serve**, non da escludere per sempre.

In compenso la frase di `not_monitored` deve dire esplicitamente che **non
monitorato non significa funzionante**: è la frase a più alto rischio di tutto il
plugin in questa versione, perché un modello che legge «il servizio X non risulta
monitorato» tende a trattarlo come un'assenza di problemi e a rispondere in tono
rassicurante.

### 3.5 La descrizione del tool decide se il tool viene invocato

Il tool viene recuperato dalla memoria procedurale per **similarità semantica con
la frase dell'utente**, e la sua descrizione — il docstring — è il testo su cui
quella similarità si calcola. Non è documentazione: è la condizione perché il
tool venga trovato.

Quindi il docstring contiene le forme in cui la domanda arriva davvero, non una
definizione astratta della funzione:

```
"non riesco ad autenticarmi su U-GOV"
"non riesco ad accedere a Esse3"
"la VPN non funziona"
"è un problema mio o del servizio?"
"il portale è giù?"
```

Un docstring scritto come «restituisce lo stato di un monitor Uptime Kuma»
descrive l'implementazione e non assomiglia a nessuna frase che un utente
scriverà mai.

## 4. Il contratto della risposta: gli esiti

Il cuore delle specifiche. La funzione di decisione restituisce
`(esito, frase)`: l'esito è ciò su cui il chiamante ragiona, la frase è ciò che
arriva al modello.

| Esito | Quando | Cosa viene detto al modello |
| --- | --- | --- |
| `known` | Da uno a cinque monitor identificati, con un codice di stato riconosciuto | Il nome reale di ciascuno e il suo stato |
| `not_monitored` | Nessun monitor corrisponde | Che non risulta alcun controllo per quel servizio, **e che questo non significa che funzioni** |
| `ambiguous` | Più di cinque monitor corrispondono | Che la richiesta non identifica un servizio, e di chiedere all'utente quale intende |
| `unknown` | Kuma irraggiungibile, non autenticato, risposta non interpretabile, alias che punta a id inesistenti, codice di stato non riconosciuto | Che lo stato non è noto, **e di non trarre conclusioni** |

Quattro esiti e non due, e nemmeno tre: `not_monitored` e `unknown` sono cose
diverse e vanno tenute separate.

«Non risulta un controllo per quel servizio» è una **risposta certa**: l'abbiamo
chiesto a Kuma, Kuma ha risposto, e quel servizio non è fra i monitor. «Non lo
so» è un **nostro malfunzionamento**. Confonderle significa che un'istanza Kuma
spenta produrrebbe «nessuno di questi servizi è monitorato», che è falso e
sposta la colpa nel posto sbagliato.

**`unknown` porta il peso di tutto il progetto.** Un controllo di stato che
dichiara un servizio sano perché il suo monitor è irraggiungibile è peggio di
nessun controllo: l'utente riceve una rassicurazione falsa e la attribuisce al
servizio, non allo strumento.

E c'è una ragione specifica per cui la frase deve contenere l'istruzione
esplicita di non concludere nulla, invece di essere una stringa vuota o
un'eccezione: un modello a cui si chiede «la VPN è giù?» **riempie il silenzio**.
Se non gli si dice di non concludere, conclude.

Questa è la regola invariante del plugin: **mai inventare uno stato.**

### Le frasi

Sono parte del prodotto, non output di debug. Sono in italiano, nominano il
monitor **esattamente come lo nomina Kuma**, e non contengono mai un URL, una
credenziale o un id di monitor.

| Caso | Frase |
| --- | --- |
| `1` up | Il servizio *<nome>* risulta attivo. |
| `0` down | Il servizio *<nome>* non risulta attualmente attivo. |
| `2` pending | Il servizio *<nome>* presenta rilevazioni intermittenti. |
| `3` maintenance | Il servizio *<nome>* è in manutenzione programmata. |
| match multiplo | Per *<richiesta>* risultano più controlli: *<nome>* …, *<nome>* … |
| `not_monitored` | Non risulta alcun controllo di disponibilità per *<richiesta>*. Non è disponibile alcuna informazione sul suo stato: non concluderne che il servizio funzioni. |
| `ambiguous` | *<richiesta>* corrisponde a troppi controlli per identificare un servizio. Chiedi all'utente quale servizio intende. |
| `unknown` | Non è stato possibile determinare lo stato di *<richiesta>*. Non trarre conclusioni: non affermare né che il servizio è attivo né che è guasto. |

## 5. Configurazione

Nel pannello, sotto *Plugins → Uptime Kuma Connector → Settings*:

| Campo | Default | Note |
| --- | --- | --- |
| URL dell'istanza Uptime Kuma | vuoto | Vuoto **disabilita** il connettore. Preferire HTTPS |
| API key | vuoto | Chiave di **sola lettura**, da *Settings → API Keys*. Vuoto **disabilita** il connettore — sezione 2.3 |
| Mappa degli alias, opzionale | vuoto | Casella **multiriga**, una voce per riga: `alias, alias: id, id` — sezione 3.2 |

La mappa degli alias chiede al pannello una casella multiriga, perché il formato
è una voce per riga e un'installazione con una decina di servizi sarebbe
altrimenti da modificare dentro un campo a riga singola che scorre di lato.

Due test coprono le due metà.

Nessuna variabile d'ambiente, e nessun altro file da toccare: i tre campi sono
tutta la configurazione del plugin.

E nient'altro. Due campi sono assenti di proposito, e ogni assenza è una
decisione: **nessun campo per il timeout** — due secondi sono una proprietà
dello stare dentro una conversazione, non una preferenza da regolare — e
**nessun campo per la cache**, che non esiste (2.2).

**Un solo punto decide se il connettore è utilizzabile**, e tutti i percorsi
passano da lì: una funzione che valuta URL configurato **e** chiave presente.

Non è pedanteria organizzativa: nell'integrazione analoga già in esercizio è
successo due volte che un punto dell'interfaccia controllasse solo il campo
dell'identificativo, mostrando l'indicatore di monitoraggio anche a integrazione
spenta. Il costo di avere una funzione sola è zero; il costo di non averla si
paga in produzione.

A connettore non utilizzabile **non si tenta alcuna chiamata di rete** e l'esito
è `unknown`.

Le descrizioni dei campi restano **una frase breve ciascuna**. Su questo
pannello un titolo lungo più una descrizione lunga fa comparire una barra di
scorrimento orizzontale, oltre i 200 caratteri circa per la coppia.

### Cosa non finisce mai in un log

La API key e l'header `Authorization` che la contiene. Sul percorso della
chiamata si registra **il tipo** dell'eccezione, non il suo testo.

## 6. Test da prevedere

Ognuno corrisponde a un errore realmente possibile, non a una copertura
formale.

I test unitari si eseguono in locale con `python run-tests.py --unit`; quelli
di integrazione, che importano il core Cheshire Cat dal container, con
`python run-tests.py --integration`. `python run-tests.py` esegue entrambi i
livelli e `--detailed` mostra il nome di ogni test.

**Sulla logica pura**, senza Cheshire Cat e senza rete:

- parsing di `/metrics` su una **risposta reale**, non su un mock scritto a
  mano: è la parte che più facilmente si assume sbagliata
- ciascuno dei quattro stati, inclusa la manutenzione distinta dal guasto
- codice di stato non riconosciuto → `unknown`, mai `down`
- riga di `/metrics` malformata o etichetta mancante: si scarta da sola, nessuna
  eccezione, le altre righe restano
- risposta senza alcuna riga `monitor_status`
- corrispondenza esatta che non viene oscurata da una più lunga
- normalizzazione: «UGOV» trova `U-GOV - Autenticazione`
- contenimento nei due sensi e con parole invertite: «autenticazione U-GOV»
  trova lo stesso monitor
- **l'alias vince sul matching per nome** anche quando il nome avrebbe trovato
  altro
- **un alias verso un id inesistente dà `unknown`, non `not_monitored`**
- alias con più id → match multiplo con tutti gli stati
- **una riga malformata nella mappa non annulla le altre**
- alias duplicato: vince il primo
- match multiplo fino a cinque → tutti elencati; oltre cinque → `ambiguous`
- richiesta più lunga di 80 caratteri: troncata, nessuna frase gigante
- `not_monitored`: la frase **non** elenca altri servizi e **non** rassicura

**Sull'integrazione**, contro un `cat` finto:

- connettore non configurato: nessuna chiamata di rete tentata
- URL configurato ma chiave assente: nessuna chiamata, esito `unknown`
- **istanza irraggiungibile: esito `unknown`, e la frase non afferma né che il
  servizio è attivo né che è giù.** È il test che conta più di tutti, perché è
  l'unico fallimento che inganna attivamente un utente
- HTTP 401: esito `unknown`, e la chiave non compare in nessun log
- timeout rispettato
- il tool non solleva mai, qualunque cosa faccia la chiamata

## 7. Fuori scope

- **Qualunque scrittura verso Uptime Kuma**, battito incluso: il plugin è un
  consumatore in sola lettura. Lo stato si interroga quando serve, e non si
  segnala nulla in senso opposto.
- Configurare o modificare Uptime Kuma: creare, sospendere o rinominare un
  monitor sta fuori da questo repository, e le credenziali per farlo sono
  deliberatamente assenti.
- Autenticarsi alla dashboard via Socket.IO.
- Ricevere webhook da Uptime Kuma. Un webhook serve a mantenere uno stato in
  cache aggiornato in tempo quasi reale; qui non esiste uno stato in cache da
  aggiornare, quindi non serve a niente.
- Un secondo tool che elenchi i servizi monitorati: sezione 3.
- Aprire ticket, inviare email, escalation operativa.

## 8. Da verificare prima di implementare

1. ~~**Confermare la forma di `/metrics` su un'istanza reale.**~~ **Fatto il
   2026-09-08**, e il risultato è a metà. *Confermato:* l'endpoint risponde `200`
   con basic auth a username vuoto, in 207 ms per 113 KB; `monitor_id` e
   `monitor_name` sono entrambi sulla riga di stato; una riga per monitor, 49 in
   tutto. *Smentito:* l'ordine delle etichette e l'assenza dei tag, sezioni 2.1 e
   3.1. *Ancora aperto:* **quali valori di stato l'istanza emetta.** Al momento
   della cattura tutti e 49 i monitor erano attivi, quindi si è osservato solo
   `1`. Questo non dice nulla su `0`, `2` e `3`: la domanda resta, e va ricatturata
   quando qualcosa è giù o in manutenzione. Non scrivere quelle righe a mano
   nella fixture.
2. **Decidere se usare i tag come terza fonte di correlazione**, ora che si sa
   che esistono. Comporta concordare una convenzione con chi gestisce l'istanza,
   e verificare come arrivano i tag con spazi o trattini, che non sono nomi di
   etichetta Prometheus validi.
2. **Un monitor in pausa o sospeso: compare in `/metrics`, e con quale valore?**
   Se comparisse con `0`, il plugin annuncerebbe come guasto un servizio
   semplicemente non più controllato, e servirebbe una regola in più — non un
   adattamento del parsing. Aperto, da verificare sull'istanza reale.
3. **Verificare che la memoria procedurale sia abilitata.** I tool vivono là: su
   un'istanza con `k = 0` il tool non viene mai recuperato e quindi mai
   invocato, per quanto corretto sia. È la causa numero uno di un «non fa
   niente», e sul deployment di riferimento quel valore è attualmente zero.
   Decisione presa: si verifica e si abilita, e il meccanismo resta il tool.
4. **Concordare la convenzione sui nomi dei monitor** con chi gestisce
   l'istanza: il nome deve contenere il termine con cui gli utenti chiamano il
   servizio. È il prerequisito che rende il livello automatico della sezione 3.2
   utile invece che decorativo.
5. **Determinare dove gira Kuma rispetto a Cheshire Cat.** Se è sullo stesso
   host Docker, l'URL diventa il nome del servizio su rete interna e la
   questione del certificato scompare. Se è su un'altra macchina, HTTP fa
   attraversare la chiave in chiaro alla rete di ateneo, e va deciso se
   accettarlo o dotare l'istanza di un certificato. Il codice funziona in
   entrambi i casi: cambia solo cosa dice questo documento. Vedi 2.3.
6. **Scegliere la licenza** e aggiungere il file: il registry richiede software
   open source, e oggi manca.

## 9. Cosa non si replica dall'integrazione esistente, e perché

L'integrazione Kuma già in esercizio su Solution Map (Laravel + PostgreSQL) è
stata la fonte del formato di `/metrics`, della convenzione di autenticazione e
di tre lezioni pagate in produzione. Non è però il modello architetturale da
seguire, perché risolve un problema diverso: mostrare un indicatore accanto a
ogni applicazione **censita in un registro**. Questo plugin non ha alcun
registro — il «record» è una frase che un utente ha scritto in quel momento.

Quanto si è preso, e quanto no.

| Da Solution Map | Qui |
| --- | --- |
| Basic auth con username vuoto | **Preso**, 2.3 |
| Formato riga `monitor_status{...}` | **Preso**, 3.1 |
| «Mai inventare uno stato» | **Preso**, ed è la regola invariante di entrambi |
| «Un solo punto decide se è monitorato» | **Preso**, 5, con la stessa motivazione: è successo due volte |
| Fixture da risposta reale, non scritta a mano | **Preso**, 6 |
| Id numerico come chiave di correlazione, e URL scartato perché instabile | **Preso**, 3.2 |
| Un id sbagliato è silenzioso | **Preso come rischio, risolto**: validiamo ogni alias contro l'elenco completo che riceviamo a ogni chiamata |
| Canale webhook in ingresso | **No.** Serve a tenere fresco uno stato conservato; qui non esiste |
| Polling schedulato e cache a due campi | **No**, per la stessa ragione. E niente schedulatore da verificare in produzione |
| Interruttore generale booleano | **No.** URL vuoto disabilita: un campo in meno che può dissentire dall'altro |
| `null` in colonna come «mai verificato» | **No** come modellazione: qui è un esito calcolato al momento della chiamata |
| Timeout di 10 secondi | **No.** Due secondi, perché la chiamata è dentro il turno |
| `2` e `3` trattati diversamente dai due canali | **No, e il problema sparisce.** Un canale solo, una decisione sola — è il difetto che quel documento stesso chiede di non replicare |
| Tunnel HTTPS per lo sviluppo locale | **No.** Serviva a far arrivare i webhook; qui basta che l'istanza Kuma sia raggiungibile in uscita |

Le tre lezioni che restano valide fuori dal loro contesto, e che vale la pena
rileggere quando si è tentati di semplificare: un identificativo inserito a mano
e non validato produce un errore che nessuno vede mai; due percorsi di codice che
decidono la stessa cosa divergono, sempre; e un controllo di disponibilità che
mente per rassicurare è peggio di un controllo che manca.
