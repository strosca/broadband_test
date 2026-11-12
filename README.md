# linkmon

Überwacht dauerhaft, ob dein Internetanschluss funktioniert, und hält jede Störung mit Beginn, Ende und Ursache fest.
Alle 3 Sekunden prüft es parallel Router, Provider, Internet, DNS und HTTPS. Daraus entstehen Ausfälle, Ping,
Paketverlust und Jitter je Strecke. Das Ergebnis gibt es als Webinterface zum Reinschauen, als CSV-Export mit Prüfsumme
und als PDF-Bericht.

Die Geschwindigkeit misst linkmon nicht. Speedtests werden von den Anbietern begrenzt (Ookla sperrt bei zu vielen
Tests) und lasten die Leitung für alle im Haushalt aus.

## Einrichten

Windows (keine Administratorrechte nötig):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
```

Linux oder Raspberry Pi:

```sh
sudo apt install python3-venv iputils-ping iproute2 traceroute
./deploy/linux/install.sh
```

Das legt `.venv` und eine `config.toml` an. In einer aktivierten `.venv` heißt der Befehl einfach `linkmon`, sonst
`.venv\Scripts\linkmon.exe` (Linux: `.venv/bin/linkmon`). Er erwartet `config.toml` im aktuellen Ordner, sonst
`--config PFAD` angeben.

## Kurzstart

```powershell
linkmon run
```

Das startet die Messung und das Webinterface. Dann jederzeit im Browser reinschauen, auf diesem Rechner unter
**http://127.0.0.1:8050**, von Handy, Tablet oder anderem PC im Heimnetz unter `http://RECHNERNAME:8050` oder
`http://IP-ADRESSE:8050`. Die Adressen stehen beim Start im Log und in `linkmon status`.

## Vor der Messung

1. **Messgerät per Kabel anschließen**, am besten direkt an den Router des Anbieters. Über WLAN erscheinen Aussetzer des
   WLANs als Störung im Heimnetz, und Funkstörungen erhöhen Ping und Verlust. WLAN wird erkannt, im Webinterface
   angezeigt und im Bericht vermerkt. Ein Wechsel der Verbindung fällt auch ohne Neustart auf.
   Auch ein Wechsel des Anschlusspunkts, etwa vom Mesh-Router direkt an die Vodafone Station, wird am geänderten
   Gateway erkannt: linkmon verwirft dann die gemerkten Router und ermittelt die Route sofort neu.
2. **Ruhezustand abschalten.** Im Standby misst nichts, und die Zeit erscheint als Messlücke.
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```
   Der Bildschirm darf ausgehen, und Sperren mit Win+L ist kein Problem.
3. **Prüfen:** `linkmon status` zeigt Ziele, Verbindungsart, Warnungen und die Werte der letzten Stunde.

## Dauerbetrieb

Windows: Aufgabe anlegen, in einer PowerShell als Administrator. Sie startet beim Hochfahren auch ohne Anmeldung und nach
einem Absturz neu.

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -RegisterTask
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -UnregisterTask   # wieder entfernen
```

Linux: `./deploy/linux/install.sh --enable` installiert und startet den systemd-Dienst `linkmon`.

Zum Ausprobieren im Vordergrund: `linkmon run`, Beenden mit Strg+C. Das Webinterface läuft so lange wie der Dienst.
Es kann immer nur ein Messdienst je Datenordner laufen. Das Log liegt
rotierend in `data/logs/linkmon.log`.

## Webinterface

Das Webinterface läuft auf einem richtigen Webserver (waitress), der mehrere Geräte gleichzeitig bedient. Mit
`host = "0.0.0.0"` in `[dashboard]` ist es im ganzen Heimnetz erreichbar, mit `"127.0.0.1"` nur auf diesem Rechner. Es
gibt kein Passwort: Jeder im Heimnetz kann die Messwerte sehen, aber nichts ändern.

**Windows-Firewall:** Ohne Freigabe blockiert Windows die anderen Geräte. `-RegisterTask` legt die Regel mit an, einzeln
geht es so (als Administrator):

```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1 -OpenFirewall
```

Die Regel gilt nur, wenn das Netzwerk in Windows als „Privat“ eingestuft ist, das Skript weist sonst darauf hin.

| Bereich | Inhalt |
|---|---|
| Zustand | Jetzt in Ordnung oder gestört, Messdienst aktiv, aktueller Ping, Verbindung, letzter Ausfall |
| Kennzahlen | Verfügbarkeit der Leitung, Ausfälle, Ping (Median und P95), Paketverlust und Jitter ins Internet |
| Verlauf | Ausfälle nach Ebene, darunter Ping und Paketverlust je Strecke mit gemeinsamer, zoombarer Zeitachse |
| Je Strecke | Verlust, Median, P95, Maximum und Jitter für Internet, Provider-Hop, Heimnetz, DNS und HTTPS |
| Nach Tageszeit | Paketverlust oder Ping-Spitzen je Stunde und Tag, zeigt etwa abendliche Engpässe |
| Ausfälle und Messlücken | Liste mit Beginn, Ende, Dauer, Ebene und Befund, filterbar |

**Wo liegt die Ursache?** Die Tabelle „Je Strecke“ hilft dabei. Steigen Verlust und Ping erst ab dem Provider-Hop, liegt
das Problem hinter deinem Router. Steigen sie schon im Heimnetz, eher beim WLAN oder Router. Einzelne Spitzen nur am
Provider-Hop sind kein Beleg: Router beantworten Pings an sich selbst oft nachrangig.

`linkmon dashboard` startet das Webinterface ohne Messung, etwa um alte Daten anzusehen.

## Messdaten leeren

```powershell
linkmon clear            # verschiebt alle Messdaten nach data/archiv/messdaten_JJJJMMTT_HHMMSS
linkmon clear --delete   # löscht sie endgültig
```

Danach beginnen Webinterface, Plots, Kennzahlen und Berichte bei null. Der Befehl fragt vorher nach (`--yes` überspringt
das). Der Messdienst muss dafür **nicht** angehalten werden: Läuft er, leert er die Daten selbst vor der nächsten
Prüfrunde und misst ohne Lücke weiter. Erhalten bleiben die ermittelte Route, die Konfiguration und das Textlog unter
`data/logs`. Archivierte Daten lassen sich ansehen, indem man den Ordner zurück nach `data/messdaten` verschiebt.

## Auswerten

| Befehl | Ergebnis |
|---|---|
| `linkmon report --from 2026-11-01 --to 2026-11-30` | `bericht.pdf`, Abbildungen als PDF, SVG und PNG, Rohdaten mit Prüfsumme |
| `linkmon export --from 2026-11-01 --to 2026-11-30` | `ausfaelle.csv`, `qualitaet_minuten.csv`, `rohdaten.jsonl`, `SHA256SUMS.txt` (`--excel-de` für deutsches Excel) |

Datumsangaben gelten in lokaler Zeit, `--to` einschließlich des ganzen Tages. Ausgaben landen unter `data/berichte` und
`data/exporte`, sofern `--out` nichts anderes vorgibt. `qualitaet_minuten.csv` enthält je Minute und Strecke die Zahl der
Pings, die verlorenen, Median, P95 und Maximum.

## Wo die Daten liegen

Alles steht als CSV in `data/messdaten`, eine Datenbank gibt es nicht. Jede Zeile wird sofort angehängt und auf die
Platte geschrieben, ein Absturz kostet also höchstens die gerade laufende Prüfrunde.

| Datei | Inhalt |
|---|---|
| `pruefungen/pruefungen_JJJJ-MM.csv` | Jede Prüfrunde, eine Spalte je Ziel, eine Datei je Monat nach UTC, rund 45 MB im Monat |
| `ausfaelle.csv` | Erkannte Ausfälle mit Ebene, Beginn, Ende und ob das Ende bestätigt ist |
| `ereignisse.csv` | Warnungen, Messlücken, Routen- und Verbindungswechsel |
| `sitzungen.csv` | Jeder Start des Dienstes mit Gerät, Verbindungsart, Versionen und Ende |
| `zustand.csv` | Interne Merkwerte, etwa die ermittelte Route und die aktuelle Verbindung |

**Prüfrunden je Monat.** Eine Zeile sieht so aus:

```
sitzung,zeit_utc,ebene,lan:192.168.68.1,ext:1.1.1.1,ext:8.8.8.8,dns,dnsref,https,interface
1,2026-11-08 19:00:18,,3,20,19,20,19,77,ok
```

Je Ziel steht die Antwortzeit in ms (ab 10 ms ganzzahlig), `ok` bei Erfolg ohne Zeit, `x` bei einem Fehler, oft mit
Grund wie `x:Timeout`, und nichts, wenn das Ziel in dieser Runde nicht geprüft wurde. Kommt ein Ziel neu hinzu, etwa
nach einem Netzwechsel, wird die Kopfzeile um eine Spalte verlängert. Ältere Zeilen enden dann einfach früher. Bei 3 s
Takt hat ein Monat höchstens 892.800 Zeilen, das passt noch in Excel (bis 1.048.576). Bei einem kürzeren Takt nicht
mehr. Ein Jahr belegt etwa 550 MB.

Monatsgrenzen gelten nach UTC: Die erste Stunde eines Monats in deutscher Zeit steht noch in der Datei des Vormonats.
Für Auswertungen nach Monat ist das egal, denn `report` und `export` wählen den Zeitraum nach lokaler Zeit über alle
Dateien hinweg.

Ältere Versionen schrieben eine Datei je Tag mit JSON-Spalte. Der Dienst wandelt sie beim ersten Start einmalig um und
legt die Originale unter `data/archiv/tagesdateien_vor_umstellung` ab. Danach können sie gelöscht werden. Die Werte
bleiben gleich, nur DNS- und HTTPS-Zeiten sind auf ganze Millisekunden gerundet. Exporte von vor der Umstellung haben
deshalb eine andere Prüfsumme als ein neuer Export desselben Zeitraums.

Zeiten stehen in UTC. Die Dateien nutzen Komma und Punkt, für deutsches Excel ist `linkmon export --excel-de` gedacht.
Ist eine Datei in Excel offen, misst der Dienst weiter und trägt die Zeilen nach, sobald sie wieder frei ist. Ändere die
Dateien nicht von Hand: Die Prüfsumme im Export zeigt sonst Daten, die nicht so gemessen wurden.

Aus der Zeit mit Speedtests liegen eventuell noch `speedtests.csv` und `speedtests_rohdaten.jsonl` im Ordner. linkmon
liest sie nicht mehr. Prüfrunden, die während eines damaligen Speedtests liefen, zählen nur als Lebenszeichen, nicht in
Ping und Verlust: Die Leitung war in der Zeit durch den Test ausgelastet.

## So wird eingeordnet

Jede Prüfrunde schickt je einen Ping an die Router im Heimnetz, an den ersten Hop des Providers und an zwei externe Ziele,
dazu eine DNS-Abfrage und einen HTTPS-Abruf. Maßgeblich für einen Ausfall ist die innerste gestörte Ebene:

| Ebene | Bedingung |
|---|---|
| Lokal | Messgerät ohne aktive Verbindung oder Standardroute |
| Router/LAN | Ein Router im Heimnetz antwortet nicht. Bei zwei Routern steht der betroffene im Befund |
| Leitung | Router erreichbar, aber **alle** externen Ziele nicht |
| DNS Heimnetz (Pi-hole) | Nur mit `dns_home_servers`: Der eigene DNS-Server antwortet nicht, ein öffentlicher schon |
| Nur DNS | Externe Ziele erreichbar, DNS scheitert auch beim öffentlichen Server, oder der DNS des Anbieters ist gestört |
| Nur HTTPS | DNS und externe Ziele in Ordnung, HTTPS scheitert |

**Pi-hole und andere eigene DNS-Server.** Die DNS-Prüfung fragt so, wie dieser Rechner DNS nutzt, also über den Pi-hole,
falls er eingetragen ist. Scheitert sie, fragt linkmon zum Vergleich einen öffentlichen Server (1.1.1.1, 8.8.8.8) direkt.
Eine eigene Prüfung des Pi-hole ist standardmäßig aus. Einschalten lässt sie sich mit `dns_home_servers = "auto"` in
`[reachability]`. Dann erscheint ein nicht antwortender Pi-hole als Ebene „DNS Heimnetz (Pi-hole)“.

Windows behält die DNS-Server getrennter Adapter. Hängt der Pi-hole etwa am Deco (192.168.68.x) und der Rechner per
Kabel an der Vodafone Station (192.168.0.x), steht der Pi-hole vom WLAN noch in der Liste, ist aber nicht erreichbar.
linkmon nutzt deshalb private DNS-Server nur, wenn sie in einem Subnetz eines verbundenen Adapters liegen. Welche
Server gerade gelten, steht bei jedem Wechsel im Log („DNS-Server: …“) und in `linkmon status`.

Standardmäßig eröffnen 3 fehlerhafte Runden in Folge einen Ausfall, 2 fehlerfreie beenden ihn. Start ist die erste
fehlerhafte Runde, Ende die erste fehlerfreie. Die Verfügbarkeit zählt nur die Ebene Leitung und bezieht sie auf die
tatsächlich überwachte Zeit.

**Kennzahlen der Verbindungsqualität:** Ping ist die Antwortzeit eines Pings, für „Internet“ aus beiden externen Zielen
zusammen. P95 ist der Wert, den 95 % der Antworten nicht überschreiten, er zeigt Spitzen. Paketverlust ist der Anteil
unbeantworteter Pings, Jitter die mittlere Abweichung aufeinanderfolgender Antwortzeiten. Im Verlauf stehen Werte je
Intervall, je nach Zeitraum von 1 Minute bis zu mehreren Stunden.

**Ausfall des Tools ist kein Leitungsausfall.** Fehlen Herzschläge länger als 30 Sekunden, gilt die Zeit als Messlücke:
weder verfügbar noch ausgefallen. Ein Ausfall, der in eine Lücke läuft, endet beim letzten Lebenszeichen und bleibt als
unbestätigt markiert.

## Grenzen

- **Keine Geschwindigkeit:** Eine langsame, aber erreichbare Leitung fällt nur über höheren Ping oder Verlust auf.
- **Leitung oder Station:** Fällt die WAN-Seite des Kabelrouters aus, sieht das aus wie ein Leitungsausfall. Fällt der
  Router ganz aus, erscheint das unter Router/LAN.
- **Nur IPv4:** Die Erreichbarkeit wird über IPv4 geprüft. Ein reiner IPv6-Ausfall fällt nicht auf.
- **Ein Ping je Runde:** Kurze Aussetzer unter 3 Sekunden können zwischen zwei Runden fallen.
- **Prüfsumme:** Sie zeigt, dass die Rohdaten seit der Erstellung unverändert sind, nicht, wer sie erzeugt hat.
- **Rechtlich:** Der Bericht ist eine eigene Aufzeichnung, kein amtliches Messprotokoll. Ansprüche bei andauernden
  Störungen (§ 58 TKG) setzen eine Störungsmeldung beim Anbieter voraus. Für eine Minderung wegen zu geringer
  Geschwindigkeit (§ 57 TKG) ist die Desktop-App der Bundesnetzagentur (breitbandmessung.de) maßgeblich.

## Entwicklung

```sh
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

Aufbau unter `src/linkmon`: `classify` (Einordnung, ohne Netzzugriff), `net` und `hostinfo` (Prüfungen, Metadaten),
`monitor` (Dienst), `store` und `cyclefile` (CSV-Speicher), `clearing` (Messdaten leeren), `quality` (Ping, Verlust, Jitter je Strecke), `stats` (Ausfälle,
Verfügbarkeit), `plots`, `report`, `export`, `dashboard`, `cli`.
