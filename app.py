"""
Hlasový kuchyňský asistent – dialogový manažer.

Aplikace stojí na frameworku SpeechCloud (viz dialog.py), stejně jako příklady
v nadřazeném adresáři.  Asistent:

  * běží jako Tornado server a obsluhuje webový frontend (složka static/),
  * mluví (TTS) a poslouchá (ASR) přes SpeechCloud,
  * používá Ollama (LLM) pro porozumění a generování odpovědí,
  * recept si uloží do jednoduché RAG databáze a deterministicky si pamatuje,
    v jakém kroku se nachází – díky tomu se v krocích NIKDY neztratí.

Komunikace s frontendem probíhá přes obecný kanál SpeechCloud:
  frontend -> DM:  dm_send_message  (čteme přes self.pop_message)
  DM -> frontend:  dm_receive_message (posíláme přes self.send_message)

Protokol zpráv (pole "type" v datech):
  Frontend -> DM:
    {"type": "set_recipe",     "text": "<celý text receptu>"}
    {"type": "user_text",      "text": "<napsaná zpráva>"}
    {"type": "set_voice_mode", "enabled": true|false}
    {"type": "reset"}
  DM -> Frontend:
    {"type": "status",        "state": "idle|thinking|speaking|listening"}
    {"type": "assistant",     "text": "..."}
    {"type": "user_speech",   "text": "..."}            # přepis řeči do chatu
    {"type": "recipe_loaded", "title", "ingredients", "steps"}
    {"type": "progress",      "index", "total", "started", "finished"}
    {"type": "info",          "text": "..."}
"""

import asyncio
import json
import logging

import httpx
from ollama import Client

import config
from dialog import SpeechCloudWS, Dialog
from recipe import RecipeRAG, RecipeState, parse_recipe

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  Klasifikace záměru uživatele (strukturovaný výstup LLM)
# --------------------------------------------------------------------------- #
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["start", "next", "previous", "repeat", "goto",
                     "ingredients", "question", "finish"],
        },
        "step_number": {"type": "integer"},
    },
    "required": ["intent"],
}

INTENT_SYSTEM = (
    "Jsi klasifikátor záměru uživatele v hlasovém kuchyňském asistentovi, "
    "který provádí uživatele receptem krok za krokem. Z poslední promluvy urči záměr:\n"
    "- start: chce začít vařit nebo přejít na první krok ('začni', 'pojďme na to')\n"
    "- next: chce další krok ('dál', 'další', 'pokračuj', 'hotovo', 'mám hotovo')\n"
    "- previous: zpět o krok ('zpět', 'předchozí', 'vrať se')\n"
    "- repeat: zopakovat aktuální krok ('zopakuj', 'co teď', 'ještě jednou')\n"
    "- goto: skok na konkrétní krok, vyplň step_number (např. 'krok tři' -> 3)\n"
    "- ingredients: ptá se na suroviny nebo co potřebuje\n"
    "- finish: chce skončit nebo dovařil ('konec', 'končím', 'to je vše')\n"
    "- question: jakýkoli jiný dotaz nebo věta (např. 'kolik mouky', 'čím nahradit máslo')\n"
    "Vrať pouze JSON podle schématu."
)

# Rychlé deterministické rozpoznání běžných krátkých příkazů (bez LLM).
_QUICK_INTENTS = {
    "next": ["další", "dál", "dále", "dalsi", "co dál", "co dále", "co bude dál",
             "a dál", "a co dál", "další krok", "pokračuj", "pokracuj", "pokračujeme",
             "hotovo", "mám hotovo", "mam hotovo", "hotovo mám", "next", "máme"],
    "previous": ["zpět", "zpet", "zpátky", "zpatky", "předchozí", "predchozi",
                 "vrať se", "vrat se", "o krok zpět"],
    "repeat": ["zopakuj", "zopakovat", "ještě jednou", "jeste jednou",
               "co teď", "co ted", "co mám dělat", "co mam delat", "znovu"],
    "start": ["začni", "zacni", "začneme", "zacneme", "začít", "zacit",
              "pojďme na to", "pojdme na to", "start", "spusť", "spust"],
    "ingredients": ["suroviny", "ingredience", "co potřebuju", "co potrebuju",
                    "co potřebuji", "co potrebuji", "co budu potřebovat"],
    "finish": ["konec", "končím", "koncim", "to je vše", "to je vse",
               "dokončit", "dokoncit", "ukončit", "ukoncit"],
}


def quick_intent(text: str):
    """Vrátí (intent, step_number) pro běžné krátké příkazy, jinak None.

    Spouští se jen na krátké promluvy, aby se zabránilo chybnému zařazení
    delších dotazů (které mohou obsahovat slovo jako 'dál')."""
    t = text.lower().strip(" .!?")
    words = t.split()
    if len(words) > 4:
        return None
    for intent, phrases in _QUICK_INTENTS.items():
        for p in phrases:
            if t == p or t.startswith(p + " ") or t.endswith(" " + p) or p in words:
                return intent, None
    return None


def _czech_ordinal_to_int(text: str):
    """Z textu vytáhne číslo kroku (číslicí i slovem)."""
    import re
    m = re.search(r"\b(\d+)\b", text)
    if m:
        return int(m.group(1))
    words = {
        "první": 1, "prvni": 1, "druhý": 2, "druhy": 2, "třetí": 3, "treti": 3,
        "čtvrtý": 4, "ctvrty": 4, "pátý": 5, "paty": 5, "šestý": 6, "sesty": 6,
        "sedmý": 7, "sedmy": 7, "osmý": 8, "osmy": 8, "devátý": 9, "devaty": 9,
        "desátý": 10, "desaty": 10,
        "jedna": 1, "dva": 2, "tři": 3, "tri": 3, "čtyři": 4, "ctyri": 4,
        "pět": 5, "pet": 5, "šest": 6, "sest": 6, "sedm": 7, "osm": 8,
        "devět": 9, "devet": 9, "deset": 10,
    }
    for w, n in words.items():
        if w in text.lower():
            return n
    return None


# --------------------------------------------------------------------------- #
#  Dialogový manažer
# --------------------------------------------------------------------------- #
class KitchenDialog(Dialog):
    """Dialogový manažer hlasového kuchyňského asistenta."""

    GREETING = (
        "Dobrý den, jsem váš hlasový kuchyňský asistent. "
        "Nahrajte nebo vložte recept a já vás jím provedu krok za krokem. "
        "Můžete psát, nebo přepnout do hlasového režimu a mluvit na mě."
    )

    async def main(self):
        # Připojení k Ollama serveru (stejně jako v příkladech).
        self.client = Client(
            host=config.OLLAMA_HOST,
            auth=httpx.DigestAuth(config.OLLAMA_USER, config.OLLAMA_PASSWORD),
        )

        # Stav asistenta.
        self.rag = None
        self.state = None          # type: RecipeState | None
        self.history = []          # konverzační historie pro LLM (QA)
        self.voice_mode = False    # True = mluvíme i posloucháme
        self.busy = False          # True během přemýšlení / mluvení
        self._asr_task = None      # běžící úloha rozpoznávání řeči

        await self.send_status("idle")
        await self.send_message({"type": "assistant", "text": self.GREETING})

        # Hlavní smyčka: souběžně čekáme na zprávu z webu i na řeč uživatele.
        self._msg_task = asyncio.ensure_future(self.pop_message())
        while True:
            tasks = {self._msg_task}
            if self.voice_mode and not self.busy:
                if self._asr_task is None:
                    self._asr_task = asyncio.ensure_future(
                        self.recognize_and_wait_for_asr_result(timeout=config.ASR_TIMEOUT)
                    )
                tasks.add(self._asr_task)

            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            if self._msg_task in done:
                msg = self._msg_task.result()
                self._msg_task = asyncio.ensure_future(self.pop_message())
                await self.handle_message(msg)

            if self._asr_task is not None and self._asr_task in done:
                result = self._asr_task.result()
                self._asr_task = None
                await self.handle_voice_result(result)

    # ----------------------------------------------------------------- #
    #  Pomocné odesílání do frontendu
    # ----------------------------------------------------------------- #
    async def send_status(self, state: str):
        await self.send_message({"type": "status", "state": state})

    async def speak(self, text: str):
        """Řekne text nahlas (jen v hlasovém režimu)."""
        if not self.voice_mode or not text:
            return
        await self.send_status("speaking")
        try:
            await self.synthesize_and_wait(text=text, voice=config.TTS_VOICE)
        except Exception:
            logger.exception("Syntéza řeči selhala.")

    async def respond(self, text: str):
        """Pošle odpověď do chatu a v hlasovém režimu ji i přečte."""
        await self.send_message({"type": "assistant", "text": text})
        await self.speak(text)

    # ----------------------------------------------------------------- #
    #  Obsluha zpráv z frontendu
    # ----------------------------------------------------------------- #
    async def handle_message(self, msg):
        data = (msg or {}).get("data") or {}
        mtype = data.get("type")
        logger.debug("Zpráva z frontendu: %s", mtype)

        if mtype == "set_recipe":
            await self.load_recipe(data.get("text", ""))
        elif mtype == "user_text":
            await self.process_user_input(data.get("text", ""), spoken=False)
        elif mtype == "set_voice_mode":
            await self.set_voice_mode(bool(data.get("enabled")))
        elif mtype == "reset":
            await self.reset()
        else:
            logger.warning("Neznámý typ zprávy: %r", mtype)

    async def handle_voice_result(self, result):
        if result is None:
            # Ticho / timeout – tiše posloucháme dál.
            return
        words = (result.get("word_1best") or "").strip()
        if not words:
            return
        await self.process_user_input(words, spoken=True)

    async def set_voice_mode(self, enabled: bool):
        self.voice_mode = enabled
        if enabled:
            await self.send_status("listening")
            await self.speak("Poslouchám.")
        else:
            # Ukončíme případné rozpoznávání.
            if self._asr_task is not None:
                self._asr_task.cancel()
                self._asr_task = None
            await self.send_status("idle")

    async def reset(self):
        self.rag = None
        self.state = None
        self.history = []
        await self.send_message({"type": "info", "text": "Konverzace i recept byly vymazány."})
        await self.respond(self.GREETING)
        if self.voice_mode:
            await self.send_status("listening")

    # ----------------------------------------------------------------- #
    #  Načtení receptu (parsování + RAG)
    # ----------------------------------------------------------------- #
    async def load_recipe(self, text: str):
        if not text.strip():
            await self.respond("Nedostal jsem žádný text receptu. Zkuste ho prosím vložit znovu.")
            return

        self.busy = True
        await self.send_status("thinking")
        await self.send_message({"type": "info", "text": "Zpracovávám recept…"})

        # Parsování a stavba RAG běží mimo event-loop (blokující HTTP volání).
        loop = asyncio.get_running_loop()
        recipe = await loop.run_in_executor(
            None, parse_recipe, self.client, config.LLM_MODEL, text
        )

        self.rag = RecipeRAG(self.client, config.EMBED_MODEL)
        await loop.run_in_executor(None, self.rag.build, recipe)

        self.state = RecipeState(recipe)
        self.history = []

        await self.send_message({
            "type": "recipe_loaded",
            "title": self.state.title,
            "ingredients": self.state.ingredients,
            "steps": self.state.steps,
        })
        await self.send_progress()

        intro = (
            f"Načetl jsem recept {self.state.title}. "
            f"Má {self.state.total} kroků a {len(self.state.ingredients)} surovin. "
            "Až budete chtít, řekněte nebo napište „začni“ a provedu vás krok za krokem. "
            "Kdykoli se můžete zeptat na suroviny nebo cokoli k postupu."
        )
        self.busy = False
        await self.respond(intro)
        if self.voice_mode:
            await self.send_status("listening")

    async def send_progress(self):
        if self.state is not None:
            await self.send_message({"type": "progress", **self.state.progress()})

    # ----------------------------------------------------------------- #
    #  Zpracování vstupu uživatele (text i řeč)
    # ----------------------------------------------------------------- #
    async def process_user_input(self, text: str, spoken: bool):
        text = (text or "").strip()
        if not text:
            return

        if spoken:
            # Přepis řeči zobrazíme v chatu jako uživatelovu zprávu.
            await self.send_message({"type": "user_speech", "text": text})

        # Zrušíme případné běžící rozpoznávání, ať asistent neslyší sám sebe.
        if self._asr_task is not None:
            self._asr_task.cancel()
            self._asr_task = None

        self.busy = True
        await self.send_status("thinking")
        try:
            await self._dispatch(text)
        except Exception:
            logger.exception("Chyba při zpracování vstupu.")
            await self.respond("Omlouvám se, něco se pokazilo. Zkuste to prosím znovu.")
        finally:
            self.busy = False
            await self.send_status("listening" if self.voice_mode else "idle")

    async def _dispatch(self, text: str):
        # Bez nahraného receptu vedeme jen obecnou konverzaci.
        if self.state is None or self.state.total == 0:
            answer = await self._llm_answer(text, context_chunks=[])
            await self.respond(answer)
            return

        intent, step_number = await self.classify(text)
        logger.debug("Záměr: %s (krok=%s)", intent, step_number)

        if intent == "start":
            self.state.start()
            await self.send_progress()
            await self.respond(self._step_phrase(prefix="Začínáme. "))

        elif intent == "next":
            if self.state.finished:
                await self.respond(
                    "Tohle byl poslední krok. Recept je hotový, dobrou chuť! "
                    "Pokud chcete, můžeme se vrátit na začátek."
                )
            else:
                self.state.next()
                await self.send_progress()
                await self.respond(self._step_phrase())

        elif intent == "previous":
            self.state.previous()
            await self.send_progress()
            await self.respond(self._step_phrase(prefix="Vracíme se. "))

        elif intent == "repeat":
            if not self.state.started:
                await self.respond("Ještě jsme nezačali. Řekněte „začni“ a pustíme se do toho.")
            else:
                await self.respond(self._step_phrase(prefix="Zopakuji. "))

        elif intent == "goto":
            n = step_number or _czech_ordinal_to_int(text)
            if not n:
                await self.respond("Na který krok chcete přejít? Řekněte třeba „krok tři“.")
            else:
                self.state.goto(n)
                await self.send_progress()
                await self.respond(self._step_phrase())

        elif intent == "ingredients":
            ings = self.state.ingredients
            if ings:
                await self.respond("Budete potřebovat: " + ", ".join(ings) + ".")
            else:
                await self.respond("U tohoto receptu nemám vypsané suroviny.")

        elif intent == "finish":
            await self.respond("Dobře, končíme. Děkuji a dobrou chuť!")

        else:  # question
            chunks = self.rag.retrieve(text, k=3) if self.rag else []
            answer = await self._llm_answer(text, context_chunks=chunks)
            await self.respond(answer)

    def _step_phrase(self, prefix: str = "") -> str:
        """Vrátí text aktuálního kroku.

        Číslo kroku se záměrně neuvádí ani v textu, ani v řeči – postup
        (krok X z Y) je vidět v postranním panelu."""
        return f"{prefix}{self.state.current_text() or ''}".strip()

    # ----------------------------------------------------------------- #
    #  Volání LLM
    # ----------------------------------------------------------------- #
    async def classify(self, text: str):
        """Vrátí (intent, step_number)."""
        quick = quick_intent(text)
        if quick is not None:
            intent = quick[0]
            return intent, (_czech_ordinal_to_int(text) if intent == "goto" else None)

        state_note = (
            f"Aktuální krok: {self.state.index + 1} z {self.state.total}."
            if self.state.started else "Vaření ještě nezačalo."
        )
        loop = asyncio.get_running_loop()

        def _call():
            resp = self.client.chat(
                model=config.LLM_MODEL,
                messages=[
                    {"role": "system", "content": INTENT_SYSTEM},
                    {"role": "user", "content": f"{state_note}\nPromluva uživatele: {text}"},
                ],
                format=INTENT_SCHEMA,
                options={"temperature": 0.0},
            )
            return json.loads(resp["message"]["content"])

        try:
            data = await loop.run_in_executor(None, _call)
            intent = data.get("intent", "question")
            step_number = data.get("step_number")
            if intent == "goto" and not step_number:
                step_number = _czech_ordinal_to_int(text)
            return intent, step_number
        except Exception:
            logger.exception("Klasifikace záměru selhala, beru to jako dotaz.")
            return "question", None

    async def _llm_answer(self, question: str, context_chunks) -> str:
        """Vygeneruje odpověď na volný dotaz s využitím RAG kontextu."""
        if self.state is not None:
            if self.state.started:
                step_info = (
                    f"Uživatel je u kroku {self.state.index + 1} z {self.state.total}: "
                    f"{self.state.current_text()}"
                )
            else:
                step_info = "Vaření ještě nezačalo."
            title = self.state.title
        else:
            step_info = "Žádný recept zatím není nahraný."
            title = "—"

        context = "\n".join(f"- {c}" for c in context_chunks) if context_chunks else "(žádné)"
        system = (
            "Jsi přátelský hlasový kuchyňský asistent. Odpovídáš VŽDY česky, stručně "
            "(ideálně 1 až 3 věty) a tak, aby se odpověď dala přečíst nahlas. "
            "Nepoužívej emoji, odrážky ani formátování. Drž se informací z receptu; "
            "co v něm není, doplň běžnou kuchařskou znalostí a upozorni, že to recept neuvádí.\n"
            f"Recept: {title}.\n{step_info}\n"
            f"Relevantní úryvky z receptu:\n{context}"
        )

        self.history.append({"role": "user", "content": question})
        messages = [{"role": "system", "content": system}] + self.history[-12:]

        loop = asyncio.get_running_loop()

        def _call():
            resp = self.client.chat(
                model=config.LLM_MODEL,
                messages=messages,
                options={"temperature": 0.4, "num_ctx": config.NUM_CTX},
            )
            return resp["message"]["content"].strip()

        answer = await loop.run_in_executor(None, _call)
        self.history.append({"role": "assistant", "content": answer})
        return answer


if __name__ == "__main__":
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(message)s", level=logging.INFO
    )
    logger.info("Spouštím hlasového kuchyňského asistenta na portu %d…", config.SERVER_PORT)
    SpeechCloudWS.run(
        KitchenDialog,
        address=config.SERVER_ADDRESS,
        port=config.SERVER_PORT,
        static_path="./static",
    )
