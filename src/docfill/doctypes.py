"""Document type detection: which kind of document was uploaded?

A machine-learning text classifier (scikit-learn: TF-IDF over character n-grams + logistic
regression). Character n-grams are robust to OCR noise ("Cetatenie", "Cetățenie", "Cetatcnie"
share most n-grams). The model starts from seed texts per type, augmented with simulated OCR
errors, and is retrained with every document type the user confirms in the wizard, so it gets
better with use. Filled PDF forms whose fields match a registered standard document are
recognised exactly from their field names.
"""

from __future__ import annotations

import random
import re
import threading
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from functools import lru_cache

UNKNOWN = "other"
MIN_CONFIDENCE = 0.4


@dataclass(frozen=True)
class DocType:
    name: str
    label: str
    description: str
    seeds: tuple[str, ...]


_ID_CARD = (
    "ROUMANIE ROMÂNIA ROMANIA CARTE D'IDENTITE CARTE DE IDENTITATE IDENTITY CARD SERIA NR CNP "
    "Nume/Nom/Last name Prenume/Prenom/First name Cetățenie/Nationalite/Nationality Română / ROU "
    "Sex/Sexe/Sex Loc naștere/Lieu de naissance/Place of birth Jud. Mun. "
    "Domiciliu/Adresse/Address Jud. Mun. Str. nr. bl. sc. et. ap. "
    "Emisă de/Delivree par/Issued by SPCLEP Valabilitate/Validite/Validity IDROU<<<<<<<< ROU",
    "ROMANIA CARTE DE IDENTITATE IDENTITY CARD SERIA NR CNP Nume Nom Last name Prenume Prenom "
    "First name Cetatenie Nationalite Nationality Romana ROU Sex Sexe Loc nastere Lieu de "
    "naissance Place of birth Domiciliu Adresse Address Emisa de Delivree par Issued by SPCLEP "
    "Valabilitate Validite Validity IDROU",
    "ROU ROMÂNIA ROMANIA CARTE DE IDENTITATE IDENTITY CARD Nume Last name Prenume First name "
    "Cetățenie Nationality Sex Data nașterii Date of birth Locul nașterii Place of birth "
    "Domiciliu Address Emisă de Issued by Data expirării Date of expiry CNP IDROU<<",
)
_BIRTH_CERTIFICATE = (
    "ROMÂNIA CERTIFICAT DE NAȘTERE CODUL NUMERIC PERSONAL SAALLZZNNNNNC Numele de familie "
    "Prenumele Sexul Anul Luna Ziua Data nașterii cifre și litere LOCUL NAȘTERII Comuna Orașul "
    "Municipiul Județul Seria N.P. nr. PĂRINȚII Numele de familie al tatălui Prenumele tatălui "
    "Numele de familie al mamei Prenumele mamei LOCUL ÎNREGISTRĂRII Nașterea a fost trecută în "
    "registrul stării civile la nr. din anul luna ziua MENȚIUNI Eliberat astăzi cu nr. "
    "Semnătura L.S.",
    "ROMANIA CERTIFICAT DE NASTERE CNP Numele Prenumele Sexul Data nasterii Locul nasterii "
    "Municipiul Orasul Comuna Judetul Tara Tatal Mama Numele si prenumele Locul inregistrarii "
    "Act de nastere nr. Data eliberarii Seria nr. Ofiter de stare civila Semnatura",
    "REPUBLICA SOCIALISTA ROMANIA CONSILIUL POPULAR COMITETUL EXECUTIV CERTIFICAT DE NASTERE "
    "CODUL NUMERIC PERSONAL Numele de familie Prenumele Sexul LOCUL NASTERII Comuna Orasul "
    "Municipiul Judetul PARINTII Numele de familie al tatalui Prenumele tatalui Numele de "
    "familie al mamei Prenumele mamei Eliberat astazi",
)
_ANEXA_2A = (
    "ANEXA Nr. 2a MINISTERUL JUSTIȚIEI OFICIUL NAȚIONAL AL REGISTRULUI COMERȚULUI OFICIUL "
    "REGISTRULUI COMERȚULUI DE PE LÂNGĂ TRIBUNALUL CERERE înregistrare în registrul comerțului "
    "persoane juridice înmatriculare înregistrare sucursală persoană juridică română străină "
    "înscriere mențiuni radiere îndreptare eroare materială actualizare obiect de activitate "
    "conform CAEN Rev. 3 Subsemnatul(a) Nume Prenume CNP/NIF domiciliat(ă) în localitatea str. "
    "nr. bloc scara etaj ap. județ/sector țara cetățenia născut(ă) în localitatea act identitate "
    "seria emis(ă) de valabil până la data regimul matrimonial în calitate de prin conform "
    "În temeiul Legii nr. 265/2022 privind registrul comerțului Obiectul cererii Înmatriculare "
    "pentru firma cu sediul social în",
    "ANEXA 2a CERERE inregistrare in registrul comertului persoane juridice Oficiul Registrului "
    "Comertului Tribunalul Subsemnatul Nume Prenume CNP domiciliat in localitatea judet tara "
    "cetatenia nascut in localitatea act identitate seria emis de la data valabil pana la data "
    "calitate Inmatriculare pentru firma sediul social Persoana pentru comunicare Date de "
    "facturare OPIS DE DOCUMENTE DEPUSE Prelucrarea datelor cu caracter personal",
)
_ANEXA_4 = (
    "Anexa nr. 4 DECLARAȚIE pe propria răspundere cu privire la îndeplinirea condițiilor de "
    "funcționare/desfășurare a activității Informațiile din prezenta declarație se transmit, pe "
    "cale electronică, către MINISTERUL JUSTIȚIEI OFICIUL NAȚIONAL AL REGISTRULUI COMERȚULUI "
    "Nr. intrare Data Subsemnatul(a) Nume prenume CNP/NIF domiciliat(ă) în localitatea str. nr. "
    "bloc scara etaj ap. județ/sector țara cetățenia născut(ă) în localitatea act identitate "
    "seria emis(ă) de valabil până la data în calitate de pentru firma având număr de ordine în "
    "registrul comerțului cod unic de înregistrare cu sediul social/profesional în DECLAR PE "
    "PROPRIA RĂSPUNDERE Îmi asum responsabilitatea sanitar sanitar-veterinar siguranța "
    "alimentelor protecției mediului protecției muncii SEDIU SOCIAL/PROFESIONAL Clasa CAEN "
    "Denumire activitate ACTIVITĂȚI DESFĂȘURATE LA TERȚI",
    "Anexa 4 DECLARATIE pe propria raspundere privind indeplinirea conditiilor de functionare "
    "desfasurare a activitatii Subsemnatul Nume prenume CNP domiciliat pentru firma cod unic de "
    "inregistrare sediul social profesional DECLAR PE PROPRIA RASPUNDERE Clasa CAEN Denumire "
    "activitate activitati desfasurate la terti sedii secundare",
)
_OTHER = (
    "FACTURĂ FISCALĂ Seria Nr. Data emiterii Furnizor Cumpărător CIF Reg. Com. Denumire produse "
    "sau servicii U.M. Cantitate Preț unitar Valoare TVA Total de plată Semnătura",
    "CONTRACT DE ÎNCHIRIERE încheiat astăzi între părțile Locator Locatar Obiectul contractului "
    "Durata contractului Chiria Obligațiile părților Încetarea contractului Litigii",
    "Dear Sir or Madam, I am writing to inform you about my new address. Please update your "
    "records accordingly. Kind regards",
    "Stimate domn, Vă rog să aprobați cererea mea. Cu stimă, Semnătura",
    "EXTRAS DE CONT Banca Titular cont IBAN Sold inițial Data tranzacției Descriere Debit "
    "Credit Sold final",
    "PROCURĂ SPECIALĂ Subsemnatul împuternicesc pe să mă reprezinte în fața autorităților "
    "notar public încheiere de autentificare",
)

DOC_TYPES: dict[str, DocType] = {
    doc.name: doc
    for doc in (
        DocType(
            "id_card",
            "Romanian identity card (CI)",
            "Carte de identitate: name, CNP, domicile, ID series/number, MRZ",
            _ID_CARD,
        ),
        DocType(
            "birth_certificate",
            "Birth certificate",
            "Certificat de naștere: name, CNP, date and place of birth, parents",
            _BIRTH_CERTIFICATE,
        ),
        DocType(
            "onrc_anexa_2a",
            "ONRC Anexa 2a - registration request",
            "Cerere de înregistrare în registrul comerțului (persoane juridice)",
            _ANEXA_2A,
        ),
        DocType(
            "onrc_anexa_4",
            "ONRC Anexa 4 - sworn statement",
            "Declarație privind îndeplinirea condițiilor de funcționare",
            _ANEXA_4,
        ),
        DocType(UNKNOWN, "Other document", "Not one of the known types", _OTHER),
    )
}


def normalize_text(text: str) -> str:
    """Accent-free lowercase text with digits masked (numbers are personal, not type cues)."""
    decomposed = unicodedata.normalize("NFD", text)
    text = "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()
    text = re.sub(r"\d", "0", text)
    return re.sub(r"\s+", " ", text)


def _ocr_noise(text: str, rng: random.Random, rate: float) -> str:
    """Simulate OCR damage: dropped diacritics, confused letters, lost/extra characters."""
    confusions = {
        "e": "c",
        "c": "e",
        "i": "l",
        "l": "i",
        "o": "0",
        "a": "o",
        "n": "m",
        "m": "n",
        "u": "v",
        "t": "f",
        "r": "n",
        "s": "5",
    }
    out = []
    for char in text:
        roll = rng.random()
        if roll < rate / 3:
            continue
        if roll < rate * 2 / 3:
            out.append(confusions.get(char.lower(), char))
        elif roll < rate:
            out.append(char + rng.choice(" .,'"))
        else:
            out.append(char)
    return "".join(out)


def seed_examples(
    augment: int = 6, seed: int = 7, types: Iterable[DocType] | None = None
) -> list[tuple[str, str]]:
    rng = random.Random(seed)
    examples: list[tuple[str, str]] = []
    for doc in types if types is not None else DOC_TYPES.values():
        for text in doc.seeds:
            examples.append((text, doc.name))
            for _ in range(augment):
                words = text.split()
                start = rng.randrange(0, max(1, len(words) // 3))
                fragment = " ".join(words[start : start + rng.randint(len(words) // 2, len(words))])
                examples.append((_ocr_noise(fragment, rng, rng.uniform(0.03, 0.15)), doc.name))
    return examples


@dataclass
class Prediction:
    doc_type: str
    confidence: float
    method: str  # "classifier" | "form fields" | "user"
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return DOC_TYPES[self.doc_type].label if self.doc_type in DOC_TYPES else self.doc_type


def _train(learned: tuple[tuple[str, str], ...], types: tuple[DocType, ...]):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline

    # Confirmed real documents count more than the synthetic seeds.
    data = seed_examples(types=types) + list(learned) * 3
    model = make_pipeline(
        TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True),
        LogisticRegression(C=8.0, max_iter=2000, class_weight="balanced"),
    )
    model.fit([normalize_text(text) for text, _ in data], [label for _, label in data])
    return model


BUILT_IN: tuple[DocType, ...] = tuple(DOC_TYPES.values())


@lru_cache(maxsize=8)
def _seed_model(types: tuple[DocType, ...] = BUILT_IN):
    """The model trained on seeds only is the same for everyone: train it once per set of
    document types."""
    return _train((), types)


class DocTypeClassifier:
    """Trained on seed texts plus confirmed examples provided by ``examples``.

    ``extra_types`` adds document types taught through the knowledge base (act constitutiv,
    proof of the registered office...); the model is retrained when they change."""

    def __init__(
        self,
        examples: Callable[[], list[tuple[str, str]]] | None = None,
        extra_types: Callable[[], list[DocType]] | None = None,
    ):
        self._examples = examples
        self._extra_types = extra_types
        self._model = None
        self._trained_on: tuple[int, tuple[DocType, ...]] | None = None
        self._lock = threading.Lock()

    def types(self) -> dict[str, DocType]:
        """Every type the classifier knows: built-in first, then the taught ones."""
        known = dict(DOC_TYPES)
        for doc in self._extra_types() if self._extra_types else []:
            known.setdefault(doc.name, doc)
        return known

    def _fit(self) -> None:
        types = tuple(self.types().values())
        learned = self._examples() if self._examples else []
        names = {doc.name for doc in types}
        learned = [(text, label) for text, label in learned if label in names]
        signature = (len(learned), types)
        if signature == self._trained_on and self._model is not None:
            return
        self._model = _train(tuple(learned), types) if learned else _seed_model(types)
        self._trained_on = signature

    def retrain(self) -> None:
        with self._lock:
            self._trained_on = None
            self._fit()

    def predict(self, text: str) -> Prediction:
        with self._lock:
            self._fit()
            model = self._model
        normalized = normalize_text(text)
        if len(normalized.strip()) < 15:
            return Prediction(UNKNOWN, 0.0, "classifier")
        probabilities = model.predict_proba([normalized])[0]
        scores = {
            str(label): round(float(p), 4)
            for label, p in zip(model.classes_, probabilities, strict=True)
        }
        best = max(scores, key=scores.get)
        if scores[best] < MIN_CONFIDENCE:
            return Prediction(UNKNOWN, scores[best], "classifier", scores)
        return Prediction(best, scores[best], "classifier", scores)


def match_form(
    form_values: dict[str, str], templates: Iterable[tuple[str, str | None, Iterable[str]]]
) -> Prediction | None:
    """A filled PDF form whose field names match a standard document is that document.

    ``templates`` yields (template name, doc type, PDF field names)."""
    if not form_values:
        return None
    names = set(form_values)
    best: tuple[float, str | None] = (0.0, None)
    for _, doc_type, fields in templates:
        fields = set(fields)
        if not fields or not doc_type:
            continue
        overlap = len(names & fields) / len(names)
        if overlap > best[0]:
            best = (overlap, doc_type)
    if best[1] and best[0] >= 0.6:
        return Prediction(best[1], round(min(0.99, 0.5 + best[0] / 2), 4), "form fields")
    return None
