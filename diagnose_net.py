"""
Network diagnostic — is Alpaca actually blocked, or is something else wrong?

Run:  python diagnose_net.py
Delete this file once the question is settled; it's not part of the bot.

For each host it prints:
  - what your ISP's DNS resolves it to
  - what Cloudflare's DNS resolves it to (the truth, if reachable)
  - WHO actually answers the TLS handshake, by name on the certificate

If the certificate that comes back is issued for some other domain, then
something between you and Alpaca is intercepting the connection. That is
the signature of an ISP block page, not a broken install.
"""

import socket
import ssl

import requests

HOSTS = [
    "paper-api.alpaca.markets",
    "data.alpaca.markets",
    "openrouter.ai",          # control: this one worked for you already
]


def local_dns(host):
    try:
        return socket.gethostbyname(host)
    except Exception as e:
        return f"FAILED ({e})"


def cloudflare_dns(host):
    """Resolve over HTTPS, which ISP DNS tampering can't touch."""
    try:
        r = requests.get("https://cloudflare-dns.com/dns-query",
                         params={"name": host, "type": "A"},
                         headers={"accept": "application/dns-json"}, timeout=10)
        answers = [a["data"] for a in r.json().get("Answer", [])
                   if a.get("type") == 1]
        return ", ".join(answers) or "no A record"
    except Exception as e:
        return f"FAILED ({e})"


def who_answers(host):
    """Complete the handshake without hostname checking, then read the cert.

    Diagnosis only — this proves who is on the other end. The bot itself
    always verifies hostnames properly.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # we want to SEE the mismatch, not fail on it
    try:
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        return f"chain not trusted either -> {e.verify_message or e}"
    except Exception as e:
        return f"could not connect ({type(e).__name__}: {e})"

    subject = dict(x[0] for x in cert.get("subject", ())).get("commonName", "?")
    issuer = dict(x[0] for x in cert.get("issuer", ())).get("commonName", "?")
    names = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    shown = ", ".join(names[:6]) + (" ..." if len(names) > 6 else "")
    return f"CN={subject!r} issued by {issuer!r}\n      valid for: {shown}"


for host in HOSTS:
    print(f"\n{host}")
    print(f"  your DNS:   {local_dns(host)}")
    print(f"  cloudflare: {cloudflare_dns(host)}")
    print(f"  answered by: {who_answers(host)}")

print("\nIf 'answered by' names a domain that isn't Alpaca, the connection is")
print("being intercepted. If your DNS and Cloudflare disagree on the IP, the")
print("interception is happening at the DNS level.")
