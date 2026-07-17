"""Automatically pick the subject (biology / physics / chemistry / mathematics) for a lecture.

The uploaded file name is the strongest signal for PW lectures (it usually names
the exact chapter), so it is weighted far above the slide text. Matching is done
at three specificities:

  * phrases  — distinctive multi-word chapter names ("structure of atom") — the
    most reliable, so weighted highest and matched as substrings;
  * stems    — word-start prefixes ("chemistr" -> chemistry/chemical) — reliable;
  * words    — short ambiguous words ("cell", "force") — matched only on word
    boundaries and weighted low, since they collide across subjects.

Separators in the file name are normalised to spaces first, so
"Structure_of_Atom_15" matches the "structure of atom" phrase.
"""
from __future__ import annotations

import re

SUBJECT_PHRASES = {
    "physics": [
        "laws of motion", "kinematics", "projectile motion", "work energy power",
        "rotational motion", "system of particles", "gravitation", "simple harmonic",
        "oscillation", "mechanical properties", "kinetic theory", "ray optics",
        "wave optics", "current electricity", "moving charges", "electromagnetic",
        "electrostatic", "electric charges", "alternating current", "dual nature",
        "semiconductor", "modern physics", "moment of inertia", "escape velocity",
        "thermal properties", "units and measurement", "motion in a plane",
        "motion in a straight line",
    ],
    "chemistry": [
        "structure of atom", "atomic structure", "mole concept", "periodic table",
        "periodic properties", "classification of elements", "chemical bonding",
        "molecular structure", "states of matter", "chemical equilibrium",
        "ionic equilibrium", "redox reaction", "electrochemistry", "chemical kinetics",
        "hydrocarbons", "organic chemistry", "physical chemistry", "inorganic chemistry",
        "coordination compound", "p block", "d block", "s block", "f block",
        "haloalkane", "alcohol phenol", "aldehyde", "carboxylic acid", "amines",
        "solid state", "chemical thermodynamics", "some basic concepts of chemistry",
        "environmental chemistry",
    ],
    "biology": [
        "cell the unit of life", "cell cycle", "cell division", "biomolecules",
        "plant kingdom", "animal kingdom", "morphology of flowering",
        "anatomy of flowering", "structural organisation", "digestion and absorption",
        "breathing and exchange", "body fluids and circulation", "excretory products",
        "neural control", "chemical coordination", "photosynthesis in higher plants",
        "respiration in plants", "plant growth", "human reproduction",
        "reproductive health", "principles of inheritance", "molecular basis of inheritance",
        "human health and disease", "microbes in human welfare", "biotechnology",
        "organisms and populations", "ecosystem", "biodiversity", "the living world",
        "biological classification", "sexual reproduction in flowering",
    ],
    "mathematics": [
        "sets and functions", "relations and functions", "trigonometric functions",
        "inverse trigonometric", "complex numbers", "quadratic equations",
        "linear inequalities", "permutations and combinations", "binomial theorem",
        "sequences and series", "straight lines", "conic sections",
        "limits and derivatives", "mathematical reasoning", "mathematical induction",
        "continuity and differentiability", "application of derivatives",
        "indefinite integral", "definite integral", "application of integrals",
        "differential equations", "vector algebra", "three dimensional geometry",
        "linear programming", "coordinate geometry",
    ],
}

SUBJECT_STEMS = {
    "physics": [
        "physic", "kinemat", "newton", "veloc", "accelerat", "momentum", "inertia",
        "gravit", "friction", "capacit", "resist", "magnet", "electrostat", "refract",
        "thermodynam", "oscillat", "projectile", "optic", "torque", "impulse",
    ],
    "chemistry": [
        "chemistr", "chemical", "stoichiom", "molecul", "atomic", "valenc", "hydrocarbon",
        "isomer", "alkan", "alken", "alkyn", "oxidation", "reduction", "periodic",
        "equilibr", "organic", "inorganic", "redox", "electrochem", "titrat",
    ],
    "biology": [
        "biolog", "photosynth", "respirat", "reproduc", "genetic", "chromosom", "enzyme",
        "hormon", "neuron", "tissue", "organism", "ecosystem", "evolution", "digest",
        "excret", "membrane", "prokaryot", "eukaryot", "physiolog", "morpholog", "anatom",
    ],
    "mathematics": [
        "mathemat", "trigonom", "logarithm", "polynomial", "calculus", "differenti",
        "integrat", "determinant", "probabilit", "permutat", "combinat", "matrices",
        "binomial", "quadratic", "parabola", "ellipse", "hyperbola", "algebra",
    ],
}

SUBJECT_WORDS = {
    "physics": ["force", "energy", "power", "wave", "field", "charge", "current",
                "circuit", "lens", "mirror", "heat", "motion", "ray", "flux", "emf"],
    "chemistry": ["mole", "atom", "bond", "acid", "base", "salt", "solution", "reaction",
                  "compound", "element", "orbital", "electron", "ion"],
    "biology": ["cell", "gene", "dna", "rna", "plant", "animal", "blood", "organ",
                "species", "leaf", "root", "flower", "seed", "protein"],
    "mathematics": ["sin", "cos", "tan", "cot", "log", "matrix", "integral", "derivative",
                    "limit", "slope", "theorem", "maths", "math"],
}

_WEIGHTS = {"phrase": 8, "stem": 5, "word": 3}   # in the file name
_BODY_WEIGHTS = {"phrase": 3, "stem": 2, "word": 1}


def _count_phrases(text: str, phrases: list[str]) -> int:
    return sum(text.count(p) for p in phrases)


def _count_stems(text: str, stems: list[str], *, distinct: bool = False) -> int:
    if distinct:
        return sum(1 for s in stems if re.search(r"\b" + re.escape(s), text))
    return sum(len(re.findall(r"\b" + re.escape(s), text)) for s in stems)


def _count_words(text: str, words: list[str], *, distinct: bool = False) -> int:
    if distinct:
        return sum(1 for w in words if re.search(r"\b" + re.escape(w) + r"\b", text))
    return sum(len(re.findall(r"\b" + re.escape(w) + r"\b", text)) for w in words)


def _score(text: str, subject: str, weights: dict, *, distinct: bool = False) -> int:
    return (
        _count_phrases(text, SUBJECT_PHRASES[subject]) * weights["phrase"]
        + _count_stems(text, SUBJECT_STEMS[subject], distinct=distinct) * weights["stem"]
        + _count_words(text, SUBJECT_WORDS[subject], distinct=distinct) * weights["word"]
    )


def detect_subject(filename: str = "", slide_text: str = "", default: str = "biology") -> str:
    """Return 'biology' | 'physics' | 'chemistry' | 'mathematics'.

    File name dominates (weighted phrases/stems/words). Slide text is only a
    tie-breaker: its stems/words count each DISTINCT term once (presence, not
    occurrences), so a physics deck that mentions sin/cos/theta on every slide
    cannot out-vote its own file name through sheer volume. Falls back to
    `default` only when nothing matches at all.
    """
    name = re.sub(r"[_\-]+", " ", (filename or "").lower())
    body = (slide_text or "").lower()
    scores = {
        subj: _score(name, subj, _WEIGHTS) + _score(body, subj, _BODY_WEIGHTS, distinct=True)
        for subj in ("physics", "chemistry", "biology", "mathematics")
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default
