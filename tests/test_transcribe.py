from scribe.transcribe import glossary_accuracy, snap_to_glossary

GLOSSARY = ["Lucan", "Ana", "Izali", "Barley", "Becklin", "Kalisandi Fire Eyes"]


def test_snap_fixes_mid_sentence_near_misses():
    assert snap_to_glossary("Then Izalli casts a spell and Becklyn nods.", GLOSSARY) == "Then Izali casts a spell and Becklin nods."


def test_snap_leaves_sentence_starts_and_common_words_alone():
    assert snap_to_glossary("Barely. Barely made it.", GLOSSARY) == "Barely. Barely made it."
    assert snap_to_glossary("they barely made it", GLOSSARY) == "they barely made it"


def test_glossary_accuracy_counts_per_mention_capped():
    ref = "Izali and Lucan. Izali again. Becklin."
    hyp = "Izzaly and Lucan. Izali again. Becklin. Becklin."
    r = glossary_accuracy(hyp, ref, GLOSSARY)
    assert (r["matched"], r["total"]) == (3, 4)
    assert r["per_name"]["Becklin"] == {"reference": 1, "matched": 1}


def test_snap_does_not_merge_a_distant_new_name():
    # A genuinely new NPC must survive so the canon rule can tag it.
    assert snap_to_glossary("They met Morwen at the inn.", GLOSSARY) == "They met Morwen at the inn."
