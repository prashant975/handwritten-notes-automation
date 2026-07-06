"""Automatically pick the subject (biology / physics / chemistry) for a lecture.

Scores keyword hits across the file name and the extracted slide text so the UI
never has to ask the user. Falls back to biology when nothing matches.
"""
from __future__ import annotations

import re

SUBJECT_KEYWORDS = {
    "physics": [
        "physic", "motion", "kinematic", "calculus", "maths", "mathematic", "vector",
        "force", "newton", "velocity", "acceleration", "wave", "optic", "lens", "mirror",
        "thermodynamic", "electric", "magnet", "current", "circuit", "capacitor",
        "gravitation", "friction", "momentum", "energy", "oscillation", "shm", "phase",
        "trigonometr", "amplitude", "frequency", "projectile", "fluid", "pressure",
    ],
    "chemistry": [
        "chemistr", "chemical", "mole", "atom", "molecul", "bond", "reaction", "organic",
        "inorganic", "periodic", "acid", "base", "salt", "equilibrium", "redox", "valence",
        "electron config", "hydrocarbon", "isomer", "ester", "alkane", "alkene", "alkyne",
        "ionic", "covalent", "stoichiometry", "ph ", "titration", "solution", "oxidation",
    ],
    "biology": [
        "biolog", "cell", "tissue", "organ", "plant", "animal", "gene", "dna", "rna",
        "chromosome", "ribosome", "mitochondr", "photosynthes", "respiration", "enzyme",
        "protein", "nucleus", "membrane", "prokaryot", "eukaryot", "ncert bio", "life",
        "reproduc", "evolution", "ecosystem", "hormone", "blood", "neuron", "digest",
    ],
}


def _score(text: str, keywords: list[str]) -> int:
    text = text.lower()
    return sum(len(re.findall(re.escape(k), text)) for k in keywords)


def detect_subject(filename: str = "", slide_text: str = "", default: str = "biology") -> str:
    """Return 'biology' | 'physics' | 'chemistry'.

    The file name is weighted heavily (it usually names the chapter), with the
    slide text as a tie-breaker.
    """
    name = (filename or "").lower()
    body = (slide_text or "").lower()
    scores = {}
    for subject, keywords in SUBJECT_KEYWORDS.items():
        scores[subject] = _score(name, keywords) * 5 + _score(body, keywords)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else default
