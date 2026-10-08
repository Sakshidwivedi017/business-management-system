"""Retrieval documents built from catalogue rows."""

from sqlalchemy import text

from app.rag.documents import ITEM, ITEM_CLASSIFICATION, build_documents, classification_document, item_document

ITEM_ROW = {
    "id": "i-1",
    "item_code": "RM-MT-CP-0008",
    "name": " MS Chequered Plate – 1250×2500×6mm ",
    "status": "active",
    "unit": "pcs",
    "hsn_code": "7208",
    "is_spare": True,
    "notes": "Custom size.",
    "category_name": "Raw Material",
    "sub_category_name": "Metal",
    "sub_category_2_name": "Chequered Plate",
}

GUIDE_ROW = {
    "id": "sc2-1",
    "name": "Chequered Plate",
    "short_code": "CP",
    "standard": "IS 3502",
    "description": "Anti-skid raised-pattern plate.",
    "naming_convention": "{Material} Chequered Plate – {W}×{L}×{T}mm",
    "convention_notes": '["Material tokens: MS, SS 304", "  "]',
    "examples": '["MS Chequered Plate \\u2013 1250\\u00d72500\\u00d76mm"]',
    "hsn_code": "7208",
    "sub_category_name": "Metal",
    "category_name": "Raw Material",
}


def test_item_document():
    doc = item_document(ITEM_ROW)
    assert (doc.source, doc.source_id) == (ITEM, "i-1")
    assert doc.title == "RM-MT-CP-0008 – MS Chequered Plate – 1250×2500×6mm"
    assert doc.content == (
        "Item: MS Chequered Plate – 1250×2500×6mm\n"
        "Item code: RM-MT-CP-0008\n"
        "Category: Raw Material > Metal > Chequered Plate\n"
        "Unit: pcs\n"
        "HSN code: 7208\n"
        "Spare part: yes\n"
        "Notes: Custom size."
    )
    assert doc.metadata == {
        "item_id": "i-1",
        "item_code": "RM-MT-CP-0008",
        "name": "MS Chequered Plate – 1250×2500×6mm",
        "status": "active",
        "category": "Raw Material > Metal > Chequered Plate",
    }


def test_item_document_skips_empty_fields():
    row = {**ITEM_ROW, "hsn_code": None, "notes": "  ", "is_spare": False, "category_name": None,
           "sub_category_name": None, "sub_category_2_name": None}
    doc = item_document(row)
    assert doc.content == "Item: MS Chequered Plate – 1250×2500×6mm\nItem code: RM-MT-CP-0008\nUnit: pcs"
    assert doc.metadata["category"] is None


def test_records_without_text_are_skipped():
    assert item_document({**ITEM_ROW, "name": "   "}) is None
    assert classification_document({**GUIDE_ROW, "name": None}) is None


def test_classification_document():
    doc = classification_document(GUIDE_ROW)
    assert (doc.source, doc.source_id) == (ITEM_CLASSIFICATION, "sc2-1")
    assert doc.title == "Chequered Plate (Raw Material > Metal > Chequered Plate)"
    assert doc.content == (
        "Item classification: Chequered Plate (CP)\n"
        "Category: Raw Material > Metal > Chequered Plate\n"
        "Standard: IS 3502\n"
        "Description: Anti-skid raised-pattern plate.\n"
        "Naming convention: {Material} Chequered Plate – {W}×{L}×{T}mm\n"
        "Convention notes:\n- Material tokens: MS, SS 304\n"
        "Examples:\n- MS Chequered Plate – 1250×2500×6mm\n"
        "HSN code: 7208"
    )
    assert doc.metadata == {
        "sub_category_2_id": "sc2-1", "name": "Chequered Plate", "short_code": "CP",
        "category": "Raw Material > Metal > Chequered Plate",
    }


def test_non_list_json_text_is_kept_verbatim():
    doc = classification_document({**GUIDE_ROW, "convention_notes": "Plain note, not JSON", "examples": "[]"})
    assert "Convention notes: Plain note, not JSON" in doc.content
    assert "Examples" not in doc.content


def test_content_hash_is_deterministic_and_sensitive():
    first, again = item_document(ITEM_ROW), item_document(dict(ITEM_ROW))
    assert first.content_hash == again.content_hash
    assert item_document({**ITEM_ROW, "notes": "Changed."}).content_hash != first.content_hash
    assert item_document({**ITEM_ROW, "status": "inactive"}).content_hash != first.content_hash


def test_live_corpus_covers_every_item_and_guide_entry(conn):
    docs = build_documents(conn)
    counts = {source: sum(d.source == source for d in docs) for source in (ITEM, ITEM_CLASSIFICATION)}
    assert counts[ITEM] == conn.execute(text("SELECT count(*) FROM inv_items")).scalar_one()
    assert counts[ITEM_CLASSIFICATION] == conn.execute(text("SELECT count(*) FROM inv_sub_categories_2")).scalar_one()
    assert len({(d.source, d.source_id) for d in docs}) == len(docs)
    # No live figures leak into the semantic corpus.
    assert not any("quantity" in d.content.lower() or "price" in d.content.lower() for d in docs)
