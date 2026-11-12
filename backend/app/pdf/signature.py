"""Digital signatures embedded in a PDF: is the document unchanged since it was signed, and who signed it?

Indian government portals sign the certificates they issue (GST REG-06 by the Goods and Services Tax Network, MCA
incorporation certificates by the Ministry of Corporate Affairs, e-PAN by the Income Tax Department's agencies).
Editing such a PDF (a changed GSTIN, name or address) breaks its signature, so a broken signature is strong evidence
of tampering, while a scan or re-saved copy simply has no signature.

Checked here, for every signature: the signed byte ranges hash to the digest inside the signature, and the signature
verifies with the signer certificate's public key; whether anything was appended after the last signature; and the
signer and issuer names. Not checked: the certificate chain up to the CCA India root (production pins the CCA roots;
see the evidence note). Pure Python over the raw bytes (no renderer), run inside the PDF sandbox like every parser.
Handles the older `adbe.pkcs7.sha1` format that government portals still use, and `adbe.pkcs7.detached` / PAdES.
"""

from __future__ import annotations

import hashlib
import re

SIG_RANGE = re.compile(rb"/ByteRange\s*\[\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*\]")
SUBFILTER = re.compile(rb"/SubFilter\s*/([A-Za-z0-9.#_-]+)")
MAX_SIGNATURES = 8
# Stored with each result: a document checked by an older reader is checked again once (runner, stage 2).
READER_VERSION = 3
_HASHES = {"sha1": hashlib.sha1, "sha256": hashlib.sha256, "sha384": hashlib.sha384, "sha512": hashlib.sha512}


def _name(name) -> dict:
    from cryptography.x509.oid import NameOID

    def get(oid):
        v = name.get_attributes_for_oid(oid)
        return str(v[0].value).strip() if v else None
    return {"cn": get(NameOID.COMMON_NAME), "o": get(NameOID.ORGANIZATION_NAME),
            "ou": get(NameOID.ORGANIZATIONAL_UNIT_NAME)}


def _verify_one(data: bytes, a: int, b: int, c: int, d: int) -> dict:
    from asn1crypto import cms
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

    if a != 0 or b <= 0 or c <= a + b or d < 0 or c + d > len(data):
        return {"status": "unreadable", "detail": "byte range does not fit the file"}
    contents = data[a + b:c].strip()
    if not (contents.startswith(b"<") and contents.endswith(b">")):
        return {"status": "unreadable", "detail": "signature contents are not a hex string"}
    raw = bytes.fromhex(contents[1:-1].decode("ascii"))
    sd = cms.ContentInfo.load(raw)["content"]  # trailing zero padding is ignored
    si = sd["signer_infos"][0]
    algo = si["digest_algorithm"]["algorithm"].native
    if algo not in _HASHES:
        return {"status": "unreadable", "detail": f"unsupported digest {algo}"}
    signed = data[a:a + b] + data[c:c + d]
    econtent = sd["encap_content_info"]["content"].native  # adbe.pkcs7.sha1: the SHA-1 of the signed bytes
    content_ok = True if econtent is None else econtent == hashlib.sha1(signed).digest()
    target = signed if econtent is None else econtent
    sattrs = si["signed_attrs"]
    if sattrs:
        md = next((x["values"][0].native for x in sattrs if x["type"].native == "message_digest"), None)
        content_ok = content_ok and md == _HASHES[algo](target).digest()
        to_verify = sattrs.untag().dump()  # the SET OF attributes, as signed
        signing_time = next((x["values"][0].native for x in sattrs if x["type"].native == "signing_time"), None)
    else:
        to_verify, signing_time = target, None
    certs = [x509.load_der_x509_certificate(cc.chosen.dump()) for cc in sd["certificates"]
             if cc.name == "certificate"]
    sid = si["sid"]
    signer = None
    if sid.name == "issuer_and_serial_number":
        serial = sid.chosen["serial_number"].native
        signer = next((x for x in certs if x.serial_number == serial), None)
    signer = signer or (certs[0] if certs else None)
    if signer is None:
        return {"status": "unreadable", "detail": "no signer certificate in the signature"}
    hash_algo = getattr(hashes, algo.upper())()
    key = signer.public_key()
    try:
        sig_algo = si["signature_algorithm"]
        if isinstance(key, rsa.RSAPublicKey) and sig_algo["algorithm"].native == "rsassa_pss":
            p = sig_algo["parameters"]  # RSA-PSS (PAdES): hash, mask function and salt length come with the signature
            pss_hash = getattr(hashes, p["hash_algorithm"]["algorithm"].native.upper())()
            mgf_hash = getattr(hashes, p["mask_gen_algorithm"]["parameters"]["algorithm"].native.upper())()
            key.verify(si["signature"].native, to_verify,
                       padding.PSS(mgf=padding.MGF1(mgf_hash), salt_length=p["salt_length"].native), pss_hash)
        elif isinstance(key, rsa.RSAPublicKey):
            key.verify(si["signature"].native, to_verify, padding.PKCS1v15(), hash_algo)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(si["signature"].native, to_verify, ec.ECDSA(hash_algo))
        else:
            return {"status": "unreadable", "detail": "unsupported key type"}
        signature_ok = True
    except Exception:  # noqa: BLE001 - InvalidSignature or a malformed value: either way it does not verify
        signature_ok = False
    return {"status": "valid" if (content_ok and signature_ok) else "broken",
            "content_matches": content_ok, "signature_verifies": signature_ok, "digest": algo,
            "signer": _name(signer.subject), "issuer": _name(signer.issuer),
            "certificate_valid": [signer.not_valid_before_utc.date().isoformat(),
                                  signer.not_valid_after_utc.date().isoformat()],
            "signing_time": signing_time.isoformat() if signing_time else None,
            "covered_to": c + d}


def read_signatures(data: bytes) -> dict:
    """{"signatures": [...], "changed_after_signing": bool} for a PDF's embedded signatures ([] when unsigned)."""
    sigs = []
    for m in list(SIG_RANGE.finditer(data))[:MAX_SIGNATURES]:
        a, b, c, d = map(int, m.groups())
        window = data[max(0, m.start() - 4000):m.end() + 4000]
        sf = SUBFILTER.search(window)
        try:
            out = _verify_one(data, a, b, c, d)
        except Exception as exc:  # noqa: BLE001 - an unparseable signature is reported, never an error
            out = {"status": "unreadable", "detail": type(exc).__name__}
        out["subfilter"] = sf.group(1).decode("ascii", "replace") if sf else None
        sigs.append(out)
    known = [s["covered_to"] for s in sigs if s.get("covered_to")]
    # Bytes after the last range a readable signature covers were added after it signed (whitespace is not content).
    # An unreadable signature proves no coverage: alone it says nothing about added content; after a readable one, the
    # readable one still covers only the older bytes.
    tail = data[max(known):].strip() if known else b""
    return {"signatures": sigs, "changed_after_signing": len(tail) > 0, "reader": READER_VERSION}
