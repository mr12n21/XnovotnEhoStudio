# Novotného Fakt Sound Studio

Smyčkový mixer pro skládání techna a elektroniky ze zvuků z [freesound.org](https://freesound.org).

Princip je stejný jako s otevřenými taby v prohlížeči: **každý tab = jedna kategorie**.
Aplikace z tabu stáhne zvuky. Každý zvuk pak běží v nekonečné smyčce srovnané na takty
a zvuky se dají libovolně zapínat a vypínat. Hraní se nahrává a v **postprodukci** jde
zpětně upravit, kdy který zvuk hrál. Všechno je v jednom okně: záložka **Mix** a záložka
**Postprodukce**.

## Instalace na Linux Mint (a Ubuntu)

Návod počítá s tím, že nemáš nic nainstalované. Všechno se dělá v **Terminálu**
(Menu → Terminál, nebo Ctrl+Alt+T). Příkazy kopíruj po jednom, vlož je do terminálu
(Ctrl+Shift+V) a potvrď Enterem. Když se `sudo` zeptá na heslo, napiš heslo ke svému
účtu. Při psaní se nezobrazuje, to je normální.

**1. Nainstaluj Python, pip a potřebné knihovny** (stačí jednou):

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git libportaudio2 libxcb-cursor0
```

- `python3-venv` a `python3-pip` na Mintu ve výchozím stavu chybí, bez nich se aplikace nespustí,
- `libportaudio2` zajistí zvuk s nejnižší latencí,
- `libxcb-cursor0` potřebuje okno aplikace (Qt 6).

**2. Stáhni aplikaci:**

```bash
cd ~
git clone https://github.com/mr12n21/XNovotnEhoStudio.git
cd XNovotnEhoStudio
```

Bez gitu to jde i přes prohlížeč: na GitHubu klikni na **Code → Download ZIP**, ZIP rozbal
(pravé tlačítko → *Rozbalit sem*) a v terminálu přejdi do rozbalené složky, např.
`cd ~/Stažené/XNovotnEhoStudio-main`.

**3. Spusť:**

```bash
bash run.sh
```

První spuštění trvá pár minut: vytvoří se složka `.venv` a stáhne se `numpy`, `soundfile`,
`PySide6` a `sounddevice` (několik set MB). Další spuštění jsou rychlá, stačí znovu:

```bash
cd ~/XNovotnEhoStudio
bash run.sh
```

### Když něco nejde

| Hláška | Řešení |
|---|---|
| `ensurepip is not available` nebo `No module named venv` | chybí `python3-venv`, zopakuj krok 1 |
| `Could not load the Qt platform plugin "xcb"` | `sudo apt install -y libxcb-cursor0 libxkbcommon-x11-0 libxcb-icccm4 libxcb-keysyms1` |
| `Permission denied` u `./run.sh` | spouštěj přes `bash run.sh` |
| `Instalace závislostí selhala` | zkontroluj internet, smaž `.venv` (`rm -rf .venv`) a spusť znovu |
| aplikace běží, ale není slyšet zvuk | zkontroluj, že máš v systému (ikona reproduktoru) vybraný správný výstup |

**Zvuk:** aplikace funguje i bez PortAudia přes Qt Multimedia (latence ~60 ms). S PortAudiem
(`libportaudio2`, na Fedoře `sudo dnf install portaudio`) použije automaticky nižší latenci.

## 1. Stahování (hlavní okno, vlevo)

Vlož URL tabů z freesound.org, jednu na řádek. Funguje cokoli, kde freesound zobrazuje seznam zvuků:

- tag: `https://freesound.org/browse/tags/?f=tag:%22short%22`
- hledání: `https://freesound.org/search/?q=techno+kick`
- pack: `https://freesound.org/people/<autor>/packs/<id>/`

Každá URL se uloží jako kategorie. Název se odvodí z URL, nebo si ho zadáš sám.
*Stránek* určuje, kolik stránek výsledků se stáhne (15 zvuků na stránku). Zvuky delší
než *Max. délka* se přeskočí. Už stažené zvuky se přeskakují, takže stejnou URL jde
stahovat opakovaně a přidávat další stránky.

## 2. Mix (záložka Mix)

Zaškrtnuté kategorie v knihovně jsou v mixu jako sloupce. Každý zvuk je zobrazený jako **vlna**:

- **klik** na vlnu = zapnout / vypnout,
- **bílá čára** = kde se smyčka právě nachází,
- barvy: zelená = hraje, tmavě zelená = zapnuto, ale stojí (Stop), žlutá = naskočí
  na další takt, modrá = načítá se.

Šířka vlny ukazuje délku smyčky. Krátký zvuk v dlouhé smyčce zabere jen začátek, takže je
hned vidět, kde v taktu je.

| Prvek | Co dělá |
|---|---|
| **▶ Play** (mezerník) | spustí / zastaví přehrávání |
| **BPM** | tempo, všechny smyčky se srovnají na takty |
| **Na takt / Na dobu / Okamžitě** | kdy se zapnutí nebo vypnutí zvuku projeví |
| **M / S** u kategorie | mute / solo kategorie |
| **slider** u kategorie / zvuku | hlasitost |
| **Náh.** | náhodně zapne 3 zvuky z kategorie |
| **Vyp** | vypne celou kategorii |
| **a1, a4, …** (délka) | délka smyčky v dobách: `auto`, `free` (přirozená délka), nebo pevně N dob |
| **⇔ (fit)** | přetáhne zvuk přesně do tempa (mění výšku tónu jako gramofon). Zapíná se samo u zvuků s tempem v názvu („…128bpm“) |
| **Vše vypnout** (Esc) | vypne všechno |

Pravé tlačítko na zvuku: otevřít na freesound.org, panorama, kopírovat do jiné kategorie
(vlastní výběry typu „kicks“), smazat.

## 3. Nahrávání a postprodukce

**● REC** nahrává. Uloží se dvě věci:

- zvuk (`exports/rec_*.wav`, 24 bit),
- **časová osa** (`exports/rec_*.arr.json`), tedy kdy který zvuk hrál.

Po zastavení nahrávání se nahrávka otevře v záložce **Postprodukce**:

- nahoře je **vlna celého mixu** (kde jsou dropy) a pravítko s takty a časem,
- pod ní je jedna stopa pro každý zvuk, s úseky, kdy hrál, a jeho vlnou.

Mezerník v této záložce přehrává časovou osu od červené čáry.

| Akce | Jak |
|---|---|
| **přidat vlastní zvuk / písničku** | tlačítko **+ Vlastní zvuk**, nebo soubor přetáhni myší na časovou osu (WAV, MP3, FLAC, OGG…). Přidá se jako nová stopa a hraje celá |
| nájezd / doznění (prolínání) | pravé tlačítko na úsek → *Nájezd (fade in)* / *Doznění (fade out)* |
| posunout úsek v čase | táhni ho (Ctrl+klik = víc úseků najednou) |
| zkrátit / prodloužit | táhni levý nebo pravý okraj |
| zapnout zvuk někde navíc | dvojklik na prázdné místo ve stopě |
| vypnout zvuk | dvojklik na úsek (nebo Delete) |
| rozdělit, hlasitost úseku, duplikovat | pravé tlačítko na úsek |
| výběr času | táhni v pravítku, pak **Vyjmout výběr** (čas zmizí, zbytek se posune) nebo **Ztišit výběr** |
| pauza před drop | **Vložit takt** na pozici kurzoru (červená čára) |
| **hlasitost v čase** (nájezd, vyjetí, ztišení části) | zapni **〰 Křivka hlasitosti**: klik do stopy = bod, táhni bod nahoru/dolů = hlasitěji/tišeji, dvojklik na bod = smazat. Nahoře v pásu MIX (oranžová čára) je hlasitost celku. Prostřední čárkovaná čára = 100 % |
| hlasitost / mute / solo stopy | vlevo u stopy (pravé tlačítko: délka smyčky, fit, smazat stopu) |
| zoom | Ctrl + kolečko, posun: Shift + kolečko |
| zpět / znovu | Ctrl+Z / Ctrl+Shift+Z |
| tempo | změna BPM přepočítá celou nahrávku |

Postprodukce nemusí začít nahrávkou. **+ Nová** vytvoří prázdnou časovou osu a mix jde
složit jen z vlastních písniček a zvuků. Vlastní soubory se kopírují do knihovny do
kategorie `vlastni`, takže je najdeš i v záložce Mix.

Křivky hlasitosti jsou navázané na čas, ne na úseky. Když posuneš úsek, křivka zůstane, kde
byla. *Vyjmout výběr* a *Vložit takt* je posouvají spolu s časem. Žlutá čára patří stopě,
oranžová celému mixu. Vlny úseků i vlna mixu ukazují hlasitost už po úpravě.

Nic se neničí: výsledek se pokaždé znovu spočítá ze zdrojových zvuků. Úpravy se uloží
(Ctrl+S, nebo samy při přepnutí nahrávky nebo zavření). **Export…** vytvoří mix, stopy
po kategoriích (stems), případně každý zvuk zvlášť, a `CREDITS.txt`. Nahrávku vybereš
nahoře v záložce. **Duplikovat** uloží kopii, takže jde zkoušet různé verze.

## Data

```
library/<kategorie>/sounds.json      metadata: autor, licence, URL
library/<kategorie>/peaks.json       náhledy vln
library/<kategorie>/<id>_<název>.ogg
exports/rec_*.wav                    nahrávky
exports/rec_*.arr.json               časové osy (postprodukce)
session.json                         otevřené kategorie, zapnuté zvuky, hlasitosti (obnoví se při startu)
```

Do složky kategorie můžeš nakopírovat i vlastní WAV, FLAC nebo MP3 (nebo použít *Importovat soubory…*).

## Licence zvuků

Zvuky na freesound.org mají licence CC0, CC-BY nebo CC-BY-NC. U **CC-BY** musíš při publikaci
uvést autora a u **NC** se zvuk nesmí použít komerčně. Proto každý export i nahrávka obsahuje
`CREDITS.txt` se seznamem použitých zvuků, autorů a licencí.

## Struktura kódu

- `studio/scraper.py` – čtení stránek freesound.org a stahování náhledů
- `studio/library.py` – knihovna na disku, načítání audia
- `studio/waveform.py` – náhledy vln
- `studio/engine.py` – engine: smyčky, kvantizace, limiter, nahrávání
- `studio/arrangement.py` – časová osa: záznam, úpravy, render
- `studio/audio_out.py` – výstup do zvukovky (PortAudio / Qt / null)
- `studio/gui.py` – hlavní okno, `gui_mixer.py` – mix, `gui_timeline.py` – postprodukce

Proměnné prostředí: `NSS_HOME` (kde jsou `library/` a `exports/`), `NSS_BACKEND=auto|sd|qt|null`.
