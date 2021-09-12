from datetime import datetime
from typing import List, Optional

from asn1crypto import algos, cms, core, crl, ocsp
from asn1crypto import pdf as asn1_pdf
from asn1crypto import tsp, x509
from cryptography.hazmat.primitives import hashes

from .general import (
    as_signing_certificate_v2,
    get_pyca_cryptography_hash,
    simple_cms_attribute,
)
from .timestamps import TimeStamper


class CMSAttributeProvider:
    """
    Base class to provide asynchronous CMS attribute values.
    """

    attribute_type: str
    """
    Name of the CMS attribute type this provider supplies. See
    :class:`cms.CMSAttributeType`.
    """

    async def build_attr_value(self, dry_run=False):
        """
        Build the attribute value asynchronously.

        :param dry_run:
            ``True`` if the signer is operating in dry-run (size estimation)
            mode.
        :return:
            An attribute value appropriate for the attribute type.
        """
        raise NotImplementedError

    async def get_attribute(self, dry_run=False) \
            -> Optional[cms.CMSAttribute]:
        value = await self.build_attr_value(dry_run=dry_run)
        if value is not None:
            return simple_cms_attribute(self.attribute_type, value)
        else:
            return None


class SigningCertificateV2Provider(CMSAttributeProvider):
    """
    Provide a value for the signing-certificate-v2 attribute.

    :param signing_cert:
        Certificate containing the signer's public key.
    """

    attribute_type: str = 'signing_certificate_v2'

    def __init__(self, signing_cert: x509.Certificate):
        self.signing_cert = signing_cert

    async def build_attr_value(self, dry_run=False) -> tsp.SigningCertificateV2:
        return as_signing_certificate_v2(self.signing_cert)


class SigningTimeProvider(CMSAttributeProvider):
    """
    Provide a value for the signing-time attribute (i.e. an otherwise
    unauthenticated timestamp).

    :param timestamp:
        Datetime object to include.
    """

    attribute_type: str = 'signing_time'

    def __init__(self, timestamp: datetime):
        self.timestamp = timestamp

    async def build_attr_value(self, dry_run=False) -> cms.Time:
        return cms.Time({'utc_time': core.UTCTime(self.timestamp)})


class AdobeRevinfoProvider(CMSAttributeProvider):
    """
    Format Adobe-style revocation information for inclusion into a CMS
    object.

    :param value:
        A (pre-formatted) RevocationInfoArchival object.
    :param ocsp_responses:
        A list of OCSP responses to include.
    :param crls:
        A list of CRLs to include.
    """

    attribute_type: str = 'adobe_revocation_info_archival'

    def __init__(self,
                 value: Optional[asn1_pdf.RevocationInfoArchival] = None,
                 ocsp_responses: Optional[List[ocsp.OCSPResponse]] = None,
                 crls: Optional[List[crl.CertificateList]] = None):
        self.ocsp_responses = ocsp_responses
        self.crls = crls
        self.value = value

    async def build_attr_value(self, dry_run=False) \
            -> Optional[asn1_pdf.RevocationInfoArchival]:
        if self.value is not None:
            return self.value
        revinfo_dict = {}
        if self.ocsp_responses:
            revinfo_dict['ocsp'] = self.ocsp_responses

        if self.crls:
            revinfo_dict['crl'] = self.crls
        if revinfo_dict:
            self.value = value = asn1_pdf.RevocationInfoArchival(revinfo_dict)
            return value
        else:
            return None


class CMSAlgorithmProtectionProvider(CMSAttributeProvider):
    attribute_type: str = 'cms_algorithm_protection'

    def __init__(self, digest_algo: str,
                 signature_algo: algos.SignedDigestAlgorithm):
        self.digest_algo = digest_algo
        self.signature_algo = signature_algo

    async def build_attr_value(self, dry_run=False) \
            -> cms.CMSAlgorithmProtection:
        return cms.CMSAlgorithmProtection({
            'digest_algorithm': algos.DigestAlgorithm(
                {'algorithm': self.digest_algo}
            ),
            'signature_algorithm': self.signature_algo
        })


class TSTProvider(CMSAttributeProvider):
    def __init__(self, digest_algorithm: str, data_to_ts: bytes,
                 timestamper: TimeStamper,
                 attr_type: str = 'signature_time_stamp_token'):
        self.attribute_type = attr_type
        self.digest_algorithm = digest_algorithm
        self.timestamper = timestamper
        self.data = data_to_ts

    async def build_attr_value(self, dry_run=False) -> cms.ContentInfo:
        digest_algorithm = self.digest_algorithm
        md_spec = get_pyca_cryptography_hash(digest_algorithm)
        md = hashes.Hash(md_spec)
        md.update(self.data)
        if dry_run:
            ts_coro = self.timestamper.async_dummy_response(digest_algorithm)
        else:
            ts_coro = self.timestamper.async_timestamp(
                md.finalize(), digest_algorithm
            )
        return await ts_coro
