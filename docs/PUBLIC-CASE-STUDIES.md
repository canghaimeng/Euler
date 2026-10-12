# Public case-study visualizations

[Read the three cases](https://canghaimeng.site/euler/cases/) in the Euler workbench:

- [Lewis–Overton](https://canghaimeng.site/euler/cases/?project=lewis-overton)
- [Nikoghosyan](https://canghaimeng.site/euler/cases/?project=nikoghosyan)
- [Jankiewicz–Norin–Wise](https://canghaimeng.site/euler/cases/?project=jnw)

The archive uses the workbench's overview, proof reader, research history and
relation map. Readers can switch projects and languages, select nodes, highlight
dependencies, and inspect recorded arguments and evidence. Its publication date
appears at the top. Original research dates and evidence labels remain attached
to their records.

## Publication boundary

The public service reads a frozen export of these three projects. It has no team
runtime, provider credentials, conversation-recovery archive or model connection.
Team chat, project creation, research intervention and restoration clients are
excluded from its browser assets. The HTTP service accepts only GET and HEAD,
and serves only manifest-listed observation responses and assets. The public
reverse proxy also rejects write methods.

The live personal workbench retains its usual research and team controls. A
snapshot is updated by exporting and reviewing a new publication, then replacing
the deployed release.

## Export a reviewed project

Run from a source checkout against a local observation server:

```bash
python scripts/public_snapshot.py export \
  --source http://127.0.0.1:8099 \
  --project example-project \
  --prefix /euler/cases \
  --output /tmp/euler-public-case

python /tmp/euler-public-case/server.py serve \
  --root /tmp/euler-public-case --port 8165
```

Repeat `--project` for each explicitly selected project. The output contains the
current reader assets, exported JSON responses, a route manifest, retained
software notices and a standalone Python server. Serving needs only Python's
standard library. The exporter refuses to overwrite an existing output folder.

Review the exported records before publication. The exporter omits operational
account and session fields and replaces local filesystem prefixes and local
service addresses in source excerpts; it preserves mathematical statements,
dependencies, status labels and recorded evidence. Author-written source prose
still requires a publication review. Original source hashes identify the
historical artifacts, whose displayed excerpts may have these location redactions.

For deployment under a URL prefix, forward that prefix to the loopback server,
using a trailing slash on `proxy_pass` to remove it:

```nginx
location = /euler/cases { return 301 /euler/cases/; }
location ^~ /euler/cases/ {
    if ($request_method !~ ^(GET|HEAD)$) { return 405; }
    proxy_pass http://127.0.0.1:8165/;
}
```

Run the server under an unprivileged account with a read-only snapshot directory.
Publish only the reviewed export. Keep the live workbench and its credentials in
the private research environment.
