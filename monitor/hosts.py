#!/usr/bin/env python3
"""
Which hosts mean nothing about who runs an agent.

Two separate lists, because two different questions are being asked and the
answers do not coincide.

`GENERIC_ENDPOINT_HOSTS` answers: does a response from this host say the agent
works? For github.com or a public IPFS gateway it does not — they answer for
anybody. Used for strict liveness.

`SHARED_HOSTING_DOMAINS` answers: does two agents sharing this domain say they
belong to the same operator? For an S3 bucket, a CDN, an IPFS gateway or a
naming service it does not — they share a filing cabinet, not a project. Used
for clustering agents into projects.

The second list is wider than the first, and deliberately so. A document served
from `myagent.vercel.app` is real evidence that agent works, so vercel.app does
not belong in the first list. But two agents on vercel.app are not one project,
so it does belong in the second.

Deciding what goes in
---------------------
A domain carrying many agents across almost as many distinct wallets is the
signature of shared hosting rather than a project. That signature is a guide
for putting a domain on this list by hand; it is not applied automatically,
because it is wrong often enough to matter. Measured on the registry in August
2026, `voltplayground.xyz` had thirteen agents on thirteen wallets — a ratio of
1.00, the same as pinata.cloud — and all thirteen were live. It is a project
that gives each agent its own wallet, and an automatic rule would have deleted
it.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Responding here proves nothing. Used for strict liveness.
# ---------------------------------------------------------------------------

GENERIC_ENDPOINT_HOSTS = {
    # code hosting and package registries
    "github.com", "raw.githubusercontent.com", "gist.github.com",
    "githubusercontent.com", "gitlab.com", "bitbucket.org", "sourceforge.net",
    "npmjs.com", "pypi.org",
    # social and messaging
    "facebook.com", "twitter.com", "x.com", "t.me", "telegram.me",
    "discord.com", "discord.gg", "linkedin.com", "instagram.com",
    "youtube.com", "youtu.be", "reddit.com", "medium.com", "mirror.xyz",
    "warpcast.com", "farcaster.xyz",
    # public IPFS and Arweave gateways
    "ipfs.io", "cloudflare-ipfs.com", "dweb.link", "gateway.pinata.cloud",
    "pinata.cloud", "mypinata.cloud", "nftstorage.link", "w3s.link",
    "4everland.io", "ipfs.filebase.io", "arweave.net", "gateway.irys.xyz",
    # specifications, documentation and naming
    "eips.ethereum.org", "ethereum.org", "schema.org", "w3.org",
    "base.eth", "docs.google.com", "drive.google.com",
    "notion.so", "notion.site",
    # placeholders
    "example.com", "example.org", "localhost",
}


# ---------------------------------------------------------------------------
# Sharing this domain says nothing about sharing an operator. Used for
# clustering. Everything above is here too: a gateway is shared hosting by
# definition.
# ---------------------------------------------------------------------------

_HOSTING_ONLY = {
    # object storage and CDNs. Suffix matching covers every region, so
    # amazonaws.com covers bucket.s3.ap-southeast-1.amazonaws.com.
    "amazonaws.com", "storage.googleapis.com", "blob.core.windows.net",
    "digitaloceanspaces.com", "r2.cloudflarestorage.com", "r2.dev",
    "cloudfront.net", "b-cdn.net", "fastly.net", "akamaized.net",
    "azureedge.net", "supabase.co",
    # application hosting where the subdomain is a tenant, not an operator
    "vercel.app", "netlify.app", "workers.dev", "pages.dev", "github.io",
    "gitlab.io", "onrender.com", "fly.dev", "railway.app", "herokuapp.com",
    "web.app", "firebaseapp.com", "ngrok.io", "ngrok-free.app",
    "replit.dev", "glitch.me", "surge.sh",
}

SHARED_HOSTING_DOMAINS = GENERIC_ENDPOINT_HOSTS | _HOSTING_ONLY


def _matches(value: str, domains: set[str]) -> bool:
    value = (value or "").lower().strip().rstrip(".")
    if not value:
        return False
    if value in domains:
        return True
    return any(value.endswith("." + d) for d in domains)


def is_generic_endpoint_host(host: str) -> bool:
    """A response from here is not evidence that the agent works."""
    return _matches(host, GENERIC_ENDPOINT_HOSTS)


def is_shared_hosting(domain: str) -> bool:
    """Two agents sharing this domain are not thereby one project."""
    return _matches(domain, SHARED_HOSTING_DOMAINS)


def hosting_list() -> list[str]:
    """Sorted, for showing the reader what has been excluded."""
    return sorted(SHARED_HOSTING_DOMAINS)


if __name__ == "__main__":
    print(f"{len(GENERIC_ENDPOINT_HOSTS)} generic endpoint hosts")
    print(f"{len(SHARED_HOSTING_DOMAINS)} shared hosting domains "
          f"({len(_HOSTING_ONLY)} of them hosting-only)\n")
    checks = [
        ("termix-platform-prod.s3.ap-southeast-1.amazonaws.com", True),
        ("ipfs.io", True), ("pinata.cloud", True), ("base.eth", True),
        ("ethereum.org", True), ("someone.github.io", True),
        ("myagent.vercel.app", True),
        ("voltplayground.xyz", False), ("olas.network", False),
        ("lobster3301.com", False), ("surfliquid.com", False),
        ("r2.markets", False), ("virtuals.io", False),
    ]
    bad = 0
    for domain, want in checks:
        got = is_shared_hosting(domain)
        bad += got != want
        print(f"  {'ok  ' if got == want else 'FAIL'} "
              f"{'excluded' if got else 'kept    '}  {domain}")
    raise SystemExit(1 if bad else 0)
