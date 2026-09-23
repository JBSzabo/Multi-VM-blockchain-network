# Ethereum Clique Testna Mreža

Privatna Ethereum blockchain mreža temeljena na **Clique Proof-of-Authority** konsenzusu, raspoređena na 3 virtualna stroja (VM), s vlastitim monitoring servisom i web nadzornom pločom (dashboard) uživo.

---

## 📁 Struktura repozitorija

```
.
├── config/
│   ├── genesis.json          # Definicija mreže i Clique parametara
│   └── password.txt          # Lozinka za otključavanje signer računa
├── monitoring/
│   └── monitor.py            # Flask servis koji nadzire Geth čvor
├── web/
│   ├── index.html            # Nadzorna ploča (dashboard)
│   ├── package.json
│   ├── package-lock.json
│   └── server.js             # Node.js static server za index.html
├── services/
│   ├── geth.service           # systemd unit za Geth čvor
│   ├── monitor.service        # systemd unit za monitoring servis
│   └── webui.service          # systemd unit za web sučelje
├── requirements.txt           # Python ovisnosti za monitoring/
├── korisne-naredbe.txt        # Brza referenca korisnih naredbi
└── README.md
```

Na svakom VM-u ova struktura se preslikava u `/opt/ethereum/`, a `.service` datoteke idu u `/etc/systemd/system/`:

```
/opt/ethereum/
├── data/              # Geth blockchain podaci (generira se automatski)
├── config/
│   ├── genesis.json
│   └── password.txt
├── monitoring/
│   ├── monitor.py
│   └── venv/          # Python virtualno okruženje (generira se)
└── web/
    ├── index.html
    ├── package.json
    ├── server.js
    └── node_modules/  # (generira se preko npm install)

/etc/systemd/system/
├── geth.service
├── monitor.service
└── webui.service
```

---

## ⚙️ Preduvjeti

- Ubuntu 22.04 / 24.04 (ili sličan) na sva 3 VM-a
- [Geth v1.13.8](https://geth.ethereum.org/downloads) (Go-Ethereum)
- Python 3.10+ i `python3-venv`
- Node.js 18+ i `npm`
- Otvoreni portovi na firewallu: `30303/tcp+udp` (P2P), `8545/tcp` (RPC), `8546/tcp` (WebSocket), `3001-3003/tcp` (Web UI), `9001-9003/tcp` (Monitor API)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip nodejs npm

sudo ufw allow 30303/tcp
sudo ufw allow 30303/udp
sudo ufw allow 8545/tcp
sudo ufw allow 8546/tcp
sudo ufw allow 3001:3003/tcp
sudo ufw allow 9001:9003/tcp
```

> Ove korake ponovi na **sva tri VM-a**. Razlike po čvoru (portovi, signer vs. peer) navedene su niže.

---

## 🚀 Postavljanje

### 1. Instalacija Getha

```bash
cd /tmp
wget https://gethstore.blob.core.windows.net/builds/geth-linux-amd64-1.13.8-b20b4a71.tar.gz
tar -xzf geth-linux-amd64-1.13.8-13ee5a58.tar.gz
sudo mv geth-linux-amd64-1.13.8-13ee5a58/geth /usr/local/bin/
geth version
```

### 2. Kloniraj repozitorij i kopiraj datoteke

```bash
git clone <URL_OVOG_REPOZITORIJA> clique-network
cd clique-network

sudo mkdir -p /opt/ethereum/{data,config,monitoring,web}
sudo cp config/genesis.json config/password.txt /opt/ethereum/config/
sudo cp monitoring/monitor.py /opt/ethereum/monitoring/
sudo cp -r web/* /opt/ethereum/web/
sudo cp requirements.txt /opt/ethereum/
```

### 3. Postavljanje `genesis.json`

`config/genesis.json` definira ID mreže, Clique parametre (`period`, `epoch`) i adrese ovlaštenih potpisnika. **Ista datoteka mora biti identična na sva 3 VM-a.**

Ako mijenjaš signer adresu, ažuriraj polje `extradata` (32 bajta nula + adresa potpisnika bez `0x` + 65 bajtova nula) i `alloc` prije inicijalizacije.

### 4. Inicijalizacija chaina

```bash
sudo geth --datadir /opt/ethereum/data init /opt/ethereum/config/genesis.json
```

Pokreni na **sva 3 VM-a**. Očekivani ispis: `Successfully wrote genesis state`.

### 5. Kreiranje / uvoz Clique signer računa

**Samo na VM-u koji je signer** (npr. Node1):

```bash
geth account new --datadir /opt/ethereum/data
```

Zapiši ispisanu adresu — mora odgovarati adresi u `genesis.json`. Ako uvoziš postojeći ključ umjesto generiranja novog:

```bash
geth account import /tmp/private_key.txt --datadir /opt/ethereum/data
```

Lozinka za otključavanje ovog računa nalazi se u `config/password.txt`.

> ⚠️ `password.txt` u ovom repozitoriju je **demo lozinka** za testnu mrežu. Nikad ne koristi ovu lozinku ili commitaj stvarne privatne ključeve/keystore datoteke u javni repozitorij — dodaj `data/keystore/` u `.gitignore`.

### 6. Konfiguracija `.service` datoteka

Kopiraj service datoteke i prilagodi ih po čvoru prije pokretanja:

```bash
sudo cp services/*.service /etc/systemd/system/
sudo nano /etc/systemd/system/geth.service
```

U `geth.service` prilagodi:

- `--datadir` — mora pokazivati na `/opt/ethereum/data`
- `--unlock` i `--miner.etherbase` — **samo na signer čvoru**, adresa iz koraka 5
- `--nat extip:10.0.0.X` — javna/interna IP adresa **ovog** VM-a, kako bi Geth ispravno objavio svoju adresu ostalim peerovima (bitno ako su VM-ovi iza NAT-a)

Primjer ključnog dijela `ExecStart` za signer čvor:

```
ExecStart=/usr/local/bin/geth \
  --datadir /opt/ethereum/data \
  --networkid 1337 \
  --port 30303 \
  --nat extip:10.0.0.X \
  --http --http.addr 0.0.0.0 --http.port 8545 \
  --http.api eth,net,web3,personal,admin,miner,debug,txpool,clique \
  --unlock 0xVAŠA_SIGNER_ADRESA \
  --password /opt/ethereum/config/password.txt \
  --mine --miner.etherbase 0xVAŠA_SIGNER_ADRESA \
  --allow-insecure-unlock
```

Za peer čvorove (Node2, Node3) izbaci `--unlock`, `--password`, `--mine`, `--miner.etherbase`.

U `monitor.service` i `webui.service` prilagodi `MONITOR_PORT` / `WEB_PORT` po čvoru (vidi tablicu portova niže).

### 7. Pokretanje Geth servisa

```bash
sudo systemctl daemon-reload
sudo systemctl enable geth
sudo systemctl start geth
sudo systemctl status geth
```

### 8. Dodavanje peer čvorova

Spoji se na lokalnu Geth konzolu:

```bash
geth attach http://127.0.0.1:8545
```

Unutar konzole dohvati enode ovog čvora:

```javascript
admin.nodeInfo.enode
```

Ponovi na sva 3 VM-a, zatim na svakom čvoru ručno dodaj preostala dva:

```javascript
admin.addPeer("enode://OSTALI_ČVOR@IP_ADRESA:30303")
admin.peers          // provjera - trebao bi vidjeti 2 peera
```

> Savjet: za trajno pamćenje peerova nakon restarta, umjesto ručnog `admin.addPeer` po svakom pokretanju, kreiraj `static-nodes.json` u `/opt/ethereum/data/` sa svim enode adresama — Geth ih automatski učitava pri startu.

### 9. Postavljanje monitoring servisa

```bash
cd /opt/ethereum/monitoring
python3 -m venv venv
source venv/bin/activate
pip install -r /opt/ethereum/requirements.txt
deactivate
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable monitor
sudo systemctl start monitor
sudo journalctl -u monitor -f
```

Provjera:

```bash
curl http://localhost:9001/api/status | jq
```

### 10. Postavljanje web sučelja

```bash
cd /opt/ethereum/web
npm install
```

```bash
sudo systemctl enable webui
sudo systemctl start webui
```

### 11. Provjera

```bash
sudo systemctl status webui
sudo systemctl status monitor
sudo systemctl status geth
```

Otvori u pregledniku:

```
http://IP_VM-a:3001   (ili 3002 / 3003, ovisno o čvoru)
```

Trebao bi vidjeti status **"Online"**, broj bloka, broj povezanih peerova i blockchain vizualizaciju.

---

## 🔢 Raspored portova po čvoru

| Servis | Node1 | Node2 | Node3 |
|---|---|---|---|
| Geth P2P | 30303 | 30303 | 30303 |
| Geth RPC | 8545 | 8545 | 8545 |
| Geth WebSocket | 8546 | 8546 | 8546 |
| Monitor API (`MONITOR_PORT`) | 9001 | 9002 | 9003 |
| Web UI (`WEB_PORT`) | 3001 | 3002 | 3003 |

Ponovi korake 1–11 na **sva tri VM-a**. Jedine razlike su: signer flagovi u `geth.service` (samo Node1, dok se ne izglasa novi potpisnik), `--nat extip` (uvijek IP tog VM-a) i port varijable u `monitor.service`/`webui.service`.

---

## 📖 Korisne naredbe

Vidi [`korisne-naredbe.txt`](./korisne-naredbe.txt) za brzu referencu čestih naredbi (status servisa, slanje transakcije, glasanje za potpisnika, provjera peerova, restart servisa i sl.).

---

## 🛠️ Rješavanje problema

| Problem | Rješenje |
|---|---|
| `admin.peers` prazno | Provjeri firewall (`30303`), ponovi `admin.addPeer` s točnom IP adresom |
| Monitor prikazuje "Offline" | `sudo journalctl -u monitor -f` — provjeri je li Geth pokrenut prije monitora |
| Nema novih blokova | Provjeri je li signer račun otključan: `sudo journalctl -u geth \| grep unlock` |
| `ImportError` u monitor.py | Provjeri je li `venv` aktiviran i jesu li ovisnosti iz `requirements.txt` instalirane |
| Web UI ne učitava se | Provjeri `WEB_PORT` u `webui.service` odgovara portu u adresnoj traci |

---

## ⚠️ Sigurnosna napomena

Ovo je **testna/edukacijska mreža**. `password.txt` sadrži demo lozinku, a mreža nema pravu zaštitu od produkcijskih prijetnji (npr. `--allow-insecure-unlock` se ne smije koristiti izvan izoliranog testnog okruženja). Ne koristi ovu konfiguraciju za stvarna sredstva ili javno dostupne čvorove.
