# Security policy

Undine is alpha software. Its server has **no sign-in yet**: anyone who can reach its port can read and change every
project it serves and run simulations and checks on the machine. Run it on your own computer (it listens on
`127.0.0.1` by default) or on a network you trust, and reach a remote server over SSH
([Connect an AI agent](docs/getting-started/connect-ai.md) shows `mcp-stdio --ssh`). Sign-in is on the
[roadmap](docs/roadmap.md).

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through GitHub:
**Security → Report a vulnerability** on <https://github.com/kadomoto-lab/undine-eda/security/advisories/new>.

Say what an attacker can do, from where (the same machine, the local network, a dependency or a PDK the project uses,
a file a person opens), and the steps to reproduce it. Fixes go into `main`; Undine has no release branches yet.

Problems that come from running the server on an untrusted network without sign-in are known and are not
vulnerabilities in themselves. Ways around the protections Undine does have are: the trust rule for a dependency's
simulator code and RTL ([Libraries and dependencies](docs/guide/libraries.md)), the import root, the MCP access token,
and an agent reaching beyond its changeset.
