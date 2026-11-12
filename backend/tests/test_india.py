from app.india.names import compare_names, normalise_name
from app.india.validators import (
    check_gstin,
    check_gstin_pan_link,
    check_ifsc,
    check_pan,
    check_pan_matches_entity,
    check_registration_number,
    gstin_check_char,
)


def test_gstin_check_char_on_published_example():
    # Widely published GSTIN format example.
    assert gstin_check_char("27AAPFU0939F1Z") == "V"
    assert check_gstin("27AAPFU0939F1ZV").ok


def test_gstin_mistyped_check_char_detected():
    r = check_gstin("27AAPFU0939F1ZA")
    assert not r.ok and "check character" in r.reason


def test_gstin_embedded_pan_link():
    assert check_gstin_pan_link("27AAPFU0939F1ZV", "AAPFU0939F").ok
    r = check_gstin_pan_link("27AAPFU0939F1ZV", "AAACN4821K")
    assert not r.ok and r.facts["embedded_pan"] == "AAPFU0939F"


def test_pan_holder_category_vs_entity():
    assert check_pan("AAACN4821K").facts["holder_code"] == "C"
    assert check_pan_matches_entity("AAACN4821K", "private_limited").ok
    personal = check_pan_matches_entity("ABCPK1234D", "private_limited")
    assert not personal.ok and personal.facts["actual_type"] == "Individual"
    assert check_pan_matches_entity("AAJFK1234D", "llp").ok


def test_identifier_whitespace_and_leading_zeros_preserved():
    assert check_ifsc(" hdfc0001234 ").ok
    assert not check_ifsc("HDFC1001234").ok  # 5th char must be 0


def test_cin_and_llpin():
    ok = check_registration_number("U72200KA2019PTC123456", "private_limited")
    assert ok.ok and ok.facts["state_code"] == "KA"
    assert not check_registration_number("U72200KA2019PLC123456", "private_limited").ok
    assert check_registration_number("AAB-1234", "llp").ok
    assert not check_registration_number("U72200KA2019PTC123456", "llp").ok


def test_name_normalisation():
    n = normalise_name("M/s. Nimbus Ledger Solutions Pvt. Ltd.")
    assert n.normalised == "NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED"
    assert n.core == "NIMBUS LEDGER SOLUTIONS"
    assert compare_names("Nimbus Ledger Solutions Pvt Ltd", "NIMBUS LEDGER SOLUTIONS PRIVATE LIMITED").relation == "equivalent"
    assert compare_names("Nimbus Ledger Solutions", "Nimbus Ledger Solutions Private Limited").relation == "suffix_differs"
    assert compare_names("Nimbus Ledger Solutions Pvt Ltd", "Ledgerline Digital").relation == "different"
