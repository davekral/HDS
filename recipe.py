"""
Zpracování receptu, RAG a sledování stavu (v jakém kroku se nacházíme).

Tento modul je záměrně nezávislý na dialogovém manažeru i na webu – dá se
testovat samostatně.  Obsahuje tři části:

1. ``parse_recipe``   – z volného textu receptu udělá strukturu
                        {title, ingredients[], steps[]}.  Primárně to dělá LLM
                        (strukturovaný výstup), s deterministickým záložním
                        parserem, kdyby model selhal.
2. ``RecipeRAG``      – jednoduchá, ale plnohodnotná RAG databáze nad receptem.
                        Recept rozseká na úseky, spočítá embeddingy a umí
                        vyhledat nejrelevantnější úseky k dotazu.  Pokud
                        embeddingy nejsou dostupné, spadne na klíčová slova.
3. ``RecipeState``    – drží, v jakém kroku se nacházíme, a poskytuje
                        deterministickou navigaci (další / předchozí / na krok).
                        Díky tomu se asistent NIKDY neztratí v krocích.
"""

import json
import logging
import math
import re
from typing import List, Optional

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
#  1. Parsování receptu
# --------------------------------------------------------------------------- #

# JSON schéma pro strukturovaný výstup LLM (formát podle ollama `format=`).
RECIPE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "ingredients": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "ingredients", "steps"],
}

_PARSE_SYSTEM = (
    "Jsi pomocník, který rozkládá kuchyňské recepty na strukturu. "
    "Z textu receptu vytáhni název pokrmu, seznam surovin a seznam kroků postupu. "
    "Každý krok musí být jedna ucelená, samostatně proveditelná činnost popsaná "
    "celou větou v češtině. Dlouhé souvětí s více činnostmi rozděl na více kroků. "
    "Neslučuj suroviny do kroků a naopak. Zachovej původní množství a časy. "
    "Odpovídej POUZE ve formátu JSON podle zadaného schématu."
)


def parse_recipe(client, model: str, text: str) -> dict:
    """Zpracuje volný text receptu na {title, ingredients, steps}.

    `client` je ollama.Client. Když LLM selže nebo vrátí nesmysl, použije se
    záložní heuristický parser, takže funkce vrátí použitelný výsledek vždy.
    """
    text = (text or "").strip()
    if not text:
        return {"title": "Recept", "ingredients": [], "steps": []}

    try:
        response = client.chat(
            model=model,
            messages=[
                {"role": "system", "content": _PARSE_SYSTEM},
                {"role": "user", "content": f"Recept:\n\n{text}"},
            ],
            format=RECIPE_SCHEMA,
            options={"temperature": 0.0, "num_ctx": 1024 * 16},
        )
        data = json.loads(response["message"]["content"])
        title = (data.get("title") or "Recept").strip()
        ingredients = [s.strip() for s in data.get("ingredients", []) if s and s.strip()]
        steps = [s.strip() for s in data.get("steps", []) if s and s.strip()]
        if steps:
            logger.info("Recept rozparsován LLM: %d surovin, %d kroků", len(ingredients), len(steps))
            return {"title": title, "ingredients": ingredients, "steps": steps}
        logger.warning("LLM nevrátil žádné kroky, používám záložní parser.")
    except Exception:  # pragma: no cover - závisí na síti/serveru
        logger.exception("Parsování receptu přes LLM selhalo, používám záložní parser.")

    return _fallback_parse(text)


def _fallback_parse(text: str) -> dict:
    """Deterministický parser bez LLM – dělí podle nadpisů a číslování."""
    lines = [ln.strip() for ln in text.splitlines()]
    title = next((ln for ln in lines if ln), "Recept")

    ingredients: List[str] = []
    steps: List[str] = []
    section = None  # None | "ing" | "steps"

    ing_hdr = re.compile(r"^(suroviny|ingredience|potřebujeme|na těsto|na omáčku)\b", re.I)
    step_hdr = re.compile(r"^(postup|příprava|pracovní postup|metoda|návod)\b", re.I)
    bullet = re.compile(r"^\s*(\d+[\.\)]|[-*•])\s*")

    for ln in lines[1:]:
        if not ln:
            continue
        if ing_hdr.match(ln):
            section = "ing"
            continue
        if step_hdr.match(ln):
            section = "steps"
            continue
        clean = bullet.sub("", ln).strip()
        if not clean:
            continue
        if section == "ing":
            ingredients.append(clean)
        elif section == "steps":
            steps.append(clean)
        else:
            # Bez nadpisů: krátké řádky bereme jako suroviny, věty jako kroky.
            if len(clean) < 45 and not re.search(r"[.!?]$", clean):
                ingredients.append(clean)
            else:
                steps.append(clean)

    # Když i tak nemáme kroky, rozsekáme zbytek textu na věty.
    if not steps:
        body = " ".join(lines[1:])
        steps = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if len(s.strip()) > 3]

    return {"title": title, "ingredients": ingredients, "steps": steps}


# --------------------------------------------------------------------------- #
#  2. RAG nad receptem
# --------------------------------------------------------------------------- #

class RecipeRAG:
    """Malá vektorová databáze nad jedním receptem.

    Recept rozdělí na úseky (suroviny + jednotlivé kroky), spočítá jejich
    embeddingy přes ollama a na dotaz vrátí nejrelevantnější úseky. Pokud
    embeddingy nejsou dostupné, vyhledává podle překryvu klíčových slov.
    """

    def __init__(self, client, embed_model: str):
        self.client = client
        self.embed_model = embed_model
        self.chunks: List[str] = []
        self.vectors: List[List[float]] = []
        self.use_embeddings = False

    def build(self, recipe: dict):
        self.chunks = []
        if recipe.get("ingredients"):
            self.chunks.append("Suroviny: " + "; ".join(recipe["ingredients"]))
        for i, step in enumerate(recipe.get("steps", []), 1):
            self.chunks.append(f"Krok {i}: {step}")

        self.vectors = []
        self.use_embeddings = False
        if not self.chunks:
            return
        try:
            resp = self.client.embed(model=self.embed_model, input=self.chunks)
            self.vectors = resp["embeddings"]
            self.use_embeddings = len(self.vectors) == len(self.chunks)
            logger.info("RAG: embeddingy spočítány pro %d úseků.", len(self.chunks))
        except Exception:  # pragma: no cover - závisí na síti/serveru
            logger.exception("RAG: embeddingy selhaly, používám vyhledávání podle klíčových slov.")
            self.use_embeddings = False

    def retrieve(self, query: str, k: int = 3) -> List[str]:
        if not self.chunks:
            return []
        if self.use_embeddings:
            try:
                qvec = self.client.embed(model=self.embed_model, input=[query])["embeddings"][0]
                scored = [(_cosine(qvec, v), c) for v, c in zip(self.vectors, self.chunks)]
                scored.sort(key=lambda x: x[0], reverse=True)
                return [c for _, c in scored[:k]]
            except Exception:  # pragma: no cover
                logger.exception("RAG: dotaz na embedding selhal, fallback na klíčová slova.")
        return self._keyword_retrieve(query, k)

    def _keyword_retrieve(self, query: str, k: int) -> List[str]:
        q_words = set(_tokenize(query))
        if not q_words:
            return self.chunks[:k]
        scored = []
        for c in self.chunks:
            overlap = len(q_words & set(_tokenize(c)))
            if overlap:
                scored.append((overlap, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:k]] or self.chunks[:k]


def _tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if len(w) > 2]


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# --------------------------------------------------------------------------- #
#  3. Sledování stavu kroků
# --------------------------------------------------------------------------- #

class RecipeState:
    """Drží aktuální pozici v receptu a poskytuje deterministickou navigaci.

    `index` == -1 znamená, že vaření ještě nezačalo (jsme u surovin / úvodu).
    """

    def __init__(self, recipe: dict):
        self.title: str = recipe.get("title", "Recept")
        self.ingredients: List[str] = recipe.get("ingredients", [])
        self.steps: List[str] = recipe.get("steps", [])
        self.index: int = -1

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def started(self) -> bool:
        return self.index >= 0

    @property
    def finished(self) -> bool:
        return self.total > 0 and self.index >= self.total - 1

    def start(self) -> Optional[str]:
        if not self.steps:
            return None
        self.index = 0
        return self.current_text()

    def next(self) -> Optional[str]:
        if not self.steps:
            return None
        if self.index < self.total - 1:
            self.index += 1
        return self.current_text()

    def previous(self) -> Optional[str]:
        if not self.steps:
            return None
        self.index = max(0, self.index - 1)
        return self.current_text()

    def goto(self, number: int) -> Optional[str]:
        """`number` je 1-based číslo kroku, jak ho zná uživatel."""
        if not self.steps:
            return None
        self.index = max(0, min(self.total - 1, number - 1))
        return self.current_text()

    def current_text(self) -> Optional[str]:
        if 0 <= self.index < self.total:
            return self.steps[self.index]
        return None

    def progress(self) -> dict:
        """Stav pro odeslání do frontendu."""
        return {
            "index": self.index,
            "total": self.total,
            "started": self.started,
            "finished": self.finished,
        }
