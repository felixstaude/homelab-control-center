"""Trusted-proxy handling: client address and forwarded scheme.

Milestone 1.5. These are the decisions that make per-address throttling work
behind a reverse proxy without letting an arbitrary caller forge its address,
so both directions are tested: the header must be believed when it comes from
a declared proxy, and ignored when it does not.
"""

from __future__ import annotations

import re

import pytest

from hcc.security.forwarded import (
    UNKNOWN_ADDRESS,
    client_address,
    describe,
    forwarded_scheme,
    is_trusted_proxy,
    parse_trusted_networks,
)

LOCAL = parse_trusted_networks(["127.0.0.1"])
DOCKER = parse_trusted_networks(["172.16.0.0/12"])
BOTH = parse_trusted_networks(["127.0.0.1", "172.16.0.0/12", "::1"])
NONE = parse_trusted_networks([])


class TestParseTrustedNetworks:
    def test_accepts_a_bare_address(self) -> None:
        assert str(parse_trusted_networks(["10.0.0.5"])[0]) == "10.0.0.5/32"

    def test_accepts_cidr(self) -> None:
        assert str(parse_trusted_networks(["172.16.0.0/12"])[0]) == "172.16.0.0/12"

    def test_accepts_ipv6(self) -> None:
        assert str(parse_trusted_networks(["::1"])[0]) == "::1/128"

    def test_accepts_host_bits_set(self) -> None:
        # strict=False, so a copy-pasted "10.0.0.5/24" is read as the network
        # rather than refused on a technicality.
        assert str(parse_trusted_networks(["10.0.0.5/24"])[0]) == "10.0.0.0/24"

    def test_tolerates_surrounding_whitespace(self) -> None:
        assert len(parse_trusted_networks([" 127.0.0.1 "])) == 1

    @pytest.mark.parametrize(
        "entry",
        ["", "   ", "not-an-ip", "10.0.0.300", "10.0.0.0/33", "example.com", "10.0.0.1-10.0.0.9"],
    )
    def test_rejects_invalid_entries(self, entry: str) -> None:
        # Raised, never skipped: a silently dropped entry would leave the
        # operator believing their proxy is trusted when it is not.
        with pytest.raises(ValueError):
            parse_trusted_networks([entry])

    def test_the_error_names_the_offending_entry(self) -> None:
        with pytest.raises(ValueError, match=re.escape("10.0.0.300")):
            parse_trusted_networks(["127.0.0.1", "10.0.0.300"])

    def test_describe_renders_the_empty_case(self) -> None:
        assert describe(NONE) == "none"
        assert "127.0.0.1/32" in describe(LOCAL)


class TestIsTrustedProxy:
    def test_exact_address(self) -> None:
        assert is_trusted_proxy("127.0.0.1", LOCAL) is True
        assert is_trusted_proxy("127.0.0.2", LOCAL) is False

    def test_within_a_network(self) -> None:
        assert is_trusted_proxy("172.18.0.3", DOCKER) is True
        assert is_trusted_proxy("10.0.0.3", DOCKER) is False

    def test_nothing_is_trusted_when_nothing_is_configured(self) -> None:
        # The default. A deployment with no declared proxy must not believe
        # forwarding headers from anyone.
        assert is_trusted_proxy("127.0.0.1", NONE) is False

    def test_unparseable_peer_is_not_trusted(self) -> None:
        assert is_trusted_proxy("garbage", LOCAL) is False
        assert is_trusted_proxy(None, LOCAL) is False


class TestClientAddress:
    def test_direct_connection_uses_the_peer(self) -> None:
        assert (
            client_address(peer="203.0.113.9", forwarded_for=None, networks=NONE) == "203.0.113.9"
        )

    def test_header_is_ignored_from_an_untrusted_peer(self) -> None:
        # The forgery case: any client can send this header, so believing it
        # without a declared proxy lets a caller rotate its apparent address
        # and bypass the per-address budget entirely.
        assert (
            client_address(peer="203.0.113.9", forwarded_for="10.0.0.1", networks=NONE)
            == "203.0.113.9"
        )

    def test_header_is_ignored_when_a_different_proxy_is_trusted(self) -> None:
        assert (
            client_address(peer="203.0.113.9", forwarded_for="10.0.0.1", networks=LOCAL)
            == "203.0.113.9"
        )

    def test_header_is_used_from_a_trusted_peer(self) -> None:
        assert (
            client_address(peer="127.0.0.1", forwarded_for="198.51.100.7", networks=LOCAL)
            == "198.51.100.7"
        )

    def test_chain_is_walked_from_the_right(self) -> None:
        # The left-hand entries were supplied by whatever came before and may
        # be forged; the right-hand ones were added by infrastructure closest
        # to us. Taking the left-most — the common mistake — would return the
        # attacker-chosen 1.2.3.4.
        address = client_address(
            peer="172.18.0.2",
            forwarded_for="1.2.3.4, 198.51.100.7, 172.18.0.9",
            networks=DOCKER,
        )
        assert address == "198.51.100.7"

    def test_all_hops_trusted_returns_the_rightmost(self) -> None:
        # Every entry is itself a trusted proxy, so there is no untrusted hop
        # to attribute the request to.
        address = client_address(
            peer="172.18.0.2", forwarded_for="172.18.0.8, 172.18.0.9", networks=DOCKER
        )
        assert address == "172.18.0.9"

    def test_trusted_peer_with_no_chain_is_itself_the_client(self) -> None:
        assert client_address(peer="127.0.0.1", forwarded_for=None, networks=LOCAL) == "127.0.0.1"
        assert client_address(peer="127.0.0.1", forwarded_for="", networks=LOCAL) == "127.0.0.1"

    def test_malformed_entry_stops_the_walk(self) -> None:
        # Past a value that cannot be interpreted, which hop added what is
        # unknowable, so the last address that was readable is used rather
        # than skipping over the gap to a client-supplied value.
        address = client_address(
            peer="127.0.0.1", forwarded_for="1.2.3.4, garbage, 198.51.100.7", networks=LOCAL
        )
        assert address == "198.51.100.7"

    def test_entirely_malformed_chain_falls_back_to_the_peer(self) -> None:
        address = client_address(
            peer="127.0.0.1", forwarded_for="garbage, nonsense", networks=LOCAL
        )
        assert address == "127.0.0.1"

    def test_chain_length_is_bounded(self) -> None:
        # The header is attacker-controlled and unbounded; parsing thousands
        # of entries per request would be free work for an attacker.
        flood = "198.51.100.7, " + ", ".join(["172.18.0.9"] * 500)
        address = client_address(peer="172.18.0.2", forwarded_for=flood, networks=DOCKER)
        # The only untrusted entry sits at the far left, outside the examined
        # window, so it is never reached and the right-most trusted hop is
        # used instead. Bounding the walk therefore costs accuracy on an
        # absurdly long chain and nothing on a real one.
        assert address == "172.18.0.9"

    def test_port_suffix_is_stripped(self) -> None:
        assert (
            client_address(peer="127.0.0.1:443", forwarded_for=None, networks=LOCAL) == "127.0.0.1"
        )
        assert (
            client_address(peer="127.0.0.1", forwarded_for="198.51.100.7:51234", networks=LOCAL)
            == "198.51.100.7"
        )

    def test_bracketed_ipv6_is_parsed(self) -> None:
        assert client_address(peer="[::1]:443", forwarded_for=None, networks=BOTH) == "::1"

    def test_bare_ipv6_is_not_split_on_its_colons(self) -> None:
        address = client_address(
            peer="::1", forwarded_for="2001:db8::7", networks=parse_trusted_networks(["::1"])
        )
        assert address == "2001:db8::7"

    def test_ipv6_scope_id_is_dropped(self) -> None:
        networks = parse_trusted_networks(["fe80::/10"])
        assert (
            client_address(peer="fe80::1%eth0", forwarded_for=None, networks=networks) == "fe80::1"
        )

    def test_unknown_peer_shares_one_budget(self) -> None:
        # A sentinel rather than an empty key, so requests with no determinable
        # address share a budget instead of each getting a distinct one — or
        # bypassing the limiter altogether.
        assert client_address(peer=None, forwarded_for=None, networks=LOCAL) == UNKNOWN_ADDRESS
        assert client_address(peer="garbage", forwarded_for=None, networks=NONE) == UNKNOWN_ADDRESS

    def test_addresses_are_canonicalised(self) -> None:
        # Two spellings of one address must not get two budgets.
        first = client_address(peer="127.0.0.1", forwarded_for="2001:0db8::0007", networks=LOCAL)
        second = client_address(peer="127.0.0.1", forwarded_for="2001:db8::7", networks=LOCAL)
        assert first == second == "2001:db8::7"


class TestForwardedScheme:
    def test_trusted_proxy_is_believed(self) -> None:
        assert (
            forwarded_scheme(peer="127.0.0.1", forwarded_proto="https", networks=LOCAL) == "https"
        )

    def test_untrusted_peer_is_not(self) -> None:
        # Otherwise a caller could claim https and influence which origin the
        # CSRF check expects.
        assert forwarded_scheme(peer="203.0.113.9", forwarded_proto="https", networks=LOCAL) is None

    def test_nothing_is_believed_without_configuration(self) -> None:
        assert forwarded_scheme(peer="127.0.0.1", forwarded_proto="https", networks=NONE) is None

    def test_absent_header_yields_none(self) -> None:
        assert forwarded_scheme(peer="127.0.0.1", forwarded_proto=None, networks=LOCAL) is None

    def test_leftmost_entry_of_a_chain_is_used(self) -> None:
        # The scheme the original client used is what decides what origin the
        # browser will send.
        assert (
            forwarded_scheme(peer="127.0.0.1", forwarded_proto="https, http", networks=LOCAL)
            == "https"
        )

    def test_case_is_normalised(self) -> None:
        assert (
            forwarded_scheme(peer="127.0.0.1", forwarded_proto=" HTTPS ", networks=LOCAL) == "https"
        )

    @pytest.mark.parametrize("value", ["ftp", "javascript", "", "  ", "https://x"])
    def test_unrecognised_schemes_are_refused(self, value: str) -> None:
        assert forwarded_scheme(peer="127.0.0.1", forwarded_proto=value, networks=LOCAL) is None
