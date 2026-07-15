from app.llm.extraction import _verify_proofs

ARTICLE = (
    "The Federal Reserve said it will hold rates steady at its June meeting.\n\n"
    "Chair Powell added that rate cuts “remain possible” in September, "
    "depending on inflation data."
)


def test_exact_quote_survives():
    proofs = [{"quote": "The Federal Reserve said it will hold rates steady at its June meeting."}]
    assert _verify_proofs(proofs, ARTICLE) == proofs


def test_whitespace_and_case_normalized_quote_survives():
    # Scraped text often differs from the model's echo in line breaks and case;
    # that must not disqualify a genuinely verbatim quote.
    proofs = [{"quote": "chair powell added  that rate cuts “remain possible” in september,"}]
    assert len(_verify_proofs(proofs, ARTICLE)) == 1


def test_hallucinated_quote_is_dropped():
    proofs = [
        {"quote": "The Federal Reserve said it will hold rates steady at its June meeting."},
        {"quote": "The Fed announced an emergency 50 basis point cut."},  # not in the text
    ]
    verified = _verify_proofs(proofs, ARTICLE)
    assert len(verified) == 1
    assert "emergency" not in verified[0]["quote"]


def test_paraphrased_quote_is_dropped():
    proofs = [{"quote": "Powell said rate cuts might happen in September."}]
    assert _verify_proofs(proofs, ARTICLE) == []


def test_empty_and_missing_quotes_are_dropped():
    assert _verify_proofs([{"quote": ""}, {"quote": "   "}, {}], ARTICLE) == []


def test_empty_proofs_list_passes_through():
    assert _verify_proofs([], ARTICLE) == []
