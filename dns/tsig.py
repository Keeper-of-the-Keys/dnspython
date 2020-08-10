# Copyright (C) Dnspython Contributors, see LICENSE for text of ISC license

# Copyright (C) 2001-2007, 2009-2011 Nominum, Inc.
#
# Permission to use, copy, modify, and distribute this software and its
# documentation for any purpose with or without fee is hereby granted,
# provided that the above copyright notice and this permission notice
# appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND NOMINUM DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL NOMINUM BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT
# OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

"""DNS TSIG support."""

import base64
import hashlib
import hmac
import struct

import dns.exception
import dns.rdataclass
import dns.name
import dns.rcode

class BadTime(dns.exception.DNSException):

    """The current time is not within the TSIG's validity time."""


class BadSignature(dns.exception.DNSException):

    """The TSIG signature fails to verify."""


class BadKey(dns.exception.DNSException):

    """The TSIG record owner name does not match the key."""


class BadAlgorithm(dns.exception.DNSException):

    """The TSIG algorithm does not match the key."""


class PeerError(dns.exception.DNSException):

    """Base class for all TSIG errors generated by the remote peer"""


class PeerBadKey(PeerError):

    """The peer didn't know the key we used"""


class PeerBadSignature(PeerError):

    """The peer didn't like the signature we sent"""


class PeerBadTime(PeerError):

    """The peer didn't like the time we sent"""


class PeerBadTruncation(PeerError):

    """The peer didn't like amount of truncation in the TSIG we sent"""


# TSIG Algorithms

HMAC_MD5 = dns.name.from_text("HMAC-MD5.SIG-ALG.REG.INT")
HMAC_SHA1 = dns.name.from_text("hmac-sha1")
HMAC_SHA224 = dns.name.from_text("hmac-sha224")
HMAC_SHA256 = dns.name.from_text("hmac-sha256")
HMAC_SHA256_128 = dns.name.from_text("hmac-sha256-128")
HMAC_SHA384 = dns.name.from_text("hmac-sha384")
HMAC_SHA384_192 = dns.name.from_text("hmac-sha384-192")
HMAC_SHA512 = dns.name.from_text("hmac-sha512")
HMAC_SHA512_256 = dns.name.from_text("hmac-sha512-256")
GSS_TSIG = dns.name.from_text("gss-tsig")

default_algorithm = HMAC_SHA256


class GSSTSig:
    """
    GSS-TSIG TSIG implementation.  This uses the GSS-API context established
    in the TKEY message handshake to sign messages using GSS-API message
    integrity codes, per the RFC.

    In order to avoid a direct GSSAPI dependency, the keyring holds a ref
    to the GSSAPI object required, rather than the key itself.
    """
    def __init__(self, gssapi_context):
        self.gssapi_context = gssapi_context
        self.data = b''
        self.name = 'gss-tsig'

    def update(self, data):
        self.data += data

    def sign(self):
        # defer to the GSSAPI function to sign
        return self.gssapi_context.get_signature(self.data)

    def verify(self, expected):
        try:
            # defer to the GSSAPI function to verify
            return self.gssapi_context.verify_signature(self.data, expected)
        except Exception:
            # note the usage of a bare exception
            raise BadSignature


class GSSTSigAdapter:
    def __init__(self, keyring):
        self.keyring = keyring

    def __call__(self, message, keyname):
        if keyname in self.keyring:
            key = self.keyring[keyname]
            if isinstance(key, Key) and key.algorithm == GSS_TSIG:
                if message:
                    GSSTSigAdapter.parse_tkey_and_step(key, message, keyname)
            return key
        else:
            return None

    @classmethod
    def parse_tkey_and_step(cls, key, message, keyname):
        # if the message is a TKEY type, absorb the key material
        # into the context using step(); this is used to allow the
        # client to complete the GSSAPI negotiation before attempting
        # to verify the signed response to a TKEY message exchange
        try:
            rrset = message.find_rrset(message.answer, keyname,
                                       dns.rdataclass.ANY,
                                       dns.rdatatype.TKEY)
            if rrset:
                token = rrset[0].key
                gssapi_context = key.secret
                return gssapi_context.step(token)
        except KeyError:
            pass


class HMACTSig:
    """
    HMAC TSIG implementation.  This uses the HMAC python module to handle the
    sign/verify operations.
    """

    _hashes = {
        HMAC_SHA1: hashlib.sha1,
        HMAC_SHA224: hashlib.sha224,
        HMAC_SHA256: hashlib.sha256,
        HMAC_SHA256_128: (hashlib.sha256, 128),
        HMAC_SHA384: hashlib.sha384,
        HMAC_SHA384_192: (hashlib.sha384, 192),
        HMAC_SHA512: hashlib.sha512,
        HMAC_SHA512_256: (hashlib.sha512, 256),
        HMAC_MD5: hashlib.md5,
    }

    def __init__(self, key, algorithm):
        try:
            hashinfo = self._hashes[algorithm]
        except KeyError:
            raise NotImplementedError(f"TSIG algorithm {algorithm} " +
                                      "is not supported")

        # create the HMAC context
        if isinstance(hashinfo, tuple):
            self.hmac_context = hmac.new(key, digestmod=hashinfo[0])
            self.size = hashinfo[1]
        else:
            self.hmac_context = hmac.new(key, digestmod=hashinfo)
            self.size = None
        self.name = self.hmac_context.name
        if self.size:
            self.name += f'-{self.size}'

    def update(self, data):
        return self.hmac_context.update(data)

    def sign(self):
        # defer to the HMAC digest() function for that digestmod
        digest = self.hmac_context.digest()
        if self.size:
            digest = digest[: (self.size // 8)]
        return digest

    def verify(self, expected):
        # re-digest and compare the results
        mac = self.sign()
        if not hmac.compare_digest(mac, expected):
            raise BadSignature


def _digest(wire, key, rdata, time=None, request_mac=None, ctx=None,
            multi=None):
    """Return a context containing the TSIG rdata for the input parameters
    @rtype: dns.tsig.HMACTSig or dns.tsig.GSSTSig object
    @raises ValueError: I{other_data} is too long
    @raises NotImplementedError: I{algorithm} is not supported
    """

    first = not (ctx and multi)
    if first:
        ctx = get_context(key)
        if request_mac:
            ctx.update(struct.pack('!H', len(request_mac)))
            ctx.update(request_mac)
    ctx.update(struct.pack('!H', rdata.original_id))
    ctx.update(wire[2:])
    if first:
        ctx.update(key.name.to_digestable())
        ctx.update(struct.pack('!H', dns.rdataclass.ANY))
        ctx.update(struct.pack('!I', 0))
    if time is None:
        time = rdata.time_signed
    upper_time = (time >> 32) & 0xffff
    lower_time = time & 0xffffffff
    time_encoded = struct.pack('!HIH', upper_time, lower_time, rdata.fudge)
    other_len = len(rdata.other)
    if other_len > 65535:
        raise ValueError('TSIG Other Data is > 65535 bytes')
    if first:
        ctx.update(key.algorithm.to_digestable() + time_encoded)
        ctx.update(struct.pack('!HH', rdata.error, other_len) + rdata.other)
    else:
        ctx.update(time_encoded)
    return ctx


def _maybe_start_digest(key, mac, multi):
    """If this is the first message in a multi-message sequence,
    start a new context.
    @rtype: dns.tsig.HMACTSig or dns.tsig.GSSTSig object
    """
    if multi:
        ctx = get_context(key)
        ctx.update(struct.pack('!H', len(mac)))
        ctx.update(mac)
        return ctx
    else:
        return None


def sign(wire, key, rdata, time=None, request_mac=None, ctx=None, multi=False):
    """Return a (tsig_rdata, mac, ctx) tuple containing the HMAC TSIG rdata
    for the input parameters, the HMAC MAC calculated by applying the
    TSIG signature algorithm, and the TSIG digest context.
    @rtype: (string, dns.tsig.HMACTSig or dns.tsig.GSSTSig object)
    @raises ValueError: I{other_data} is too long
    @raises NotImplementedError: I{algorithm} is not supported
    """

    ctx = _digest(wire, key, rdata, time, request_mac, ctx, multi)
    mac = ctx.sign()
    tsig = dns.rdtypes.ANY.TSIG.TSIG(dns.rdataclass.ANY, dns.rdatatype.TSIG,
                                     key.algorithm, time, rdata.fudge, mac,
                                     rdata.original_id, rdata.error,
                                     rdata.other)

    return tsig, _maybe_start_digest(key, mac, multi)


def validate(wire, key, owner, rdata, now, request_mac, tsig_start, ctx=None,
             multi=False):
    """Validate the specified TSIG rdata against the other input parameters.

    @raises FormError: The TSIG is badly formed.
    @raises BadTime: There is too much time skew between the client and the
    server.
    @raises BadSignature: The TSIG signature did not validate
    @rtype: dns.tsig.HMACTSig or dns.tsig.GSSTSig object"""

    (adcount,) = struct.unpack("!H", wire[10:12])
    if adcount == 0:
        raise dns.exception.FormError
    adcount -= 1
    new_wire = wire[0:10] + struct.pack("!H", adcount) + wire[12:tsig_start]
    if rdata.error != 0:
        if rdata.error == dns.rcode.BADSIG:
            raise PeerBadSignature
        elif rdata.error == dns.rcode.BADKEY:
            raise PeerBadKey
        elif rdata.error == dns.rcode.BADTIME:
            raise PeerBadTime
        elif rdata.error == dns.rcode.BADTRUNC:
            raise PeerBadTruncation
        else:
            raise PeerError('unknown TSIG error code %d' % rdata.error)
    if abs(rdata.time_signed - now) > rdata.fudge:
        raise BadTime
    if key.name != owner:
        raise BadKey
    if key.algorithm != rdata.algorithm:
        raise BadAlgorithm
    ctx = _digest(new_wire, key, rdata, None, request_mac, ctx, multi)
    ctx.verify(rdata.mac)
    return _maybe_start_digest(key, rdata.mac, multi)


def get_context(key):
    """Returns an HMAC context for the specified key.

    @rtype: HMAC context
    @raises NotImplementedError: I{algorithm} is not supported
    """

    if key.algorithm == GSS_TSIG:
        return GSSTSig(key.secret)
    else:
        return HMACTSig(key.secret, key.algorithm)


class Key:
    def __init__(self, name, secret, algorithm=default_algorithm):
        if isinstance(name, str):
            name = dns.name.from_text(name)
        self.name = name
        if isinstance(secret, str):
            secret = base64.decodebytes(secret.encode())
        self.secret = secret
        if isinstance(algorithm, str):
            algorithm = dns.name.from_text(algorithm)
        self.algorithm = algorithm

    def __eq__(self, other):
        return (isinstance(other, Key) and
                self.name == other.name and
                self.secret == other.secret and
                self.algorithm == other.algorithm)
