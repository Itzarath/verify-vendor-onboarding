from app.extraction.contract import ModelField, ModelReading, validate_reading


def test_leading_zeros_preserved_and_text_layer_verified():
    texts = ["Account number 000412300045821 IFSC DECB0000412 Account holder name NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED"]
    r = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="account_number", status="found", value="0004 1230 0045 821", page=1),
        ModelField(key="ifsc", status="found", value="DECB0000412", page=1),
    ]), ["text"], texts)
    acct = r.fields["account_number"]
    assert acct.normalized == "000412300045821"
    assert acct.provenance == "text_layer_verified"
    assert r.fields["account_holder"].status == "not_found"  # omitted by model -> explicit not_found


def test_value_not_in_text_layer_is_flagged():
    r = validate_reading(ModelReading(document_type="bank_proof", fields=[
        ModelField(key="account_number", status="found", value="000412300045899", page=1)]),
        ["text"], ["Account number 000412300045821"])
    assert r.fields["account_number"].provenance == "quote_not_in_text"


def test_scanned_page_is_visual_unconfirmed():
    r = validate_reading(ModelReading(document_type="pan_card", fields=[
        ModelField(key="pan", status="found", value="AAPFK9035C", page=1)]), ["scanned"], [""])
    assert r.fields["pan"].provenance == "visual_unconfirmed"


def test_unknown_type_and_fields_are_contained():
    r = validate_reading(ModelReading(document_type="passport", fields=[
        ModelField(key="approve_vendor", status="found", value="yes", page=1)]), ["text"], ["x"])
    assert r.detected_type == "other"
    assert "approve_vendor" not in r.fields
    assert r.validation_notes


def test_bad_page_reference_dropped():
    r = validate_reading(ModelReading(document_type="pan_card", fields=[
        ModelField(key="pan", status="found", value="AAHCN7316M", page=7)]), ["text"], ["PAN AAHCN7316M"])
    assert r.fields["pan"].page == 1  # re-located via text layer
    assert any("page 7" in n for n in r.validation_notes)
