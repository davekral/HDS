# 🍳 Hlasový kuchyňský asistent

Hlasový dialogový systém, který uživatele **provede receptem krok za krokem**.
Uživatel nahraje recept (text se surovinami a postupem), asistent si ho uloží do
RAG databáze a pak ho receptem provádí — ovládat ho lze **psaním v chatu** nebo
**hlasem**. Asistent si přitom **deterministicky pamatuje, v jakém kroku se
nachází**, takže se ve vaření nikdy neztratí.

Postaveno na stejných technologiích jako příklady v předmětu:

- **Ollama** (LLM + embeddingy) — porozumění, generování odpovědí, RAG
- **SpeechCloud** — rozpoznávání řeči (ASR, „poslouchání“) a syntéza řeči (TTS, „mluvení“)
- **Tornado** — webový server a dialogový manažer (framework `dialog.py`)

Rozhraní je inspirované konverzačními asistenty (např. Gemini): běžný **chat**,
ze kterého lze přepnout do **hlasového režimu** s přehledem konverzace.

---

## Jak to funguje

```
        Prohlížeč (static/)                    Server (Tornado, app.py)
 ┌────────────────────────────┐        ┌─────────────────────────────────┐
 │  Chat  +  Hlasový režim     │        │   KitchenDialog (dialogový mng.) │
 │                            │  /ws   │                                 │
 │  SpeechCloud klient  ◄─────┼────────┼──►  ASR / TTS  (SpeechCloud)     │
 │   (mikrofon, reproduktor)  │        │                                 │
 └────────────┬───────────────┘        │   ┌─────────────┐  ┌──────────┐ │
              │  dm_send_message        │   │ RecipeState │  │ RecipeRAG│ │
              │  dm_receive_message     │   │ (kroky)     │  │ (FAISS-  │ │
              ▼                         │   └─────────────┘  │  -like)  │ │
        Text / příkazy                  │        Ollama LLM  └──────────┘ │
                                        └─────────────────────────────────┘
```

- **`RecipeState`** – drží číslo aktuálního kroku a poskytuje navigaci
  (další / předchozí / na konkrétní krok). Navigace je čistě v kódu, takže je
  100 % spolehlivá — model se v krocích nemůže „zaseknout“ ani přeskočit.
- **`RecipeRAG`** – recept rozdělí na úseky (suroviny + jednotlivé kroky),
  spočítá jejich embeddingy (model `qwen3-embedding`) a u volných dotazů
  vyhledá nejrelevantnější části. Když embeddingy nejsou dostupné, použije
  vyhledávání podle klíčových slov.
- **Klasifikace záměru** – každá promluva se zařadí (`start`, `next`,
  `previous`, `repeat`, `goto`, `ingredients`, `finish`, `question`). Běžné
  krátké příkazy se rozpoznají okamžitě bez LLM, ostatní zařadí LLM.

---

## Instalace

Doporučeno ve virtuálním prostředí:

```bash
cd "Hlasový kuchyňský asistent"
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Závislosti: `tornado`, `ollama`, `httpx`. Balíček `event_emitter` z materiálů
předmětu je volitelný — pokud chybí, použije se přiložená záloha
`_event_emitter.py`.

> **Přihlašovací údaje a modely** se nastavují v `config.py` (Ollama server,
> uživatel/heslo, model, hlas pro TTS). Výchozí hodnoty míří na školní server
> KKY a používají model `gemma3:12b` a embeddingy `qwen3-embedding`.

---

## Spuštění

```bash
python app.py
```

Server začne poslouchat na `http://0.0.0.0:8888`. Otevřete v prohlížeči:

```
http://localhost:8888/static/index.html
```

> ⚠️ Hlasový režim potřebuje **mikrofon** a **HTTPS / localhost** (prohlížeče
> jinak přístup k mikrofonu blokují). Na `localhost` to funguje. Při prvním
> spuštění prohlížeč požádá o povolení mikrofonu — povolte ho.

V pravém horním rohu sledujte indikátor připojení — jakmile je **„připraveno“**,
asistent je nachystaný.

---

## Ovládání

### 1. Nahrání receptu
Klikněte na **„＋ Nahrát recept“**, vložte text (suroviny i postup) nebo nahrajte
`.txt` soubor, případně použijte tlačítka **Ukázky** (palačinky, svíčková).
Potvrďte **„Načíst recept“**. Asistent recept rozparsuje a v levém panelu zobrazí
suroviny a očíslované kroky.

### 2. Textový chat
Pište do spodního pole. Můžete:
- **„začni“** – přejít na první krok,
- **„další“ / „dál“ / „pokračuj“ / „hotovo“** – další krok,
- **„zpět“** – předchozí krok,
- **„zopakuj“** – zopakovat aktuální krok,
- **„krok tři“** – skok na konkrétní krok,
- **„suroviny“** – přečíst suroviny,
- libovolný **dotaz** – např. *„kolik mouky?“*, *„čím nahradit máslo?“*,
  *„jak dlouho to péct?“* (asistent odpoví z receptu pomocí RAG).

Aktuální krok je v panelu zvýrazněný, hotové kroky odškrtnuté a vidíte i ukazatel
postupu.

### 3. Hlasový režim
Klikněte na ikonu **🎤** (nebo otevřete overlay). Asistent začne **poslouchat** a
své odpovědi i **říká nahlas**. V overlayi vidíte:
- animovaný indikátor stavu (poslouchám / přemýšlím / mluvím),
- živý přepis toho, co říkáte,
- přehled konverzace.

Mluvte stejné příkazy jako v textu („další“, „zopakuj“, „kolik mouky“ …).
Tlačítkem **„Pozastavit poslech“** poslech dočasně vypnete, **✕** hlasový režim
zavře a vrátí vás do chatu.

### 4. Reset
Tlačítko **↺** vpravo nahoře vymaže konverzaci i recept.

---

## Struktura projektu

```
Hlasový kuchyňský asistent/
├── app.py              # Dialogový manažer (KitchenDialog) + spuštění serveru
├── recipe.py           # Parsování receptu, RAG, sledování kroků (RecipeState)
├── config.py           # Konfigurace (Ollama, SpeechCloud, model, hlas, port)
├── dialog.py           # Framework SpeechCloud (z materiálů předmětu)
├── _event_emitter.py   # Záložní EventEmitter (když chybí balíček z kurzu)
├── requirements.txt
├── README.md
└── static/             # Frontend (servíruje Tornado na /static/…)
    ├── index.html
    ├── style.css
    ├── app.js
    └── sample_recipes/ # Ukázkové recepty
        ├── palacinky.txt
        └── svickova.txt
```

---

## Konfigurace (`config.py`)

| Proměnná          | Význam                                              |
|-------------------|-----------------------------------------------------|
| `OLLAMA_HOST`     | Adresa Ollama serveru                               |
| `OLLAMA_USER/PASSWORD` | Přihlašovací údaje (digest auth)               |
| `LLM_MODEL`       | Konverzační model (např. `gemma3:12b`, `phi4:latest`)|
| `EMBED_MODEL`     | Model pro embeddingy / RAG (`qwen3-embedding`)      |
| `SPEECHCLOUD_URI` | Adresa SpeechCloud modelu (ASR + TTS)               |
| `TTS_VOICE`       | Hlas pro syntézu (např. `Iva210`, `Jan210`)         |
| `ASR_TIMEOUT`     | Limit jednoho naslouchání v hlasovém režimu [s]     |
| `SERVER_PORT`     | Port webového serveru                               |

> Adresa SpeechCloud je kvůli načítání knihovny v prohlížeči uvedena i ve
> `static/app.js` (proměnná `SPEECHCLOUD_URI`). Pokud ji měníte, upravte ji na
> obou místech.

---

## Řešení potíží

- **„připojuji…“ se nezmění na „připraveno“** – zkontrolujte dostupnost
  SpeechCloud serveru a že stránku otevíráte přes `localhost` (kvůli mikrofonu).
- **Asistent nereaguje na hlas** – povolte v prohlížeči přístup k mikrofonu a
  ujistěte se, že jste v hlasovém režimu (ikona 🎤 svítí).
- **Chyba u modelu / parsování receptu** – ověřte přihlašovací údaje a název
  modelu v `config.py`; recept lze i tak ovládat, použije se záložní parser.
- **Asistent mluví anglicky** – zkuste jiný model (`gemma3:12b` zvládá češtinu
  dobře); odpovědi jsou systémovým promptem vynuceny v češtině.
